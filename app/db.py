"""SQLite schema and connection helpers.

Each successful publish stores one immutable snapshot: every manifest version
keeps the submitted ticket rows, the submitted part rows and the package
boundaries computed from them. Details are joined when a package is
downloaded, restricted to that package's ticket range, so serving a package
reads only that package's source records. Downloads always read by version,
so publishing a newer version never changes an older manifest.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "supply.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifests (
    version        TEXT PRIMARY KEY,
    batch_id       TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    row_limit      INTEGER NOT NULL,
    total_rows     INTEGER NOT NULL,
    ticket_count   INTEGER NOT NULL,
    package_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_manifests_batch_seq
    ON manifests(batch_id, seq);

-- One row per submitted ticket. ``position`` preserves the submitted order
-- and keeps duplicate ticket rows.
CREATE TABLE IF NOT EXISTS tickets (
    version    TEXT NOT NULL REFERENCES manifests(version) ON DELETE CASCADE,
    ticket_no  TEXT NOT NULL,
    position   INTEGER NOT NULL,
    station    TEXT NOT NULL,
    PRIMARY KEY (version, ticket_no, position)
);

CREATE INDEX IF NOT EXISTS idx_tickets_version_ticket
    ON tickets(version, ticket_no);

-- One row per submitted part line. ``position`` preserves the submitted
-- order and keeps duplicate part rows.
CREATE TABLE IF NOT EXISTS parts (
    version   TEXT NOT NULL REFERENCES manifests(version) ON DELETE CASCADE,
    ticket_no TEXT NOT NULL,
    position  INTEGER NOT NULL,
    part_code TEXT NOT NULL,
    PRIMARY KEY (version, ticket_no, position)
);

CREATE INDEX IF NOT EXISTS idx_parts_version_ticket
    ON parts(version, ticket_no);

-- Package boundaries and counts shown in the manifest. No joined detail
-- rows are stored here.
CREATE TABLE IF NOT EXISTS manifest_packages (
    version         TEXT NOT NULL REFERENCES manifests(version) ON DELETE CASCADE,
    package_no      INTEGER NOT NULL,
    first_ticket_no TEXT NOT NULL,
    last_ticket_no  TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    PRIMARY KEY (version, package_no)
);
"""


def database_path() -> Path:
    return Path(os.environ.get("SUPPLY_DB", str(DEFAULT_DB_PATH)))


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        isolation_level=None,
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL is persistent once set; re-asserting it can fail with "database is
    # locked" while another connection is mid-transaction, which is harmless.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Exclusive writer transaction: all-or-nothing snapshot publication."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
