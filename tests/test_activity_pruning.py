"""A pruned activity log is not a job where nothing happened (poly-a162).

activity_log is trimmed to a GLOBAL row cap -- runner.activity_log_max,
a DELETE in runner.py -- so a job's history does not age out on its own
schedule: a chatty neighbour evicts it. Measured on a live builder, the
5000-row window held 3 distinct jobs out of 1,452, and the page told the
other 1,449 "No activity recorded for this job".

Three states have to read differently, which is the whole bug: pruned,
nothing-yet, and partly pruned.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import activity_pruning

HORIZON = "2026-09-23T12:00:00+00:00"
BEFORE = "2026-09-23T06:00:00+00:00"
AFTER = "2026-09-23T18:00:00+00:00"


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (job_id, state, type, origin, flavor, bundle_dir, "
        "created_ts_utc, path, last_seen_at, target) VALUES "
        "('j1','escalated','patch','devel/foo','','',?,'',?,'@2026Q3')",
        (BEFORE, AFTER))
    return conn


def _started(conn: sqlite3.Connection, ts: str, job_id: str = "j1") -> None:
    conn.execute(
        "INSERT INTO job_events (ts, job_id, to_state, event_name) "
        "VALUES (?, ?, 'patching', 'claim')", (ts, job_id))


def _activity(conn: sqlite3.Connection, ts: str, job_id: str,
              stage: str = "llm_turn") -> None:
    conn.execute(
        "INSERT INTO activity_log (ts, job_id, stage, message, extra_json) "
        "VALUES (?, ?, ?, 'x', '{\"attempt\": 1, \"turn\": 1}')",
        (ts, job_id, stage))


# --- the query --------------------------------------------------------------

def test_a_job_that_started_before_the_horizon_is_pruned(tmp_path):
    conn = _db(tmp_path)
    _started(conn, BEFORE)
    _activity(conn, HORIZON, "someone-else")   # the surviving window
    conn.commit()

    got = activity_pruning(conn, "j1")
    assert got["pruned"] is True
    assert got["rows"] == 0
    assert got["job_started"] == BEFORE
    assert got["oldest_retained"] == HORIZON


def test_a_job_that_started_inside_the_window_is_not_pruned(tmp_path):
    conn = _db(tmp_path)
    _started(conn, AFTER)
    _activity(conn, HORIZON, "someone-else")
    conn.commit()

    assert activity_pruning(conn, "j1")["pruned"] is False


def test_it_fails_closed_with_no_activity_at_all(tmp_path):
    """A fresh install has no horizon to compare against, and must not
    announce pruning it cannot demonstrate."""
    conn = _db(tmp_path)
    _started(conn, BEFORE)
    conn.commit()

    got = activity_pruning(conn, "j1")
    assert got["pruned"] is False
    assert got["oldest_retained"] is None


def test_a_job_that_never_ran_is_not_pruned(tmp_path):
    conn = _db(tmp_path)
    _activity(conn, HORIZON, "someone-else")
    conn.commit()

    assert activity_pruning(conn, "j1")["pruned"] is False


# --- the page ---------------------------------------------------------------

def _body(tmp_path: Path, path: str = "/agentic/jobs/j1") -> str:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from dportsv3.tracker.server import create_app  # noqa: PLC0415

    with TestClient(create_app(tmp_path / "state.db")) as client:
        return client.get(path).text


def test_the_page_says_aged_out_not_nothing_recorded(tmp_path):
    conn = _db(tmp_path)
    _started(conn, BEFORE)
    _activity(conn, HORIZON, "someone-else")
    conn.commit(); conn.close()

    body = _body(tmp_path)
    assert 'id="activity-pruned"' in body
    assert "Activity has aged out" in body
    assert "No activity recorded for this job" not in body
    # The durable record is what the reader should trust instead.
    assert "were not pruned" in body


def test_a_job_with_nothing_yet_still_says_nothing_recorded(tmp_path):
    """The state the old sentence was written for, and it stays."""
    conn = _db(tmp_path)
    _started(conn, AFTER)
    _activity(conn, HORIZON, "someone-else")
    conn.commit(); conn.close()

    body = _body(tmp_path)
    assert "No activity recorded for this job" in body
    assert "aged out" not in body


def test_surviving_turns_are_marked_as_a_remnant(tmp_path):
    """Worse than the empty case: the survivors look like the whole story,
    and the attempt strip counts only them."""
    conn = _db(tmp_path)
    _started(conn, BEFORE)
    _activity(conn, HORIZON, "someone-else")
    _activity(conn, AFTER, "j1")
    conn.commit(); conn.close()

    body = _body(tmp_path)
    assert 'id="activity-partly-pruned"' in body
    assert "Earlier turns have aged out" in body
    assert 'id="turn-cards"' in body, "the survivors still render"


def test_the_transcript_says_it_too(tmp_path):
    """It is the surface that claims to be the WHOLE transcript."""
    conn = _db(tmp_path)
    _started(conn, BEFORE)
    _activity(conn, HORIZON, "someone-else")
    conn.commit(); conn.close()

    body = _body(tmp_path, "/agentic/jobs/j1/transcript")
    assert "Activity has aged out" in body
    assert "No activity recorded for this job" not in body
