"""poly-qqx9.12: the env a job ran in reaches the job row.

env_resolver's precedence puts the job's own env above tracker_active_env
because an operator can change that selection mid-job. The value only ever
lived in the queue file, so the tracker could not resolve the workspace for
a diff (worker.emit_diff) or the per-port build log (worker.dsynth_log).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from dportsv3.db.schema import init_db


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    return c


def _job(conn, job_id="job-1"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO jobs (job_id, state, type, origin, created_ts_utc) "
        "VALUES (?, 'claimed', 'patch', 'devel/llvm19', ?)",
        (job_id, now),
    )
    conn.commit()


def _dev_env(conn, job_id="job-1"):
    return conn.execute(
        "SELECT dev_env FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()["dev_env"]


def test_jobs_has_a_dev_env_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert "dev_env" in cols


def test_resolving_an_env_records_it_on_the_job(conn, monkeypatch):
    from dportsv3.agent import runner

    _job(conn)
    monkeypatch.setattr(runner, "_state_db_conn", conn)
    runner._record_job_env({"job_id": "job-1"}, "dfly-64-main")
    assert _dev_env(conn) == "dfly-64-main"


def test_a_later_resolution_to_another_env_wins(conn, monkeypatch):
    """The row holds what the work is pointed at now, not the first guess."""
    from dportsv3.agent import runner

    _job(conn)
    monkeypatch.setattr(runner, "_state_db_conn", conn)
    runner._record_job_env({"job_id": "job-1"}, "dfly-64-main")
    runner._record_job_env({"job_id": "job-1"}, "dfly-64-2026Q3")
    assert _dev_env(conn) == "dfly-64-2026Q3"


def test_no_env_and_no_job_are_both_no_ops(conn, monkeypatch):
    from dportsv3.agent import runner

    _job(conn)
    monkeypatch.setattr(runner, "_state_db_conn", conn)
    runner._record_job_env({"job_id": "job-1"}, None)
    runner._record_job_env(None, "dfly-64-main")
    runner._record_job_env({}, "dfly-64-main")
    assert _dev_env(conn) is None


def test_a_write_failure_never_breaks_the_job(conn, monkeypatch):
    """Best-effort, like every other write to the read model."""
    from dportsv3.agent import runner

    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runner, "_state_db_conn", _Boom())
    runner._record_job_env({"job_id": "job-1"}, "dfly-64-main")


def test_migration_adds_the_column_to_a_db_that_predates_it():
    """An existing state.db must not need a wipe (poly-hhv is the warning).

    The 'old' table is spelled out rather than derived, so this keeps
    testing the migration even as jobs grows new columns.
    """
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE jobs (
               job_id TEXT PRIMARY KEY, state TEXT, type TEXT, origin TEXT,
               flavor TEXT, bundle_dir TEXT, created_ts_utc TEXT, path TEXT,
               last_error TEXT, last_seen_at TEXT, target TEXT,
               last_transition_at TEXT, retire_reason TEXT, bundle_id TEXT,
               owner_id TEXT
           )"""
    )
    c.commit()
    assert "dev_env" not in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}

    init_db(c)
    assert "dev_env" in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}


def test_init_db_is_idempotent_once_the_column_exists():
    """The ALTER runs on every init; the duplicate must stay caught."""
    c = sqlite3.connect(":memory:")
    init_db(c)
    init_db(c)
    assert "dev_env" in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
