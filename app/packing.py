"""CSV parsing, ticket grouping and package planning.

Ticket numbers are compared and ordered with plain lexicographic (byte-wise
``BINARY``) collation, matching SQLite's default, so the packing order and
the order used when serving a package can never disagree.

Planning a batch only counts rows per ticket number; it never materializes
the joined detail rows. Details are joined lazily, one package at a time,
when that package is downloaded.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

TICKET_HEADER = ("ticket_no", "station")
PARTS_HEADER = ("ticket_no", "part_code")
DETAIL_HEADER = ("ticket_no", "station", "part_code")

# Accept the canonical English column names and the Chinese business names.
TICKET_COLUMNS = (("ticket_no", "工单号"), ("station", "送修点"))
PARTS_COLUMNS = (("ticket_no", "工单号"), ("part_code", "零件码"))


class InputError(ValueError):
    """Raised when an uploaded CSV cannot be accepted."""


class TicketTooLarge(InputError):
    def __init__(self, ticket_no: str, row_count: int, row_limit: int) -> None:
        super().__init__(
            f"ticket {ticket_no!r} joins to {row_count} rows, "
            f"exceeding the per-package limit of {row_limit}"
        )
        self.ticket_no = ticket_no
        self.row_count = row_count
        self.row_limit = row_limit


@dataclass(frozen=True)
class Ticket:
    ticket_no: str
    station: str


@dataclass(frozen=True)
class Part:
    ticket_no: str
    part_code: str


@dataclass(frozen=True)
class DetailRow:
    ticket_no: str
    station: str | None
    part_code: str | None


@dataclass(frozen=True)
class Package:
    package_no: int
    first_ticket_no: str
    last_ticket_no: str
    row_count: int


def _read_csv(
    data: bytes, columns: Sequence[Sequence[str]], label: str
) -> List[Tuple[str, ...]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"{label} CSV must be UTF-8 encoded") from exc

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise InputError(f"{label} CSV is empty") from exc

    header = [cell.strip() for cell in header]
    if len(header) != len(columns) or any(
        cell not in aliases for cell, aliases in zip(header, columns)
    ):
        expected = ",".join(aliases[0] for aliases in columns)
        raise InputError(
            f"{label} CSV header must be {expected}, got {','.join(header)}"
        )

    rows: List[Tuple[str, ...]] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(columns):
            raise InputError(
                f"{label} CSV line {line_no} has {len(row)} columns, "
                f"expected {len(columns)}"
            )
        values = tuple(cell.strip() for cell in row)
        if not values[0]:
            raise InputError(f"{label} CSV line {line_no} has an empty ticket number")
        rows.append(values)
    return rows


def parse_tickets(data: bytes) -> List[Ticket]:
    tickets = [
        Ticket(ticket_no=ticket_no, station=station)
        for ticket_no, station in _read_csv(data, TICKET_COLUMNS, "ticket")
    ]
    if not tickets:
        raise InputError("ticket CSV has no data rows")
    return tickets


def parse_parts(data: bytes) -> List[Part]:
    return [
        Part(ticket_no=ticket_no, part_code=part_code)
        for ticket_no, part_code in _read_csv(data, PARTS_COLUMNS, "part")
    ]


def join_detail_rows(
    tickets: Iterable[Ticket], parts: Iterable[Part]
) -> List[DetailRow]:
    """Left join parts onto tickets on ticket number, keeping every duplicate.

    A ticket with *n* submitted rows and *m* matching part lines yields
    ``n * m`` joined rows. A ticket with no matching part line yields one row
    per submitted ticket row with an empty part code. Ticket order follows
    the submitted ticket table; part order follows the submitted part table.

    This exists for ad-hoc checks and tests; publishing and downloads do the
    same join in SQL.
    """
    parts_by_ticket: Dict[str, List[str]] = {}
    for part in parts:
        parts_by_ticket.setdefault(part.ticket_no, []).append(part.part_code)

    rows: List[DetailRow] = []
    for ticket in tickets:
        codes = parts_by_ticket.get(ticket.ticket_no)
        if codes:
            for code in codes:
                rows.append(DetailRow(ticket.ticket_no, ticket.station, code))
        else:
            rows.append(DetailRow(ticket.ticket_no, ticket.station, None))
    return rows


def group_counts(tickets: Sequence[Ticket], parts: Sequence[Part]) -> Dict[str, int]:
    """Rows each ticket number joins to, without materializing those rows."""
    ticket_rows: Dict[str, int] = {}
    for ticket in tickets:
        ticket_rows[ticket.ticket_no] = ticket_rows.get(ticket.ticket_no, 0) + 1

    part_rows: Dict[str, int] = {}
    for part in parts:
        part_rows[part.ticket_no] = part_rows.get(part.ticket_no, 0) + 1

    return {
        ticket_no: count * part_rows.get(ticket_no, 1)
        for ticket_no, count in ticket_rows.items()
    }


def plan(
    tickets: Sequence[Ticket], parts: Sequence[Part], row_limit: int
) -> List[Package]:
    """Assign ticket numbers to packages in lexicographic order.

    A ticket number's whole joined group always stays in one package. A new
    package is opened only when the next group does not fit in the current
    one (``current + group > row_limit``). A group that alone exceeds
    ``row_limit`` rejects the whole batch with :class:`TicketTooLarge`.

    Only per-ticket counts are computed here; joined detail rows are never
    expanded while building the manifest.
    """
    if row_limit < 1:
        raise InputError("row_limit must be a positive integer")

    counts = group_counts(tickets, parts)
    packages: List[Package] = []
    current_count = 0
    current_first = ""
    current_last = ""

    for ticket_no in sorted(counts):
        group = counts[ticket_no]
        if group > row_limit:
            raise TicketTooLarge(ticket_no, group, row_limit)

        if current_count and current_count + group > row_limit:
            packages.append(
                Package(len(packages) + 1, current_first, current_last, current_count)
            )
            current_count = 0

        if not current_count:
            current_first = ticket_no
        current_last = ticket_no
        current_count += group

    if current_count:
        packages.append(
            Package(len(packages) + 1, current_first, current_last, current_count)
        )
    return packages


def render_manifest_csv(version: str, packages: Sequence[dict]) -> str:
    """Render package metadata only; joined detail rows are never expanded."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        ["version", "package_no", "first_ticket_no", "last_ticket_no", "row_count"]
    )
    for package in packages:
        writer.writerow(
            [
                version,
                package["package_no"],
                package["first_ticket_no"],
                package["last_ticket_no"],
                package["row_count"],
            ]
        )
    return buffer.getvalue()


def detail_csv_header() -> str:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow(DETAIL_HEADER)
    return buffer.getvalue()
