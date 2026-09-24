"""The HTTP transport means the same thing as the local one (poly-fij.12
step 2).

Parity between the transports holds BY CONSTRUCTION -- both build the same
payload and hand it to the same ``db.presence.apply`` -- so the parity
test here is a guard on that architecture rather than a behaviour check.
Mutation-checked, and worth stating because it is easy to over-trust:
breaking the shared SQL breaks both transports identically and parity
still passes. What it does catch is a SECOND code path appearing, and
whatever the wire itself mangles -- a dict that does not survive JSON, an
int arriving as a string, a field dropped in transit.

The behaviour lives in the over-the-wire tests below it, and in
``test_state_store_seam.py`` for the local side.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.agent import runner as rm
from dportsv3.agent import state_store
from dportsv3.artifact_store import ArtifactStore
from dportsv3.db.schema import init_db
from dportsv3.tracker.server import create_app

#: One sequence exercising every event, and the transitions between them
#: that carry the subtle behaviour: two status writes on the same job
#: (started_at held), then one on a different job (started_at reset).
SEQUENCE = [
    ("register", ()),
    ("status", ("processing", "job-1", "patch_start", None)),
    ("heartbeat", ()),
    ("status", ("processing", "job-1", "dsynth_build", None)),
    # A dict, because that is the one payload field the wire can mangle:
    # it round-trips through JSON twice before it becomes extra_json.
    ("status", ("processing", "job-2", "patch_start",
                {"origin": "devel/glib20", "attempt": 2})),
    ("heartbeat", ()),
    ("deregister", ()),
]

RUNNER = "builder-A"


def _drive(store) -> None:
    for event, args in SEQUENCE:
        if event == "register":
            store.register_runner(RUNNER)
        elif event == "heartbeat":
            store.heartbeat(RUNNER)
        elif event == "status":
            store.set_runner_status(*args)
        else:
            store.deregister_runner(RUNNER)


def _shape(conn: sqlite3.Connection) -> dict:
    """The rows, with the timestamps that differ per run stripped to the
    fact the test is about: whether they moved together."""
    runners = [dict(r) for r in conn.execute(
        "SELECT runner_id, hostname, pid, stopped_at IS NULL AS live "
        "FROM runners ORDER BY runner_id"
    ).fetchall()]
    st = conn.execute(
        "SELECT status, job_id, current_stage, "
        "       started_at = updated_at AS clock_reset, extra_json "
        "FROM runner_status WHERE id = 1"
    ).fetchone()
    return {"runners": runners, "status": dict(st)}


@pytest.fixture
def local(monkeypatch, tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "local.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    monkeypatch.setattr(rm, "_state_db_conn", conn, raising=False)
    return conn


@pytest.fixture
def served(tmp_path: Path):
    """A real tracker app over its own state.db, and an HttpStore pointed
    at it. TestClient's base URL stands in for loopback."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    db_path = evidence / "state.db"
    store = ArtifactStore.from_evidence_root(evidence)
    app = create_app(db_path)
    app.state.artifact_store = store
    with TestClient(app) as client:
        yield client, store.conn


# --- parity ---------------------------------------------------------------


def test_both_transports_leave_the_same_rows(local, served, monkeypatch) -> None:
    """One sequence, two transports, identical rows. True by construction
    today; the test is what notices if that stops being true."""
    client, remote_conn = served

    _drive(state_store.LocalStore())

    http = state_store.HttpStore()
    monkeypatch.setattr(
        http, "_post",
        lambda payload: client.post("/v1/runners/presence", json=payload),
    )
    _drive(http)

    assert _shape(local) == _shape(remote_conn)


def test_started_at_is_held_across_the_wire(served) -> None:
    """Spelled out rather than left to the parity test, because this is
    the clause an endpoint loses: the UI's elapsed clock measures the JOB,
    so a status write that does not change job_id must not reset it."""
    client, conn = served

    def post(**kw):
        r = client.post("/v1/runners/presence",
                        json={"runner_id": RUNNER, **kw})
        assert r.status_code == 200, r.text

    post(event="status", status="processing", job_id="job-1", stage="a")
    first = conn.execute(
        "SELECT started_at FROM runner_status WHERE id = 1").fetchone()[0]

    post(event="status", status="processing", job_id="job-1", stage="b")
    assert conn.execute(
        "SELECT started_at FROM runner_status WHERE id = 1"
    ).fetchone()[0] == first

    post(event="status", status="processing", job_id="job-2", stage="a")
    assert conn.execute(
        "SELECT started_at FROM runner_status WHERE id = 1"
    ).fetchone()[0] != first


def test_a_status_extra_dict_survives_the_wire(served) -> None:
    """The only field whose TYPE has to survive transport: a dict becomes
    JSON to travel, is parsed back, and is re-serialised into extra_json.
    An int arriving as a string, or a dict flattened on the way, is a
    transport bug the parity test is there for."""
    client, conn = served

    r = client.post("/v1/runners/presence", json={
        "runner_id": RUNNER, "event": "status", "status": "processing",
        "job_id": "job-1", "stage": "dsynth_build",
        "extra": {"origin": "devel/glib20", "attempt": 2},
    })
    assert r.status_code == 200, r.text

    stored = conn.execute(
        "SELECT extra_json FROM runner_status WHERE id = 1").fetchone()[0]
    assert json.loads(stored) == {"origin": "devel/glib20", "attempt": 2}


def test_the_heartbeat_moves_both_clocks_over_http(served) -> None:
    client, conn = served
    client.post("/v1/runners/presence",
                json={"runner_id": RUNNER, "event": "register",
                      "hostname": "h", "pid": 1})
    client.post("/v1/runners/presence",
                json={"runner_id": RUNNER, "event": "status",
                      "status": "processing", "job_id": "job-1"})
    conn.execute("UPDATE runner_status SET updated_at = '2020-01-01T00:00:00Z'")
    conn.execute("UPDATE runners SET last_heartbeat_at = '2020-01-01T00:00:00Z'")
    conn.commit()

    r = client.post("/v1/runners/presence",
                    json={"runner_id": RUNNER, "event": "heartbeat"})

    assert r.json() == {"ok": True, "event": "heartbeat"}
    assert conn.execute(
        "SELECT updated_at FROM runner_status WHERE id = 1"
    ).fetchone()[0] > "2020"
    assert conn.execute(
        "SELECT last_heartbeat_at FROM runners WHERE runner_id = ?", (RUNNER,)
    ).fetchone()[0] > "2020"


# --- a client that got it wrong hears about it ---------------------------


@pytest.mark.parametrize("body,expect", [
    ({}, "event must be one of"),
    ({"event": "nonsense", "runner_id": "r"}, "event must be one of"),
    ({"event": "heartbeat"}, "runner_id required"),
    ({"event": "register", "runner_id": "r"}, "requires hostname and pid"),
    ({"event": "status", "runner_id": "r"}, "status required"),
    ({"event": "status", "runner_id": "r", "status": "x", "extra": 5},
     "extra must be an object"),
])
def test_a_bad_payload_is_a_400_naming_the_problem(served, body, expect) -> None:
    """400, not 500: the caller sent it, and the message says which field.
    A relay forwards this body verbatim (poly-fij.7), so the shape is part
    of the contract."""
    client, _ = served
    r = client.post("/v1/runners/presence", json=body)

    assert r.status_code == 400
    assert expect in json.loads(r.text)["error"]


def test_presence_writes_no_events_row(served) -> None:
    """Every other /v1 write records itself in `events`, which nothing
    prunes. At 12 heartbeats a minute that is ~17k rows a day per builder,
    so presence deliberately emits nothing."""
    client, conn = served
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    for _ in range(5):
        client.post("/v1/runners/presence",
                    json={"runner_id": RUNNER, "event": "heartbeat"})

    assert conn.execute(
        "SELECT COUNT(*) FROM events").fetchone()[0] == before


# --- telemetry must never break a build ----------------------------------


def test_an_unreachable_tracker_never_raises(capsys) -> None:
    """Telemetry does not get to fail a build, and this is also what keeps
    a trackerless deployment working. 127.0.0.1:1 refuses immediately."""
    http = state_store.HttpStore(
        url="http://127.0.0.1:1/v1/runners/presence", timeout=0.25,
    )

    http.register_runner(RUNNER)
    http.heartbeat(RUNNER)
    http.set_runner_status("processing", "job-1", "patch_start")
    http.deregister_runner(RUNNER)  # no raise is the assertion


def test_an_unreachable_tracker_says_so_except_on_the_tick(capsys) -> None:
    """Warning where LocalStore warns, silent where it is. Silence
    everywhere was the first version of this and it was wrong: a builder
    pointed at the wrong URL would fail to enroll and report nothing."""
    http = state_store.HttpStore(
        url="http://127.0.0.1:1/v1/runners/presence", timeout=0.25,
    )

    http.heartbeat(RUNNER)
    assert capsys.readouterr().err == "", "the 12-a-minute call stays quiet"

    http.register_runner(RUNNER)
    assert "could not register runner" in capsys.readouterr().err

    http.set_runner_status("processing", "job-1", "patch_start")
    assert "Failed to update runner status" in capsys.readouterr().err

    http.deregister_runner(RUNNER)
    assert "could not deregister runner" in capsys.readouterr().err


def test_the_timeout_cannot_outlive_the_heartbeat_interval() -> None:
    """A call that could outlast its own slot would leave ticks
    overlapping and the thread behind the cadence it defines."""
    assert state_store.HttpStore()._timeout_seconds() < rm.HEARTBEAT_INTERVAL


# --- the flip -------------------------------------------------------------


def test_the_transport_setting_selects_the_store(monkeypatch) -> None:
    from dportsv3 import settings

    previous = state_store.store()
    try:
        monkeypatch.setattr(settings, "get", lambda k, *a, **kw: (
            "http" if k == "runner.state_transport" else settings.get(k)))
        assert rm.select_state_store() == "http"
        assert isinstance(state_store.store(), state_store.HttpStore)
    finally:
        state_store.set_store(previous)


def test_an_unknown_transport_is_refused_not_guessed(monkeypatch) -> None:
    """Falling back to 'local' on a typo would look exactly like a working
    remote builder until someone read the tracker and found nothing."""
    from dportsv3 import settings

    previous = state_store.store()
    try:
        monkeypatch.setattr(settings, "get", lambda k, *a, **kw: (
            "htpp" if k == "runner.state_transport" else settings.get(k)))
        with pytest.raises(ValueError, match="must be 'local' or 'http'"):
            rm.select_state_store()
    finally:
        state_store.set_store(previous)
