"""Tests for the stored snapshot shape and streaming behaviour."""

import csv
import io
import sqlite3

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "supply.db"
    monkeypatch.setenv("SUPPLY_DB", str(db_path))
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client, db_path


def publish(client, tickets, parts, row_limit, batch="b1"):
    return client.post(
        f"/api/batches/{batch}/publish",
        files={
            "tickets": ("tickets.csv", tickets.encode(), "text/csv"),
            "parts": ("parts.csv", parts.encode(), "text/csv"),
        },
        data={"row_limit": str(row_limit)},
    )


def read_rows(text):
    return list(csv.reader(io.StringIO(text)))


def test_no_joined_detail_table_is_created(env):
    client, db_path = env
    publish(
        client,
        "ticket_no,station\nT1,S1\nT2,S2\n",
        "ticket_no,part_code\nT1,P1\nT1,P2\n",
        row_limit=10,
    )
    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row[0].startswith("sqlite_")
    }
    assert tables == {"manifests", "tickets", "parts", "manifest_packages"}

    # Only the two submitted source tables are stored; details are joined on
    # download. Duplicate source rows are preserved verbatim.
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 2
    # T1 joins to 2 rows, T2 has no parts and still keeps its 1 row.
    assert conn.execute(
        "SELECT row_count FROM manifest_packages"
    ).fetchall() == [(3,)]
    conn.close()


def test_large_package_streams_every_row(env):
    client, _ = env
    count = 3000
    tickets = "ticket_no,station\n" + "".join(
        f"W{i:05d},S{i % 7}\n" for i in range(count)
    )
    parts = "ticket_no,part_code\n" + "".join(
        f"W{i:05d},P{i % 11}\n" for i in range(count)
    )
    manifest = publish(client, tickets, parts, row_limit=count).json()
    assert manifest["package_count"] == 1
    package = manifest["packages"][0]
    assert package["row_count"] == count

    body = read_rows(client.get(package["download_url"]).text)
    assert len(body) == count + 1
    assert body[1] == ["W00000", "S0", "P0"]
    assert body[-1] == [f"W{count - 1:05d}", f"S{(count - 1) % 7}", f"P{(count - 1) % 11}"]
    assert [row[0] for row in body[1:]] == sorted(row[0] for row in body[1:])


def test_every_package_row_count_matches_its_download(env):
    client, _ = env
    tickets = "ticket_no,station\n" + "".join(
        f"T{i:03d},S\n" for i in range(40)
    )
    # Ticket T005 fans out 5 ways, so packages must not split it.
    parts = "ticket_no,part_code\n" + "".join(
        f"T{i:03d},P\n" for i in range(40)
    ) + "".join(f"T005,P{i}\n" for i in range(4))
    manifest = publish(client, tickets, parts, row_limit=7).json()
    assert sum(p["row_count"] for p in manifest["packages"]) == manifest["total_rows"]
    assert manifest["total_rows"] == 44

    previous_last = None
    for package in manifest["packages"]:
        body = read_rows(client.get(package["download_url"]).text)
        assert len(body) - 1 == package["row_count"]
        assert body[1][0] == package["first_ticket_no"]
        assert body[-1][0] == package["last_ticket_no"]
        if previous_last is not None:
            assert previous_last < package["first_ticket_no"]
        previous_last = package["last_ticket_no"]
