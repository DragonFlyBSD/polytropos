"""Indexes over ``activity_log``, and the query plans that justify them.

activity_log is the firehose — every llm_turn and tool call — and it is what
the job timeline and the per-bundle attempt aggregates read. The plans are
asserted rather than the index names alone, because an index nothing uses is
the failure mode being guarded against here, not just a missing one.
"""

from __future__ import annotations

import sqlite3

import pytest

from dportsv3.tracker.db import init_db


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = init_db(":memory:")
    connection.executemany(
        "INSERT INTO activity_log(ts, job_id, stage, message) "
        "VALUES ('t', ?, ?, 'm')",
        [(f"j-{i % 200}", "llm_turn" if i % 3 else "attempt_start")
         for i in range(4000)],
    )
    connection.executemany(
        "INSERT INTO jobs(job_id, state, type, origin, bundle_id, "
        "created_ts_utc) VALUES (?, 'done', 'patch', 'a/b', ?, 't')",
        [(f"j-{i}", f"b-{i}") for i in range(200)],
    )
    connection.commit()
    yield connection
    connection.close()


def _plan(conn: sqlite3.Connection, sql: str, params=()) -> str:
    return " | ".join(
        str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    )


def test_the_job_timeline_does_not_scan_the_firehose(conn):
    """activity_for_job's shape: WHERE job_id = ? ORDER BY id DESC."""
    plan = _plan(
        conn,
        "SELECT * FROM activity_log WHERE job_id = ? ORDER BY id DESC LIMIT 50",
        ("j-7",),
    )

    assert "SCAN activity_log" not in plan
    assert "idx_activity_log_job" in plan
    # The composite carries the ordering, so no sort is materialized.
    assert "TEMP B-TREE" not in plan


def test_a_per_bundle_stage_aggregate_is_covered(conn):
    """Counting attempt_start per bundle reaches jobs through job_id, so the
    index carries it and the aggregate never touches the table."""
    plan = _plan(
        conn,
        "SELECT j.bundle_id, COUNT(*) FROM activity_log a "
        "JOIN jobs j ON j.job_id = a.job_id "
        "WHERE a.stage = 'attempt_start' GROUP BY j.bundle_id",
    )

    assert "SCAN a" not in plan
    assert "COVERING INDEX idx_activity_log_stage" in plan


def test_the_bundle_index_stays_partial(conn):
    """Only the tracker's own endpoints write bundle_id. Indexing just those
    rows is the design, not an oversight -- a full index would carry the
    runner's entire firehose to serve a handful of operator actions."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_activity_log_bundle'"
    ).fetchone()

    assert row is not None
    assert "WHERE bundle_id IS NOT NULL" in row[0]


def test_no_index_on_runs_build_run_id(conn):
    """Deliberately absent. The run view's per-row bundle lookup was assumed
    to scan `runs`; measured, the planner drives from bundles by origin and
    reaches runs by primary key, so an index on build_run_id is never used.
    Asserting its absence keeps someone from adding it on the old reasoning.
    """
    indexed_cols = set()
    for idx in conn.execute("PRAGMA index_list('runs')").fetchall():
        for col in conn.execute(f"PRAGMA index_info({idx[1]!r})").fetchall():
            indexed_cols.add(col[2])

    assert "build_run_id" not in indexed_cols

    plan = _plan(
        conn,
        "SELECT br.origin, (SELECT b.bundle_id FROM bundles b "
        "JOIN runs r ON r.run_id = b.run_id "
        "WHERE r.build_run_id = br.build_run_id AND b.origin = br.origin "
        "ORDER BY b.ts_utc DESC LIMIT 1) "
        "FROM build_results br WHERE br.build_run_id = ? LIMIT 1000",
        (1,),
    )
    assert "SCAN r" not in plan
