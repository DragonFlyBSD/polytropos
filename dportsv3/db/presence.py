"""Runner presence: enrollment, liveness, and what a runner is doing now.

ONE IMPLEMENTATION, TWO CALLERS. The runner reaches this directly today
through ``agent.state_store.LocalStore``; over HTTP it arrives at
``POST /v1/runners/presence`` and lands here with the tracker's
connection instead. The precedent is ``agent.lifecycle.apply``, which the
dsynth hooks already reach over ``/v1/jobs/transition`` while the runner
calls it directly -- one state machine, two ways in.

The point of the arrangement is that the SQL cannot drift between them.
``runner_status``'s upsert holds ``started_at`` across writes that do not
change the job, so the UI's elapsed clock measures the job rather than
the last status write; written twice, that clause is exactly the kind of
thing one copy loses.

EVENT-SHAPED, one route rather than one per verb (operator's call,
poly-fij.12). ``apply`` is the single entry point and dispatches on
``event``; the four functions under it are the operations, callable
directly where that reads better.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

#: The events ``apply`` accepts. A payload naming anything else is a
#: client bug, not a state the tracker should invent a meaning for.
EVENTS = ("register", "heartbeat", "status", "deregister")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register(
    conn: sqlite3.Connection,
    runner_id: str,
    hostname: str,
    pid: int,
    *,
    now: str | None = None,
) -> None:
    """Announce a runner. Idempotent: a process returning with the same id
    re-enrolls and clears ``stopped_at`` rather than failing on the key."""
    ts = now or _now()
    conn.execute(
        """INSERT INTO runners
           (runner_id, hostname, pid, started_at, last_heartbeat_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(runner_id) DO UPDATE SET
             last_heartbeat_at = excluded.last_heartbeat_at,
             stopped_at = NULL""",
        (runner_id, hostname, pid, ts, ts),
    )


def heartbeat(
    conn: sqlite3.Connection,
    runner_id: str,
    tail: dict[str, Any] | None = None,
    *,
    now: str | None = None,
) -> None:
    """Both liveness clocks, and the running build's last lines if any.

    The clocks: the singleton's, which is what tells a working runner from
    one that died with ``processing`` still on the row, and this runner's
    own.

    THE TAIL RIDES THIS CALL because it is the one already happening at
    the right cadence with the right identity (poly-pvs2). ``tail=None``
    CLEARS the row rather than leaving it: the value is live-only, and a
    tail that outlived its build would be shown against whatever the
    builder did next.
    """
    ts = now or _now()
    conn.execute(
        "UPDATE runner_status SET updated_at = ? WHERE id = 1", (ts,),
    )
    conn.execute(
        "UPDATE runners SET last_heartbeat_at = ? WHERE runner_id = ?",
        (ts, runner_id),
    )
    set_tail(conn, runner_id, tail, now=ts)


def set_tail(
    conn: sqlite3.Connection,
    runner_id: str,
    tail: dict[str, Any] | None,
    *,
    now: str | None = None,
) -> None:
    """Overwrite this builder's tail row, or blank it when nothing runs.

    One row per builder, overwritten in place -- see the schema comment on
    why it is not keyed by job. Blanking writes NULLs rather than deleting,
    so the row's mere existence never implies a live build.
    """
    ts = now or _now()
    if not tail:
        conn.execute(
            """INSERT INTO runner_tail (runner_id, updated_at)
               VALUES (?, ?)
               ON CONFLICT(runner_id) DO UPDATE SET
                 job_id = NULL, tool = NULL, text = NULL, lines = NULL,
                 total_bytes = NULL, skipped = NULL, max_bytes = NULL,
                 log_mtime = NULL, updated_at = excluded.updated_at""",
            (runner_id, ts),
        )
        return
    conn.execute(
        """INSERT INTO runner_tail
           (runner_id, job_id, tool, text, lines, total_bytes, skipped,
            max_bytes, log_mtime, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(runner_id) DO UPDATE SET
             job_id = excluded.job_id,
             tool = excluded.tool,
             text = excluded.text,
             lines = excluded.lines,
             total_bytes = excluded.total_bytes,
             skipped = excluded.skipped,
             max_bytes = excluded.max_bytes,
             log_mtime = excluded.log_mtime,
             updated_at = excluded.updated_at""",
        (
            runner_id,
            tail.get("job_id"),
            tail.get("tool"),
            tail.get("text") or "",
            int(tail.get("lines") or 0),
            int(tail.get("total_bytes") or 0),
            int(tail.get("skipped") or 0),
            int(tail.get("max_bytes") or 0),
            float(tail["log_mtime"]) if tail.get("log_mtime") else None,
            ts,
        ),
    )


def set_status(
    conn: sqlite3.Connection,
    status: str,
    job_id: str | None = None,
    stage: str | None = None,
    extra: dict[str, Any] | None = None,
    *,
    now: str | None = None,
) -> None:
    """Record what the runner is doing.

    ``started_at`` is held unless ``job_id`` changes -- see the module
    docstring. ``stage`` arrives already resolved: the runner's memory of
    the last stage is the runner's, and the tracker is told a stage rather
    than asked to remember the previous one.
    """
    ts = now or _now()
    conn.execute(
        """INSERT INTO runner_status
           (id, status, job_id, current_stage, started_at, updated_at,
            extra_json)
           VALUES (1, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             status = excluded.status,
             job_id = excluded.job_id,
             current_stage = excluded.current_stage,
             started_at = CASE
               WHEN excluded.job_id != runner_status.job_id
               THEN excluded.started_at
               ELSE runner_status.started_at END,
             updated_at = excluded.updated_at,
             extra_json = excluded.extra_json""",
        (status, job_id, stage, ts, ts,
         json.dumps(extra) if extra else None),
    )


def deregister(
    conn: sqlite3.Connection, runner_id: str, *, now: str | None = None,
) -> None:
    """Clean shutdown, for this runner only -- with N builders on one
    tracker, stamping every row would report a fleet as gone.

    Blanks the tail too: a runner that is stopping has no running build,
    and the job page's "is a tool still running" gate reads activity rows,
    which a stopped runner leaves looking exactly like a build in progress.
    A crash cannot run this -- there the tail's own "last line N ago"
    ticker is what reveals it, counting up from whenever the log stopped.
    """
    ts = now or _now()
    conn.execute(
        "UPDATE runners SET stopped_at = ? WHERE runner_id = ?",
        (ts, runner_id),
    )
    set_tail(conn, runner_id, None, now=ts)


def apply(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one presence event and commit.

    Raises ``ValueError`` for a payload the caller got wrong, so the
    endpoint can answer 400 rather than 500. ``runner_id`` is required on
    every event, the status event included: it identifies the caller, and
    an endpoint that cannot say who called it cannot be authenticated
    later (poly-fij.4) without changing the wire.
    """
    event = payload.get("event")
    if event not in EVENTS:
        raise ValueError(
            f"event must be one of {', '.join(EVENTS)}; got {event!r}"
        )
    runner_id = payload.get("runner_id")
    if not runner_id:
        raise ValueError("runner_id required")

    if event == "register":
        hostname = payload.get("hostname")
        pid = payload.get("pid")
        if not hostname or pid is None:
            raise ValueError("register requires hostname and pid")
        register(conn, str(runner_id), str(hostname), int(pid))
    elif event == "heartbeat":
        tail = payload.get("tail")
        if tail is not None and not isinstance(tail, dict):
            raise ValueError("tail must be an object")
        heartbeat(conn, str(runner_id), tail)
    elif event == "status":
        status = payload.get("status")
        if not status:
            raise ValueError("status required")
        extra = payload.get("extra")
        if extra is not None and not isinstance(extra, dict):
            raise ValueError("extra must be an object")
        set_status(
            conn,
            str(status),
            payload.get("job_id"),
            payload.get("stage"),
            extra,
        )
    else:  # deregister
        deregister(conn, str(runner_id))

    conn.commit()
    return {"ok": True, "event": event}
