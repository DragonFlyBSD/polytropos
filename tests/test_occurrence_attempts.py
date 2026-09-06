"""How many jobs worked an occurrence, and how far they got.

The cockpit's occurrence selector shows both per occurrence. Both had
durable sources and neither had a query (poly-0e02.7): job_events records
every transition and is never pruned, jobs.bundle_id is indexed, and the
whole thing is one indexed join.

What is deliberately NOT here is the intra-job retry detail ("attempt 2 of
3"). That exists only as activity_log rows, which are pruned to a global
rolling cap, and a number that silently becomes 0 when it is evicted is
worse than no number.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.agent.lifecycle import (
    JOB_OUTCOME_STATES,
    JOB_STATE_DEPTH,
    JobState,
)
from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import occurrence_attempts
from dportsv3.tracker.server import create_app

TARGET = "@main"


def _bundle(conn, bundle_id, origin="devel/foo"):
    conn.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
        "result, issue_key) VALUES (?, 'r-1', ?, ?, '2026-09-05T00:00:00Z', "
        "'failure', 'i-1')",
        (bundle_id, origin, TARGET),
    )


def _job(conn, job_id, bundle_id, states, origin="devel/foo"):
    conn.execute(
        "INSERT INTO jobs(job_id, state, type, origin, target, bundle_id, "
        "created_ts_utc) VALUES (?, ?, 'patch', ?, ?, ?, 't')",
        (job_id, states[-1], origin, TARGET, bundle_id),
    )
    for n, state in enumerate(states):
        conn.execute(
            "INSERT INTO job_events(ts, job_id, to_state, event_name) "
            "VALUES (?, ?, ?, 'x')",
            (f"2026-09-05T00:{n:02d}:00Z", job_id, state),
        )


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    yield connection
    connection.close()


# --- the vocabulary ------------------------------------------------------


def test_every_job_state_is_either_a_depth_or_an_outcome() -> None:
    """A state in neither table would silently vanish from the aggregate."""
    covered = set(JOB_STATE_DEPTH) | JOB_OUTCOME_STATES

    assert {s.value for s in JobState} == covered


def test_the_terminals_are_outcomes_and_not_depths() -> None:
    """done, escalated and dead say how it ended, not how far it got, and
    ranking them as "furthest" would hide the work that preceded them."""
    for state in JOB_OUTCOME_STATES:
        assert state not in JOB_STATE_DEPTH


# --- the aggregate -------------------------------------------------------


def test_the_furthest_state_is_the_deepest_across_every_job(conn) -> None:
    """Two attempts: one died early, one got much further. The occurrence
    reached what the second reached."""
    _bundle(conn, "b-1")
    _job(conn, "j-1", "b-1", ["queued", "claimed", "triaging", "dead"])
    _job(conn, "j-2", "b-1",
         ["queued", "claimed", "triaging", "triaged", "patching", "done"])
    conn.commit()

    row = occurrence_attempts(conn, ["b-1"])["b-1"]

    assert row["jobs"] == 2
    assert row["furthest_state"] == "patching"
    assert row["outcome"] == "done"


def test_depth_is_comparable_across_job_types(conn) -> None:
    """A verify-fix job jumps from claimed straight to its own state, so
    the ranking is depth rather than a path through the machine."""
    _bundle(conn, "b-1")
    _job(conn, "j-1", "b-1", ["queued", "claimed", "verifying_fix", "done"])
    conn.commit()

    assert occurrence_attempts(conn, ["b-1"])["b-1"]["furthest_state"] == (
        "verifying_fix")


def test_the_newest_ending_is_the_outcome(conn) -> None:
    """A retried occurrence has several. The one that matters is the last."""
    _bundle(conn, "b-1")
    _job(conn, "j-1", "b-1", ["queued", "claimed", "dead"])
    conn.execute(
        "INSERT INTO jobs(job_id, state, type, origin, target, bundle_id, "
        "created_ts_utc) VALUES ('j-2', 'done', 'patch', 'devel/foo', ?, "
        "'b-1', 't')", (TARGET,),
    )
    conn.execute(
        "INSERT INTO job_events(ts, job_id, to_state, event_name) "
        "VALUES ('2026-09-06T00:00:00Z', 'j-2', 'done', 'x')"
    )
    conn.commit()

    assert occurrence_attempts(conn, ["b-1"])["b-1"]["outcome"] == "done"


def test_a_job_still_running_has_no_outcome(conn) -> None:
    _bundle(conn, "b-1")
    _job(conn, "j-1", "b-1", ["queued", "claimed", "patching"])
    conn.commit()

    row = occurrence_attempts(conn, ["b-1"])["b-1"]

    assert row["furthest_state"] == "patching"
    assert row["outcome"] is None


def test_an_occurrence_with_no_jobs_is_absent_not_zero(conn) -> None:
    """Absent means "no job was ever created". The caller renders that as
    "no jobs", not as an attempt count of 0."""
    _bundle(conn, "b-1")
    conn.commit()

    assert occurrence_attempts(conn, ["b-1"]) == {}


def test_it_answers_for_a_whole_page_in_one_pass(conn) -> None:
    """The selector shows every occurrence of an issue at once; a query per
    row would be N round-trips."""
    for i in range(12):
        _bundle(conn, f"b-{i}")
        _job(conn, f"j-{i}", f"b-{i}", ["queued", "claimed", "triaging"])
    conn.commit()

    rows = occurrence_attempts(conn, [f"b-{i}" for i in range(12)])

    assert len(rows) == 12
    assert all(r["furthest_state"] == "triaging" for r in rows.values())


def test_an_empty_id_list_asks_nothing(conn) -> None:
    assert occurrence_attempts(conn, []) == {}
    assert occurrence_attempts(conn, [None, ""]) == {}


def test_an_unknown_state_in_the_history_does_not_break_it(conn) -> None:
    """job_events is never pruned, so it holds transitions written by older
    versions of the state machine."""
    _bundle(conn, "b-1")
    _job(conn, "j-1", "b-1", ["queued", "claimed", "some_retired_state"])
    conn.commit()

    row = occurrence_attempts(conn, ["b-1"])["b-1"]

    assert row["furthest_state"] == "claimed"


# --- the page ------------------------------------------------------------


def test_the_occurrence_selector_says_how_far_each_one_got(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "updated_at) VALUES ('i-1', ?, 'devel/foo', 'unresolved', 2, 't')",
        (TARGET,),
    )
    _bundle(db, "b-1")
    _bundle(db, "b-2")
    _job(db, "j-1", "b-1",
         ["queued", "claimed", "triaging", "triaged", "patching", "done"])
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = client.get("/agentic/issues/i-1").text

    assert "1 job" in body
    assert "reached patching" in body
    assert "ended done" in body
    # The occurrence nothing ran on says so rather than showing a zero.
    assert "no jobs" in body
