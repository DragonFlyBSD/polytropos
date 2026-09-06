"""Reads for the pipeline's delivery stage (poly-0e02.2).

Two things were missing. Nothing returned delivery rows as a *set* -- the
drawer would have been open_delivery_bundle_ids plus one lookup per id --
and the figure the stage was going to show, issues in `resolving` labelled
"open PRs", measures a different population than the one it names.

Since A2 an issue resolves when a build proves the fix, never when a PR
merges, so the two axes come apart in both directions. poly-8e2 measured
17 issues that reached `resolved` with a create_failed delivery and no PR
ever opened. The counts here keep them apart by name.
"""

from __future__ import annotations

import sqlite3

import pytest

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import (
    DELIVERY_OPEN_STATUSES,
    DELIVERY_STATUSES,
    count_deliveries,
    delivery_counts,
    list_deliveries,
)

TARGET = "@main"


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_db(db)
    return db


def _issue(db, key, state="resolving"):
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "updated_at) VALUES (?, ?, ?, ?, 1, 't')",
        (key, TARGET, f"devel/{key}", state),
    )


def _bundle(db, bundle_id, *, origin="devel/thing", issue_key=None,
            resolution=None):
    db.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target, "
        "issue_key, resolution) VALUES (?, ?, 't1', 'failure', ?, ?, ?)",
        (bundle_id, origin, TARGET, issue_key, resolution),
    )


def _delivery(db, bundle_id, status, *, provider="github", pr=None,
              branch=None, error=None):
    db.execute(
        "INSERT INTO bundle_review_requests(bundle_id, provider, "
        "provider_pr_id, url, branch, status, created_at, error) "
        "VALUES (?, ?, ?, ?, ?, ?, 't1', ?)",
        (bundle_id, provider, pr,
         f"https://example.invalid/pull/{pr}" if pr else None,
         branch, status, error),
    )


# --- the newest row per bundle is the delivery ----------------------------


def test_the_newest_row_is_the_one_that_counts(conn) -> None:
    """A PR that was merged out of band must not read as open because an
    older row still says created -- the same rule
    open_delivery_bundle_ids keeps, spelled as a predicate."""
    _bundle(conn, "b-1")
    _delivery(conn, "b-1", "created", pr="10")
    _delivery(conn, "b-1", "merged", pr="10")

    rows = list_deliveries(conn)

    assert [r["status"] for r in rows] == ["merged"]
    assert list_deliveries(conn, statuses=DELIVERY_OPEN_STATUSES) == []


def test_one_row_per_bundle_however_many_attempts(conn) -> None:
    _bundle(conn, "b-1")
    for n in range(5):
        _delivery(conn, "b-1", "create_failed", branch=f"fix/attempt-{n}")

    assert count_deliveries(conn) == 1
    assert list_deliveries(conn)[0]["branch"] == "fix/attempt-4"


def test_each_bundle_carries_its_own_latest(conn) -> None:
    _bundle(conn, "b-1")
    _bundle(conn, "b-2")
    _delivery(conn, "b-1", "created", pr="1")
    _delivery(conn, "b-2", "created", pr="2")
    _delivery(conn, "b-1", "merged", pr="1")

    by_bundle = {r["bundle_id"]: r["status"] for r in list_deliveries(conn)}

    assert by_bundle == {"b-1": "merged", "b-2": "created"}


# --- the drawer's row shape -----------------------------------------------


def test_a_row_carries_the_issue_and_origin_it_belongs_to(conn) -> None:
    """The whole point of the query: (issue, origin, provider, PR, status)
    in one read rather than a lookup per row."""
    _issue(conn, "i-1", state="resolving")
    _bundle(conn, "b-1", origin="graphics/jpeg-turbo", issue_key="i-1",
            resolution="accepted")
    _delivery(conn, "b-1", "created", pr="4242", branch="fix/jpeg")

    row = list_deliveries(conn)[0]

    assert row["origin"] == "graphics/jpeg-turbo"
    assert row["issue_key"] == "i-1"
    assert row["issue_state"] == "resolving"
    assert row["bundle_resolution"] == "accepted"
    assert row["provider_pr_id"] == "4242"
    assert row["url"] == "https://example.invalid/pull/4242"
    assert row["target"] == TARGET


def test_a_delivery_whose_bundle_is_gone_still_lists(conn) -> None:
    """A delivery row outlives its bundle being pruned. It is still a real
    PR upstream, and dropping it would make the drawer disagree with the
    count that sits above it."""
    _delivery(conn, "b-vanished", "created", pr="7")

    rows = list_deliveries(conn)

    assert len(rows) == 1
    assert rows[0]["origin"] is None
    assert count_deliveries(conn) == 1


def test_a_bundle_with_no_issue_still_lists(conn) -> None:
    _bundle(conn, "b-1", issue_key=None)
    _delivery(conn, "b-1", "created", pr="7")

    assert list_deliveries(conn)[0]["issue_state"] is None


def test_deliveries_come_back_newest_first(conn) -> None:
    """id is AUTOINCREMENT, so delivery order needs no timestamp and no
    clock -- every row here carries the same created_at."""
    for n in range(4):
        _bundle(conn, f"b-{n}")
        _delivery(conn, f"b-{n}", "created", pr=str(n))

    assert [r["bundle_id"] for r in list_deliveries(conn)] == [
        "b-3", "b-2", "b-1", "b-0",
    ]


# --- filters --------------------------------------------------------------


def test_an_unknown_status_raises_rather_than_matching_nothing(conn) -> None:
    """A filter that quietly returns an empty page reads exactly like a
    stage with no work in it."""
    with pytest.raises(ValueError, match="shipped"):
        list_deliveries(conn, statuses=["shipped"])


def test_an_empty_status_filter_matches_nothing(conn) -> None:
    _bundle(conn, "b-1")
    _delivery(conn, "b-1", "created", pr="1")

    assert list_deliveries(conn, statuses=[]) == []
    assert count_deliveries(conn, statuses=[]) == 0


def test_the_open_statuses_are_the_ones_still_upstream(conn) -> None:
    for n, status in enumerate(DELIVERY_STATUSES):
        _bundle(conn, f"b-{n}")
        _delivery(conn, f"b-{n}", status, pr=str(n))

    open_rows = list_deliveries(conn, statuses=DELIVERY_OPEN_STATUSES)

    assert {r["status"] for r in open_rows} == {"created", "updated"}


def test_provider_filters(conn) -> None:
    _bundle(conn, "b-1")
    _bundle(conn, "b-2")
    _delivery(conn, "b-1", "created", provider="github", pr="1")
    _delivery(conn, "b-2", "created", provider="local-patch")

    assert count_deliveries(conn, provider="local-patch") == 1


@pytest.mark.parametrize("term", ["graphics/", "4242", "fix/jpeg"])
def test_search_answers_what_an_operator_types_or_pastes(conn, term) -> None:
    _bundle(conn, "b-1", origin="graphics/jpeg-turbo")
    _bundle(conn, "b-2", origin="devel/other")
    _delivery(conn, "b-1", "created", pr="4242", branch="fix/jpeg")
    _delivery(conn, "b-2", "created", pr="9", branch="fix/other")

    rows = list_deliveries(conn, search=term)

    assert [r["bundle_id"] for r in rows] == ["b-1"]


def test_search_wildcards_are_literal(conn) -> None:
    """An operator searching for _ must not match every row."""
    _bundle(conn, "b-1", origin="devel/a_b")
    _bundle(conn, "b-2", origin="devel/axb")
    _delivery(conn, "b-1", "created", pr="1")
    _delivery(conn, "b-2", "created", pr="2")

    assert [r["bundle_id"] for r in list_deliveries(conn, search="a_b")] == [
        "b-1",
    ]


def test_the_page_and_its_total_describe_the_same_set(conn) -> None:
    for n in range(7):
        _bundle(conn, f"b-{n}", origin="devel/wanted" if n % 2 else "x/other")
        _delivery(conn, f"b-{n}", "created", pr=str(n))

    page = list_deliveries(conn, search="wanted", limit=2)
    rest = list_deliveries(conn, search="wanted", limit=2, offset=2)

    assert count_deliveries(conn, search="wanted") == 3
    assert len(page) == 2
    assert len(rest) == 1
    assert not {r["id"] for r in page} & {r["id"] for r in rest}


# --- the two axes ---------------------------------------------------------


def test_the_status_histogram_is_zero_filled(conn) -> None:
    counts = delivery_counts(conn)

    assert counts["by_status"] == {s: 0 for s in DELIVERY_STATUSES}
    assert counts["total"] == 0
    assert counts["open"] == 0


def test_a_status_nobody_named_is_counted_rather_than_dropped(conn) -> None:
    """update_review_request_status takes any string, so a writer can add a
    status. Losing it would make the stage look emptier than it is; showing
    an unnamed one on the page is the signal to name it."""
    _bundle(conn, "b-1")
    _delivery(conn, "b-1", "abandoned")

    counts = delivery_counts(conn)

    assert counts["by_status"]["abandoned"] == 1
    assert counts["total"] == 1


def test_open_and_failed_are_read_off_the_histogram(conn) -> None:
    for n, status in enumerate(DELIVERY_STATUSES):
        _bundle(conn, f"b-{n}")
        _delivery(conn, f"b-{n}", status, pr=str(n))

    counts = delivery_counts(conn)

    assert counts["open"] == 2          # created + updated
    assert counts["failed"] == 1        # create_failed
    assert counts["total"] == len(DELIVERY_STATUSES)


def test_awaiting_confirm_build_is_not_the_open_pr_count(conn) -> None:
    """The conflation this bead exists to stop. `resolving` means a fix was
    accepted and a build must prove it; it says nothing about whether a PR
    is open, and here the two figures disagree in both directions."""
    _issue(conn, "i-waiting", state="resolving")     # no delivery at all
    _issue(conn, "i-open", state="unresolved")
    _bundle(conn, "b-open", issue_key="i-open")
    _delivery(conn, "b-open", "created", pr="1")     # open PR, not resolving

    counts = delivery_counts(conn)

    assert counts["awaiting_confirm_build"] == 1
    assert counts["open"] == 1
    assert counts["by_status"]["created"] == 1


def test_a_resolved_issue_can_have_a_delivery_that_never_landed(conn) -> None:
    """poly-8e2's population, in one fixture: the build proved the fix, the
    issue archived as resolved, and no PR was ever opened. It is a
    create_failed delivery and it must be countable as one."""
    _issue(conn, "i-1", state="resolved")
    _bundle(conn, "b-1", issue_key="i-1", resolution="accepted")
    _delivery(conn, "b-1", "create_failed", error="GitApplyConflict")

    counts = delivery_counts(conn)
    row = list_deliveries(conn, statuses=["create_failed"])[0]

    assert counts["failed"] == 1
    assert counts["open"] == 0
    assert counts["awaiting_confirm_build"] == 0
    assert row["issue_state"] == "resolved"
    assert row["bundle_resolution"] == "accepted"
    assert row["error"] == "GitApplyConflict"
    assert row["url"] is None
