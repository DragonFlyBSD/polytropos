"""What a verify request actually did, and where.

The tracker wrote verify_requests and never read it back, so everything the
row records was invisible: whether a verify was in flight, which env it was
asked to run in, and whether one never started at all (poly-0e02.6).

The env matters most. `bundles` has no env column, so the request is the
only record of where a verification ran -- and "verified" means very little
without it.

Reading the row is not enough. Its status stops at `enqueued`: the runner
sets pending -> enqueued (or failed) and stops, and the result posts back to
`bundles` without a request id to close. So an `enqueued` row may be
running, finished, or attached to a job that died, and the projection has to
reconcile three places to say which.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker import fix_state as fs
from dportsv3.tracker.agentic_queries import (
    latest_verify_request,
    verify_requests_for_bundle,
)
from dportsv3.tracker.server import create_app


def _req(**over):
    row = {"status": "enqueued", "env": "dev1", "requested_at": "t1",
           "job_id": "j-1", "job_state": None, "error": None}
    row.update(over)
    return row


# --- the states ----------------------------------------------------------


@pytest.mark.parametrize(("name", "bundle", "request_row", "key"), [
    ("nobody asked, nothing ran", {}, None, "none"),
    ("asked, not picked up", {}, _req(status="pending"), "queued"),
    ("enqueue itself failed", {}, _req(status="failed", error="no env"),
     "not_started"),
    ("job created, state unknown", {}, _req(), "starting"),
    ("job working", {}, _req(job_state="verifying_fix"), "running"),
    ("job ended, no result", {}, _req(job_state="done"), "lost"),
    ("verified",
     {"verification_status": "verified", "verification_at": "t2"},
     _req(job_state="done"), "passed"),
    ("verify failed",
     {"verification_status": "verification_failed", "verification_at": "t2"},
     _req(job_state="done"), "failed"),
])
def test_each_shape_reconciles_to_one_state(name, bundle, request_row, key):
    assert fs.verify_state(bundle, request_row).key == key, name


def test_the_env_is_carried_into_the_label() -> None:
    """"verified" without a where is close to meaningless, and the request
    is the only place the env is recorded."""
    state = fs.verify_state(
        {"verification_status": "verified", "verification_at": "t2"},
        _req(env="dports-2026Q3", job_state="done"),
    )

    assert state.env == "dports-2026Q3"
    assert state.label == "verified in dports-2026Q3"


def test_a_verification_with_no_request_says_it_does_not_know_where() -> None:
    """The agent's own verify path records a result without a request, so
    the env is genuinely unknown rather than missing."""
    state = fs.verify_state(
        {"verification_status": "verified", "verification_at": "t2"}, None)

    assert state.key == "passed"
    assert state.env is None
    assert "somewhere unrecorded" in state.detail


def test_an_older_result_does_not_answer_a_newer_request() -> None:
    """A re-verify while a green result is on the row must read as running,
    not as already verified. Compared by timestamp because the post-back
    carries no request id."""
    state = fs.verify_state(
        {"verification_status": "verified", "verification_at": "t0"},
        _req(requested_at="t1", job_state="verifying_fix"),
    )

    assert state.key == "running"


def test_a_failed_verify_says_what_went_wrong() -> None:
    """poly-9az stored the reason; this is what reads it back."""
    state = fs.verify_state(
        {"verification_status": "verification_failed", "verification_at": "t2",
         "verification_exit_code": 1,
         "verification_reason": "check-sanity: ineffective options helper"},
        _req(job_state="done"),
    )

    assert "dsynth exited 1" in state.detail
    assert "ineffective options helper" in state.detail


def test_a_job_that_died_is_not_reported_as_running() -> None:
    """`enqueued` is not a terminal status and nothing ever closes it, so
    without the job state a dead verify reads as in flight forever."""
    running = fs.verify_state({}, _req(job_state="verifying_fix"))
    dead = fs.verify_state({}, _req(job_state="dead"))

    assert running.key == "running"
    assert dead.key == "lost"
    assert "run it again" in dead.detail.lower()


# --- the query -----------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    connection.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, result) "
        "VALUES ('b-1', 'r-1', 'devel/foo', '@main', 't', 'failure')"
    )
    connection.execute(
        "INSERT INTO jobs(job_id, state, type, origin, target, bundle_id, "
        "created_ts_utc) VALUES ('j-1', 'verifying_fix', 'verify', "
        "'devel/foo', '@main', 'b-1', 't')"
    )
    for i, (env, at, status, job) in enumerate((
        ("dev-old", "2026-09-01T00:00:00Z", "enqueued", None),
        ("dev-new", "2026-09-05T00:00:00Z", "enqueued", "j-1"),
    )):
        connection.execute(
            "INSERT INTO verify_requests(bundle_id, env, requested_by, "
            "requested_at, status, job_id) VALUES ('b-1', ?, 'operator', ?, ?, ?)",
            (env, at, status, job),
        )
    connection.commit()
    yield connection
    connection.close()


def test_the_latest_request_is_the_newest_one(conn) -> None:
    assert latest_verify_request(conn, "b-1")["env"] == "dev-new"


def test_the_request_carries_its_jobs_state(conn) -> None:
    """Without it the projection cannot tell a running verify from a dead
    one -- the request's own status says `enqueued` for both."""
    assert latest_verify_request(conn, "b-1")["job_state"] == "verifying_fix"


def test_a_request_with_no_job_yet_carries_no_state(conn) -> None:
    older = verify_requests_for_bundle(conn, "b-1")[1]

    assert older["env"] == "dev-old"
    assert older["job_state"] is None


def test_the_history_is_newest_first(conn) -> None:
    """`bundles` records only the LAST verification, so the requests are the
    only trace that an earlier one was asked for and where."""
    envs = [r["env"] for r in verify_requests_for_bundle(conn, "b-1")]

    assert envs == ["dev-new", "dev-old"]


def test_an_unverified_bundle_has_no_request(conn) -> None:
    assert latest_verify_request(conn, "b-nothing") is None


# --- the page ------------------------------------------------------------


def test_the_bundle_page_says_where_a_verification_ran(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
        "result, resolution, verification_status, verification_at) "
        "VALUES ('b-1', 'r-1', 'devel/foo', '@main', 't', 'failure', "
        "'agent_fixed', 'verified', '2026-09-05T12:00:00Z')"
    )
    db.execute(
        "INSERT INTO verify_requests(bundle_id, env, requested_by, "
        "requested_at, status, job_id) VALUES ('b-1', 'dports-2026Q3', "
        "'operator', '2026-09-05T11:00:00Z', 'enqueued', 'j-1')"
    )
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = client.get("/agentic/bundles/b-1").text

    assert "verified in dports-2026Q3" in body


def test_the_bundle_page_says_a_verify_never_started(tmp_path: Path) -> None:
    """The row recorded it and nothing ever showed it."""
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
        "result, resolution) VALUES ('b-1', 'r-1', 'devel/foo', '@main', 't', "
        "'failure', 'agent_fixed')"
    )
    db.execute(
        "INSERT INTO verify_requests(bundle_id, env, requested_by, "
        "requested_at, status, error) VALUES ('b-1', 'gone', 'operator', "
        "'2026-09-05T11:00:00Z', 'failed', 'dev-env gone')"
    )
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = client.get("/agentic/bundles/b-1").text

    assert "verify never started" in body
    assert "dev-env gone" in body
