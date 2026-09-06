"""SQLite-backed build tracker database helpers.

The tracker is a read+write consumer of ``state.db``. The schema is
defined once in ``dportsv3.db.schema``; this module imports it and
provides the tracker-specific query helpers on top.

Two processes write state.db — this one and the runner — under SQLite
WAL: one writer at a time at the SQLite layer, readers proceed in
parallel. The ``ArtifactStore`` half of the tracker's writes goes
through its own connection in this same process. Each connection opens with the same PRAGMA set (WAL,
busy_timeout=5000, foreign_keys=ON) via ``open_db`` / ``init_db``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from dportsv3.common.validation import is_compose_target
from dportsv3.db.schema import DEFAULT_BUILD_TYPES, init_db as _init_state_db

_LOG = logging.getLogger(__name__)

VALID_BUILD_RESULTS = frozenset({"success", "failure", "skipped", "ignored"})

# A row is either in flight or finished, never both. enqueue_ports writes
# result='' status='queued'; record_results writes status = result. So a
# finished row's status is a copy of its result, and the two columns are one
# axis, not two -- BUILD_RESULT_STATES is that axis whole.
INFLIGHT_BUILD_STATUSES = frozenset({"queued", "building"})
BUILD_RESULT_STATES = VALID_BUILD_RESULTS | INFLIGHT_BUILD_STATUSES

# The evidence one origin's failure produced during one build run. Only
# failures upload a bundle, so this is NULL for every other row. It stays a
# correlated subselect rather than a join because SQLite then evaluates it
# once per row the page returns, not once per row the run recorded -- with
# LIMIT 50 over 13,440 results that is 50 lookups instead of 13,440. Expects
# the build_results row to be aliased ``br``.
BUNDLE_FOR_RESULT_SQL = """(
        SELECT b.bundle_id
          FROM bundles b
          JOIN runs r ON r.run_id = b.run_id
         WHERE r.build_run_id = br.build_run_id
           AND b.origin = br.origin
         ORDER BY b.ts_utc DESC
         LIMIT 1
    )"""


def open_db(db_path: str | Path) -> sqlite3.Connection:
    """Open one configured SQLite connection for tracker operations.

    PRAGMAs match what ``dportsv3.db.schema.init_db`` sets so that
    artifact-store and tracker write under identical conditions. Pragmas
    are per-connection in SQLite — applying them here on every
    connection (the tracker server opens fresh ones per request after
    commit a14fe9c4dab) is the only safe pattern.
    """
    path_text = str(db_path)
    conn = sqlite3.connect(path_text, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path_text != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class ActiveBuildError(RuntimeError):
    """Raised when a target/build_type already has an active run."""

    def __init__(self, active_run: dict[str, Any]) -> None:
        self.active_run = active_run
        run_id = active_run.get("id")
        started_at = active_run.get("started_at")
        target = active_run.get("target")
        build_type = active_run.get("build_type")
        super().__init__(
            f"Active build already exists for {target} {build_type}: run {run_id}"
            f" (started_at={started_at})"
        )


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """Open state.db and ensure the schema + seed + migrations are present.

    Delegates to ``dportsv3.db.schema.init_db`` so the schema definition
    lives in one place. Idempotent on existing files (artifact-store may
    already have initialized the same DB).
    """
    conn = open_db(db_path)
    _init_state_db(conn)
    return conn


def get_active_run(
    conn: sqlite3.Connection,
    target: str,
    build_type: str,
) -> dict[str, Any] | None:
    """Return the active run for one target/build_type, if present."""
    row = conn.execute(
        """
        SELECT *
        FROM build_runs
        WHERE target = ? AND build_type = ? AND finished_at IS NULL
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (target, build_type),
    ).fetchone()
    return _row_to_dict(row)


def last_activity_at(conn: sqlite3.Connection, run_id: int) -> str | None:
    """The newest timestamp this run has to show for itself: its most
    recent recorded result, or its start when it has recorded none."""
    row = conn.execute(
        """
        SELECT MAX(build_results.recorded_at) AS last_result,
               build_runs.started_at AS started_at
        FROM build_runs
        LEFT JOIN build_results ON build_results.build_run_id = build_runs.id
        WHERE build_runs.id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    last_result = row["last_result"]
    started_at = row["started_at"]
    if last_result and started_at:
        return max(str(last_result), str(started_at))
    return str(last_result or started_at or "") or None


def run_is_stale(
    conn: sqlite3.Connection,
    run_id: int,
    now: str | None = None,
    stale_hours: int | None = None,
) -> bool:
    """Whether an unfinished run has recorded nothing for long enough to
    be considered dead.

    Measured from the last *result*, not from the start: a run building
    one very slow port is alive and must never be superseded, while a run
    whose dsynth was killed records nothing again, ever. ``finished_at``
    cannot answer this — runner.dsynth_active documents why it refuses to
    use that column as a gate.
    """
    if stale_hours is None:
        from dportsv3 import settings  # noqa: PLC0415
        stale_hours = int(settings.get("tracker.stale_build_run_hours"))
    if stale_hours <= 0:
        return False
    last = last_activity_at(conn, run_id)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return False
    now_dt = (
        datetime.fromisoformat(now) if now
        else datetime.now(last_dt.tzinfo) if last_dt.tzinfo
        else datetime.now()
    )
    if last_dt.tzinfo is None and now_dt.tzinfo is not None:
        last_dt = last_dt.replace(tzinfo=now_dt.tzinfo)
    elif now_dt.tzinfo is None and last_dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=last_dt.tzinfo)
    return (now_dt - last_dt) >= timedelta(hours=stale_hours)


def _supersede_stale_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Close a dead run at its last known activity.

    Only ``finished_at`` is touched: ``finish_build_run`` also writes the
    three commit columns, and passing None for them would erase metadata
    a partially-reported run may already carry. The timestamp is the last
    activity rather than now, because claiming the build ran until now is
    a duration this run never had.
    """
    stamp = last_activity_at(conn, run_id) or _utc_now()
    with conn:
        conn.execute(
            "UPDATE build_runs SET finished_at = ? WHERE id = ? "
            "AND finished_at IS NULL",
            (stamp, run_id),
        )


def create_build_run(
    conn: sqlite3.Connection,
    target: str,
    build_type: str,
    started_at: str | None,
) -> int:
    """Create a new build run and return its numeric ID.

    A run left open by an interrupted dsynth used to block every later
    build on the same (target, build_type) indefinitely: this raised, the
    hook took the 409 and set TRACKING_DISABLED for its whole run, and
    the tracker recorded nothing while the farm kept building. Silent,
    unbounded, and the only trace was inside the chroot. So a *stale*
    active run is superseded here rather than defended.
    """
    _validate_target(target)
    _validate_build_type(conn, build_type)
    started_value = started_at or _utc_now()
    active_run = get_active_run(conn, target, build_type)
    if active_run is not None:
        active_id = int(active_run["id"])
        if run_is_stale(conn, active_id):
            _LOG.warning(
                "superseding stale build run %s (%s %s): no result recorded "
                "since %s. It was left open by a build that never finished; "
                "the new run records normally.",
                active_id, target, build_type, last_activity_at(conn, active_id),
            )
            _supersede_stale_run(conn, active_id)
        else:
            # Log on the tracker side too. The refusal is only visible in
            # the builder's hook log otherwise, which lives inside the
            # chroot and is not what an operator reads.
            _LOG.warning(
                "refusing to start a build for %s %s: run %s is still "
                "active and recorded results as recently as %s.",
                target, build_type, active_id, last_activity_at(conn, active_id),
            )
            raise ActiveBuildError(active_run)

    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO build_runs(target, build_type, started_at)
                VALUES (?, ?, ?)
                """,
                (target, build_type, started_value),
            )
    except sqlite3.IntegrityError as exc:
        active_run = get_active_run(conn, target, build_type)
        if active_run is not None:
            raise ActiveBuildError(active_run) from exc
        raise
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise RuntimeError("Failed to create build run")
    return int(cast(int, lastrowid))


def finish_build_run(
    conn: sqlite3.Connection,
    run_id: int,
    finished_at: str | None,
    commit_sha: str | None = None,
    commit_branch: str | None = None,
    commit_pushed_at: str | None = None,
) -> None:
    """Mark one build run finished and optionally store commit metadata."""
    _require_build_run(conn, run_id)
    finished_value = finished_at or _utc_now()
    with conn:
        cursor = conn.execute(
            """
            UPDATE build_runs
            SET finished_at = ?,
                commit_sha = ?,
                commit_branch = ?,
                commit_pushed_at = ?
            WHERE id = ?
            """,
            (finished_value, commit_sha, commit_branch, commit_pushed_at, run_id),
        )
    if cursor.rowcount == 0:
        raise ValueError(f"Unknown build run: {run_id}")


def record_results(
    conn: sqlite3.Connection,
    run_id: int,
    target: str,
    results: list[dict[str, Any]],
) -> int:
    """Record results for one build run and update current per-port status."""
    run = _require_build_run(conn, run_id)
    if str(run["target"]) != target:
        raise ValueError(
            f"Build run {run_id} belongs to target {run['target']}, not {target}"
        )

    with conn:
        for result in results:
            origin = str(result.get("origin", "")).strip()
            version = str(result.get("version", "")).strip()
            outcome = str(result.get("result", "")).strip()
            log_url = result.get("log_url")
            if not origin:
                raise ValueError("Result origin must be non-empty")
            if not version:
                raise ValueError("Result version must be non-empty")
            _validate_build_result(outcome)

            recorded_at = str(result.get("recorded_at") or _utc_now())
            conn.execute(
                """
                INSERT INTO build_results(
                    build_run_id,
                    origin,
                    version,
                    result,
                    log_url,
                    recorded_at,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(build_run_id, origin) DO UPDATE SET
                    version = excluded.version,
                    result = excluded.result,
                    log_url = excluded.log_url,
                    recorded_at = excluded.recorded_at,
                    status = excluded.status
                """,
                (run_id, origin, version, outcome, log_url, recorded_at, outcome),
            )

            success_version = version if outcome == "success" else None
            success_at = recorded_at if outcome == "success" else None
            success_run_id = run_id if outcome == "success" else None
            conn.execute(
                """
                INSERT INTO port_status(
                    target,
                    origin,
                    last_attempt_version,
                    last_attempt_result,
                    last_attempt_at,
                    last_attempt_run_id,
                    last_success_version,
                    last_success_at,
                    last_success_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target, origin) DO UPDATE SET
                    last_attempt_version = excluded.last_attempt_version,
                    last_attempt_result = excluded.last_attempt_result,
                    last_attempt_at = excluded.last_attempt_at,
                    last_attempt_run_id = excluded.last_attempt_run_id,
                    last_success_version = CASE
                        WHEN excluded.last_success_version IS NOT NULL
                            THEN excluded.last_success_version
                        ELSE port_status.last_success_version
                    END,
                    last_success_at = CASE
                        WHEN excluded.last_success_at IS NOT NULL
                            THEN excluded.last_success_at
                        ELSE port_status.last_success_at
                    END,
                    last_success_run_id = CASE
                        WHEN excluded.last_success_run_id IS NOT NULL
                            THEN excluded.last_success_run_id
                        ELSE port_status.last_success_run_id
                    END
                """,
                (
                    target,
                    origin,
                    version,
                    outcome,
                    recorded_at,
                    run_id,
                    success_version,
                    success_at,
                    success_run_id,
                ),
            )
    return len(results)


def enqueue_ports(
    conn: sqlite3.Connection,
    run_id: int,
    ports: list[dict[str, Any]],
    total_expected: int | None = None,
) -> int:
    """Bulk-insert queued ports for a build run. Returns count inserted."""
    _require_build_run(conn, run_id)
    inserted = 0
    with conn:
        for port in ports:
            origin = str(port.get("origin", "")).strip()
            version = str(port.get("version", "")).strip()
            if not origin or not version:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO build_results(
                    build_run_id, origin, version, result, log_url, recorded_at, status
                ) VALUES (?, ?, ?, '', NULL, '', 'queued')
                """,
                (run_id, origin, version),
            )
            inserted += cursor.rowcount
        if total_expected is not None:
            conn.execute(
                "UPDATE build_runs SET total_expected = ? WHERE id = ?",
                (total_expected, run_id),
            )
    return inserted


def update_port_status(
    conn: sqlite3.Connection,
    run_id: int,
    origin: str,
    status: str,
) -> None:
    """Update the status of one port in a build run (e.g. queued -> building)."""
    _require_build_run(conn, run_id)
    with conn:
        cursor = conn.execute(
            """
            UPDATE build_results SET status = ?
            WHERE build_run_id = ? AND origin = ?
            """,
            (status, run_id, origin),
        )
    if cursor.rowcount == 0:
        raise ValueError(f"No result row for run {run_id}, origin {origin}")


def get_active_builds_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return summary info for all active (unfinished) builds."""
    rows = conn.execute(
        """
        SELECT
            build_runs.id,
            build_runs.target,
            build_runs.build_type,
            build_runs.started_at,
            build_runs.total_expected,
            COALESCE(SUM(CASE WHEN br.status = 'queued' THEN 1 ELSE 0 END), 0) AS queued_count,
            COALESCE(SUM(CASE WHEN br.status = 'building' THEN 1 ELSE 0 END), 0) AS building_count,
            COALESCE(SUM(CASE WHEN br.status IN ('success', 'failure', 'skipped', 'ignored', 'recorded') THEN 1 ELSE 0 END), 0) AS done_count,
            COALESCE(SUM(CASE WHEN br.status = 'success' OR (br.status = 'recorded' AND br.result = 'success') THEN 1 ELSE 0 END), 0) AS success_count,
            COALESCE(SUM(CASE WHEN br.status = 'failure' OR (br.status = 'recorded' AND br.result = 'failure') THEN 1 ELSE 0 END), 0) AS failure_count
        FROM build_runs
        LEFT JOIN build_results br ON br.build_run_id = build_runs.id
        WHERE build_runs.finished_at IS NULL
        GROUP BY build_runs.id
        ORDER BY build_runs.started_at DESC
        """
    ).fetchall()
    summaries = [_row_dict_required(row) for row in rows]
    # An open run that is recording nothing is the shape of an interrupted
    # dsynth. start-build supersedes it on the next attempt, but until one
    # comes it is invisible, and invisible is how 137 builds went
    # unrecorded for two and a half hours. Say so where someone looks.
    for summary in summaries:
        run_id = int(summary["id"])
        summary["last_activity_at"] = last_activity_at(conn, run_id)
        summary["stale"] = run_is_stale(conn, run_id)
    return summaries


def get_build_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    """Return one build run with aggregate result counts."""
    row = conn.execute(
        """
        SELECT
            build_runs.id,
            build_runs.target,
            build_runs.build_type,
            build_runs.started_at,
            build_runs.finished_at,
            build_runs.commit_sha,
            build_runs.commit_branch,
            build_runs.commit_pushed_at,
            build_runs.total_expected,
            COUNT(build_results.origin) AS result_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'success' THEN 1 ELSE 0 END), 0) AS success_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'failure' THEN 1 ELSE 0 END), 0) AS failure_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'skipped' THEN 1 ELSE 0 END), 0) AS skipped_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'ignored' THEN 1 ELSE 0 END), 0) AS ignored_count,
            COALESCE(SUM(CASE WHEN build_results.status = 'queued' THEN 1 ELSE 0 END), 0) AS queued_count,
            COALESCE(SUM(CASE WHEN build_results.status = 'building' THEN 1 ELSE 0 END), 0) AS building_count
        FROM build_runs
        LEFT JOIN build_results ON build_results.build_run_id = build_runs.id
        WHERE build_runs.id = ?
        GROUP BY build_runs.id
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown build run: {run_id}")
    return _row_dict_required(row)


def list_build_runs(
    conn: sqlite3.Connection,
    target: str | None = None,
    build_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List build runs, newest first."""
    clauses: list[str] = []
    params: list[Any] = []
    if target is not None:
        clauses.append("build_runs.target = ?")
        params.append(target)
    if build_type is not None:
        clauses.append("build_runs.build_type = ?")
        params.append(build_type)
    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT
            build_runs.id,
            build_runs.target,
            build_runs.build_type,
            build_runs.started_at,
            build_runs.finished_at,
            build_runs.commit_sha,
            build_runs.commit_branch,
            build_runs.commit_pushed_at,
            build_runs.total_expected,
            COUNT(build_results.origin) AS result_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'success' THEN 1 ELSE 0 END), 0) AS success_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'failure' THEN 1 ELSE 0 END), 0) AS failure_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'skipped' THEN 1 ELSE 0 END), 0) AS skipped_count,
            COALESCE(SUM(CASE WHEN build_results.result = 'ignored' THEN 1 ELSE 0 END), 0) AS ignored_count,
            COALESCE(SUM(CASE WHEN build_results.status = 'queued' THEN 1 ELSE 0 END), 0) AS queued_count,
            COALESCE(SUM(CASE WHEN build_results.status = 'building' THEN 1 ELSE 0 END), 0) AS building_count
        FROM build_runs
        LEFT JOIN build_results ON build_results.build_run_id = build_runs.id
        {where_sql}
        GROUP BY build_runs.id
        ORDER BY build_runs.started_at DESC, build_runs.id DESC
        LIMIT ?
        """,
        (*params, max(1, int(limit))),
    ).fetchall()
    return [_row_dict_required(row) for row in rows]


def get_build_results(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    """Return all recorded results for one build run."""
    _require_build_run(conn, run_id)
    rows = conn.execute(
        """
        SELECT build_run_id, origin, version, result, log_url, recorded_at, status
        FROM build_results
        WHERE build_run_id = ?
        ORDER BY
            CASE status
                WHEN 'building' THEN 0
                WHEN 'queued' THEN 1
                ELSE 2
            END,
            origin ASC
        """,
        (run_id,),
    ).fetchall()
    return [_row_dict_required(row) for row in rows]


def latest_run_for_target(
    conn: sqlite3.Connection, target: str
) -> dict[str, Any] | None:
    """The newest run recorded for one target, with its counts, or None.

    /target/{target} follows whichever run is latest rather than naming
    one, so the page has to resolve it the same way the progress adapter
    does before it can render a header for it.
    """
    row = conn.execute(
        """SELECT id FROM build_runs WHERE target = ?
           ORDER BY started_at DESC, id DESC LIMIT 1""",
        (target,),
    ).fetchone()
    return get_build_run(conn, int(row[0])) if row else None


def get_build_results_page(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    state: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return one page of a run's origin results, plus the matching total.

    The searchable, filterable table ``get_build_results`` cannot serve: it
    reads every row of the run (13,440 for #1842) and carries no link to the
    evidence a failure produced.

    ``state`` is one value from ``BUILD_RESULT_STATES`` -- see that constant
    for why status and result are one axis. ``search`` is a case-insensitive
    substring of the origin; its LIKE wildcards are escaped, so searching for
    ``_`` finds an underscore rather than everything.

    Ordered by origin, which is free: build_results is keyed
    (build_run_id, origin), so the WHERE is a primary-key prefix scan already
    in that order. ``get_build_results`` orders by a CASE over status
    instead, which costs a temp b-tree over the whole run before LIMIT
    applies -- 5.94 ms against 0.26 ms at offset 13000. In-flight rows are
    reached here by filtering for them, not by sorting them to the front.
    """
    _require_build_run(conn, run_id)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    clauses = ["br.build_run_id = ?"]
    params: list[Any] = [run_id]
    if state is not None:
        if state not in BUILD_RESULT_STATES:
            raise ValueError(f"Invalid build result state: {state}")
        column = "br.status" if state in INFLIGHT_BUILD_STATUSES else "br.result"
        clauses.append(f"{column} = ?")
        params.append(state)
    if search:
        clauses.append(r"br.origin LIKE ? ESCAPE '\'")
        params.append(_like_contains(search))
    where_sql = " AND ".join(clauses)

    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM build_results br WHERE {where_sql}",
            params,
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT
            br.build_run_id,
            br.origin,
            br.version,
            br.result,
            br.log_url,
            br.recorded_at,
            br.status,
            {BUNDLE_FOR_RESULT_SQL} AS bundle_id
        FROM build_results br
        WHERE {where_sql}
        ORDER BY br.origin ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [_row_dict_required(row) for row in rows],
    }


def build_filter_options(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """The distinct targets and build types the Builds filters offer.

    ``get_target_summary`` also knows the targets, but it runs two queries
    per target to aggregate port_status; a pair of select boxes needs the
    names and nothing else.
    """
    targets = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT target FROM build_runs ORDER BY target ASC"
        ).fetchall()
    ]
    build_types = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT build_type FROM build_runs ORDER BY build_type ASC"
        ).fetchall()
    ]
    return {"targets": targets, "build_types": build_types}


def get_port_history(
    conn: sqlite3.Connection,
    target: str,
    origin: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent build history for one origin on one target."""
    rows = conn.execute(
        """
        SELECT
            build_runs.id AS build_run_id,
            build_runs.target,
            build_runs.build_type,
            build_runs.started_at,
            build_runs.finished_at,
            build_results.origin,
            build_results.version,
            build_results.result,
            build_results.log_url,
            build_results.recorded_at
        FROM build_results
        JOIN build_runs ON build_runs.id = build_results.build_run_id
        WHERE build_runs.target = ? AND build_results.origin = ?
        ORDER BY build_runs.started_at DESC, build_runs.id DESC
        LIMIT ?
        """,
        (target, origin, max(1, int(limit))),
    ).fetchall()
    return [_row_dict_required(row) for row in rows]


def get_port_status(
    conn: sqlite3.Connection,
    target: str | None = None,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    """Return current status rows filtered by target and/or origin."""
    clauses: list[str] = []
    params: list[Any] = []
    if target is not None:
        clauses.append("target = ?")
        params.append(target)
    if origin is not None:
        clauses.append("origin = ?")
        params.append(origin)
    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT *
        FROM port_status
        {where_sql}
        ORDER BY target ASC, origin ASC
        """,
        params,
    ).fetchall()
    return [_row_dict_required(row) for row in rows]


def get_failures(conn: sqlite3.Connection, target: str) -> list[dict[str, Any]]:
    """Return current failures for one target."""
    rows = conn.execute(
        """
        SELECT *
        FROM port_status
        WHERE target = ? AND last_attempt_result = 'failure'
        ORDER BY origin ASC
        """,
        (target,),
    ).fetchall()
    return [_row_dict_required(row) for row in rows]


def get_diff(
    conn: sqlite3.Connection,
    target_a: str,
    target_b: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return current per-port differences between two targets."""
    statuses_a = {row["origin"]: row for row in get_port_status(conn, target=target_a)}
    statuses_b = {row["origin"]: row for row in get_port_status(conn, target=target_b)}

    only_a: list[dict[str, Any]] = []
    only_b: list[dict[str, Any]] = []
    differ: list[dict[str, Any]] = []

    for origin in sorted(set(statuses_a) | set(statuses_b)):
        row_a = statuses_a.get(origin)
        row_b = statuses_b.get(origin)
        if row_a is None:
            assert row_b is not None
            row_b_required = row_b
            only_b.append(
                {
                    "origin": origin,
                    "target": target_b,
                    "version": row_b_required["last_attempt_version"],
                    "result": row_b_required["last_attempt_result"],
                }
            )
            continue
        if row_b is None:
            assert row_a is not None
            row_a_required = row_a
            only_a.append(
                {
                    "origin": origin,
                    "target": target_a,
                    "version": row_a_required["last_attempt_version"],
                    "result": row_a_required["last_attempt_result"],
                }
            )
            continue
        if (
            row_a["last_attempt_version"] != row_b["last_attempt_version"]
            or row_a["last_attempt_result"] != row_b["last_attempt_result"]
        ):
            differ.append(
                {
                    "origin": origin,
                    "version_a": row_a["last_attempt_version"],
                    "result_a": row_a["last_attempt_result"],
                    "version_b": row_b["last_attempt_version"],
                    "result_b": row_b["last_attempt_result"],
                }
            )

    return {"only_a": only_a, "only_b": only_b, "differ": differ}


def get_target_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return per-target current summary rows for the dashboard index."""
    targets = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT target FROM port_status
            UNION
            SELECT target FROM build_runs
            """
        ).fetchall()
    }

    summaries: list[dict[str, Any]] = []
    for target in sorted(targets):
        counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total_ports,
                COALESCE(SUM(CASE WHEN last_attempt_result = 'success' THEN 1 ELSE 0 END), 0) AS successes,
                COALESCE(SUM(CASE WHEN last_attempt_result = 'failure' THEN 1 ELSE 0 END), 0) AS failures,
                COALESCE(SUM(CASE WHEN last_attempt_result = 'skipped' THEN 1 ELSE 0 END), 0) AS skipped,
                COALESCE(SUM(CASE WHEN last_attempt_result = 'ignored' THEN 1 ELSE 0 END), 0) AS ignored
            FROM port_status
            WHERE target = ?
            """,
            (target,),
        ).fetchone()
        last_run = conn.execute(
            """
            SELECT id, build_type, started_at, finished_at
            FROM build_runs
            WHERE target = ?
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (target,),
        ).fetchone()
        count_dict = _row_dict_required(counts) if counts is not None else {}
        last_run_dict = _row_to_dict(last_run) or {}
        summaries.append(
            {
                "target": target,
                "total_ports": count_dict.get("total_ports", 0),
                "successes": count_dict.get("successes", 0),
                "failures": count_dict.get("failures", 0),
                "skipped": count_dict.get("skipped", 0),
                "ignored": count_dict.get("ignored", 0),
                "last_build_id": last_run_dict.get("id"),
                "last_build_type": last_run_dict.get("build_type"),
                "last_build_started_at": last_run_dict.get("started_at"),
                "last_build_finished_at": last_run_dict.get("finished_at"),
                "last_build_at": last_run_dict.get("finished_at")
                or last_run_dict.get("started_at"),
            }
        )
    return summaries


def compare_builds(
    conn: sqlite3.Connection,
    run_id_a: int,
    run_id_b: int,
) -> dict[str, Any]:
    """Compare two build runs and categorize origin deltas."""
    run_a = get_build_run(conn, run_id_a)
    run_b = get_build_run(conn, run_id_b)
    results_a = {row["origin"]: row for row in get_build_results(conn, run_id_a)}
    results_b = {row["origin"]: row for row in get_build_results(conn, run_id_b)}

    new_successes: list[dict[str, Any]] = []
    new_failures: list[dict[str, Any]] = []
    still_failing: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    version_changes: list[dict[str, Any]] = []
    still_succeeding = 0

    for origin in sorted(set(results_a) | set(results_b)):
        row_a = results_a.get(origin)
        row_b = results_b.get(origin)
        if row_a is None:
            assert row_b is not None
            added.append(
                {
                    "origin": origin,
                    "version_b": row_b["version"],
                    "result_b": row_b["result"],
                }
            )
            continue
        if row_b is None:
            removed.append(
                {
                    "origin": origin,
                    "version_a": row_a["version"],
                    "result_a": row_a["result"],
                }
            )
            continue

        if row_a["version"] != row_b["version"]:
            version_changes.append(
                {
                    "origin": origin,
                    "version_a": row_a["version"],
                    "result_a": row_a["result"],
                    "version_b": row_b["version"],
                    "result_b": row_b["result"],
                }
            )

        result_a = row_a["result"]
        result_b = row_b["result"]
        if result_a == "failure" and result_b == "success":
            new_successes.append(
                {
                    "origin": origin,
                    "version_a": row_a["version"],
                    "result_a": result_a,
                    "version_b": row_b["version"],
                    "result_b": result_b,
                }
            )
        elif result_a == "success" and result_b == "failure":
            new_failures.append(
                {
                    "origin": origin,
                    "version_a": row_a["version"],
                    "result_a": result_a,
                    "version_b": row_b["version"],
                    "result_b": result_b,
                }
            )
        elif result_a == "failure" and result_b == "failure":
            still_failing.append(
                {
                    "origin": origin,
                    "version_a": row_a["version"],
                    "result_a": result_a,
                    "version_b": row_b["version"],
                    "result_b": result_b,
                }
            )
        elif result_a == "success" and result_b == "success":
            still_succeeding += 1

    return {
        "run_a": run_a,
        "run_b": run_b,
        "summary": {
            "new_successes": len(new_successes),
            "new_failures": len(new_failures),
            "still_failing": len(still_failing),
            "still_succeeding": still_succeeding,
            "added": len(added),
            "removed": len(removed),
            "version_changes": len(version_changes),
        },
        "new_successes": new_successes,
        "new_failures": new_failures,
        "still_failing": still_failing,
        "added": added,
        "removed": removed,
        "version_changes": version_changes,
    }


def _require_build_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM build_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown build run: {run_id}")
    return _row_dict_required(row)


def _validate_target(target: str) -> None:
    if not is_compose_target(target):
        raise ValueError(f"Invalid build target: {target}")


def _validate_build_type(conn: sqlite3.Connection, build_type: str) -> None:
    row = conn.execute(
        "SELECT name FROM build_types WHERE name = ?",
        (build_type,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown build type: {build_type}")


def _validate_build_result(result: str) -> None:
    if result not in VALID_BUILD_RESULTS:
        raise ValueError(f"Invalid build result: {result}")


def _like_contains(term: str) -> str:
    """A LIKE pattern matching ``term`` anywhere, wildcards taken literally.

    Without this an operator searching for ``_`` matches every origin, and
    one searching for ``%`` matches every origin twice over.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {str(key): row[key] for key in row.keys()}


def _row_dict_required(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
