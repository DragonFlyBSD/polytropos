from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from dportsv3.tracker.db import (
    ActiveBuildError,
    get_active_builds_summary,
    last_activity_at,
    run_is_stale,
    compare_builds,
    create_build_run,
    finish_build_run,
    get_active_run,
    get_build_results,
    get_build_run,
    get_diff,
    get_failures,
    get_port_status,
    get_target_summary,
    init_db,
    list_build_runs,
    record_results,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = init_db(":memory:")
    yield connection
    connection.close()


def test_init_db_seeds_default_build_types(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT name FROM build_types ORDER BY name ASC").fetchall()

    assert [row[0] for row in rows] == ["release", "test"]


def test_create_and_finish_build_run_records_commit_metadata(
    conn: sqlite3.Connection,
) -> None:
    run_id = create_build_run(conn, "@main", "release", "2026-03-17T12:00:00+00:00")

    finish_build_run(
        conn,
        run_id,
        "2026-03-17T14:00:00+00:00",
        commit_sha="abc123",
        commit_branch="main",
        commit_pushed_at="2026-03-17T14:10:00+00:00",
    )
    run = get_build_run(conn, run_id)

    assert run["target"] == "@main"
    assert run["build_type"] == "release"
    assert run["finished_at"] == "2026-03-17T14:00:00+00:00"
    assert run["commit_sha"] == "abc123"
    assert run["commit_branch"] == "main"
    assert run["commit_pushed_at"] == "2026-03-17T14:10:00+00:00"
    assert run["result_count"] == 0


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_create_build_run_enforces_single_active_run_per_target_and_type(
    conn: sqlite3.Connection,
) -> None:
    # Recent, so the run reads as live. A run that has recorded nothing for
    # longer than tracker.stale_build_run_hours is superseded instead --
    # see test_create_build_run_supersedes_a_stale_active_run.
    first_run = create_build_run(conn, "@2026Q1", "test", _ago(0.5))

    with pytest.raises(ActiveBuildError) as exc:
        create_build_run(conn, "@2026Q1", "test", _ago(0.1))

    assert exc.value.active_run["id"] == first_run
    assert get_active_run(conn, "@2026Q1", "test")["id"] == first_run


def test_create_build_run_supersedes_a_stale_active_run(
    conn: sqlite3.Connection,
) -> None:
    """An interrupted dsynth leaves its run open forever, and the unique
    active run then blocks every later build: the hook takes the 409 and
    sets TRACKING_DISABLED for its whole run. Measured once at 137 builds
    and 30 failures lost over 2.5 hours (poly-gd5)."""
    dead = create_build_run(conn, "@main", "test", _ago(9))

    fresh = create_build_run(conn, "@main", "test", None)

    assert fresh != dead
    assert get_active_run(conn, "@main", "test")["id"] == fresh
    assert get_build_run(conn, dead)["finished_at"] is not None


def test_a_long_but_live_run_is_never_superseded(
    conn: sqlite3.Connection,
) -> None:
    """Staleness is measured from the last result, not from the start.
    A run that began yesterday but recorded a port minutes ago is alive,
    and superseding it would split one build across two runs."""
    run_id = create_build_run(conn, "@main", "test", _ago(30))
    record_results(conn, run_id, "@main", [
        {"origin": "devel/llvm19", "version": "19.1.7", "result": "success",
         "recorded_at": _ago(0.2)},
    ])

    assert run_is_stale(conn, run_id) is False
    with pytest.raises(ActiveBuildError):
        create_build_run(conn, "@main", "test", None)


def test_superseded_run_is_closed_at_its_last_activity(
    conn: sqlite3.Connection,
) -> None:
    """Not at now: claiming the build ran until the moment we noticed
    would invent a duration it never had."""
    last = _ago(20)
    dead = create_build_run(conn, "@main", "test", _ago(26))
    record_results(conn, dead, "@main", [
        {"origin": "www/nginx", "version": "1.27.4", "result": "failure",
         "recorded_at": last},
    ])

    create_build_run(conn, "@main", "test", None)

    assert get_build_run(conn, dead)["finished_at"] == last
    assert last_activity_at(conn, dead) == last


def test_superseding_preserves_commit_metadata(
    conn: sqlite3.Connection,
) -> None:
    """finish_build_run also writes the three commit columns, so reusing
    it here would erase what a partially reported run already carries."""
    dead = create_build_run(conn, "@main", "release", _ago(9))
    conn.execute(
        "UPDATE build_runs SET commit_sha = ?, commit_branch = ? WHERE id = ?",
        ("abc123", "main", dead),
    )
    conn.commit()

    create_build_run(conn, "@main", "release", None)

    run = get_build_run(conn, dead)
    assert run["finished_at"] is not None
    assert run["commit_sha"] == "abc123"
    assert run["commit_branch"] == "main"


def test_active_builds_summary_flags_a_stale_run(
    conn: sqlite3.Connection,
) -> None:
    """Nothing surfaced the open run for two and a half hours; an operator
    found it by noticing a missing port. The summary the dashboard reads
    now carries it."""
    create_build_run(conn, "@main", "test", _ago(9))

    summary = get_active_builds_summary(conn)

    assert len(summary) == 1
    assert summary[0]["stale"] is True
    assert summary[0]["last_activity_at"] is not None


def test_create_build_run_allows_parallel_types_for_same_target(
    conn: sqlite3.Connection,
) -> None:
    test_run = create_build_run(conn, "@2026Q1", "test", "2026-03-17T09:00:00+00:00")
    release_run = create_build_run(
        conn,
        "@2026Q1",
        "release",
        "2026-03-17T09:05:00+00:00",
    )

    runs = list_build_runs(conn, target="@2026Q1", limit=10)

    assert {run["id"] for run in runs} == {test_run, release_run}


def test_record_results_updates_port_status_and_preserves_last_success(
    conn: sqlite3.Connection,
) -> None:
    run_a = create_build_run(conn, "@main", "release", "2026-03-17T08:00:00+00:00")
    record_results(
        conn,
        run_a,
        "@main",
        [
            {
                "origin": "devel/foo",
                "version": "1.0",
                "result": "success",
                "log_url": "https://logs.example/devel/foo.log.gz",
                "recorded_at": "2026-03-17T08:10:00+00:00",
            }
        ],
    )
    finish_build_run(conn, run_a, "2026-03-17T09:00:00+00:00")

    run_b = create_build_run(conn, "@main", "release", "2026-03-18T08:00:00+00:00")
    record_results(
        conn,
        run_b,
        "@main",
        [
            {
                "origin": "devel/foo",
                "version": "1.1",
                "result": "failure",
                "recorded_at": "2026-03-18T08:10:00+00:00",
            }
        ],
    )

    results = get_build_results(conn, run_a)
    status = get_port_status(conn, target="@main", origin="devel/foo")[0]

    assert results[0]["log_url"] == "https://logs.example/devel/foo.log.gz"
    assert status["last_attempt_version"] == "1.1"
    assert status["last_attempt_result"] == "failure"
    assert status["last_attempt_run_id"] == run_b
    assert status["last_success_version"] == "1.0"
    assert status["last_success_run_id"] == run_a


def test_get_failures_and_get_diff_compare_current_target_status(
    conn: sqlite3.Connection,
) -> None:
    run_main = create_build_run(conn, "@main", "release", "2026-03-17T08:00:00+00:00")
    record_results(
        conn,
        run_main,
        "@main",
        [
            {"origin": "devel/foo", "version": "1.0", "result": "failure"},
            {"origin": "editors/bar", "version": "2.0", "result": "success"},
        ],
    )
    finish_build_run(conn, run_main, "2026-03-17T09:00:00+00:00")

    run_q = create_build_run(conn, "@2026Q1", "release", "2026-03-17T10:00:00+00:00")
    record_results(
        conn,
        run_q,
        "@2026Q1",
        [
            {"origin": "devel/foo", "version": "1.0", "result": "success"},
            {"origin": "lang/baz", "version": "3.0", "result": "failure"},
        ],
    )

    failures = get_failures(conn, "@main")
    diff = get_diff(conn, "@main", "@2026Q1")

    assert [row["origin"] for row in failures] == ["devel/foo"]
    assert diff["only_a"] == [
        {
            "origin": "editors/bar",
            "target": "@main",
            "version": "2.0",
            "result": "success",
        }
    ]
    assert diff["only_b"] == [
        {
            "origin": "lang/baz",
            "target": "@2026Q1",
            "version": "3.0",
            "result": "failure",
        }
    ]
    assert diff["differ"] == [
        {
            "origin": "devel/foo",
            "version_a": "1.0",
            "result_a": "failure",
            "version_b": "1.0",
            "result_b": "success",
        }
    ]


def test_get_target_summary_reports_counts_and_last_build(
    conn: sqlite3.Connection,
) -> None:
    run_a = create_build_run(conn, "@main", "test", "2026-03-17T08:00:00+00:00")
    record_results(
        conn,
        run_a,
        "@main",
        [
            {"origin": "devel/foo", "version": "1.0", "result": "success"},
            {"origin": "devel/bar", "version": "2.0", "result": "failure"},
        ],
    )
    finish_build_run(conn, run_a, "2026-03-17T09:00:00+00:00")

    run_b = create_build_run(conn, "@main", "release", "2026-03-18T08:00:00+00:00")
    record_results(
        conn,
        run_b,
        "@main",
        [{"origin": "devel/foo", "version": "1.1", "result": "success"}],
    )

    summary = get_target_summary(conn)

    assert summary == [
        {
            "target": "@main",
            "total_ports": 2,
            "successes": 1,
            "failures": 1,
            "skipped": 0,
            "ignored": 0,
            "last_build_id": run_b,
            "last_build_type": "release",
            "last_build_started_at": "2026-03-18T08:00:00+00:00",
            "last_build_finished_at": None,
            "last_build_at": "2026-03-18T08:00:00+00:00",
        }
    ]


def test_compare_builds_categorizes_changes_across_runs(
    conn: sqlite3.Connection,
) -> None:
    run_a = create_build_run(conn, "@main", "release", "2026-03-10T08:00:00+00:00")
    record_results(
        conn,
        run_a,
        "@main",
        [
            {"origin": "devel/fixme", "version": "1.0", "result": "failure"},
            {"origin": "www/regress", "version": "2.0", "result": "success"},
            {"origin": "lang/stillbad", "version": "3.0", "result": "failure"},
            {"origin": "editors/stable", "version": "4.0", "result": "success"},
            {"origin": "archivers/versioned", "version": "5.0", "result": "success"},
            {"origin": "devel/removed", "version": "6.0", "result": "success"},
        ],
    )
    finish_build_run(conn, run_a, "2026-03-10T18:00:00+00:00")

    run_b = create_build_run(conn, "@2026Q1", "release", "2026-03-14T08:00:00+00:00")
    record_results(
        conn,
        run_b,
        "@2026Q1",
        [
            {"origin": "devel/fixme", "version": "1.1", "result": "success"},
            {"origin": "www/regress", "version": "2.1", "result": "failure"},
            {"origin": "lang/stillbad", "version": "3.0", "result": "failure"},
            {"origin": "editors/stable", "version": "4.0", "result": "success"},
            {"origin": "archivers/versioned", "version": "5.1", "result": "success"},
            {"origin": "devel/added", "version": "7.0", "result": "success"},
        ],
    )

    report = compare_builds(conn, run_a, run_b)

    assert report["run_a"]["target"] == "@main"
    assert report["run_b"]["target"] == "@2026Q1"
    assert report["summary"] == {
        "new_successes": 1,
        "new_failures": 1,
        "still_failing": 1,
        "still_succeeding": 2,
        "added": 1,
        "removed": 1,
        "version_changes": 3,
    }
    assert [row["origin"] for row in report["new_successes"]] == ["devel/fixme"]
    assert [row["origin"] for row in report["new_failures"]] == ["www/regress"]
    assert [row["origin"] for row in report["still_failing"]] == ["lang/stillbad"]
    assert [row["origin"] for row in report["added"]] == ["devel/added"]
    assert [row["origin"] for row in report["removed"]] == ["devel/removed"]
    assert [row["origin"] for row in report["version_changes"]] == [
        "archivers/versioned",
        "devel/fixme",
        "www/regress",
    ]


def test_compare_builds_handles_empty_runs(conn: sqlite3.Connection) -> None:
    run_a = create_build_run(conn, "@main", "test", "2026-03-10T08:00:00+00:00")
    finish_build_run(conn, run_a, "2026-03-10T09:00:00+00:00")
    run_b = create_build_run(conn, "@main", "release", "2026-03-14T08:00:00+00:00")

    report = compare_builds(conn, run_a, run_b)

    assert report["summary"] == {
        "new_successes": 0,
        "new_failures": 0,
        "still_failing": 0,
        "still_succeeding": 0,
        "added": 0,
        "removed": 0,
        "version_changes": 0,
    }
