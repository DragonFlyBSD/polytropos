"""Two processes migrate the same state.db, and neither one dies of it.

poly-5d58. The tracker and the runner both call init_db at startup and
`deploy install` restarts them back to back, so a deploy carrying a
migration races them. On 2026-09-26 the runner won, migrated cleanly, and
went on serving; the tracker lost with

    sqlite3.OperationalError: database is locked
    ERROR:    Application startup failed. Exiting.

and stayed down until restarted by hand, because that raise happens inside
the uvicorn startup lifespan. HTTP was dead and nothing retried or alerted.

busy_timeout was set to 5000 the whole time and could not help: the old
migration opened a SAVEPOINT, READ ``PRAGMA table_info``, then WROTE a
CREATE TABLE. That is a lock UPGRADE, and sqlite refuses an upgrade
immediately rather than invoking the busy handler, because waiting on one
can deadlock. The fix takes the write lock before reading anything, so the
handler applies and the loser waits for the winner instead of dying.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from dportsv3.db import schema
from dportsv3.db.schema import init_db

#: The pre-flavor shape of the two tables the rebuild migration touches.
#: A DB in this state is what makes the migration do real work; once
#: migrated it returns early and the race cannot be provoked.
OLD_TABLES = """
DROP TABLE IF EXISTS build_results;
DROP TABLE IF EXISTS port_status;
CREATE TABLE build_results (
    build_run_id INTEGER NOT NULL,
    origin TEXT NOT NULL,
    version TEXT NOT NULL,
    result TEXT NOT NULL,
    log_url TEXT,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded',
    PRIMARY KEY (build_run_id, origin)
);
CREATE TABLE port_status (
    target TEXT NOT NULL,
    origin TEXT NOT NULL,
    last_attempt_version TEXT,
    last_attempt_result TEXT,
    last_attempt_at TEXT,
    last_attempt_run_id INTEGER,
    last_success_version TEXT,
    last_success_at TEXT,
    last_success_run_id INTEGER,
    PRIMARY KEY (target, origin)
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@pytest.fixture
def unmigrated(tmp_path: Path) -> Path:
    """A state.db whose build_results/port_status predate the flavor key."""
    path = tmp_path / "state.db"
    conn = _connect(path)
    init_db(conn)
    # A real run for the results to hang off: the rebuilt table declares
    # the foreign key, and it is enforced, so an orphan row would fail the
    # copy for a reason that has nothing to do with locking.
    conn.execute(
        "INSERT INTO build_runs (id, target, build_type, started_at) "
        "VALUES (1, '@main', 'release', 't0')")
    conn.commit()
    conn.executescript(OLD_TABLES)
    conn.execute(
        "INSERT INTO build_results (build_run_id, origin, version, result, "
        "recorded_at) VALUES (1, 'devel/glib20', '2.80', 'success', 't0')")
    conn.commit()
    conn.close()
    return path


def _flavored(path: Path) -> bool:
    conn = _connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(build_results)")}
        return "flavor" in cols
    finally:
        conn.close()


def test_the_fixture_really_is_unmigrated(unmigrated: Path) -> None:
    """Otherwise every test below passes by doing nothing."""
    assert not _flavored(unmigrated)


def test_a_second_process_waits_for_the_first_instead_of_dying(
    unmigrated: Path,
) -> None:
    """THE regression. Another writer holds the lock, releases it shortly
    after, and init_db rides it out and completes."""
    holder = _connect(unmigrated)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO build_results (build_run_id, origin, version,"
                   " result, recorded_at) VALUES (1, 'x/y', '1', 'success',"
                   " 't')")
    released = threading.Event()

    def release() -> None:
        time.sleep(0.4)
        holder.commit()
        released.set()

    threading.Thread(target=release, daemon=True).start()

    conn = _connect(unmigrated)
    started = time.monotonic()
    try:
        init_db(conn)            # raised instantly, before poly-5d58
    finally:
        conn.close()
        released.wait(timeout=5)
        holder.close()

    assert _flavored(unmigrated), "the migration did not run"
    assert time.monotonic() - started >= 0.3, (
        "returned too fast to have waited for the other writer -- the lock "
        "was not actually contended, so this test proves nothing"
    )


def test_the_wait_is_bounded_and_the_failure_is_loud(
    unmigrated: Path, monkeypatch,
) -> None:
    """A writer that never lets go must not hang a service start forever.
    The wait is the migration timeout, and what comes out the other end is
    the same OperationalError as before -- louder is not the goal, waiting
    first is."""
    monkeypatch.setattr(schema, "MIGRATION_LOCK_TIMEOUT_MS", 300)
    holder = _connect(unmigrated)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO build_results (build_run_id, origin, version,"
                   " result, recorded_at) VALUES (1, 'x/z', '1', 'success',"
                   " 't')")

    conn = _connect(unmigrated)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        init_db(conn)
    waited = time.monotonic() - started

    conn.close()
    holder.rollback()
    holder.close()

    assert waited >= 0.25, (
        f"gave up after {waited:.3f}s against a 0.3s timeout -- the busy "
        f"handler was never invoked, which is the poly-5d58 defect"
    )
    assert waited < 3.0, f"waited {waited:.3f}s, far past the timeout"


def test_the_loser_finds_the_work_done_and_skips_it(unmigrated: Path) -> None:
    """What the wait buys. The second process through does not repeat the
    rebuild -- it sees the column and returns early, which is why waiting
    is enough and no coordination is needed."""
    first = _connect(unmigrated)
    init_db(first)
    first.close()

    rows_before = _rows(unmigrated)
    second = _connect(unmigrated)
    init_db(second)              # the loser, arriving late
    second.close()

    assert _flavored(unmigrated)
    assert _rows(unmigrated) == rows_before, "the second pass rewrote rows"


def _rows(path: Path) -> list[tuple]:
    conn = _connect(path)
    try:
        return list(conn.execute(
            "SELECT build_run_id, origin, version FROM build_results "
            "ORDER BY build_run_id"))
    finally:
        conn.close()


def test_the_connection_is_left_on_the_ordinary_timeout(
    unmigrated: Path,
) -> None:
    """The generous timeout is for the migration phase only. Left in
    place it would make every later query on this connection wait half a
    minute on a lock it should report at once."""
    conn = _connect(unmigrated)
    init_db(conn)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_a_caller_that_already_has_a_transaction_keeps_it(
    unmigrated: Path,
) -> None:
    """BEGIN IMMEDIATE cannot nest, so the phase joins the caller's
    transaction rather than failing the start. The lock is then the
    caller's problem, which the docstring says and this pins: what must
    not happen is a raise, and what must still happen is the migration.
    """
    conn = _connect(unmigrated)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO build_results (build_run_id, origin, version, "
                 "result, recorded_at) VALUES (1, 'a/b', '1', 'success', 't')")
    assert conn.in_transaction

    init_db(conn)

    conn.close()
    assert _flavored(unmigrated)


def test_a_migration_failure_leaves_no_half_built_table(
    unmigrated: Path, monkeypatch,
) -> None:
    """The rebuild drops and renames. If it raises midway the transaction
    must take the wreckage with it, or the next start finds a stray
    build_results_flavored and its own CREATE fails."""
    real = schema._add_build_flavor_dimension

    def boom(conn):
        real(conn)
        raise RuntimeError("migration exploded after the rebuild")

    monkeypatch.setattr(schema, "_add_build_flavor_dimension", boom)
    conn = _connect(unmigrated)
    with pytest.raises(RuntimeError, match="exploded"):
        init_db(conn)
    conn.close()

    check = _connect(unmigrated)
    names = {r[0] for r in check.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()

    assert not _flavored(unmigrated), "a failed migration was committed"
    assert not [n for n in names if n.endswith("_flavored")], names
