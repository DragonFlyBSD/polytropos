"""Activity-log + event reads for the tracker's agentic endpoints."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from dportsv3.agent.lifecycle import ACTIVE_WORK_STATE_VALUES
from dportsv3.tracker.agentic_queries._util import (
    _row_dict,
    _maybe,
    _decode_extra_json,
)


def recent_activity_for_bundle(
    conn: sqlite3.Connection,
    bundle_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Activity rows tagged with a specific bundle_id.

    Tracker-side endpoints (accept, delivery, etc.) write rows
    with ``bundle_id`` populated but no ``job_id`` because they
    don't originate from a runner job. This query surfaces them
    on the bundle detail page where the job-scoped activity
    ribbon can't see them.
    """
    rows = conn.execute(
        "SELECT * FROM activity_log WHERE bundle_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (bundle_id, max(1, int(limit))),
    ).fetchall()
    return [_decode_extra_json(_row_dict(row)) for row in rows]


def recent_activity(
    conn: sqlite3.Connection,
    limit: int = 10,
    target: str | None = None,
) -> list[dict[str, Any]]:
    """Most recent activity_log rows, newest first.

    activity_log itself has no target column — filter is applied via
    a join to the originating job's target when target is supplied.
    Rows whose job_id doesn't resolve are dropped under filter.
    """
    if target is None:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT activity_log.*
               FROM activity_log
               JOIN jobs ON jobs.job_id = activity_log.job_id
               WHERE jobs.target = ?
               ORDER BY activity_log.id DESC
               LIMIT ?""",
            (target, max(1, int(limit))),
        ).fetchall()
    return [_decode_extra_json(_row_dict(row)) for row in rows]


def activity_for_job(
    conn: sqlite3.Connection,
    job_id: str,
    limit: int = 50,
    since_id: int = 0,
    stage_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Activity-log rows for one ``job_id``.

    Newest first by default (the static initial render).

    With ``since_id > 0``, returns rows with ``id > since_id`` in
    **oldest-first** order — the polling shape, so the client can
    prepend each new row at the top of an existing newest-first table.

    ``stage_filter`` (Step 9b):
    - ``"llm_turn"`` → rows where ``stage`` matches ``%llm_turn``
      (catches the canonical name plus any prefixed variant like
      ``convert:llm_turn`` written by earlier convert-flow builds)
    - ``"tool"``      → rows where ``stage LIKE 'tool:%'``
    - any other value or ``None`` → no filter
    """
    clauses = ["job_id = ?"]
    params: list[Any] = [job_id]
    if stage_filter == "llm_turn":
        clauses.append("stage LIKE ?")
        params.append("%llm_turn")
    elif stage_filter == "tool":
        clauses.append("stage LIKE ?")
        params.append("tool:%")
    if since_id and since_id > 0:
        clauses.append("id > ?")
        params.append(int(since_id))
        order = "ASC"
    else:
        order = "DESC"
    params.append(max(1, int(limit)))
    sql = (
        "SELECT * FROM activity_log WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY id {order} LIMIT ?"
    )
    rows = conn.execute(sql, params).fetchall()
    return [_decode_extra_json(_row_dict(row)) for row in rows]


def attempt_boundaries(
    conn: sqlite3.Connection, job_id: str,
) -> list[dict[str, Any]]:
    """Every attempt_start / attempt_end this job wrote, oldest first.

    A handful of rows per job, and the only place an attempt's wall clock
    has both edges. The job page fetches a bounded row window, so the
    strip cannot be built from the stream (poly-qqx9.6).
    """
    rows = conn.execute(
        "SELECT id, ts, stage, extra_json FROM activity_log "
        "WHERE job_id = ? AND stage IN ('attempt_start', 'attempt_end') "
        "ORDER BY id ASC",
        (job_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _decode_extra_json(_row_dict(row))
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        out.append({
            "id": item.get("id"), "ts": item.get("ts"),
            "stage": item.get("stage"),
            "attempt": extra.get("attempt"),
            "rebuild_ok": extra.get("rebuild_ok"),
        })
    return out


def attempt_tool_totals(
    conn: sqlite3.Connection, job_id: str,
) -> list[dict[str, Any]]:
    """Per attempt and tool: how many calls, how much wall clock, and
    whether they failed.

    Aggregated in SQL rather than in Python over the fetched rows: a
    908-event job has hundreds of tool rows and the strip needs all of
    them, across every attempt, to say where the time went.
    """
    rows = conn.execute(
        "SELECT json_extract(extra_json, '$.attempt') AS attempt, "
        "       substr(stage, 6) AS tool, "
        "       json_extract(extra_json, '$.ok') AS ok, "
        "       COUNT(*) AS n, "
        "       SUM(COALESCE(duration_ms, 0)) AS ms "
        "FROM activity_log "
        "WHERE job_id = ? AND stage LIKE 'tool:%' "
        "GROUP BY attempt, tool, ok",
        (job_id,),
    ).fetchall()
    return [
        {"attempt": r[0], "tool": r[1],
         # json_extract gives 1/0/NULL for a JSON bool; None stays None,
         # which means NO VERDICT and must not read as a failure.
         "ok": None if r[2] is None else bool(r[2]),
         "n": int(r[3] or 0), "ms": int(r[4] or 0)}
        for r in rows
    ]


def attempt_turn_totals(
    conn: sqlite3.Connection, job_id: str,
) -> list[dict[str, Any]]:
    """Per attempt: how many model turns and what they billed."""
    rows = conn.execute(
        "SELECT json_extract(extra_json, '$.attempt') AS attempt, "
        "       COUNT(*) AS n, "
        "       SUM(COALESCE(json_extract(extra_json, '$.billable_tokens'), "
        "                    json_extract(extra_json, '$.total_tokens'), "
        "                    0)) AS billable "
        "FROM activity_log "
        "WHERE job_id = ? AND stage LIKE '%llm_turn' "
        "GROUP BY attempt",
        (job_id,),
    ).fetchall()
    return [
        {"attempt": r[0], "n": int(r[1] or 0), "billable": int(r[2] or 0)}
        for r in rows
    ]


#: Tools whose call means "this attempt changed that file". The same set
#: attempt_loop uses to decide what a retry is told it already did.
WRITE_TOOLS = ("put_file", "edit_file", "apply_intent", "install_patches",
               "genpatch", "make_patch", "write_file", "apply_patch")


def write_tool_calls(
    conn: sqlite3.Connection, job_id: str,
) -> list[dict[str, Any]]:
    """Every write-tool call this job made, oldest first, with its args.

    The attempt a file first appeared in is the first attempt whose
    write-tool row names that path -- which is a query, not a schema
    change, now that poly-qqx9.13 stores the arguments as fields
    (poly-qqx9.7).
    """
    placeholders = ",".join("?" * len(WRITE_TOOLS))
    rows = conn.execute(
        f"SELECT stage, extra_json FROM activity_log "
        f"WHERE job_id = ? AND stage IN ({placeholders}) "
        f"ORDER BY id ASC",
        (job_id, *(f"tool:{t}" for t in WRITE_TOOLS)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _decode_extra_json({"extra_json": row[1]})
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        out.append({
            "tool": str(row[0])[5:],
            "attempt": extra.get("attempt"),
            "args": extra.get("args") or {},
        })
    return out


def latest_activity_extra(
    conn: sqlite3.Connection, job_id: str, stage: str,
) -> dict[str, Any]:
    """The ``extra`` of this job's newest row with that stage.

    The now-bar's denominators are STORED, not settings: attempt_start
    carries the iterations and the token budget the run actually used, so
    a job that ran under a different setting still reports its own
    numbers (poly-qqx9.3). The page fetches a bounded row window and a
    long attempt's start row falls outside it, hence the query.
    """
    row = conn.execute(
        "SELECT extra_json FROM activity_log "
        "WHERE job_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
        (job_id, stage),
    ).fetchone()
    if row is None:
        return {}
    item = _decode_extra_json({"extra_json": row[0]})
    extra = item.get("extra")
    return extra if isinstance(extra, dict) else {}


def count_llm_turns_for_job(conn: sqlite3.Connection, job_id: str) -> int:
    """How many model turns this job has taken, all of them.

    The job page fetches a bounded row window and windows that to five
    turns; counting the turns IN that window would have the link offer
    "all 67 turns" on a job that took 300 (poly-qqx9.5). The LIKE matches
    the canonical stage plus prefixed variants like ``convert:llm_turn``,
    the same way activity_for_job's filter does.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM activity_log "
        "WHERE job_id = ? AND stage LIKE '%llm_turn'",
        (job_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def events_since(
    conn: sqlite3.Connection,
    last_id: int = 0,
    target: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return events with ``id > last_id``, oldest first.

    Used by the SSE endpoint to tail events. Target filter is best-effort:
    an event's ``data_json`` carries ``target`` when the originating
    write knew it (post-step-5). Pre-step-5 events have no target and
    surface only when no filter is set.
    """
    rows = conn.execute(
        "SELECT id, ts, type, data_json FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
        (int(last_id), max(1, int(limit))),
    ).fetchall()
    items = [_row_dict(row) for row in rows]
    if target is None:
        return items
    out: list[dict[str, Any]] = []
    for item in items:
        raw = item.get("data_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if payload.get("target") == target:
            out.append(item)
    return out


def activity_pruning(conn: Any, job_id: str) -> dict[str, Any]:
    """Whether the rolling cap has eaten any of this job's activity.

    activity_log is trimmed to a GLOBAL row cap -- runner.activity_log_max,
    a DELETE in runner.py -- so a job's history does not age out on its own
    schedule: a chatty neighbour evicts it. Measured on a live builder, the
    5000-row window held 3 distinct jobs out of 1,452 (poly-a162).

    Two durable sources make the question answerable rather than a guess.
    ``jobs`` and ``job_events`` are never pruned, so a job whose first
    transition predates the oldest activity row the table still holds has
    lost activity to the cap.

    SAYS "SOME OR ALL", NEVER "SOME". Whether a job emitted any activity is
    not knowable once the rows are gone, and a verify or confirm job may
    legitimately have emitted none. The claim that holds either way is that
    anything it did record from before the horizon is gone.

    Fails CLOSED: with an empty activity_log there is no horizon to compare
    against, so ``pruned`` stays False rather than announcing pruning on a
    fresh install.

    Returns ``{oldest_retained, job_started, rows, pruned}``.
    """
    oldest = conn.execute("SELECT MIN(ts) FROM activity_log").fetchone()[0]
    started = conn.execute(
        "SELECT MIN(ts) FROM job_events WHERE job_id = ?", (job_id,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE job_id = ?", (job_id,)
    ).fetchone()[0]
    return {
        "oldest_retained": oldest,
        "job_started": started,
        "rows": int(rows or 0),
        "pruned": bool(oldest and started and str(started) < str(oldest)),
    }


# ---------------------------------------------------------------------
# Step 28a: origin skip flags
# ---------------------------------------------------------------------


def job_tail(conn: Any, job_id: str) -> dict[str, Any] | None:
    """The running build's last lines for this job, as its builder
    published them (poly-pvs2).

    ``runner_tail`` is keyed by runner and carries ``job_id`` as a column,
    so this returns nothing once that builder moved to other work -- which
    is the right answer: a finished job's output is its ``logs/full.log.gz``,
    not a tail frozen at whatever the last heartbeat caught.

    ``None`` when no builder is tailing this job, which is the ordinary
    state for every job that is not running dsynth right now.
    """
    row = conn.execute(
        """SELECT tool, text, lines, total_bytes, skipped, max_bytes,
                  log_mtime, origin, n_origins, updated_at
           FROM runner_tail
           WHERE job_id = ? AND text IS NOT NULL
           ORDER BY updated_at DESC LIMIT 1""",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "ok": True,
        "tool": row["tool"],
        "text": row["text"] or "",
        "lines": int(row["lines"] or 0),
        "total_bytes": int(row["total_bytes"] or 0),
        "skipped": int(row["skipped"] or 0),
        "max_bytes": int(row["max_bytes"] or 0),
        # The template's ticker reads this to say "last line 35s ago": the
        # LOG's clock, not the poll's.
        "mtime": row["log_mtime"],
        # Which port of the run this log belongs to, and how many the call
        # covers. Both absent on a row written before poly-quu3, and the
        # template omits the line rather than guessing.
        "origin": row["origin"],
        "n_origins": int(row["n_origins"] or 0),
        "updated_at": row["updated_at"],
    }
