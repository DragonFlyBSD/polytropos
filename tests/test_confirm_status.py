"""Where an issue is in the confirm-build loop, and how it says so.

`resolving` is one word covering a whole loop: a fix has been accepted and a
confirm build must prove it. Seven columns say where in that loop the issue
is, and until confirm_status existed none of them reached a screen -- an
operator could see that an issue was stuck but not whether a build was
running, queued, backing off, or given up on (poly-chf).

The predicates here have to agree with issues_needing_build: a badge that
says "queued" for an issue the reconcile feed will never pick up is worse
than no badge.
"""

from __future__ import annotations

import sqlite3

import pytest

from dportsv3.db.schema import init_db
from dportsv3.tracker import issue_state
from dportsv3.tracker.agentic_queries import (
    issues_needing_build,
    runner_is_live,
)

NOW = "2026-09-06T12:00:00+00:00"
LATER = "2099-01-01T00:00:00+00:00"


def _issue(**over):
    row = {
        "issue_key": "i-1", "state": "resolving", "target": "@main",
        "requested_build_generation": 1,
        "last_confirmed_build_generation": 0,
        "building_generation": None,
        "confirm_green_count": 0,
        "confirm_failure_count": 0,
        "next_eligible_at": None,
        "green_head_run_id": None,
    }
    row.update(over)
    return row


def _status(**over):
    return issue_state.confirm_status(
        _issue(**over), threshold=2, max_failures=3, now=NOW,
        runner_live=over.pop("_runner_live", True),
    )


# --- the ten states ------------------------------------------------------


@pytest.mark.parametrize(("over", "key"), [
    ({}, "queued"),
    ({"building_generation": 1}, "building"),
    ({"confirm_green_count": 1}, "provisional"),
    ({"confirm_failure_count": 3}, "spent"),
    ({"confirm_failure_count": 1, "next_eligible_at": LATER}, "waiting"),
    ({"last_confirmed_build_generation": 1}, "withdrawn"),
    ({"state": "resolved", "green_head_run_id": 42}, "confirmed"),
    ({"state": "resolved"}, "manual"),
    ({"state": "unresolved"}, "absent"),
    ({"state": "muted"}, "absent"),
])
def test_each_shape_of_the_columns_reads_as_one_state(over, key) -> None:
    assert issue_state.confirm_status(
        _issue(**over), threshold=2, max_failures=3, now=NOW,
    ).key == key


def test_a_marker_left_by_a_dead_runner_is_not_a_running_build() -> None:
    """building_generation survives a crash until the next runner start
    clears it, so without the heartbeat the two are identical."""
    running = issue_state.confirm_status(
        _issue(building_generation=1), now=NOW, runner_live=True)
    dead = issue_state.confirm_status(
        _issue(building_generation=1), now=NOW, runner_live=False)

    assert running.key == "building"
    assert dead.key == "stalled"
    assert "runner is not running" in dead.detail
    assert "next runner start" in dead.detail


def test_an_unknown_liveness_is_treated_as_running() -> None:
    """None means "not asked", not "dead". Claiming a stall on no evidence
    would send an operator chasing a healthy build."""
    assert issue_state.confirm_status(
        _issue(building_generation=1), now=NOW).key == "building"


def test_a_stale_generation_marker_does_not_hide_a_newer_request() -> None:
    """A re-accept bumps the request past an in-flight build. The feed
    treats that as claimable again, so the badge must not say "running"."""
    status = issue_state.confirm_status(
        _issue(requested_build_generation=3, building_generation=1), now=NOW)

    assert status.key == "queued"


def test_the_provisional_count_is_shown_against_its_threshold() -> None:
    status = issue_state.confirm_status(
        _issue(confirm_green_count=1), threshold=3, now=NOW)

    assert status.label == "provisional green 1/3"
    assert status.greens == 1
    assert status.threshold == 3


def test_a_resolved_issue_names_its_known_good_watermark() -> None:
    status = issue_state.confirm_status(
        _issue(state="resolved", green_head_run_id=42), now=NOW)

    assert "#42" in status.detail
    assert status.green_head_run_id == 42


def test_a_hand_resolved_issue_says_it_has_no_watermark() -> None:
    """It is the difference between "a build proved this" and "someone said
    so", and it changes how a recurrence is detected."""
    status = issue_state.confirm_status(_issue(state="resolved"), now=NOW)

    assert status.key == "manual"
    assert status.green_head_run_id is None
    assert "timestamp" in status.detail


# --- agreement with the reconcile feed -----------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    yield connection
    connection.close()


def _insert(conn: sqlite3.Connection, **over) -> dict:
    row = _issue(**over)
    conn.execute(
        "INSERT INTO issues(issue_key, target, origin, state, "
        "requested_build_generation, last_confirmed_build_generation, "
        "building_generation, confirm_green_count, confirm_failure_count, "
        "next_eligible_at, green_head_run_id, times_seen, updated_at) "
        "VALUES (:issue_key, :target, 'devel/foo', :state, "
        ":requested_build_generation, :last_confirmed_build_generation, "
        ":building_generation, :confirm_green_count, :confirm_failure_count, "
        ":next_eligible_at, :green_head_run_id, 1, '2026-09-06T00:00:00Z')",
        row,
    )
    conn.commit()
    return row


@pytest.mark.parametrize(("over", "expected_in_feed"), [
    ({}, True),                                             # queued
    ({"confirm_green_count": 1}, True),                     # provisional
    ({"building_generation": 1}, False),                    # building
    ({"last_confirmed_build_generation": 1}, False),        # withdrawn
    ({"confirm_failure_count": 1, "next_eligible_at": LATER}, False),  # waiting
    ({"requested_build_generation": 3, "building_generation": 1}, True),
    ({"state": "resolved"}, False),
    ({"state": "unresolved"}, False),
])
def test_the_badge_agrees_with_the_feed_about_whether_a_build_is_coming(
    conn: sqlite3.Connection, over, expected_in_feed,
) -> None:
    """confirm_status' predicates mirror issues_needing_build's WHERE. If
    they drift, the panel promises a build that will never be claimed."""
    row = _insert(conn, **over)
    status = issue_state.confirm_status(row, now=NOW)

    in_feed = bool(issues_needing_build(conn, now=NOW))
    assert in_feed is expected_in_feed
    # "the feed will act on this next pass" is exactly these two states.
    assert (status.key in ("queued", "provisional")) is expected_in_feed


def test_reaching_the_cap_reopens_the_issue_rather_than_parking_it(
    conn: sqlite3.Connection,
) -> None:
    """The bead asked for a "stalled with building_generation set after a
    gave-up" state. It cannot happen: _record_confirm_failure clears the
    marker AND reopens the issue to `unresolved` in the same transaction
    that reaches the cap. So an issue at or past the cap while still
    `resolving` only exists if the cap was lowered underneath it."""
    row = _insert(conn, confirm_failure_count=5)
    status = issue_state.confirm_status(row, max_failures=3, now=NOW)

    assert status.key == "spent"
    assert "reopens this to the worklist" in status.detail
    assert "stopped retrying" not in status.detail


# --- the label the merge cannot make true --------------------------------


def test_a_resolving_issue_no_longer_claims_to_await_delivery() -> None:
    """delivery_sync returns early for a `resolving` issue -- since A2 the
    build is the authority, not the merge -- so "awaiting delivery" named
    the one thing that provably will not move it."""
    status = issue_state.issue_status({"state": "resolving"})

    assert status.label == "confirming"
    assert "deliver" not in status.label


def test_the_worklist_band_says_what_it_is_waiting_for() -> None:
    bands = dict((k, label) for k, label, _ in
                 issue_state.ISSUE_WORKLIST_SECTIONS)

    assert "confirming" in bands
    assert bands["confirming"] == "Awaiting build confirmation"
    assert "delivering" not in bands


def test_a_resolving_issue_buckets_as_confirming() -> None:
    assert issue_state.issue_bucket({"state": "resolving"}, []) == "confirming"


# --- the heartbeat -------------------------------------------------------


def test_an_absent_heartbeat_is_not_a_live_runner(
    conn: sqlite3.Connection,
) -> None:
    assert runner_is_live(conn) is False


def test_a_fresh_heartbeat_is_live_and_a_stale_one_is_not(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO runner_status(id, status, updated_at) "
        "VALUES (1, 'processing', '2026-09-06T12:00:00+00:00')"
    )
    conn.commit()

    assert runner_is_live(conn, now="2026-09-06T12:00:30+00:00") is True
    assert runner_is_live(conn, now="2026-09-06T12:05:00+00:00") is False


def test_an_unparseable_heartbeat_is_not_live(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO runner_status(id, status, updated_at) "
        "VALUES (1, 'processing', 'not a timestamp')"
    )
    conn.commit()

    assert runner_is_live(conn) is False


def test_no_breadcrumb_still_calls_the_repairs_view_agentic() -> None:
    """The crumb links to /agentic, which the shell's nav calls Repairs.
    Leaving it as "Agentic" made every one of these pages contradict the
    header directly above it."""
    from pathlib import Path

    templates = Path(__file__).resolve().parents[1] / "dportsv3" / "tracker" / "templates"
    offenders = [
        f.name for f in templates.glob("*.html")
        if '"label": "Agentic"' in f.read_text()
    ]

    assert offenders == []
