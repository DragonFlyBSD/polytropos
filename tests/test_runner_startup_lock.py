"""Multi-runner is refused, not tolerated.

Every startup sweep is global and unqualified — reap_orphans marks all
inflight rows DEAD, _reset_building_markers discards queued confirm jobs.
Holding an exclusive lock is what makes them sound: one runner means every
inflight row really is an orphan. See poly-bg1.
"""
from __future__ import annotations

import fcntl
import os
import socket
import sqlite3

import pytest

import dportsv3.db.schema as schema
from dportsv3.agent import runner as runner_mod


@pytest.fixture
def queue_root(tmp_path):
    for sub in ("pending", "inflight", "done", "failed"):
        (tmp_path / sub).mkdir()
    yield tmp_path
    runner_mod.release_runner_lock()


@pytest.fixture
def conn(monkeypatch) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    schema.init_db(c)
    monkeypatch.setattr(runner_mod, "_state_db_conn", c, raising=False)
    return c


def test_lock_lives_under_the_queue_root(queue_root) -> None:
    assert runner_mod.acquire_runner_lock(queue_root) is True
    assert (queue_root / runner_mod.RUNNER_LOCK_NAME).exists()


def test_second_acquire_is_refused(queue_root) -> None:
    assert runner_mod.acquire_runner_lock(queue_root) is True
    assert runner_mod.acquire_runner_lock(queue_root) is False


def test_release_allows_reacquire(queue_root) -> None:
    assert runner_mod.acquire_runner_lock(queue_root) is True
    runner_mod.release_runner_lock()
    assert runner_mod.acquire_runner_lock(queue_root) is True


def test_holding_process_keeps_the_fd_open(queue_root) -> None:
    """Regression: dropping the reference would silently release the lock,
    because flock dies with the fd."""
    runner_mod.acquire_runner_lock(queue_root)
    assert runner_mod._runner_lock_file is not None
    assert not runner_mod._runner_lock_file.closed


def _insert_runner(conn, *, pid, stopped_at=None, hostname=None):
    conn.execute(
        """INSERT INTO runners
           (runner_id, hostname, pid, started_at, last_heartbeat_at, stopped_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (f"r-{pid}", hostname or socket.gethostname(), pid,
         "2026-08-23T00:00:00Z", "2026-08-23T00:00:05Z", stopped_at),
    )
    conn.commit()


def test_describe_names_a_live_runner(conn) -> None:
    _insert_runner(conn, pid=os.getpid())
    detail = runner_mod.describe_lock_holder()
    assert str(os.getpid()) in detail
    assert "last heartbeat" in detail


def test_describe_skips_a_dead_pid(conn) -> None:
    dead = 999_999
    with pytest.raises(ProcessLookupError):
        os.kill(dead, 0)  # the test is meaningless if this pid exists
    _insert_runner(conn, pid=dead)
    assert "no live pid" in runner_mod.describe_lock_holder()


def test_describe_ignores_a_cleanly_stopped_runner(conn) -> None:
    _insert_runner(conn, pid=os.getpid(), stopped_at="2026-08-23T01:00:00Z")
    assert "no live pid" in runner_mod.describe_lock_holder()


def test_describe_ignores_another_host(conn) -> None:
    _insert_runner(conn, pid=os.getpid(), hostname="some-other-host")
    assert "no live pid" in runner_mod.describe_lock_holder()


def test_main_refuses_and_sweeps_nothing_when_locked(queue_root, monkeypatch) -> None:
    """The whole point: a second runner must not reach the sweeps.

    Uses ``--once`` so the assertion is bounded — the sweeps run before
    main() branches on it, so a one-shot invocation is gated exactly like
    the service. That matters in its own right: running ``--once`` by hand
    while the service is up would otherwise reap the service's in-flight
    jobs.
    """
    held = open(queue_root / runner_mod.RUNNER_LOCK_NAME, "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    swept = []
    monkeypatch.setattr(runner_mod, "register_runner",
                        lambda: swept.append("register"), raising=False)
    monkeypatch.setattr(runner_mod, "start_heartbeat",
                        lambda: swept.append("heartbeat"), raising=False)
    monkeypatch.setattr(runner_mod, "_reset_building_markers",
                        lambda *a, **k: swept.append("markers"), raising=False)

    rc = runner_mod.main(["--queue-root", str(queue_root), "--once"])

    assert rc == runner_mod.EXIT_RUNNER_LOCKED
    assert swept == [], f"a refused runner still ran: {swept}"
    held.close()


def test_main_refusal_is_distinguishable_from_failure(queue_root) -> None:
    """A supervisor has to tell 'already running' from a broken start."""
    assert runner_mod.EXIT_RUNNER_LOCKED not in (0, 1)
