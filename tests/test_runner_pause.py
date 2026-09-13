"""An operator can stop the runner without killing it (poly-0w6j).

The runner pauses itself for three reasons -- broken dev-env, no env
resolved, dsynth holding the build lock -- and an operator had no way to
stop it short of killing the process, which loses whatever job is in
flight.

runner_status.status is not the field: the runner rewrites it on
essentially every tick, so a tracker write there would be overwritten, and
the runner could not tell its own pause from the operator's. Both are the
same string in the same column.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import get_runner_control, set_runner_pause
from dportsv3.tracker.server import create_app

RUNNER = Path(__file__).resolve().parents[1] / "dportsv3" / "agent" / "runner.py"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.close()
    return path


@pytest.fixture
def client(db_path: Path) -> TestClient:
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# --- the intent is durable ------------------------------------------------


def test_a_tracker_that_has_never_been_paused_is_not_paused(
    db_path: Path,
) -> None:
    conn = _conn(db_path)
    assert get_runner_control(conn)["paused"] is False
    conn.close()


def test_a_pause_survives_being_read_back(db_path: Path) -> None:
    """Durable, because a pause that vanishes on runner restart is a
    trap."""
    conn = _conn(db_path)
    set_runner_pause(conn, True, reason="dsynth is rebuilding the ports tree")
    conn.close()

    conn = _conn(db_path)
    control = get_runner_control(conn)
    conn.close()

    assert control["paused"] is True
    assert control["reason"] == "dsynth is rebuilding the ports tree"
    assert control["requested_at"]


def test_resuming_clears_the_reason(db_path: Path) -> None:
    """A stale reason beside "paused: no" reads as though it were still in
    force. What happened lives in the activity log."""
    conn = _conn(db_path)
    set_runner_pause(conn, True, reason="holding for a release")
    control = set_runner_pause(conn, False)
    conn.close()

    assert control["paused"] is False
    assert control["reason"] is None


def test_pausing_twice_replaces_rather_than_accumulates(
    db_path: Path,
) -> None:
    conn = _conn(db_path)
    set_runner_pause(conn, True, reason="first")
    set_runner_pause(conn, True, reason="second")
    rows = conn.execute("SELECT COUNT(*) FROM runner_control").fetchone()[0]
    control = get_runner_control(conn)
    conn.close()

    assert rows == 1
    assert control["reason"] == "second"


# --- the gate reads it, and reads it first --------------------------------


def test_the_gate_checks_the_operator_pause_before_its_own(
) -> None:
    """Ordered the other way, a health pause clearing would log "resumed"
    and start claiming work again while the operator's hold stood."""
    src = RUNNER.read_text()
    gate = src[src.index("def _gate_blocked()"):]
    gate = gate[:gate.index("\n    try:")]

    assert "_operator_pause()" in gate
    # ...before the health probe, the no-env hold and the dsynth-busy gate.
    assert gate.index("_operator_pause()") < gate.index("probe_health_cached")
    assert gate.index("_operator_pause()") < gate.index("_no_env_reason()")
    assert gate.index("_operator_pause()") < gate.index("dsynth_active(")


def test_an_unreadable_control_row_does_not_stop_the_runner() -> None:
    """A runner that stops working because it could not read a table is a
    worse failure than one that keeps going."""
    src = RUNNER.read_text()
    fn = src[src.index("def _operator_pause()"):]
    fn = fn[:fn.index("\ndef ")]

    assert "except sqlite3.Error" in fn
    assert fn.count('"paused": False') >= 2


def test_the_runner_says_who_paused_it() -> None:
    """"paused" on that page has meant four different things; the stage
    string is what tells them apart."""
    src = RUNNER.read_text()

    assert "operator_paused by" in src


# --- the endpoint ---------------------------------------------------------


def test_pausing_is_an_endpoint(client: TestClient, db_path: Path) -> None:
    resp = client.put("/api/runner/pause",
                      json={"paused": True, "reason": "upgrading dsynth"})

    assert resp.status_code == 200
    assert resp.json()["paused"] is True
    conn = _conn(db_path)
    assert get_runner_control(conn)["reason"] == "upgrading dsynth"
    conn.close()


def test_resuming_is_the_same_endpoint(
    client: TestClient, db_path: Path,
) -> None:
    client.put("/api/runner/pause", json={"paused": True})

    resp = client.put("/api/runner/pause", json={"paused": False})

    assert resp.json()["paused"] is False


def test_a_pause_without_a_reason_is_allowed(client: TestClient) -> None:
    """An urgent pause should not be gated behind typing."""
    assert client.put(
        "/api/runner/pause", json={"paused": True},
    ).status_code == 200


def test_the_body_has_to_say_which_way(client: TestClient) -> None:
    assert client.put("/api/runner/pause", json={}).status_code == 400
    assert client.put(
        "/api/runner/pause", json={"paused": "yes"},
    ).status_code == 400


def test_an_anonymous_reader_cannot_pause_the_runner(
    db_path: Path, set_setting,
) -> None:
    """Every other write is behind this gate; stopping the fleet is not
    the one to leave open."""
    set_setting("tracker.public_readonly", True)

    with TestClient(create_app(db_path)) as anon:
        assert anon.put(
            "/api/runner/pause", json={"paused": True},
        ).status_code == 403


# --- the page -------------------------------------------------------------


def test_the_runner_page_offers_the_hold(client: TestClient) -> None:
    body = client.get("/agentic/runner").text

    assert "runner-hold-btn" in body
    assert "The runner is claiming work" in body


def test_the_page_says_a_running_job_is_not_abandoned(
    client: TestClient,
) -> None:
    """It stops claiming, not the job in flight. Killing the process is
    what loses work, and this exists so nobody has to."""
    body = client.get("/agentic/runner").text

    assert "runs to its end" in body


def test_a_held_runner_says_so_and_why(
    client: TestClient, db_path: Path,
) -> None:
    conn = _conn(db_path)
    set_runner_pause(conn, True, reason="waiting on a kernel bump")
    conn.close()

    body = client.get("/agentic/runner").text

    assert "You have paused the runner" in body
    assert "waiting on a kernel bump" in body
    assert ">Resume<" in body or "Resume" in body


def test_the_page_no_longer_claims_there_is_no_operator_pause(
    client: TestClient,
) -> None:
    body = client.get("/agentic/runner").text

    assert "There is no operator pause" not in body
