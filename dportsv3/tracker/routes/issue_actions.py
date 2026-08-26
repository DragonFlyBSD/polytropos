"""Issue-level operator-action routes: mute / unmute / resolve / reopen.

The occurrence-level fix actions (accept/reject/…) stay on the bundle;
these act on the fingerprinted *problem*. Each endpoint checks the
authoritative gate (`issue_state.issue_action_allowed`) then writes the
transition under one `BEGIN IMMEDIATE`, the same single-writer discipline
the bundle actions and the merge reconciler use.

The transition bodies are module-level functions taking a write
connection so they can be unit-tested without a live app; each writes the
new state, its audit timestamps, and one issue event.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from dportsv3.tracker import issue_state
from dportsv3.tracker.agentic_queries import (
    get_issue,
    green_head_watermark,
)
from dportsv3.tracker.routes._common import HTTPException


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _watermark_for_issue(
    write_conn: sqlite3.Connection, issue_key: str,
) -> int | None:
    """The Green-Head boundary for an issue whose target the caller doesn't
    already hold."""
    row = write_conn.execute(
        "SELECT target FROM issues WHERE issue_key = ?", (issue_key,)
    ).fetchone()
    return green_head_watermark(write_conn, row["target"] if row else None)


def _recompute_open_state(write_conn: sqlite3.Connection, issue_key: str) -> str:
    """The resolution state a muted issue returns to when unmuted.

    ``resolved_at`` is the whole answer: an issue that carries one was
    resolved and then came back (reopen clears it), so it goes back to
    ``resolved`` and the projection re-derives the ``regressed`` badge from
    its occurrences. Anything else was plainly open.

    This used to answer "regressed?" itself, by comparing occurrence
    timestamps against ``resolved_at`` — a second, subtly different
    definition from the writer's. C3 leaves exactly one.
    """
    row = write_conn.execute(
        "SELECT resolved_at FROM issues WHERE issue_key = ?", (issue_key,)
    ).fetchone()
    resolved_at = (row["resolved_at"] if row is not None else None)
    return (issue_state.ISSUE_RESOLVED if resolved_at
            else issue_state.ISSUE_UNRESOLVED)


def mute_issue(write_conn: sqlite3.Connection, issue_key: str, *,
               now: str, actor: str) -> str:
    """Silence an issue: drops it from surfacing and (WS8) stops
    auto-triage. Returns the new state (``muted``)."""
    write_conn.execute(
        "UPDATE issues SET state = 'muted', muted_at = ?, muted_by = ?, "
        "updated_at = ? WHERE issue_key = ?",
        (now, actor, now, issue_key),
    )
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_muted",
               {"issue_key": issue_key, "actor": actor})
    return issue_state.ISSUE_MUTED


def unmute_issue(write_conn: sqlite3.Connection, issue_key: str, *,
                 now: str, actor: str) -> str:
    """Return a muted issue to the worklist, restoring the resolution state
    it was muted from. Returns the new state."""
    new_state = _recompute_open_state(write_conn, issue_key)
    write_conn.execute(
        "UPDATE issues SET state = ?, muted_at = NULL, muted_by = NULL, "
        "updated_at = ? WHERE issue_key = ?",
        (new_state, now, issue_key),
    )
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_unmuted",
               {"issue_key": issue_key, "actor": actor, "new_state": new_state})
    return new_state


def resolve_issue(write_conn: sqlite3.Connection, issue_key: str, *,
                  now: str, actor: str) -> str:
    """Manually mark an issue resolved (operator judgement, no merge).
    Returns the new state (``resolved``).

    Records the Green-Head watermark even though no build proved anything:
    the operator is asserting the problem is fixed as of now, and the newest
    build at that moment is exactly the boundary a later recurrence has to be
    past to count as a regression (C3). Without it this issue would fall back
    to comparing wall clocks forever."""
    write_conn.execute(
        "UPDATE issues SET state = 'resolved', resolved_at = ?, "
        "green_head_run_id = ?, updated_at = ? WHERE issue_key = ?",
        (now, _watermark_for_issue(write_conn, issue_key), now, issue_key),
    )
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_resolved",
               {"issue_key": issue_key, "actor": actor, "source": "manual"})
    return issue_state.ISSUE_RESOLVED


def mark_issue_resolving(write_conn: sqlite3.Connection, issue_key: str, *,
                         bundle_id: str, now: str, actor: str) -> str | None:
    """Nominate an accepted occurrence's fix for delivery: move the issue to
    ``resolving`` (resolved · awaiting delivery) and record the deliverable
    occurrence in ``delivery_bundle_id``.

    Only transitions from an open state (or supersedes an existing
    ``resolving`` with a newer accepted occurrence). A ``muted`` issue (the
    operator silenced it) or an already-``resolved`` one is left untouched, so
    accepting a stray fix can't override those. Returns the resulting state,
    or None if the issue vanished. Caller owns the surrounding transaction.

    A regressed issue is stored ``resolved`` (C3), so `resolved` joins the
    allowed set exactly when the projection derives ``regressed`` for it —
    accepting a fix for a problem that came back must work, while a genuinely
    resolved issue stays as untouchable as it was."""
    allowed = [issue_state.ISSUE_UNRESOLVED, issue_state.ISSUE_RESOLVING]
    current = get_issue(write_conn, issue_key)
    if (current is not None
            and issue_state.effective_state(current)
            == issue_state.ISSUE_REGRESSED):
        allowed.append(issue_state.ISSUE_RESOLVED)
    # Bump requested_build_generation in the SAME statement that flips to
    # `resolving` (C1): recording the desired-build intent rides the state
    # write under the caller's transaction, so a crash leaves either the old
    # or the new generation, never a half-written intent. A re-accept of a
    # newer fix just bumps the counter again — the runner reconcile loop
    # (C2) dedups; there is no separate command queue to double-fire.
    cur = write_conn.execute(
        "UPDATE issues SET state = 'resolving', delivery_bundle_id = ?, "
        "requested_build_generation = requested_build_generation + 1, "
        "updated_at = ? WHERE issue_key = ? "
        f"AND state IN ({','.join('?' * len(allowed))})",
        (bundle_id, now, issue_key, *allowed),
    )
    if cur.rowcount:
        gen_row = write_conn.execute(
            "SELECT requested_build_generation FROM issues WHERE issue_key = ?",
            (issue_key,),
        ).fetchone()
        generation = (gen_row["requested_build_generation"]
                      if gen_row is not None else None)
        from dportsv3.artifact_store import emit_event  # noqa: PLC0415
        emit_event(write_conn, "issue_resolving",
                   {"issue_key": issue_key, "actor": actor,
                    "delivery_bundle_id": bundle_id,
                    "requested_build_generation": generation})
        return issue_state.ISSUE_RESOLVING
    row = write_conn.execute(
        "SELECT state FROM issues WHERE issue_key = ?", (issue_key,)
    ).fetchone()
    return row["state"] if row else None


def resolve_issue_build_confirmed(
    write_conn: sqlite3.Connection, issue_key: str, *,
    now: str, green_head_run_id: int | None,
    actor: str = "runner",
) -> str | None:
    """Resolve an issue because a confirm build came back GREEN (A2).

    This is the execution oracle: `resolved` now means the fix was actually
    built, not that a PR merged. Guarded to ``state='resolving'`` only — a
    muted issue (operator silenced it), an unresolved one (nothing was
    accepted), and an already-resolved one are all left untouched, so a
    stray green can't override an operator decision.

    ``green_head_run_id`` is the known-good watermark (the newest
    ``build_runs`` ordinal at confirm time), stored so C3 can derive
    ``regressed`` when a LATER build re-emits the fingerprint.

    Returns the new state, or None when the guard rejected the transition
    (caller can read the actual state if it cares). Caller owns the
    surrounding transaction."""
    cur = write_conn.execute(
        "UPDATE issues SET state = 'resolved', resolved_at = ?, "
        "green_head_run_id = ?, updated_at = ? "
        "WHERE issue_key = ? AND state = 'resolving'",
        (now, green_head_run_id, now, issue_key),
    )
    if not cur.rowcount:
        return None
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_resolved",
               {"issue_key": issue_key, "actor": actor,
                "source": "build-confirmed",
                "green_head_run_id": green_head_run_id})
    return issue_state.ISSUE_RESOLVED


def reopen_issue_build_failed(
    write_conn: sqlite3.Connection, issue_key: str, *,
    now: str, actor: str = "runner", detail: str = "",
) -> str | None:
    """Reopen an issue because its confirm build came back RED (A3).

    The accepted fix did not hold, so the issue goes back to ``unresolved``
    rather than sitting in ``resolving`` looking as good as fixed. Clears the
    same delivery/resolution history as the operator
    :func:`reopen_issue` (``resolved_at`` / ``delivery_bundle_id``) and stamps
    the reopen forensics. Clearing ``resolved_at`` is what retires the
    derived ``regressed`` badge with it — there is no stored flag to reset.

    Guarded to ``state='resolving'`` — the mirror of
    :func:`resolve_issue_build_confirmed`. A muted issue, or one an operator
    already moved on, is left untouched.

    Deliberately does NOT re-enqueue any agent work: a fix that was accepted
    and then failed its build is a human judgement call, and the runner writes
    ``analysis/manual_handoff.md`` alongside this. Returns the new state, or
    None when the guard rejected the transition. Caller owns the transaction."""
    cur = write_conn.execute(
        "UPDATE issues SET state = 'unresolved', resolved_at = NULL, "
        "delivery_bundle_id = NULL, "
        "reopened_at = ?, reopened_by = ?, updated_at = ? "
        "WHERE issue_key = ? AND state = 'resolving'",
        (now, actor, now, issue_key),
    )
    if not cur.rowcount:
        return None
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_reopened",
               {"issue_key": issue_key, "actor": actor,
                "source": "build-confirmed", "detail": detail})
    return issue_state.ISSUE_UNRESOLVED


def request_confirm_build(write_conn: sqlite3.Connection, issue_key: str, *,
                          now: str, actor: str,
                          reason: str = "operator") -> int | None:
    """Standalone build-intent bump — the C1 seam for a future
    operator-triggered "start build from tracker". Increments
    ``requested_build_generation`` and returns the new generation (or None if
    the issue vanished).

    The accept→``resolving`` path (:func:`mark_issue_resolving`) folds the same
    bump inline for single-statement atomicity; this is the operator-facing
    entry point kept separate so a future ``POST /api/issues/{key}/build`` can
    request a (re)build without duplicating the counter logic. The runner
    reconcile loop (C2) acts on ``requested > confirmed`` regardless of who
    bumped it.

    NOTE: the C2 feed today only surfaces issues in ``resolving``; honouring a
    manual build on an unresolved/resolved issue means broadening that
    predicate — that is part of the operator-build feature, not this seam.
    Caller owns the surrounding transaction."""
    cur = write_conn.execute(
        "UPDATE issues SET "
        "requested_build_generation = requested_build_generation + 1, "
        "updated_at = ? WHERE issue_key = ?",
        (now, issue_key),
    )
    if not cur.rowcount:
        return None
    gen_row = write_conn.execute(
        "SELECT requested_build_generation FROM issues WHERE issue_key = ?",
        (issue_key,),
    ).fetchone()
    generation = (gen_row["requested_build_generation"]
                  if gen_row is not None else None)
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_build_requested",
               {"issue_key": issue_key, "actor": actor, "reason": reason,
                "requested_build_generation": generation})
    return generation


def reopen_issue(write_conn: sqlite3.Connection, issue_key: str, *,
                 now: str, actor: str) -> str:
    """Reopen a resolved (or resolving) issue (operator says it's not
    actually fixed): back to ``unresolved``, resolution + pending-delivery
    history cleared. Returns the new state."""
    write_conn.execute(
        "UPDATE issues SET state = 'unresolved', resolved_at = NULL, "
        "delivery_bundle_id = NULL, "
        "reopened_at = ?, reopened_by = ?, "
        "updated_at = ? WHERE issue_key = ?",
        (now, actor, now, issue_key),
    )
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_reopened",
               {"issue_key": issue_key, "actor": actor})
    return issue_state.ISSUE_UNRESOLVED


def cancel_confirm_build(write_conn: sqlite3.Connection, issue_key: str, *,
                         now: str, actor: str) -> int | None:
    """Withdraw the pending confirm-build request for an issue.

    Levels the desired state back down to what has already been confirmed
    (``requested_build_generation = last_confirmed_build_generation``) and
    drops the in-flight marker, so the reconcile loop stops re-deriving work
    for it. The counterpart to the bounded retry (yt7): an operator who knows
    the build will never succeed can stop it now instead of waiting for the
    retry budget to burn.

    A build already running in the runner is not killed — but its verdict is
    harmlessly ignored when it lands, because the stale-verdict guard rejects
    any generation that is no longer ahead of ``last_confirmed``.

    The issue stays in ``resolving``: its fix is still accepted, there is just
    no build pending. The operator's Resolve / Reopen remain available.
    Returns the levelled generation. Caller owns the transaction."""
    write_conn.execute(
        "UPDATE issues SET requested_build_generation = "
        "last_confirmed_build_generation, building_generation = NULL, "
        "updated_at = ? WHERE issue_key = ?",
        (now, issue_key),
    )
    row = write_conn.execute(
        "SELECT requested_build_generation FROM issues WHERE issue_key = ?",
        (issue_key,),
    ).fetchone()
    generation = row["requested_build_generation"] if row is not None else None
    from dportsv3.artifact_store import emit_event  # noqa: PLC0415
    emit_event(write_conn, "issue_build_cancelled",
               {"issue_key": issue_key, "actor": actor,
                "requested_build_generation": generation})
    return generation


_ACTIONS = {
    "mute": mute_issue,
    "unmute": unmute_issue,
    "resolve": resolve_issue,
    "reopen": reopen_issue,
}

# Build-control actions: same gate + transaction discipline as _ACTIONS, but
# they move the build-intent counter rather than the lifecycle state, so they
# report a generation instead of a new state.
_BUILD_ACTIONS = {
    "build": request_confirm_build,
    "cancel-build": cancel_confirm_build,
}


def register(app, ctx):
    _conn = ctx.conn

    def _do(action: str, issue_key: str, body: dict[str, Any] | None) -> dict[str, Any]:
        with _conn() as conn:
            issue = get_issue(conn, issue_key)
        if issue is None:
            raise HTTPException(status_code=404,
                                detail=f"Unknown issue: {issue_key}")
        # Effective, not stored: a regressed issue's row reads `resolved`
        # (C3) but it must keep the open-issue controls.
        state = issue_state.effective_state(issue)
        if not issue_state.issue_action_allowed(action, state):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot {action} an issue in state {state!r}",
            )
        actor = str((body or {}).get("actor") or "operator")
        now = _now()
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        write_conn.execute("PRAGMA busy_timeout=5000")
        try:
            write_conn.execute("BEGIN IMMEDIATE")
            try:
                new_state = _ACTIONS[action](
                    write_conn, issue_key, now=now, actor=actor)
                write_conn.execute("COMMIT")
            except Exception:
                write_conn.execute("ROLLBACK")
                raise
        finally:
            write_conn.close()
        return {"ok": True, "issue_key": issue_key,
                "state": new_state, "actor": actor}

    def _do_build(action: str, issue_key: str,
                  body: dict[str, Any] | None) -> dict[str, Any]:
        """Build-control actions (request / cancel a confirm build).

        Same gate and single-writer discipline as `_do`, but these move the
        build-intent counter instead of the lifecycle state, so the response
        carries the generation. The runner does the rest: it re-derives from
        `requested > confirmed` on its next pass and owns the queue.
        """
        with _conn() as conn:
            issue = get_issue(conn, issue_key)
        if issue is None:
            raise HTTPException(status_code=404,
                                detail=f"Unknown issue: {issue_key}")
        # Effective, not stored: a regressed issue's row reads `resolved`
        # (C3) but it must keep the open-issue controls.
        state = issue_state.effective_state(issue)
        if not issue_state.issue_action_allowed(action, state):
            raise HTTPException(
                status_code=409,
                detail=(f"Cannot {action} for an issue in state {state!r} — "
                        "a confirm build applies to an accepted fix "
                        "(state 'resolving')"),
            )
        actor = str((body or {}).get("actor") or "operator")
        now = _now()
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        write_conn.execute("PRAGMA busy_timeout=5000")
        try:
            write_conn.execute("BEGIN IMMEDIATE")
            try:
                generation = _BUILD_ACTIONS[action](
                    write_conn, issue_key, now=now, actor=actor)
                write_conn.execute("COMMIT")
            except Exception:
                write_conn.execute("ROLLBACK")
                raise
        finally:
            write_conn.close()
        return {"ok": True, "issue_key": issue_key, "state": state,
                "requested_build_generation": generation, "actor": actor}

    @app.post("/api/issues/{issue_key}/build")
    def api_issue_build(issue_key: str, body: dict[str, Any] | None = None):
        """Request a confirm build (operator-triggered).

        Bumps the desired-build generation; the runner's reconcile loop
        enqueues the job. Requesting while a build is already in flight cannot
        double-enqueue — the runner claims single-flight per generation — it
        simply supersedes the in-flight one with a newer request.
        """
        return _do_build("build", issue_key, body)

    @app.post("/api/issues/{issue_key}/build/cancel")
    def api_issue_build_cancel(issue_key: str,
                               body: dict[str, Any] | None = None):
        """Withdraw a pending confirm-build request."""
        return _do_build("cancel-build", issue_key, body)

    @app.post("/api/issues/{issue_key}/mute")
    def api_issue_mute(issue_key: str, body: dict[str, Any] | None = None):
        return _do("mute", issue_key, body)

    @app.post("/api/issues/{issue_key}/unmute")
    def api_issue_unmute(issue_key: str, body: dict[str, Any] | None = None):
        return _do("unmute", issue_key, body)

    @app.post("/api/issues/{issue_key}/resolve")
    def api_issue_resolve(issue_key: str, body: dict[str, Any] | None = None):
        return _do("resolve", issue_key, body)

    @app.post("/api/issues/{issue_key}/reopen")
    def api_issue_reopen(issue_key: str, body: dict[str, Any] | None = None):
        return _do("reopen", issue_key, body)
