"""Active-env config + env-health reads for the tracker's agentic endpoints."""

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


def get_active_env(
    conn: sqlite3.Connection, runner_id: str | None = None
) -> str | None:
    """The active dev-env for this builder, or the deployment default.

    Source of truth for the runner's per-job-dispatch env resolution
    (precedence step 2) and for the verify-fix CLI's fallback when
    ``--env`` is omitted.

    TWO LEVELS, because a dev-env belongs to a host. ``tracker_active_env``
    is a singleton by CHECK constraint and stays the operator's
    DEPLOYMENT DEFAULT; a builder that has picked its own overrides it via
    ``runners.active_env``. A builder has the envs it has, and one global
    answer is wrong for every builder that lacks that env (poly-fij.13).

    ``runner_id`` omitted returns the default, which is what every caller
    got before there was anything else -- so a single-builder install
    behaves exactly as it did.
    """
    if runner_id:
        row = conn.execute(
            "SELECT active_env FROM runners WHERE runner_id = ?", (runner_id,)
        ).fetchone()
        if row is not None:
            val = row["active_env"]
            if isinstance(val, str) and val:
                return val
    row = conn.execute(
        "SELECT env_name FROM tracker_active_env WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    val = row["env_name"]
    return val if isinstance(val, str) and val else None


def set_active_env(
    conn: sqlite3.Connection,
    env_name: str | None,
    runner_id: str | None = None,
) -> None:
    """Upsert an active dev-env. ``None`` clears it.

    With ``runner_id`` this sets that builder's own choice; without one it
    sets the deployment default every builder falls back to (poly-fij.13).
    Clearing a builder's choice returns it to the default rather than
    leaving it with none.

    No server-side validation against the envs that actually exist —
    the runner / CLI surface a clear error on use if the name doesn't
    resolve. Validation here would couple the tracker to filesystem
    state it can't reliably read (tracker runs unprivileged).
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    now = datetime.now(timezone.utc).isoformat()
    if runner_id:
        # UPDATE, not upsert: a runner row is created by enrollment and the
        # heartbeat, and inventing one here would put a builder in the table
        # that has never reported for duty.
        conn.execute(
            "UPDATE runners SET active_env = ? WHERE runner_id = ?",
            (env_name, runner_id),
        )
        conn.commit()
        return
    conn.execute(
        """INSERT INTO tracker_active_env (singleton, env_name, set_at)
           VALUES (1, ?, ?)
           ON CONFLICT(singleton) DO UPDATE SET
             env_name = excluded.env_name,
             set_at   = excluded.set_at""",
        (env_name, now),
    )
    conn.commit()


def builder_env_selections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every enrolled builder and the env it has chosen, if any.

    ``active_env`` NULL means the builder follows the deployment default, so
    the UI can show "default" rather than a blank (poly-fij.13). Ordered by
    liveness, newest first, because a restarted runner leaves its old rows
    behind -- runner_id is regenerated every process start until poly-fij.3
    persists it, so the top row is the one that is actually running.
    """
    rows = conn.execute(
        """SELECT runner_id, hostname, active_env, last_heartbeat_at,
                  stopped_at
             FROM runners
            ORDER BY last_heartbeat_at DESC"""
    ).fetchall()
    return [_row_dict(r) for r in rows]


def env_health_statuses(
    conn: sqlite3.Connection, runner_id: str | None = None
) -> list[dict[str, Any]]:
    """Latest persisted health probe per (builder, dev-env).

    Also the operator's list of which envs EXIST: the runner stubs a row per
    env on disk at start, because this is where the UI sources its env list
    from (runner.stub_unprobed_envs). So with N builders these rows are the
    inventory of who has what, and the runner_id is what makes that legible
    rather than a union of names (poly-fij.13).

    ``runner_id`` filters to one builder. Omitted, every row comes back,
    ordered by builder then env so a grouped render needs no second sort.
    ``runner_id`` of '' means unattributed -- a row that predates the host
    dimension.
    """
    sql = """SELECT runner_id, env, status, probed_at, operator_action,
                    detail_json, updated_at
               FROM env_health_status"""
    params: tuple[Any, ...] = ()
    if runner_id is not None:
        sql += " WHERE runner_id = ?"
        params = (runner_id,)
    sql += " ORDER BY runner_id ASC, env ASC"
    rows = conn.execute(sql, params).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = _row_dict(row)
        raw = item.get("detail_json")
        checks: list[dict[str, Any]] = []
        if raw:
            try:
                detail = json.loads(raw)
                item["detail"] = detail
                checks = detail.get("checks") or [] if isinstance(detail, dict) else []
            except (TypeError, ValueError):
                item["detail"] = raw
        else:
            item["detail"] = None
        item["checks"] = checks
        items.append(item)
    return items


# --- Operator pause -------------------------------------------------------
#
# Separate from runner_status on purpose. That column is the runner talking
# about itself and it rewrites it every tick, so a tracker write there would
# be overwritten and the runner could not tell its own pause from the
# operator's (poly-0w6j).

def get_runner_control(conn: sqlite3.Connection) -> dict[str, Any]:
    """Whether an operator has stopped the runner claiming new work.

    Always returns a row; an untouched tracker has never been paused.
    """
    row = conn.execute(
        "SELECT paused, reason, requested_by, requested_at "
        "FROM runner_control WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"paused": False, "reason": None,
                "requested_by": None, "requested_at": None}
    control = _row_dict(row)
    control["paused"] = bool(control.get("paused"))
    return control


def set_runner_pause(
    conn: sqlite3.Connection,
    paused: bool,
    *,
    reason: str | None = None,
    requested_by: str = "operator",
    now: str | None = None,
) -> dict[str, Any]:
    """Pause or resume. Returns the control row as it now stands.

    Resuming clears the reason rather than keeping it as history: the
    activity log is where what happened lives, and a stale reason beside
    `paused: no` reads as though it were still in force.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    stamp = now or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO runner_control
               (id, paused, reason, requested_by, requested_at)
           VALUES (1, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               paused = excluded.paused,
               reason = excluded.reason,
               requested_by = excluded.requested_by,
               requested_at = excluded.requested_at""",
        (1 if paused else 0,
         (reason or None) if paused else None,
         requested_by if paused else None,
         stamp),
    )
    return get_runner_control(conn)
