"""Agentic read API + bundle detail/chat/verify routes."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import parse_qs

from dportsv3.tracker import (
    fix_state,
    render,
    worktree_source,
)
from dportsv3.tracker.agentic_queries import (
    queue_operator_note,
    attempt_boundaries,
    attempt_tool_totals,
    attempt_turn_totals,
    latest_activity_extra,
    activity_for_job,
    agentic_status,
    append_chat_turn,
    clear_chat_turns,
    runner_is_live,
    env_health_statuses,
    get_active_env,
    get_bundle,
    get_runner_control,
    set_runner_pause,
    get_job,
    get_run,
    list_bundles,
    list_jobs,
    list_chat_turns,
    list_jobs_for_bundle,
    list_runs,
    recent_activity,
    runner_status,
    set_active_env,
)
from dportsv3.tracker.routes._common import (
    RedirectResponse,
    Request,
    can_operate,
    forbid_anonymous,
    HTTPException,
    Query,
    _LOG,
    _chat_llm_config,
    _pick_default_session_relpath,
)


def register(app, ctx):
    _conn = ctx.conn
    templates = ctx.templates

    @app.get("/api/agentic-status")
    def api_agentic_status() -> dict[str, Any]:
        with _conn() as conn:
            return agentic_status(conn)

    @app.get("/api/activity")
    def api_activity(
        limit: int = Query(default=10, ge=1, le=500),
        target: str | None = None,
        job_id: str | None = None,
        since_id: int = Query(default=0, ge=0),
        stage_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Activity-log query.

        - ``job_id`` set → per-job rows. With ``since_id > 0`` returns
          new rows oldest-first (polling shape for the job detail
          page's live refresh — Step 9c). Optional ``stage_filter``
          narrows to ``llm_turn`` or ``tool`` (Step 9b).
        - ``job_id`` unset → global recent (newest-first), optionally
          filtered by target.
        """
        with _conn() as conn:
            if job_id:
                return activity_for_job(
                    conn, job_id, limit=limit, since_id=since_id,
                    stage_filter=stage_filter,
                )
            return recent_activity(conn, limit=limit, target=target)

    @app.get("/api/jobs/{job_id}/activity-fragment")
    def api_job_activity_fragment(
        job_id: str,
        since_id: int = Query(default=0, ge=0),
        stage_filter: str | None = None,
        limit: int = Query(default=500, ge=10, le=5000),
        attempt: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        """Server-rendered activity since a cursor, for the live feed.

        Two shapes off one poll, both from the templates the initial page
        render uses — one render path, no client-side duplication:

        ``html``        the new ``_activity_row.html`` rows, oldest-first
                        (the caller inserts each at the top of the raw
                        table so the newest lands highest).
        ``cards_html``  the WHOLE re-rendered ``_turn_cards.html`` window.
                        A new tool row belongs INSIDE an existing turn
                        card, so the card stream cannot be built by
                        prepending; the client swaps the container
                        (poly-qqx9.4).

        ``changed`` is false when ``since_id`` has not advanced, and then
        neither body is rendered — the 3s poll costs one cheap query
        rather than a re-render. ``job_state`` lets the client stop
        polling on a terminal state without a second request.
        """
        row_tmpl = templates.env.get_template("_activity_row.html")
        cards_tmpl = templates.env.get_template("_turn_cards.html")
        bar_tmpl = templates.env.get_template("_now_bar.html")
        strip_tmpl = templates.env.get_template("_attempt_strip.html")
        wt_tmpl = templates.env.get_template("_worktree.html")
        with _conn() as conn:
            rows = activity_for_job(
                conn, job_id, limit=200, since_id=since_id,
                stage_filter=stage_filter,
            )
            job = get_job(conn, job_id)
            window = (
                activity_for_job(
                    conn, job_id, limit=limit, stage_filter=stage_filter,
                )
                if rows else []
            )
            attempt_extra = (
                latest_activity_extra(conn, job_id, "attempt_start")
                if rows else {}
            )
            decision_extra = (
                latest_activity_extra(conn, job_id, "decision")
                if rows else {}
            )
            # NOT gated on `rows`, unlike everything else here. A running
            # attempt's remainder is its `run` segment, and
            # total_ms = max(elapsed_ms, measured) -- so the track grows
            # with the WALL CLOCK. Gated on new rows it would sit frozen
            # for the whole of a long build, which is the bug in a smaller
            # window (poly-qqx9.16). Three GROUP BYs over one job through
            # idx_activity_log_job, on a table capped at
            # runner.activity_log_max rows.
            strip = (
                render.attempt_strip(
                    attempt_boundaries(conn, job_id),
                    attempt_tool_totals(conn, job_id),
                    attempt_turn_totals(conn, job_id),
                )
                if job is not None else {"attempts": [], "scale_ms": 0})
            # The same window the page renders, or the 3s swap would replace
            # five cards with the whole stream (poly-qqx9.5). Built inside
            # the connection block because the working-tree read below needs
            # both it and the connection.
            cards = render.group_activity_into_cards(window) if rows else []
            # The working tree, so the band advances while the agent edits
            # instead of only on reload (poly-5tgc). Gated on `rows` --
            # unlike the strip -- because the band changes only when a write
            # tool RETURNS, and that return is itself a new row. ``attempt``
            # carries the operator's pinned version, which the swap must not
            # steal back.
            tree = (
                worktree_source.working_tree_for(
                    conn, job, job_id, cards,
                    app.state.artifact_root, want_attempt=attempt)
                if rows and job is not None else None
            )
        html = "".join(row_tmpl.render(a=row) for row in rows)
        cards_html = (
            cards_tmpl.render(cards=render.window_cards(cards))
            if rows else ""
        )
        max_id = max((int(r["id"]) for r in rows if r.get("id")), default=since_id)
        payload: dict[str, Any] = {
            "html": html,
            "cards_html": cards_html,
            "changed": bool(rows),
            "since_id": max_id,
            "job_state": (job or {}).get("state"),
            "count": len(rows),
        }
        # THE BAR IS OMITTED WHEN NOTHING IS NEW, not sent empty. The client
        # guards with `nowbar_html !== undefined`, which an empty string
        # passes -- so sending "" erased the bar on the first quiet poll and
        # left startElapsedClock with nothing to tick. Rows arrive in bursts
        # at turn boundaries, and a 41-minute build is 41 minutes of quiet
        # polls, so the bar vanished during exactly the wait it explains
        # (poly-qqx9.15).
        #
        # An empty body still means "clear it", which is why this is a
        # presence check and not a truthiness one: on a poll that HAS rows,
        # now_bar returns None for a MANUAL tier or a job that just ended,
        # and the slot must then go empty.
        #
        # Nothing is lost by omitting it: with no new rows there is no new
        # turn and no new tool, so the bar's content cannot have changed,
        # and its elapsed clock runs client-side.
        if rows:
            payload["nowbar_html"] = bar_tmpl.render(
                now=render.now_bar(cards, attempt_extra, decision_extra))
        if rows and tree is not None:
            payload["worktree_html"] = wt_tmpl.render(
                tree=tree, wt_link=lambda n: f"?attempt={n}")
        # The one body sent on EVERY poll, for the wall-clock reason above.
        # It renders empty for a job type with no attempts, which correctly
        # empties the slot rather than leaving a stale chart.
        payload["strip_html"] = strip_tmpl.render(strip=strip)
        return payload

    @app.get("/api/runner-status")
    def api_runner_status() -> dict[str, Any]:
        """The singleton runner_status row, plus whether to believe it.

        `status` is whatever the runner last wrote, and it survives the
        process dying -- a runner killed mid-job leaves `processing` on the
        row forever. `live` is the heartbeat read (poly-chf), and it is the
        only thing that separates a runner working from one that stopped.
        """
        with _conn() as conn:
            return {**runner_status(conn), "live": runner_is_live(conn)}

    @app.get("/api/env-health")
    def api_env_health() -> list[dict[str, Any]]:
        with _conn() as conn:
            return env_health_statuses(conn)

    @app.get("/api/config/active-env")
    def api_get_active_env() -> dict[str, Any]:
        with _conn() as conn:
            return {"name": get_active_env(conn)}

    @app.put("/api/runner/pause")
    def api_runner_pause(payload: dict[str, Any]) -> dict[str, Any]:
        """Stop the runner claiming new work, or let it start again.

        Not runner_status.status: the runner rewrites that every tick, so a
        write there would be overwritten and it could not tell its own
        pause from this one. This is a durable row its gate reads, checked
        before its three self-pauses so a health pause clearing does not
        undo an operator hold (poly-0w6j).

        It stops claiming, not the job already in flight. That job runs to
        its end -- killing the process is what loses work, and this exists
        so nobody has to.
        """
        forbid_anonymous("Pausing the runner")
        paused = payload.get("paused")
        if not isinstance(paused, bool):
            raise HTTPException(
                status_code=400, detail="body must include 'paused': true|false",
            )
        reason = payload.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise HTTPException(
                status_code=400, detail="'reason' must be a string",
            )
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        try:
            control = set_runner_pause(
                write_conn, paused, reason=(reason or "").strip() or None,
            )
            from dportsv3.artifact_store import emit_event  # noqa: PLC0415
            emit_event(
                write_conn,
                "runner_paused" if paused else "runner_resumed",
                {"reason": control.get("reason")},
            )
        finally:
            write_conn.close()
        return {"ok": True, **control}

    @app.put("/api/config/active-env")
    def api_put_active_env(payload: dict[str, Any]) -> dict[str, Any]:
        forbid_anonymous("Choosing the active dev-env")
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            raise HTTPException(
                status_code=400,
                detail="name must be a string or null",
            )
        # Empty string normalizes to None (clear).
        if isinstance(name, str) and not name.strip():
            name = None
        # With a runner_id this sets that BUILDER's env; without one it sets
        # the deployment default every builder falls back to. A dev-env
        # belongs to a host, so one global answer is wrong for every builder
        # that does not have it (poly-fij.13).
        runner = payload.get("runner_id")
        if runner is not None and not isinstance(runner, str):
            raise HTTPException(
                status_code=400, detail="runner_id must be a string or null")
        runner = runner.strip() if isinstance(runner, str) else None
        with _conn() as conn:
            set_active_env(conn, name, runner_id=runner or None)
            return {"name": get_active_env(conn, runner or None),
                    "runner_id": runner or None}

    @app.get("/api/runs")
    def api_runs(
        target: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        with _conn() as conn:
            return list_runs(conn, target=target, limit=limit)

    @app.get("/api/runs/{run_id}")
    def api_run_detail(run_id: str) -> dict[str, Any]:
        with _conn() as conn:
            row = get_run(conn, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
        return row

    @app.get("/api/jobs")
    def api_jobs(
        state: str | None = None,
        target: str | None = None,
        q: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """``q`` is a case-insensitive substring of the origin or job id."""
        with _conn() as conn:
            return list_jobs(
                conn, state=state, target=target,
                search=(q or "").strip() or None, limit=limit, offset=offset,
            )

    @app.get("/api/jobs/{job_id}")
    def api_job_detail(job_id: str) -> dict[str, Any]:
        with _conn() as conn:
            row = get_job(conn, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        return row

    @app.post("/api/jobs/{job_id}/notes")
    async def api_queue_operator_note(request: Request, job_id: str) -> Any:
        """Queue something the operator knows for the job's next turn.

        The only control a running job had was Abandon -- watch or kill --
        so knowing early that a line of attack was wrong could only be
        spent by throwing away the attempt budget and the workspace
        (poly-qqx9.11). This is delivered inside the current attempt, on
        the turn composed when the running tool returns.

        Stored, not held: poly-pf4a is open precisely because the fix
        chat was never persisted.
        """
        forbid_anonymous("Sending a note to a running job")
        # Parsed here rather than through request.form(), which pulls in
        # python-multipart for a single textarea. The plain HTML form
        # posts urlencoded and keeps working without JavaScript; a fetch
        # caller can send JSON.
        body = await request.body()
        raw = body.decode("utf-8", errors="replace")
        if "application/json" in str(request.headers.get("content-type", "")):
            try:
                text = str((json.loads(raw) or {}).get("text") or "")
            except ValueError:
                text = ""
        else:
            text = " ".join(parse_qs(raw).get("text", [""]))
        with _conn() as conn:
            job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        from dportsv3.agent.lifecycle import (  # noqa: PLC0415
            ACTIVE_WORK_STATE_VALUES,
        )
        if job.get("state") not in ACTIVE_WORK_STATE_VALUES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Job {job_id} is {job.get('state')}, so there is no next "
                    "turn to deliver a note on."
                ),
            )
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False)
        write_conn.row_factory = sqlite3.Row
        try:
            note = queue_operator_note(
                write_conn, job_id, text, author="operator")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            write_conn.close()
        accept = str(getattr(request, "headers", {}).get("accept", ""))
        if "application/json" in accept:
            return {"ok": True, "note": note}
        return RedirectResponse(
            url=str(request.url_for("agentic_job_detail", job_id=job_id))
            + "#note-composer",
            status_code=303,
        )

    @app.post("/api/jobs/{job_id}/abandon")
    def api_job_abandon(job_id: str) -> dict[str, Any]:
        """Operator-triggered kill. Transitions a QUEUED or in-flight
        job to DEAD with ``retire_reason='abandoned'``. Rejects calls
        against terminal states (DONE/DEAD/ESCALATED) — the operator
        can't abandon something that's already retired."""
        forbid_anonymous("Abandoning a job")
        from dportsv3.agent import lifecycle as _lc  # noqa: PLC0415
        with _conn() as conn:
            row = get_job(conn, job_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown job: {job_id}",
            )
        # lifecycle.apply runs explicit BEGIN IMMEDIATE / COMMIT and
        # is incompatible with sqlite3's default deferred-transaction
        # wrapper. Use a dedicated autocommit-mode connection so the
        # explicit transaction works as authored.
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        try:
            try:
                new_state = _lc.apply(
                    write_conn, job_id, _lc.JobEvent.ABANDON, actor="operator",
                )
            except _lc.IllegalTransition as exc:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot abandon job in state {row.get('state')!r}: "
                        f"{exc}"
                    ),
                )
        finally:
            write_conn.close()
        return {
            "ok": True,
            "job_id": job_id,
            "previous_state": row.get("state"),
            "new_state": new_state.value,
            "retire_reason": "abandoned",
        }

    @app.get("/api/bundles")
    def api_bundles(
        target: str | None = None,
        origin: str | None = None,
        q: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """``origin`` is exact; ``q`` is a case-insensitive substring of the
        origin or bundle id."""
        with _conn() as conn:
            return list_bundles(
                conn, target=target, origin=origin,
                search=(q or "").strip() or None, limit=limit, offset=offset,
            )

    @app.get("/api/bundles/{bundle_id}")
    def api_bundle_detail(
        bundle_id: str,
        include: str = "",
    ) -> dict[str, Any]:
        """Return the bundle row + its artifacts. With ``include=jobs``
        also attaches the list of jobs that touched this bundle
        (linked via jobs.bundle_dir basename → bundle_id) so the
        analyzer subagent doesn't need a separate list-jobs join."""
        with _conn() as conn:
            row = get_bundle(conn, bundle_id)
            if row is None:
                raise HTTPException(
                    status_code=404, detail=f"Unknown bundle: {bundle_id}",
                )
            includes = {t.strip() for t in include.split(",") if t.strip()}
            if "jobs" in includes:
                row["jobs"] = list_jobs_for_bundle(conn, bundle_id)
        return row

    @app.post("/api/bundles/{bundle_id}/chat")
    def api_bundle_chat(
        bundle_id: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        """Operator Q&A about a completed fix (tools-off).

        Seeds a fresh LLM call with this bundle's **frozen artifacts** —
        the diff, triage, proposed_fix, errors, and the agent's session
        dump — assembled by ``fix_chat.build_chat_messages``, then carries
        the operator's chat turns. The original agent process is gone;
        this "chats with" a fresh model given the record the job produced.
        No tools are passed — pure explanation, nothing re-run or re-read
        from a live tree.

        Body::

            {
              "messages": [{"role":"user"|"assistant","content":str}, ...],
              "session_relpath": "<optional override>"
            }

        ``messages`` is the full client-held chat history ending with the
        operator's newest question; nothing is persisted server-side
        (v1 is ephemeral). Returns ``{ok, reply, session_relpath,
        artifacts_included, session_truncated, usage}``.

        Gated by ``llm.chat.model``: 503 when it is empty, and by the
        audience: tier 3 (agent working material), not build status.
        """
        forbid_anonymous("Fix-review chat")
        cfg = _chat_llm_config()
        if cfg is None:
            raise HTTPException(
                status_code=503,
                detail="chat is disabled: set llm.chat.model in polytropos.toml on the "
                       "tracker process to enable fix-review chat",
            )

        # The new question, and only that: the conversation is stored
        # against the bundle now, so the client no longer carries it and
        # two operators on the same port see the same thread (poly-pf4a).
        # `messages` is still accepted -- the last user turn in it is the
        # question -- so an older client keeps working.
        question = body.get("message")
        if not isinstance(question, str) or not question.strip():
            raw = body.get("messages")
            question = None
            if isinstance(raw, list):
                for m in raw:
                    if (isinstance(m, dict) and m.get("role") == "user"
                            and isinstance(m.get("content"), str)
                            and m["content"].strip()):
                        question = m["content"]
        if not isinstance(question, str) or not question.strip():
            raise HTTPException(
                status_code=400,
                detail="body must include a non-empty 'message'",
            )
        question = question.strip()

        with _conn() as conn:
            bundle = get_bundle(conn, bundle_id)
        if bundle is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown bundle: {bundle_id}",
            )
        override = body.get("session_relpath")
        session_relpath = (
            str(override).strip() if override else None
        ) or _pick_default_session_relpath(bundle)
        if session_relpath and not render.is_session_relpath(session_relpath):
            raise HTTPException(
                status_code=400,
                detail=f"not a session artifact: {session_relpath}",
            )
        if not session_relpath and not (bundle.get("artifacts") or []):
            raise HTTPException(
                status_code=404,
                detail="this bundle has no artifacts to chat about",
            )

        # Reader over THIS bundle's artifacts only: the bundle row
        # carries each artifact's ref fields (backend/sha256/fs_path), so
        # we resolve + read from the store with no extra DB round-trips
        # and no path escape (an unknown relpath simply returns None).
        artifacts_by_relpath = {
            str(a.get("relpath")): a
            for a in (bundle.get("artifacts") or [])
            if a.get("relpath")
        }

        def _read_artifact_text(relpath: str) -> str | None:
            ref = artifacts_by_relpath.get(relpath)
            if ref is None:
                return None
            p = render.resolve_artifact_path(app.state.artifact_root, ref)
            if p is None or not p.exists():
                return None
            gz = relpath.endswith(".gz") or ref.get("kind") == "gzip"
            try:
                if gz:
                    import gzip as _gzip  # noqa: PLC0415
                    with _gzip.open(p, "rt", encoding="utf-8",
                                    errors="replace") as fh:
                        return fh.read()
                return p.read_text(errors="replace")
            except OSError:
                return None

        # History from the store, not from the request: whatever the last
        # operator asked is part of this conversation even if it was asked
        # from another browser.
        with _conn() as conn:
            stored = list_chat_turns(conn, bundle_id)
        chat_turns: list[dict[str, str]] = [
            {"role": t["role"], "content": t["content"]} for t in stored
        ]
        chat_turns.append({"role": "user", "content": question})

        from dportsv3.agent import fix_chat  # noqa: PLC0415
        messages, assembled = fix_chat.build_chat_messages(
            bundle_meta=bundle,
            read_artifact=_read_artifact_text,
            session_relpath=session_relpath,
            chat_turns=chat_turns,
            cap=cfg["context_cap"],
        )

        try:
            from dportsv3.agent import llm  # noqa: PLC0415
            resp = llm.complete(
                messages,
                model=cfg["model"],
                api_base=cfg["api_base"],
                api_key=cfg["api_key"],
                custom_llm_provider=cfg["custom_llm_provider"],
                timeout=cfg["timeout"],
                reasoning=cfg["reasoning"],
            )
        except Exception as exc:  # noqa: BLE001 — surface as 502
            _LOG.warning(
                "bundle chat: llm.complete failed (bundle=%s): %s",
                bundle_id, exc,
            )
            raise HTTPException(
                status_code=502, detail=f"chat model error: {exc}",
            )

        # Both turns, and only now: a question whose answer 502'd would
        # otherwise sit in the thread forever with nothing under it.
        reply_text = resp.text or ""
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        try:
            append_chat_turn(write_conn, bundle_id, "user", question)
            append_chat_turn(
                write_conn, bundle_id, "assistant", reply_text,
                session_relpath=session_relpath,
                artifacts_included=assembled["artifacts_included"],
            )
        finally:
            write_conn.close()

        return {
            "ok": True,
            "bundle_id": bundle_id,
            "session_relpath": session_relpath,
            "artifacts_included": assembled["artifacts_included"],
            "session_truncated": assembled["session_truncated"],
            "reply": reply_text,
            # Server-rendered so the panel reuses the same Markdown subset
            # (headings/lists/code/tables) the artifact previews use,
            # rather than shipping a JS renderer. render.render_markdown escapes
            # all content, so this is innerHTML-safe.
            "reply_html": render.render_markdown(reply_text),
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
                # Without these the panel cannot tell traffic from cost,
                # and total re-counts the cached prefix (poly-0g0).
                "cached_tokens": resp.usage.cached_tokens,
                "billable_tokens": resp.usage.billable_tokens,
            },
        }

    @app.delete("/api/bundles/{bundle_id}/chat")
    def api_bundle_chat_clear(bundle_id: str) -> dict[str, Any]:
        """Drop this occurrence's fix-review conversation.

        A delete, not a soft-hide: an operator asking for it to be gone is
        asking for it to be gone, and a conversation nobody wants kept is
        not evidence (poly-pf4a).
        """
        forbid_anonymous("Fix-review chat")
        with _conn() as conn:
            if get_bundle(conn, bundle_id) is None:
                raise HTTPException(
                    status_code=404, detail=f"Unknown bundle: {bundle_id}",
                )
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        try:
            removed = clear_chat_turns(write_conn, bundle_id)
        finally:
            write_conn.close()
        return {"ok": True, "bundle_id": bundle_id, "removed": removed}

    @app.post("/api/bundles/{bundle_id}/verification")
    def api_bundle_verification(
        bundle_id: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        """Record an independent-verification outcome for a bundle
        (plan Step 11b Slice 2).

        Body shape (validated minimally on purpose — the orchestrator
        in Slice 3 owns the schema):

            {
              "ok": bool,                          # required
              "applied_diff_sha256": "<hex>"|null, # required (forensics)
              "verified_at": "<iso>"|null,         # optional; server fills
              "dsynth_exit": int|null,             # optional; persisted
              "reason": "<short text>"|null,       # optional; persisted
            }

        Updates five columns on bundles: verification_status (set to
        'verified' or 'verification_failed'), verification_at,
        verification_applied_diff_sha256, verification_exit_code and
        verification_reason. Emits a bundle_verified event so the SSE
        stream picks it up. Idempotent: re-POSTing with a different
        applied_diff_sha256 overwrites — the columns record the *last*
        verification attempt, not a history.

        ``dsynth_exit`` and ``reason`` used to be accepted and dropped
        (the exit code reached only the SSE payload), so a failed
        verification recorded no reason anywhere and an operator could
        see THAT it failed and never why. The full log is uploaded
        separately as analysis/verification.log by the orchestrator;
        these two columns are what the bundle page reads so it can say
        what happened without fetching an artifact.
        """
        from datetime import datetime, timezone  # noqa: PLC0415

        if "ok" not in body or not isinstance(body["ok"], bool):
            raise HTTPException(
                status_code=400,
                detail="body must include boolean 'ok'",
            )
        if "applied_diff_sha256" not in body:
            raise HTTPException(
                status_code=400,
                detail="body must include 'applied_diff_sha256' "
                       "(null acceptable if no diff was applied)",
            )

        with _conn() as conn:
            row = get_bundle(conn, bundle_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown bundle: {bundle_id}",
            )

        status = "verified" if body["ok"] else "verification_failed"
        verified_at = body.get("verified_at") or (
            datetime.now(timezone.utc).isoformat()
        )
        applied_diff_sha = body.get("applied_diff_sha256")
        dsynth_exit = body.get("dsynth_exit")
        reason = body.get("reason")
        if reason is not None:
            reason = str(reason)[:2000]

        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        try:
            write_conn.execute(
                """UPDATE bundles SET
                       verification_status = ?,
                       verification_at = ?,
                       verification_applied_diff_sha256 = ?,
                       verification_exit_code = ?,
                       verification_reason = ?,
                       last_seen_at = ?
                   WHERE bundle_id = ?""",
                (status, verified_at, applied_diff_sha,
                 dsynth_exit, reason, verified_at, bundle_id),
            )
            from dportsv3.artifact_store import emit_event  # noqa: PLC0415
            emit_event(write_conn, "bundle_verified", {
                "bundle_id": bundle_id,
                "verification_status": status,
                "verification_at": verified_at,
                "applied_diff_sha256": applied_diff_sha,
                "dsynth_exit": dsynth_exit,
                "reason": reason,
            })
        finally:
            write_conn.close()

        return {
            "ok": True,
            "bundle_id": bundle_id,
            "verification_status": status,
            "verification_at": verified_at,
            "applied_diff_sha256": applied_diff_sha,
            "verification_exit_code": dsynth_exit,
            "verification_reason": reason,
        }

    @app.post("/api/bundles/{bundle_id}/verify")
    def api_bundle_verify(
        bundle_id: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        forbid_anonymous("Verify")
        """Operator-triggered verify (Step 11c). Writes a row to
        ``verify_requests``; the runner's poll loop picks it up,
        calls ``dportsv3.verify_fix.run_verify_fix`` in-process, and
        the result POSTs back to ``/verification`` (Slice 2) when
        done.

        Body: ``{"env": "<dev-env-name>"}``. Operator-chosen env;
        auto-provisioning is a follow-up.

        The tracker doesn't import the runner or touch the queue
        filesystem any more (layer-violation cleanup). The
        ``verify_requests`` table mirrors the
        ``user_context_requests`` pattern: the tracker records
        intent, the runner reconciles.
        """
        from datetime import datetime, timezone  # noqa: PLC0415

        env = (body or {}).get("env")
        if not env or not isinstance(env, str):
            raise HTTPException(
                status_code=400,
                detail="body must include 'env' (dev-env name)",
            )
        with _conn() as conn:
            row = get_bundle(conn, bundle_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Unknown bundle: {bundle_id}",
            )
        if not fix_state.action_allowed(
            "verify", row.get("resolution"), row.get("verification_status"),
            can_operate=can_operate(),
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot verify bundle in terminal state "
                    f"{row.get('resolution')!r}"
                ),
            )

        now = datetime.now(timezone.utc).isoformat()
        write_conn = sqlite3.connect(
            str(app.state.db_path), check_same_thread=False,
            isolation_level=None,
        )
        write_conn.row_factory = sqlite3.Row
        try:
            cur = write_conn.execute(
                """INSERT INTO verify_requests
                       (bundle_id, env, requested_by, requested_at, status)
                   VALUES (?, ?, 'operator', ?, 'pending')""",
                (bundle_id, env, now),
            )
            request_id = cur.lastrowid
            from dportsv3.artifact_store import emit_event  # noqa: PLC0415
            emit_event(write_conn, "verify_requested", {
                "bundle_id": bundle_id,
                "request_id": request_id,
                "env": env,
                "requested_at": now,
            })
        finally:
            write_conn.close()

        return {
            "ok": True,
            "bundle_id": bundle_id,
            "request_id": request_id,
            "status": "pending",
            "env": env,
        }

