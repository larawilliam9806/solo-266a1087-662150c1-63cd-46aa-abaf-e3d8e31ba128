import pytest

from app import packing
from app.packing import Part, Ticket


def tickets(*rows):
    return [Ticket(t, s) for t, s in rows]


def parts(*rows):
    return [Part(t, p) for t, p in rows]


def summary_tuples(summaries):
    return [
        (p.package_no, p.first_ticket_no, p.last_ticket_no, p.row_count)
        for p in summaries
    ]


def test_left_join_keeps_tickets_without_parts_and_duplicates():
    rows = packing.join_detail_rows(
        tickets(("A", "s1"), ("A", "s1"), ("B", "s2")),
        parts(("A", "p1"), ("A", "p1")),
    )
    assert [(r.ticket_no, r.station, r.part_code) for r in rows] == [
        ("A", "s1", "p1"),
        ("A", "s1", "p1"),
        ("A", "s1", "p1"),
        ("A", "s1", "p1"),
        ("B", "s2", None),
    ]


def test_plan_orders_by_ticket_number_and_keeps_groups_whole():
    summaries = packing.plan(
        tickets(("B", "sb"), ("A", "sa"), ("C", "sc")),
        parts(("B", "pb1"), ("B", "pb2"), ("A", "pa")),
        row_limit=2,
    )
    assert summary_tuples(summaries) == [
        (1, "A", "A", 1),
        (2, "B", "B", 2),
        (3, "C", "C", 1),
    ]


def test_plan_fills_package_until_next_group_does_not_fit():
    summaries = packing.plan(
        tickets(("1", ""), ("2", ""), ("3", "")),
        parts(("1", "x"), ("1", "x"), ("2", "y"), ("2", "y")),
        row_limit=3,
    )
    # group "1" = 2 rows; adding group "2" would make 4 > 3, so package 1
    # holds only "1". Package 2 takes "2" (2 rows) then "3" (1 row) fits.
    assert summary_tuples(summaries) == [
        (1, "1", "1", 2),
        (2, "2", "3", 3),
    ]


def test_plan_uses_lexicographic_ticket_order():
    summaries = packing.plan(
        tickets(("T10", ""), ("T2", ""), ("T1", ""), ("t1", "")),
        parts(),
        row_limit=10,
    )
    assert summary_tuples(summaries) == [(1, "T1", "t1", 4)]


def test_plan_rejects_single_ticket_over_limit():
    with pytest.raises(packing.TicketTooLarge) as excinfo:
        packing.plan(
            tickets(("A", "s")),
            parts(("A", "p1"), ("A", "p2"), ("A", "p3")),
            row_limit=2,
        )
    assert excinfo.value.ticket_no == "A"
    assert excinfo.value.row_count == 3
    assert excinfo.value.row_limit == 2


def test_fanout_counts_every_duplicate_ticket_row():
    # two duplicate ticket rows x two part lines = 4 joined rows.
    summaries = packing.plan(
        tickets(("A", ""), ("A", "")),
        parts(("A", "p1"), ("A", "p2")),
        row_limit=4,
    )
    assert summary_tuples(summaries) == [(1, "A", "A", 4)]


def test_group_larger_than_limit_detected_after_earlier_packages():
    with pytest.raises(packing.TicketTooLarge) as excinfo:
        packing.plan(
            tickets(("A", ""), ("Z", "")),
            parts(("A", "p"), ("Z", "p1"), ("Z", "p2"), ("Z", "p3")),
            row_limit=2,
        )
    assert excinfo.value.ticket_no == "Z"
    assert excinfo.value.row_count == 3


def test_group_exactly_at_limit_is_accepted():
    summaries = packing.plan(
        tickets(("A", ""), ("A", "")),
        parts(("A", "p1"),),
        row_limit=2,
    )
    assert summary_tuples(summaries) == [(1, "A", "A", 2)]
