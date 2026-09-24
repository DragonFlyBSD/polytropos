"""The runner's state writes go through one seam (poly-fij.12 step 1).

Step 1 changes no behaviour, so the rest of the suite passing says almost
nothing about it. What these tests pin are the properties that would
break SILENTLY -- a store that writes nowhere, or an endpoint in step 2
that drops a clause nobody noticed was load-bearing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from dportsv3.agent import runner as rm
from dportsv3.agent import state_store
from dportsv3.db.schema import init_db


@pytest.fixture
def conn(monkeypatch):
    """A state.db installed the way the runner's own tests install one:
    by assigning the module global AFTER import."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_db(c)
    monkeypatch.setattr(rm, "_state_db_conn", c, raising=False)
    return c


# --- the connection is resolved per call ----------------------------------


def test_the_store_finds_a_connection_assigned_after_it_was_built(
    monkeypatch,
) -> None:
    """THE failure this seam could have introduced. Eight test modules and
    the demo driver assign runner._state_db_conn after import. A store
    that captured the connection in __init__ would write nowhere, and
    every one of those tests would pass while asserting on an empty
    table. So the store is built FIRST here, deliberately."""
    store = state_store.LocalStore()

    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_db(c)
    monkeypatch.setattr(rm, "_state_db_conn", c, raising=False)

    store.register_runner("builder-1")

    assert c.execute("SELECT runner_id FROM runners").fetchone()[0] == "builder-1"


def test_no_connection_is_a_silent_no_op(monkeypatch, capsys) -> None:
    """The runner runs trackerless on purpose in some deployments; a
    missing state.db disables the read model, it does not fail a build."""
    monkeypatch.setattr(rm, "_state_db_conn", None, raising=False)
    store = state_store.LocalStore()

    store.register_runner("builder-1")
    store.heartbeat("builder-1")
    store.set_runner_status("processing", "job-1", "patch_start")
    store.deregister_runner("builder-1")

    assert capsys.readouterr().err == ""


# --- presence ------------------------------------------------------------


def test_registering_twice_re_enrolls_rather_than_failing(conn) -> None:
    """A restart reuses nothing -- runner_id is regenerated per process
    (poly-fij.3 owns that) -- but a runner that somehow returns with its
    old id must re-enroll, not raise on the primary key."""
    store = state_store.LocalStore()

    store.register_runner("builder-1")
    store.deregister_runner("builder-1")
    store.register_runner("builder-1")

    row = conn.execute(
        "SELECT stopped_at FROM runners WHERE runner_id = ?", ("builder-1",)
    ).fetchone()
    assert row["stopped_at"] is None, "re-enrollment must clear stopped_at"


def test_deregistering_marks_only_this_runner(conn) -> None:
    """With N builders on one tracker, a clean shutdown must not stamp
    every row in the table."""
    store = state_store.LocalStore()
    store.register_runner("builder-1")
    store.register_runner("builder-2")

    store.deregister_runner("builder-1")

    stopped = dict(
        conn.execute("SELECT runner_id, stopped_at FROM runners").fetchall()
    )
    assert stopped["builder-1"] is not None
    assert stopped["builder-2"] is None


def test_the_heartbeat_moves_both_clocks(conn) -> None:
    """runner_status.updated_at is what the UI reads to tell a working
    runner from one that died with `processing` still on the row;
    runners.last_heartbeat_at is the same fact per builder."""
    store = state_store.LocalStore()
    store.register_runner("builder-1")
    store.set_runner_status("processing", "job-1", "patch_start")
    conn.execute("UPDATE runner_status SET updated_at = '2020-01-01T00:00:00+00:00'")
    conn.execute("UPDATE runners SET last_heartbeat_at = '2020-01-01T00:00:00+00:00'")
    conn.commit()

    store.heartbeat("builder-1")

    assert conn.execute(
        "SELECT updated_at FROM runner_status WHERE id = 1"
    ).fetchone()[0] > "2020"
    assert conn.execute(
        "SELECT last_heartbeat_at FROM runners WHERE runner_id = ?", ("builder-1",)
    ).fetchone()[0] > "2020"


def test_the_heartbeat_does_not_warn_though_its_neighbours_do(
    conn, capsys,
) -> None:
    """A deliberate asymmetry, and step 2 must not tidy it away: the
    heartbeat runs 12x a minute, so a warning per failed tick would bury
    the log it is written to. register/status/deregister run at job
    boundaries and say something."""
    store = state_store.LocalStore()
    conn.execute("DROP TABLE runners")
    conn.execute("DROP TABLE runner_status")
    conn.commit()

    store.heartbeat("builder-1")
    assert capsys.readouterr().err == ""

    store.register_runner("builder-1")
    assert "could not register runner" in capsys.readouterr().err

    store.set_runner_status("processing")
    assert "Failed to update runner status" in capsys.readouterr().err

    store.deregister_runner("builder-1")
    assert "could not deregister runner" in capsys.readouterr().err


# --- the started_at clause, which an endpoint would drop ------------------


def test_started_at_survives_a_status_update_on_the_same_job(conn) -> None:
    """The UI's elapsed clock measures the JOB, so started_at is held
    across every status write that does not change job_id. This is the
    clause step 2's endpoint is most likely to lose, because it looks
    like a plain upsert until you read the CASE."""
    store = state_store.LocalStore()
    store.set_runner_status("processing", "job-1", "patch_start")
    first = conn.execute(
        "SELECT started_at FROM runner_status WHERE id = 1"
    ).fetchone()[0]

    store.set_runner_status("processing", "job-1", "dsynth_build")

    row = conn.execute(
        "SELECT started_at, current_stage FROM runner_status WHERE id = 1"
    ).fetchone()
    assert row["started_at"] == first
    assert row["current_stage"] == "dsynth_build"


def test_started_at_resets_when_the_job_changes(conn) -> None:
    store = state_store.LocalStore()
    store.set_runner_status("processing", "job-1", "patch_start")
    conn.execute(
        "UPDATE runner_status SET started_at = '2020-01-01T00:00:00+00:00'"
    )
    conn.commit()

    store.set_runner_status("processing", "job-2", "patch_start")

    assert conn.execute(
        "SELECT started_at FROM runner_status WHERE id = 1"
    ).fetchone()[0] > "2020"


# --- the call sites actually go through the seam --------------------------


class _Recorder:
    """A store that records instead of writing. This is the shape step 2's
    HttpStore has to satisfy, and what makes the runner's own tests able
    to assert on intent rather than on rows."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def register_runner(self, runner_id):
        self.calls.append(("register", runner_id))

    def heartbeat(self, runner_id):
        self.calls.append(("heartbeat", runner_id))

    def set_runner_status(self, status, job_id=None, stage=None, extra=None):
        self.calls.append(("status", status, job_id, stage, extra))

    def deregister_runner(self, runner_id):
        self.calls.append(("deregister", runner_id))


@pytest.fixture
def recorder():
    rec = _Recorder()
    previous = state_store.set_store(rec)
    yield rec
    state_store.set_store(previous)


def test_set_store_restores_the_previous_one(recorder) -> None:
    """The flip is wholesale: there is never a run with some writes local
    and some remote. Which means swapping has to be reversible, or a test
    that swaps leaks into the next one."""
    assert state_store.store() is recorder
    inner = _Recorder()
    previous = state_store.set_store(inner)
    assert previous is recorder
    state_store.set_store(previous)
    assert state_store.store() is recorder


def test_the_runners_presence_calls_cross_the_seam(recorder) -> None:
    rm.register_runner()
    rm.deregister_runner()

    assert [c[0] for c in recorder.calls] == ["register", "deregister"]
    # Same identity on both, or a shutdown stamps nothing.
    assert recorder.calls[0][1] == recorder.calls[1][1] == rm.runner_id()


def test_update_runner_status_crosses_the_seam(recorder) -> None:
    rm.update_runner_status("processing", "job-1", "patch_start")

    assert recorder.calls == [
        ("status", "processing", "job-1", "patch_start", None)
    ]


def test_the_remembered_stage_is_applied_before_the_seam(recorder) -> None:
    """``_current_stage`` is the runner's own memory. It stays on this
    side of the boundary: the tracker is told a stage, never asked to
    remember the last one it was told."""
    rm.update_runner_status("processing", "job-1", "dsynth_build")
    rm.update_runner_status("processing", "job-1", None)

    assert recorder.calls[-1] == (
        "status", "processing", "job-1", "dsynth_build", None
    )
