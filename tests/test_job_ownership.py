"""Runner ownership is recorded, never enforced.

Multi-runner is not supported: nothing branches on ``jobs.owner_id``.
These tests pin that down in both directions — the stamp is written,
and the sweep that would break under naive scoping stays unscoped.
"""
from __future__ import annotations

import sqlite3

import pytest

import dportsv3.db.schema as schema
from dportsv3.agent import lifecycle as L
from dportsv3.agent import runner as runner_mod


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    schema.init_db(c)
    return c


def _owner(conn: sqlite3.Connection, job_id: str) -> str | None:
    row = conn.execute("SELECT owner_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row[0] if row else None


def test_apply_stamps_owner(conn: sqlite3.Connection) -> None:
    L.apply(conn, "j1", L.JobEvent.HOOK_ENQUEUED, actor="hook")
    L.apply(conn, "j1", L.JobEvent.CLAIM, owner="host-1-aaaa")
    assert _owner(conn, "j1") == "host-1-aaaa"


def test_ownerless_transition_preserves_owner(conn: sqlite3.Connection) -> None:
    """The COALESCE matters: a hook-side event carries no owner and must
    not blank the runner that claimed the job."""
    L.apply(conn, "j1", L.JobEvent.HOOK_ENQUEUED, actor="hook")
    L.apply(conn, "j1", L.JobEvent.CLAIM, owner="host-1-aaaa")
    L.apply(conn, "j1", L.JobEvent.TRIAGE_START, actor="hook")
    assert _owner(conn, "j1") == "host-1-aaaa"


def test_terminal_transition_stamps_owner_and_reason(conn: sqlite3.Connection) -> None:
    L.apply(conn, "j1", L.JobEvent.HOOK_ENQUEUED, actor="hook")
    L.apply(conn, "j1", L.JobEvent.CLAIM, owner="host-1-aaaa")
    L.apply(conn, "j1", L.JobEvent.TRIAGE_START, owner="host-1-aaaa")
    L.apply(conn, "j1", L.JobEvent.TRIAGE_FAIL, owner="host-1-aaaa")
    row = conn.execute(
        "SELECT state, owner_id, retire_reason FROM jobs WHERE job_id = 'j1'"
    ).fetchone()
    assert row == ("dead", "host-1-aaaa", "triage_failed")


def test_reap_orphans_ignores_owner(conn: sqlite3.Connection) -> None:
    """Regression guard. Scoping the sweep to the current runner would
    skip the previous process's rows — exactly the orphans — and leave
    them inflight forever."""
    L.apply(conn, "j1", L.JobEvent.HOOK_ENQUEUED, actor="hook")
    L.apply(conn, "j1", L.JobEvent.CLAIM, owner="host-dead-1111")
    assert L.reap_orphans(conn, actor="runner-host-live-2222") == 1
    assert conn.execute(
        "SELECT state FROM jobs WHERE job_id = 'j1'"
    ).fetchone()[0] == "dead"


def test_owner_id_migrates_onto_preexisting_db() -> None:
    """An existing state.db predating the column keeps its rows."""
    old = schema.SCHEMA.replace(
        ",\n    -- which runner last transitioned this job (see runners.runner_id)."
        "\n    -- Recorded only; no code branches on it.\n    owner_id TEXT",
        "",
    ).replace("CREATE TABLE IF NOT EXISTS runners", "CREATE TABLE IF NOT EXISTS unused_runners")
    assert "owner_id" not in old, "fixture no longer strips the column"

    c = sqlite3.connect(":memory:")
    c.executescript(old)
    c.execute("INSERT INTO jobs (job_id, state) VALUES ('old-1', 'queued')")
    c.commit()

    schema.init_db(c)
    assert c.execute(
        "SELECT job_id, state, owner_id FROM jobs"
    ).fetchall() == [("old-1", "queued", None)]
    schema.init_db(c)  # duplicate ADD COLUMN must stay tolerated


def test_runner_id_stable_within_process(monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "_runner_id", "", raising=False)
    first = runner_mod.runner_id()
    assert first == runner_mod.runner_id()
    assert str(runner_mod.os.getpid()) in first


def test_register_and_deregister_runner(conn: sqlite3.Connection, monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "_state_db_conn", conn, raising=False)
    monkeypatch.setattr(runner_mod, "_runner_id", "host-1-aaaa", raising=False)

    runner_mod.register_runner()
    row = conn.execute(
        "SELECT runner_id, pid, started_at, last_heartbeat_at, stopped_at FROM runners"
    ).fetchone()
    assert row[0] == "host-1-aaaa"
    assert row[1] == runner_mod.os.getpid()
    assert row[2] and row[3] and row[4] is None

    runner_mod.deregister_runner()
    assert conn.execute("SELECT stopped_at FROM runners").fetchone()[0] is not None


def test_register_runner_clears_stopped_at_on_restart(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    monkeypatch.setattr(runner_mod, "_state_db_conn", conn, raising=False)
    monkeypatch.setattr(runner_mod, "_runner_id", "host-1-aaaa", raising=False)
    runner_mod.register_runner()
    runner_mod.deregister_runner()
    runner_mod.register_runner()
    assert conn.execute("SELECT stopped_at FROM runners").fetchone()[0] is None


def test_nothing_branches_on_owner_id() -> None:
    """Ownership is recorded, not enforced. If a read of ``owner_id``
    appears in the agent package, multi-runner semantics arrived without
    the liveness machinery to back them."""
    import pathlib

    agent_dir = pathlib.Path(runner_mod.__file__).parent
    offenders = []
    for path in agent_dir.glob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if "owner_id" not in line or line.lstrip().startswith(("#", "--")):
                continue
            # The lifecycle upsert is the sole writer.
            if path.name == "lifecycle.py":
                continue
            offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"owner_id read outside lifecycle.py: {offenders}"
