"""Application service layer: validation, publishing, manifests and downloads."""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterator, List

from . import packing
from .db import transaction as db_transaction

BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

STREAM_CHUNK_BYTES = 64 * 1024


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


class CorruptSnapshot(Exception):
    pass


def require_batch_id(batch_id: str | None) -> str:
    value = (batch_id or "").strip()
    if not BATCH_ID_RE.match(value):
        raise BadRequest(
            "batch_id must be 1-64 characters of letters, digits, '.', '_' or '-'"
        )
    return value


def parse_row_limit(raw: str | int | None) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise BadRequest("row_limit is required")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise BadRequest("row_limit must be a positive integer") from exc
    if value < 1:
        raise BadRequest("row_limit must be a positive integer")
    return value


def publish(
    conn: sqlite3.Connection,
    batch_id: str,
    ticket_bytes: bytes,
    part_bytes: bytes,
    row_limit: int,
) -> dict:
    """Validate a batch and publish a new immutable manifest version.

    Validation (CSV shape, then per-ticket fan-out) happens before any write.
    If any single ticket joins to more rows than ``row_limit``,
    :class:`packing.TicketTooLarge` propagates and nothing is written, so the
    batch's previous manifest stays current. The version grows with each
    successful publish in the batch, allowing resubmission of both tables and
    the limit.
    """
    tickets = packing.parse_tickets(ticket_bytes)
    parts = packing.parse_parts(part_bytes)
    # Raises TicketTooLarge before anything touches the database. Planning
    # only counts rows per ticket number.
    packages = packing.plan(tickets, parts, row_limit)

    with db_transaction(conn):
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM manifests WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()[0]
        version = f"{batch_id}.v{seq:04d}"
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        conn.execute(
            "INSERT INTO manifests (version, batch_id, seq, row_limit,"
            " total_rows, ticket_count, package_count, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                batch_id,
                seq,
                row_limit,
                sum(package.row_count for package in packages),
                len({ticket.ticket_no for ticket in tickets}),
                len(packages),
                created_at,
            ),
        )
        conn.executemany(
            "INSERT INTO tickets (version, ticket_no, position, station)"
            " VALUES (?, ?, ?, ?)",
            (
                (version, ticket.ticket_no, position, ticket.station)
                for position, ticket in enumerate(tickets)
            ),
        )
        conn.executemany(
            "INSERT INTO parts (version, ticket_no, position, part_code)"
            " VALUES (?, ?, ?, ?)",
            (
                (version, part.ticket_no, position, part.part_code)
                for position, part in enumerate(parts)
            ),
        )
        conn.executemany(
            "INSERT INTO manifest_packages (version, package_no, first_ticket_no,"
            " last_ticket_no, row_count) VALUES (?, ?, ?, ?, ?)",
            (
                (
                    version,
                    package.package_no,
                    package.first_ticket_no,
                    package.last_ticket_no,
                    package.row_count,
                )
                for package in packages
            ),
        )
    return get_manifest(conn, version)


def _manifest_row(conn: sqlite3.Connection, version: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT version, batch_id, row_limit, total_rows, ticket_count,"
        " package_count, created_at FROM manifests WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        raise NotFound(f"manifest {version!r} not found")
    return row


def _packages(conn: sqlite3.Connection, version: str) -> List[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT package_no, first_ticket_no, last_ticket_no, row_count"
            " FROM manifest_packages WHERE version = ? ORDER BY package_no",
            (version,),
        )
    ]


def _manifest(conn: sqlite3.Connection, version: str) -> dict:
    manifest = dict(_manifest_row(conn, version))
    manifest["packages"] = _packages(conn, version)
    return manifest


def get_manifest(conn: sqlite3.Connection, version: str) -> dict:
    return _manifest(conn, version)


def latest_manifest(conn: sqlite3.Connection, batch_id: str) -> dict:
    row = conn.execute(
        "SELECT version FROM manifests WHERE batch_id = ? ORDER BY seq DESC LIMIT 1",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"batch {batch_id!r} has no manifest")
    return _manifest(conn, row["version"])


def list_manifests(conn: sqlite3.Connection, batch_id: str) -> List[dict]:
    versions = [
        row["version"]
        for row in conn.execute(
            "SELECT version FROM manifests WHERE batch_id = ? ORDER BY seq DESC",
            (batch_id,),
        )
    ]
    if not versions:
        raise NotFound(f"batch {batch_id!r} has no manifest")
    return [_manifest(conn, version) for version in versions]


def with_download_urls(manifest: dict) -> dict:
    """Attach each package's download URL to a manifest for API responses."""
    enriched = dict(manifest)
    enriched["packages"] = [
        {
            **package,
            "download_url": (
                f"/api/manifests/{manifest['version']}"
                f"/packages/{package['package_no']}.csv"
            ),
        }
        for package in manifest["packages"]
    ]
    return enriched


def manifest_csv(manifest: dict) -> str:
    return packing.render_manifest_csv(manifest["version"], manifest["packages"])


def get_package_metadata(
    conn: sqlite3.Connection, version: str, package_no: int
) -> dict:
    row = conn.execute(
        "SELECT package_no, first_ticket_no, last_ticket_no, row_count"
        " FROM manifest_packages WHERE version = ? AND package_no = ?",
        (version, package_no),
    ).fetchone()
    if row is not None:
        return dict(row)
    if conn.execute("SELECT 1 FROM manifests WHERE version = ?", (version,)).fetchone() is None:
        raise NotFound(f"manifest {version!r} not found")
    raise NotFound(f"package {package_no} of manifest {version!r} not found")


def iter_package_csv(
    conn: sqlite3.Connection, version: str, package_no: int
) -> Iterator[str]:
    """Stream one package's CSV, reading only that package's source records.

    The left join is replayed in SQL for the package's inclusive ticket-number
    range only; a ticket number is never split across packages. Ticket order
    is ascending, part order is the submitted order, and duplicate rows on
    both sides are preserved. The package's recorded row count bounds the
    output exactly.
    """
    bounds = get_package_metadata(conn, version, package_no)
    first = bounds["first_ticket_no"]
    last = bounds["last_ticket_no"]
    expected = bounds["row_count"]

    yield packing.detail_csv_header()

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    emitted = 0

    rows = conn.execute(
        "SELECT t.ticket_no, t.station, p.part_code"
        " FROM tickets AS t"
        " LEFT JOIN parts AS p"
        "   ON p.version = t.version AND p.ticket_no = t.ticket_no"
        " WHERE t.version = ? AND t.ticket_no >= ? AND t.ticket_no <= ?"
        " ORDER BY t.ticket_no, t.position, p.position",
        (version, first, last),
    )
    for row in rows:
        writer.writerow(
            [
                row["ticket_no"],
                row["station"],
                "" if row["part_code"] is None else row["part_code"],
            ]
        )
        emitted += 1

        if buffer.tell() >= STREAM_CHUNK_BYTES:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    if emitted != expected:
        raise CorruptSnapshot(
            f"package {package_no} of manifest {version!r} expected {expected}"
            f" rows but read {emitted}"
        )
    remainder = buffer.getvalue()
    if remainder:
        yield remainder
