"""HTML page routes + manual-request and progress JSON endpoints."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dportsv3 import settings
from dportsv3.tracker import (
    delivery_sync,
    fix_state,
    issue_state,
    preflight_status,
    render,
)
from dportsv3.tracker.agentic_queries import (
    active_job_for_port,
    activity_for_job,
    agentic_status,
    bundles_for_run,
    discard_manual_request,
    distinct_targets,
    env_health_statuses,
    get_active_env,
    get_artifact_ref,
    get_bundle,
    open_delivery_bundle_ids,
    get_job,
    count_bundles,
    count_deliveries,
    count_manual_requests,
    count_issues,
    count_jobs,
    delivery_counts,
    get_manual_request,
    get_issue,
    get_run,
    issue_for_bundle,
    issue_inventory,
    issues_with_occurrences,
    job_events_for_job,
    job_outcome_counts,
    latest_review_request_for_bundle,
    latest_verify_request,
    list_bundles,
    list_deliveries,
    list_issues,
    list_jobs,
    list_jobs_for_bundle,
    list_manual_requests,
    list_port_bundles,
    occurrence_attempts,
    port_attempt_summary,
    recent_activity,
    recent_activity_for_bundle,
    RUNNER_BAND,
    regressed_issue_count,
    runner_is_live,
    runner_status,
    worklist_band_counts,
    token_usage_for_job,
    token_usage_for_port,
    upsert_user_context_text,
    verify_requests_for_bundle,
)
from dportsv3.tracker.agentic_queries.deliveries import (
    DELIVERY_OPEN_STATUSES,
    DELIVERY_STATUSES,
)
from dportsv3.tracker.db import (
    INFLIGHT_BUILD_STATUSES,
    build_filter_options,
    compare_builds,
    get_active_builds_summary,
    get_build_results_page,
    get_build_run,
    get_diff,
    get_port_history,
    get_port_status,
    get_target_summary,
    latest_run_for_target,
    list_build_runs,
)
from dportsv3.tracker.models import (
    ManualContextRequest,
    ManualContextResponse,
    ManualDiscardRequest,
    ManualDiscardResponse,
)
from dportsv3.tracker.progress_adapter import (
    run_history_chunk,
    run_summary,
    target_history_chunk,
    target_summary,
)
from dportsv3.tracker.routes._common import (
    can_operate,
    forbid_anonymous,
    HTMLResponse,
    HTTPException,
    Query,
    RedirectResponse,
    RequestType,
    _chat_llm_config,
    _pick_default_session_relpath,
)


# One screen of origins. 13,440 rows is a run, not a page.
_ORIGIN_PAGE = 50

# One screen of an operator list. The old caps -- 300 issues, 200 bundles,
# 200 jobs -- were not pages: they were the whole answer, and a port outside
# them could not be found by typing its name because the search was
# client-side over what had already been fetched.
_LIST_PAGE = 100

# `regressed` is derived from occurrences, so it cannot be a SQL filter: the
# rows come back as stored `resolved` and the split happens in Python, which
# means SQL paging would leave holes. Those two filters page in Python over a
# bounded fetch instead, and the page says when the bound bit.
_DERIVED_STATE_FILTERS = frozenset({"resolved", "regressed"})
_DERIVED_FETCH_CAP = 1000

# The worklist groups by band rather than paging, so its bound is a cap
# and not a page. It is named here so the page can say when it bit.
_WORKLIST_CAP = 500

# The chips over the origin table, each paired with the run count that
# fills it. A state the run never produced gets no chip -- an operator does
# not need a "Skipped 0" to click.
_STATE_CHIPS = (
    ("", "All", "result_count"),
    ("failure", "Failed", "failure_count"),
    ("success", "Built", "success_count"),
    ("building", "Building", "building_count"),
    ("queued", "Queued", "queued_count"),
    ("skipped", "Skipped", "skipped_count"),
    ("ignored", "Ignored", "ignored_count"),
)


def _state_filters(
    run: dict[str, Any] | None, selected: str
) -> list[tuple[str, str, int]]:
    """The state chips this run earns, plus whichever one is selected.

    The selected chip stays even at zero: it is how the operator sees that
    the filter they are looking through matched nothing, rather than the
    control vanishing out from under the click.
    """
    if run is None:
        return []
    chips = []
    for key, label, count_key in _STATE_CHIPS:
        count = int(run.get(count_key) or 0)
        if key == "" or count or key == selected:
            chips.append((key, label, count))
    return chips


def _port_link(request: Any):
    """``port_link(target, origin)`` -> the port page, or None.

    dashboard_port_detail routes on cat and port separately, so an origin
    that is not exactly ``cat/port`` has no page to link to. Resolving that
    here means the template renders plain text instead of a broken URL.
    """

    def port_link(target: str, origin: str) -> str | None:
        parts = str(origin).split("/")
        if len(parts) != 2 or not all(parts):
            return None
        return str(
            request.url_for(
                "dashboard_port_detail", target=target, cat=parts[0], port=parts[1]
            )
        )

    return port_link


def _confirm_for(conn: Any):
    """A ``confirm_for(issue)`` for one request's templates.

    `resolving` is one word covering a whole loop, and the list views show
    it as often as the detail page does. The runner's thresholds and its
    heartbeat are read once here rather than per row.
    """
    threshold = int(settings.get("runner.confirm_green_threshold"))
    max_failures = int(settings.get("runner.confirm_max_failures"))
    now = datetime.now(timezone.utc).isoformat()
    live = runner_is_live(conn)

    def confirm_for(issue: dict[str, Any]) -> Any:
        return issue_state.confirm_status(
            issue, threshold=threshold, max_failures=max_failures,
            now=now, runner_live=live,
        )

    return confirm_for


def _query_for(base: dict[str, Any]):
    """A ``query_for(**overrides)`` for one request's templates.

    Every link on the Builds page is this page with one thing changed, so
    the template says what changed and the rest carries. Empty and None
    drop out, which is what clears a filter.
    """

    def query_for(**overrides: Any) -> str:
        merged = dict(base)
        merged.update(overrides)
        return urlencode(
            [(k, str(v)) for k, v in merged.items() if v not in (None, "")]
        )

    return query_for


def register(app, ctx):
    _conn = ctx.conn
    templates = ctx.templates


    @app.get("/", response_class=HTMLResponse)
    def dashboard_index(
        request: RequestType,
        target: str | None = None,
        build_type: str | None = None,
        run: int | None = None,
        state: str | None = None,
        q: str | None = None,
        page: int = Query(default=1, ge=1),
    ) -> Any:
        """Builds — the landing view: what is running, what finished, and
        one run's origins.

        The origin table is server-rendered from get_build_results_page, so
        search, state filter and paging are ordinary links and form GETs
        rather than a client that has to hold 13,440 rows to filter them.
        """
        with _conn() as conn:
            active_builds = [
                row
                for row in get_active_builds_summary(conn)
                if (not target or row["target"] == target)
                and (not build_type or row["build_type"] == build_type)
            ]
            recent_runs = [
                row
                for row in list_build_runs(
                    conn, target=target, build_type=build_type, limit=40
                )
                if row["finished_at"]
            ][:10]

            selected_run = None
            if run is not None:
                try:
                    selected_run = get_build_run(conn, run)
                except ValueError as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
            elif active_builds:
                selected_run = get_build_run(conn, int(active_builds[0]["id"]))
            elif recent_runs:
                selected_run = get_build_run(conn, int(recent_runs[0]["id"]))

            results: dict[str, Any] = {
                "total": 0, "limit": _ORIGIN_PAGE, "offset": 0, "results": [],
            }
            if selected_run is not None:
                try:
                    results = get_build_results_page(
                        conn,
                        int(selected_run["id"]),
                        state=state or None,
                        search=q,
                        limit=_ORIGIN_PAGE,
                        offset=(page - 1) * _ORIGIN_PAGE,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

            options = build_filter_options(conn)

        base = {
            "target": target, "build_type": build_type,
            "run": selected_run["id"] if selected_run else None,
            "state": state, "q": q, "page": page if page > 1 else None,
        }
        return templates.TemplateResponse(
            request,
            "builds_dashboard.html",
            {
                "title": "Builds",
                "active_builds": active_builds,
                "recent_runs": recent_runs,
                "selected_run": selected_run,
                "selected_run_id": selected_run["id"] if selected_run else None,
                "results": results,
                "page": page,
                "search": q,
                "selected_state": state or "",
                "state_filters": _state_filters(selected_run, state or ""),
                "inflight_statuses": INFLIGHT_BUILD_STATUSES,
                "target_options": options["targets"],
                "build_type_options": options["build_types"],
                "selected_target": target,
                "selected_build_type": build_type,
                "query_for": _query_for(base),
                "port_link": _port_link(request),
                "carried_params": [
                    (k, v) for k, v in base.items()
                    if v not in (None, "") and k not in ("q", "page")
                ],
                "refresh_seconds": 30 if active_builds else None,
            },
        )

    @app.get("/targets", response_class=HTMLResponse)
    def dashboard_targets(request: RequestType) -> Any:
        """The per-target rollup that used to be the landing page. Builds
        took / over; this keeps the cumulative view reachable."""
        with _conn() as conn:
            return templates.TemplateResponse(
                request,
                "targets.html",
                {"title": "Targets", "targets": get_target_summary(conn)},
            )

    # ------------------------------------------------------------------
    # Pipeline: the system overview (UI-4).
    # ------------------------------------------------------------------

    def _pipeline_flow(status, issues, regressed, bands, deliveries,
                       outcomes, active_runs, runner_live):
        """The six nodes, as data.

        Assembled here rather than in the template so that each one names
        the query it came from in one place -- that label is on the page,
        and naming the wrong function is worse than naming none.

        The tint is a CONDITION, not a census. The mock paints node 01 red
        whenever any failure has been ingested and node 02 amber whenever
        any issue exists, which is every working system, permanently; a
        tint that is always on stops being a signal. These fire on
        something an operator would act on.
        """
        band_work = sum(
            n for key, n in bands.items()
            if key not in (RUNNER_BAND, "confirming")
        )
        queued = status["jobs"]["pending"]
        inflight = status["jobs"]["inflight"]
        return [
            {"key": "failures", "n": 1, "name": "Build failures",
             "value": status["bundles"], "unit": "occurrences",
             "sub": (f"ingested from {len(active_runs)} active "
                     f"{'run' if len(active_runs) == 1 else 'runs'}"
                     if active_runs else "no active runs"),
             "tint": "", "src": "agentic_status()"},
            {"key": "issues", "n": 2, "name": "Issues",
             "value": issues["total"], "unit": "fingerprints",
             "sub": (f"{issues['systemic']} systemic · {regressed} regressed"
                     if issues["total"] else "nothing fingerprinted"),
             "tint": "",
             "src": "issue_inventory() · regressed_issue_count()"},
            {"key": "automation", "n": 3, "name": "Automated work",
             "value": inflight, "unit": "in flight",
             "sub": (f"{queued} queued · {bands[RUNNER_BAND]} "
                     f"issue{'' if bands[RUNNER_BAND] == 1 else 's'} held"),
             # Queued work and no runner to do it is a fault, not a census.
             "tint": "alert" if queued and not runner_live else "",
             "src": "agentic_status() · worklist_band_counts()"},
            {"key": "operator", "n": 4, "name": "Operator work",
             "value": band_work, "unit": "need you",
             "sub": (f"{status['manual_pending']} waiting for context"
                     if status["manual_pending"] else "no manual escalations"),
             "tint": "warning" if band_work else "",
             "src": "worklist_band_counts()"},
            {"key": "delivery", "n": 5, "name": "Delivery",
             "value": deliveries["open"], "unit": "open upstream",
             # Two axes, named apart. The mock has one node reading
             # "open PRs / awaiting confirm build" and poly-8e2 measured
             # 17 live counterexamples to their being the same thing.
             "sub": (f"{deliveries['awaiting_confirm_build']} awaiting a "
                     f"confirm build · {deliveries['failed']} never sent"),
             # No tint. A never-sent delivery IS a stranded fix (poly-8e2)
             # but the rows are terminal and historical, so alerting on
             # them is red forever on any system with a past -- the same
             # census-not-condition mistake the mock makes on nodes 01 and
             # 02. Telling today's from last quarter's needs a time-window
             # query, which is the one thing this page has none of.
             "tint": "",
             "src": "delivery_counts()"},
            {"key": "outcome", "n": 6, "name": "Outcome",
             "value": issues["by_state"]["resolved"], "unit": "resolved",
             "sub": (f"{regressed} came back after resolve" if regressed
                     else f"{outcomes['dead']['failed']} jobs failed "
                          f"the work"),
             "tint": "success",
             "src": "issue_inventory() · job_outcome_counts()"},
        ]

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline_overview(request: RequestType) -> Any:
        """Is the system healthy and moving -- a different question from
        what needs me, which is the Repairs worklist.

        Every number here is a real count of a named population, uncapped.
        None of them is a rate: there is no time-window aggregation query,
        and a made-up throughput figure on a page whose whole job is to be
        trusted is worse than no figure.
        """
        with _conn() as conn:
            status = agentic_status(conn)
            issues = issue_inventory(conn)
            regressed = regressed_issue_count(conn)
            bands = worklist_band_counts(conn)
            deliveries = delivery_counts(conn)
            outcomes = job_outcome_counts(conn)
            active_runs = get_active_builds_summary(conn)
            live = runner_is_live(conn)
            return templates.TemplateResponse(
                request,
                "pipeline.html",
                {
                    "title": "Pipeline",
                    "status": status,
                    "issues": issues,
                    "regressed": regressed,
                    "bands": bands,
                    "band_labels": dict(
                        (key, label)
                        for key, label, _cls in
                        issue_state.ISSUE_WORKLIST_SECTIONS
                    ),
                    "deliveries": deliveries,
                    "outcomes": outcomes,
                    "runner": runner_status(conn),
                    "runner_live": live,
                    "env_health": env_health_statuses(conn),
                    "preflight": preflight_status.current(),
                    "active_runs": active_runs,
                    "flow": _pipeline_flow(
                        status, issues, regressed, bands, deliveries,
                        outcomes, active_runs, live,
                    ),
                },
            )

    @app.get("/pipeline/deliveries", response_class=HTMLResponse)
    def pipeline_deliveries(
        request: RequestType,
        status: str | None = None,
        provider: str | None = None,
        q: str | None = None,
        page: int = Query(default=1, ge=1),
    ) -> Any:
        """The delivery stage's drawer: one row per delivered bundle, its
        newest delivery row, and the issue it belongs to."""
        status_value = (status or "").strip() or None
        provider_value = (provider or "").strip() or None
        search = (q or "").strip() or None
        # `open` is not a status, it is the two that are still upstream.
        # Offered because it is the question the overview links on.
        if status_value == "open":
            statuses: tuple[str, ...] | None = DELIVERY_OPEN_STATUSES
        elif status_value in DELIVERY_STATUSES:
            statuses = (status_value,)
        else:
            statuses = None
            status_value = None
        offset = (page - 1) * _LIST_PAGE
        with _conn() as conn:
            return templates.TemplateResponse(
                request,
                "pipeline_deliveries.html",
                {
                    "title": "Deliveries",
                    "deliveries": list_deliveries(
                        conn, statuses=statuses, provider=provider_value,
                        search=search, limit=_LIST_PAGE, offset=offset,
                    ),
                    "total": count_deliveries(
                        conn, statuses=statuses, provider=provider_value,
                        search=search,
                    ),
                    "counts": delivery_counts(conn),
                    "page": page,
                    "per_page": _LIST_PAGE,
                    "search": search,
                    "query_for": _query_for({
                        "status": status_value, "provider": provider_value,
                        "q": search, "page": page if page > 1 else None,
                    }),
                    "selected_status": status_value,
                    "statuses": DELIVERY_STATUSES,
                },
            )

    # ------------------------------------------------------------------
    # Phase 4 step 6: agentic HTML views.
    # ------------------------------------------------------------------

    @app.get("/agentic", response_class=HTMLResponse)
    def agentic_index(request: RequestType) -> Any:
        # Lazy delivery reconcile FIRST: a PR merged upstream resolves its
        # issue (WS4), so it runs before the issues are read and the render
        # sees current states. One call, throttled process-wide by
        # tracker.delivery_sweep_seconds -- this page is polled every few
        # seconds by everyone watching, and it used to open one SQLite
        # connection per open delivery on every single render.
        delivery_sync.reconcile_open_deliveries(
            db_path=app.state.db_path, provider="github",
        )
        with _conn() as conn:
            # The landing is an issue worklist: fingerprinted problems that
            # need you, grouped and bucketed by their actionable occurrence.
            # 500 is a generous window — resolved/muted issues live in the
            # collapsed archives, not the actionable bands.
            issues = issues_with_occurrences(conn, limit=_WORKLIST_CAP)
            # The cap orders times_seen DESC, so what it drops is the long
            # tail -- exactly where a specific port someone is looking for
            # usually lives. Say so rather than looking complete.
            issue_total = count_issues(conn)
            worklist = issue_state.build_issue_worklist(issues)
            bands = [
                {
                    "key": key,
                    "label": label,
                    "cls": cls,
                    "count": len(worklist[key]),
                    "groups": worklist[key],
                }
                for key, label, cls in issue_state.ISSUE_WORKLIST_SECTIONS
                if key not in ("done", "muted")
            ]
            focus_count = sum(b["count"] for b in bands)
            return templates.TemplateResponse(
                request,
                "agentic_index.html",
                {
                    "title": "Agentic",
                    "status": agentic_status(conn),
                    "env_health": env_health_statuses(conn),
                    "active_env": get_active_env(conn),
                    "bands": bands,
                    "focus_count": focus_count,
                    "done_groups": worklist["done"],
                    "muted_groups": worklist["muted"],
                    "issue_total": issue_total,
                    "worklist_cap": _WORKLIST_CAP,
                    "confirm_for": _confirm_for(conn),
                },
            )

    @app.get("/agentic/bundles", response_class=HTMLResponse)
    def agentic_bundles(
        request: RequestType,
        target: str | None = None,
        origin: str | None = None,
        q: str | None = None,
        page: int = Query(default=1, ge=1),
    ) -> Any:
        target_value = target or None
        origin_value = (origin or "").strip() or None
        search = (q or "").strip() or None
        offset = (page - 1) * _LIST_PAGE
        with _conn() as conn:
            return templates.TemplateResponse(
                request,
                "agentic_bundles.html",
                {
                    "title": "Bundles",
                    "bundles": list_bundles(
                        conn, target=target_value, origin=origin_value,
                        search=search, limit=_LIST_PAGE, offset=offset,
                    ),
                    "total": count_bundles(
                        conn, target=target_value, origin=origin_value,
                        search=search,
                    ),
                    "page": page,
                    "per_page": _LIST_PAGE,
                    "search": search,
                    "query_for": _query_for({
                        "target": target_value, "origin": origin_value,
                        "q": search, "page": page if page > 1 else None,
                    }),
                    "target_options": distinct_targets(conn),
                    "selected_target": target_value,
                    "selected_origin": origin_value,
                },
            )

    @app.get("/agentic/issues", response_class=HTMLResponse)
    def agentic_issues(
        request: RequestType,
        target: str | None = None,
        state: str | None = None,
        q: str | None = None,
        page: int = Query(default=1, ge=1),
    ) -> Any:
        target_value = target or None
        state_value = (state or "").strip() or None
        search = (q or "").strip() or None
        offset = (page - 1) * _LIST_PAGE
        # `regressed` is derived, so it cannot be a SQL filter: narrow to the
        # stored states that could present as the requested one, then filter
        # exactly on the effective state. Both `resolved` and `regressed`
        # narrow to stored `resolved` and the split happens here.
        states = (
            issue_state.stored_states_for(state_value) if state_value else None
        )
        truncated = False
        with _conn() as conn:
            if state_value in _DERIVED_STATE_FILTERS:
                # SQL paging would leave holes: the rows this filter drops
                # are interleaved with the ones it keeps.
                fetched = issues_with_occurrences(
                    conn, target=target_value, states=states, search=search,
                    limit=_DERIVED_FETCH_CAP,
                )
                truncated = len(fetched) == _DERIVED_FETCH_CAP
                matching = [
                    i for i in fetched
                    if issue_state.effective_state(i) == state_value
                ]
                total = len(matching)
                issues = matching[offset:offset + _LIST_PAGE]
            else:
                total = count_issues(
                    conn, target=target_value, states=states, search=search,
                )
                issues = issues_with_occurrences(
                    conn, target=target_value, states=states, search=search,
                    limit=_LIST_PAGE, offset=offset,
                )
            return templates.TemplateResponse(
                request,
                "agentic_issues.html",
                {
                    "title": "Issues",
                    "issues": issues,
                    "confirm_for": _confirm_for(conn),
                    "total": total,
                    "truncated": truncated,
                    "page": page,
                    "per_page": _LIST_PAGE,
                    "search": search,
                    "query_for": _query_for({
                        "target": target_value, "state": state_value,
                        "q": search, "page": page if page > 1 else None,
                    }),
                    "target_options": distinct_targets(conn),
                    "selected_target": target_value,
                    "selected_state": state_value,
                },
            )

    @app.get("/agentic/issues/{issue_key}", response_class=HTMLResponse)
    def agentic_issue_detail(request: RequestType, issue_key: str) -> Any:
        with _conn() as conn:
            issue = get_issue(conn, issue_key)
            if issue is None:
                raise HTTPException(
                    status_code=404, detail=f"Unknown issue: {issue_key}"
                )
            group = issue_state.issue_group(
                issue, issue.get("occurrences") or []
            )
            # `resolving` is one word covering a whole loop. The projection
            # says where in it this issue is; the runner's own thresholds
            # and its heartbeat are what let it tell a build that is running
            # from a marker a dead runner left behind.
            confirm = _confirm_for(conn)(issue)
            # poly-0e02.7: per occurrence, how many jobs worked it and how
            # far they got. One aggregate for the whole selector rather than
            # a query per row, over the two DURABLE sources -- jobs and
            # job_events, neither of which is ever pruned.
            attempts = occurrence_attempts(
                conn, [o.get("bundle_id") for o in group["occurrences"]],
            )
            return templates.TemplateResponse(
                request,
                "agentic_issue.html",
                {
                    "title": issue.get("origin") or "Issue",
                    "issue": issue,
                    "group": group,
                    "confirm": confirm,
                    "attempts": attempts,
                },
            )

    @app.get("/agentic/bundles/{bundle_id}", response_class=HTMLResponse)
    def agentic_bundle_detail(
        request: RequestType,
        bundle_id: str,
        artifact: str | None = None,
    ) -> Any:
        with _conn() as conn:
            bundle = get_bundle(conn, bundle_id)
            # Lazy delivery reconcile: if this bundle's PR merged upstream,
            # flip it terminal now so the page shows "merged" + drops
            # Accept/Reject rather than offering a re-Accept that would
            # spawn a duplicate PR. No-op (throttled) unless it has an open
            # GitHub delivery row; re-read on a merge so the projection +
            # action surface below reflect the new terminal state.
            if bundle is not None:
                _new_status = delivery_sync.reconcile_bundle_delivery(
                    db_path=app.state.db_path, bundle_id=bundle_id,
                    target=bundle.get("target"),
                )
                if _new_status == "merged":
                    bundle = get_bundle(conn, bundle_id)
            # WS9c: the issue this occurrence belongs to, so the page can
            # frame itself as one event under its fingerprinted problem
            # (breadcrumb up + sibling count). None on pre-issue rows.
            issue = issue_for_bundle(conn, bundle_id) if bundle is not None else None
            bundle_jobs = list_jobs_for_bundle(conn, bundle_id)
            tool_trace_ref = get_artifact_ref(conn, bundle_id, "analysis/tool_trace.jsonl")
            selected_relpath = artifact or (render.default_artifact_relpath(bundle) if bundle else None)
            selected_ref = (
                get_artifact_ref(conn, bundle_id, selected_relpath)
                if selected_relpath else None
            )
            # Step 9: prior attempts table. Other bundles for the same
            # (origin, target) so the operator can see the agent's
            # history at a glance from any bundle page.
            prior_attempts = (
                [b for b in list_port_bundles(
                    conn, origin=bundle.get("origin"),
                    target=bundle.get("target"), limit=10,
                ) if b["bundle_id"] != bundle_id]
                if bundle is not None else []
            )
            # Step 9: lifetime token usage for this port, across
            # every job (triage + each patch attempt).
            port_token_usage = (
                token_usage_for_port(
                    conn, origin=bundle.get("origin"),
                    target=bundle.get("target"),
                )
                if bundle is not None and bundle.get("origin") else None
            )
            # Step 20f / Step 11c layer-violation cleanup: the dops
            # state is now persisted to bundles.dops_state at triage
            # time by the runner (which has chroot access). The
            # tracker no longer reaches into the host filesystem to
            # compute it live. NULL on legacy rows where no triage
            # ran post-this-change — the template hides the pill in
            # that case.
            dops_state = bundle.get("dops_state") if bundle is not None else None
            # Step 11d-2: most-recent delivery attempt for the Delivery
            # card. None on bundles that haven't been delivered yet
            # (the template hides the card in that case).
            delivery_request = latest_review_request_for_bundle(
                conn, bundle_id,
            )
            # The occurrence selector: this occurrence plus its siblings,
            # each with how many jobs worked it and how far they got.
            occurrence_ids = [bundle_id] + [
                b["bundle_id"] for b in prior_attempts
            ] if bundle is not None else []
            occurrence_stats = occurrence_attempts(conn, occurrence_ids)
            # poly-0e02.6: the verify request is the ONLY record of which
            # env a verification ran in -- bundles has no env column -- and
            # of a verify that never started at all. Nothing read it back
            # until now.
            verify = (
                fix_state.verify_state(
                    bundle, latest_verify_request(conn, bundle_id),
                )
                if bundle is not None else None
            )
            verify_history = (
                verify_requests_for_bundle(conn, bundle_id)
                if bundle is not None else []
            )
            # Visibility plan: tracker-side activity rows
            # (bundle_accepted, delivery_complete) live with
            # bundle_id set but job_id=NULL. Surface them on the
            # bundle page directly so accept-and-deliver outcomes
            # are visible without dropping out to the CLI.
            bundle_activity = recent_activity_for_bundle(
                conn, bundle_id, limit=20,
            )
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"Unknown bundle: {bundle_id}")
        if selected_relpath and selected_ref is None:
            raise HTTPException(status_code=404, detail="Unknown artifact")
        selected_artifact = (
            render.artifact_view_data(app.state.artifact_root, bundle_id, selected_relpath, selected_ref)
            if selected_relpath and selected_ref else None
        )
        if selected_relpath and selected_artifact is None:
            raise HTTPException(status_code=404, detail="Artifact file missing")
        tool_trace = render.load_tool_trace(app.state.artifact_root, tool_trace_ref)
        # Operator-action surface: which buttons this page shows/enables.
        # The policy (and the authoritative endpoint gate) lives in
        # fix_state — one place, tested, instead of the former inline
        # matrix. See that module for the allowed-vs-surface split.
        acts = fix_state.bundle_actions(bundle, can_operate=can_operate())
        # Env picker for the Verify button — a live DB read, so it stays
        # here rather than in the pure policy. Populate only when Verify
        # is eligible; default-select the active env, falling back to the
        # first known env when the active one is cleared/decommissioned.
        verify_envs: list[str] = []
        verify_default_env: str | None = None
        if acts["can_verify"]:
            with _conn() as _envs_conn:
                verify_envs = [
                    str(r.get("env"))
                    for r in env_health_statuses(_envs_conn)
                    if r.get("env")
                ]
                verify_default_env = get_active_env(_envs_conn)
            if (
                verify_default_env is not None
                and verify_default_env not in verify_envs
            ):
                verify_default_env = verify_envs[0] if verify_envs else None
            elif verify_default_env is None and verify_envs:
                verify_default_env = verify_envs[0]

        operator_actions = {
            **acts,
            "verify_envs": verify_envs,
            "verify_default_env": verify_default_env,
        }
        # Fix-review chat: only offer the panel when the tracker has a
        # chat model configured (llm.chat.model) AND this bundle
        # carries a session dump to seed it. Both must hold or the panel
        # is hidden — no dead UI.
        chat_session_relpath = _pick_default_session_relpath(bundle)
        chat_enabled = (
            _chat_llm_config() is not None and chat_session_relpath is not None
        )
        return templates.TemplateResponse(
            request,
            "agentic_bundle.html",
            {
                "title": bundle_id,
                "bundle": bundle,
                "issue": issue,
                "bundle_jobs": bundle_jobs,
                "tool_trace": tool_trace,
                "selected_artifact": selected_artifact,
                "selected_artifact_relpath": selected_relpath,
                "artifact_groups": render.group_artifacts(bundle),
                "prior_attempts": prior_attempts,
                "port_token_usage": port_token_usage,
                "dops_state": dops_state,
                "operator_actions": operator_actions,
                "delivery_request": delivery_request,
                "occurrence_stats": occurrence_stats,
                "verify": verify,
                "verify_history": verify_history,
                "bundle_activity": bundle_activity,
                "chat_enabled": chat_enabled,
                "chat_session_relpath": chat_session_relpath,
            },
        )

    @app.get(
        "/agentic/bundles/{bundle_id}/artifact-fragment",
        response_class=HTMLResponse,
        name="agentic_bundle_artifact_fragment",
    )
    def agentic_bundle_artifact_fragment(
        request: RequestType,
        bundle_id: str,
        artifact: str,
    ) -> Any:
        """Render just the artifact reader's detail pane (header + body)
        for one artifact, so the page can swap it in place without a
        full reload. Returns the same `_artifact_detail.html` partial
        the full page includes — one renderer, no drift. Sessions are
        not inline-previewable; the reader links them straight to the
        structured viewer, so they never reach this endpoint.
        """
        with _conn() as conn:
            bundle = get_bundle(conn, bundle_id)
            ref = get_artifact_ref(conn, bundle_id, artifact)
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"Unknown bundle: {bundle_id}")
        if ref is None:
            raise HTTPException(status_code=404, detail="Unknown artifact")
        selected_artifact = render.artifact_view_data(
            app.state.artifact_root, bundle_id, artifact, ref,
        )
        if selected_artifact is None:
            raise HTTPException(status_code=404, detail="Artifact file missing")
        return templates.TemplateResponse(
            request,
            "_artifact_detail.html",
            {"selected_artifact": selected_artifact},
        )

    @app.get("/agentic/bundles/{bundle_id}/artifacts/{relpath:path}", response_class=HTMLResponse)
    def agentic_bundle_artifact_view(
        request: RequestType,
        bundle_id: str,
        relpath: str,
    ) -> Any:
        # Session dumps under analysis/sessions/ get the structured
        # viewer instead of the default text/octet-stream renderer.
        # Redirect rather than re-route so the canonical URL for a
        # session is /sessions/<filename>, not /artifacts/...jsonl.gz.
        if render.is_session_relpath(relpath):
            filename = Path(relpath).name
            return RedirectResponse(
                url=str(request.url_for(
                    "agentic_bundle_session_view",
                    bundle_id=bundle_id,
                    filename=filename,
                )),
                status_code=302,
            )
        with _conn() as conn:
            bundle = get_bundle(conn, bundle_id)
            ref = get_artifact_ref(conn, bundle_id, relpath)
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"Unknown bundle: {bundle_id}")
        if ref is None:
            raise HTTPException(status_code=404, detail="Unknown artifact")
        artifact = render.artifact_view_data(app.state.artifact_root, bundle_id, relpath, ref)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact file missing")
        return templates.TemplateResponse(
            request,
            "agentic_artifact.html",
            {"title": relpath, "bundle": bundle, "artifact": artifact},
        )

    @app.get(
        "/agentic/bundles/{bundle_id}/sessions/{filename}",
        response_class=HTMLResponse,
        name="agentic_bundle_session_view",
    )
    def agentic_bundle_session_view(
        request: RequestType,
        bundle_id: str,
        filename: str,
    ) -> Any:
        """Structured per-turn viewer for analysis/sessions/*.jsonl[.gz]
        — replaces the gzip-octet-stream download with a per-message
        rendering: collapsible system + user prompts (with section
        breakdown), chronological assistant-turn cards with
        reasoning_content + tool_calls + tool results, and a right-rail
        TOC. The relpath is always under analysis/sessions/ — we accept
        only the filename in the URL to keep links short."""
        forbid_anonymous("The session viewer")
        relpath = f"analysis/sessions/{filename}"
        with _conn() as conn:
            bundle = get_bundle(conn, bundle_id)
            ref = get_artifact_ref(conn, bundle_id, relpath)
            # tool_trace.jsonl is what carries per-turn token counts;
            # join the session's assistant turns to its llm_turn events.
            tool_trace_ref = get_artifact_ref(
                conn, bundle_id, "analysis/tool_trace.jsonl",
            )
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"Unknown bundle: {bundle_id}")
        if ref is None:
            raise HTTPException(status_code=404, detail="Unknown session artifact")
        session = render.session_view_data(
            app.state.artifact_root, bundle_id, relpath, ref,
            tool_trace_ref=tool_trace_ref,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session file missing")
        return templates.TemplateResponse(
            request,
            "agentic_session.html",
            {"title": filename, "bundle": bundle, "session": session},
        )

    @app.get("/agentic/jobs", response_class=HTMLResponse)
    def agentic_jobs(
        request: RequestType,
        target: str | None = None,
        state: str | None = None,
        q: str | None = None,
        page: int = Query(default=1, ge=1),
    ) -> Any:
        target_value = target or None
        state_value = state or None
        search = (q or "").strip() or None
        offset = (page - 1) * _LIST_PAGE
        with _conn() as conn:
            return templates.TemplateResponse(
                request,
                "agentic_jobs.html",
                {
                    "title": "Jobs",
                    "jobs": list_jobs(
                        conn, state=state_value, target=target_value,
                        search=search, limit=_LIST_PAGE, offset=offset,
                    ),
                    "total": count_jobs(
                        conn, state=state_value, target=target_value,
                        search=search,
                    ),
                    "page": page,
                    "per_page": _LIST_PAGE,
                    "search": search,
                    "query_for": _query_for({
                        "target": target_value, "state": state_value,
                        "q": search, "page": page if page > 1 else None,
                    }),
                    "target_options": distinct_targets(conn),
                    "selected_target": target_value,
                    "selected_state": state_value,
                },
            )

    @app.get("/agentic/jobs/{job_id}", response_class=HTMLResponse)
    def agentic_job_detail(
        request: RequestType,
        job_id: str,
        limit: int = 500,
        stage_filter: str | None = None,
    ) -> Any:
        limit = max(10, min(int(limit), 5000))
        # Normalize the filter. Step 9b — three pills: all/llm_turn/tool.
        sf = stage_filter if stage_filter in ("llm_turn", "tool") else None
        with _conn() as conn:
            job = get_job(conn, job_id)
            activity = (activity_for_job(conn, job_id, limit=limit,
                                          stage_filter=sf)
                        if job is not None else [])
            transitions = (
                job_events_for_job(conn, job_id, limit=limit)
                if job is not None else []
            )
            attempt_summary = (
                port_attempt_summary(
                    conn,
                    target=job.get("target"),
                    origin=job.get("origin"),
                    window_hours=int(settings.get("runner.attempt_window_hours")),
                    max_attempts=int(settings.get("runner.max_patch_attempts")),
                ) if job is not None else None
            )
            token_usage = (
                token_usage_for_job(conn, job_id)
                if job is not None else None
            )
            # Step 9: prior-attempts table — recent bundles for the
            # same (origin, target) so the operator can see history.
            prior_attempts = (
                list_port_bundles(
                    conn, origin=job.get("origin"),
                    target=job.get("target"), limit=10,
                )
                if job is not None and job.get("origin") else []
            )
            # Step 9: when a job ends in 'escalated', operators
            # currently have to bounce out to /agentic/manual to read
            # the handoff. Inline it: pull the most recent bundle for
            # this (origin, target) that has manual_handoff.md and
            # render it next to the activity timeline.
            handoff = None
            if job is not None and job.get("state") == "escalated" and prior_attempts:
                for cand in prior_attempts:
                    ref = get_artifact_ref(
                        conn, cand["bundle_id"], "analysis/manual_handoff.md",
                    )
                    if ref is not None:
                        handoff = render.artifact_view_data(
                            app.state.artifact_root,
                            cand["bundle_id"],
                            "analysis/manual_handoff.md",
                            ref,
                        )
                        handoff = dict(handoff or {})
                        handoff["bundle_id"] = cand["bundle_id"]
                        handoff["run_id"] = cand.get("run_id")
                        break
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        # Activity rows stay newest-first (the query default) so the
        # autorefresh JS — which prepends new rows to the top — keeps
        # the table consistent. Mixing ASC initial + prepended new
        # rows produced a chaotic sort (gperf/liblz4 2026-05-26):
        # live rows piled up at the top above an ASC-sorted body.
        # Cursor for the live-refresh polling — the client polls
        # /api/activity?job_id=X&since_id=N for new rows.
        max_id = max((a.get("id") or 0) for a in activity) if activity else 0
        # Whether the job is still doing its own work — drives the
        # live-poll indicator. Computed server-side from the single
        # canonical set (lifecycle.ACTIVE_WORK_STATES) rather than an
        # inline template literal, so it can't drift from the runner's
        # retriage guard and the dashboard count. Notably: a `triaged`
        # job is NOT active (it handed off to a spawned patch/convert
        # job and rests there), and `verifying_fix` IS active.
        from dportsv3.agent.lifecycle import (  # noqa: PLC0415
            ACTIVE_WORK_STATE_VALUES,
        )
        job_is_active = job.get("state") in ACTIVE_WORK_STATE_VALUES
        return templates.TemplateResponse(
            request,
            "agentic_job.html",
            {
                "title": job_id,
                "job": job,
                "activity": activity,
                "activity_attempts": render.group_activity_by_attempt(activity),
                "transitions": transitions,
                "attempt_summary": attempt_summary,
                "token_usage": token_usage,
                "max_activity_id": max_id,
                "limit": limit,
                "limit_options": [50, 200, 500, 2000, 5000],
                "stage_filter": sf,
                "prior_attempts": prior_attempts,
                "handoff": handoff,
                "job_is_active": job_is_active,
            },
        )

    @app.get("/agentic/runs/{run_id}", response_class=HTMLResponse)
    def agentic_run_detail(request: RequestType, run_id: str) -> Any:
        with _conn() as conn:
            run = get_run(conn, run_id)
            bundles = bundles_for_run(conn, run_id) if run is not None else []
        if run is None:
            raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
        return templates.TemplateResponse(
            request,
            "agentic_run.html",
            {"title": run_id, "run": run, "bundles": bundles},
        )

    @app.get("/agentic/runner", response_class=HTMLResponse)
    def agentic_runner(request: RequestType) -> Any:
        with _conn() as conn:
            return templates.TemplateResponse(
                request,
                "agentic_runner.html",
                {
                    "title": "Runner",
                    "runner": runner_status(conn),
                    # The row says what the runner last wrote; the heartbeat
                    # says whether to believe it. A runner killed mid-job
                    # leaves `processing` on the row forever.
                    "runner_live": runner_is_live(conn),
                },
            )

    @app.get("/agentic/activity", response_class=HTMLResponse)
    def agentic_activity(
        request: RequestType,
        target: str | None = None,
        limit: int = 200,
    ) -> Any:
        target_value = target or None
        limit = max(10, min(int(limit), 5000))
        with _conn() as conn:
            return templates.TemplateResponse(
                request,
                "agentic_activity.html",
                {
                    "title": "Activity",
                    "activity": recent_activity(conn, limit=limit, target=target_value),
                    "target_options": distinct_targets(conn),
                    "selected_target": target_value,
                    "limit": limit,
                    "limit_options": [50, 200, 500, 2000, 5000],
                },
            )

    # ------------------------------------------------------------------
    # Manual escalation queue (post-impl plan, Step 4).
    # Operators land here when a job escalates to MANUAL — read the
    # handoff artifact, type context, hit "Try again with this
    # context." POST writes the context_text + bumps context_rev; the
    # runner's existing process_user_context_updates loop picks it up
    # and re-enqueues a triage job.
    # ------------------------------------------------------------------

    @app.get("/agentic/manual", response_class=HTMLResponse)
    def agentic_manual_list(
        request: RequestType,
        open_only: bool = True,
    ) -> Any:
        with _conn() as conn:
            return templates.TemplateResponse(
                request,
                "agentic_manual_list.html",
                {
                    "title": "Manual Queue",
                    "requests": list_manual_requests(conn, open_only=open_only),
                    "open_only": open_only,
                    # open_only is the default view, not a filter anyone
                    # chose, so an empty page needs to know whether any
                    # request has ever existed to say which nothing it is.
                    "total_requests": count_manual_requests(conn),
                },
            )

    @app.get("/agentic/manual/{run_id}/{origin:path}", response_class=HTMLResponse)
    def agentic_manual_detail(
        request: RequestType,
        run_id: str,
        origin: str,
    ) -> Any:
        with _conn() as conn:
            mr = get_manual_request(conn, run_id, origin)
            handoff = None
            blocking_job = (
                active_job_for_port(
                    conn, origin=origin, target=mr.get("target"),
                )
                if mr is not None else None
            )
            if mr is not None and mr.get("bundle_id"):
                ref = get_artifact_ref(
                    conn, mr["bundle_id"], "analysis/manual_handoff.md",
                )
                if ref is not None:
                    handoff = render.artifact_view_data(
                        app.state.artifact_root,
                        mr["bundle_id"],
                        "analysis/manual_handoff.md",
                        ref,
                    )
        if mr is None:
            raise HTTPException(
                status_code=404,
                detail=f"No manual request for run={run_id} origin={origin}",
            )
        return templates.TemplateResponse(
            request,
            "agentic_manual_detail.html",
            {
                "title": f"Manual: {origin}",
                "request_row": mr,
                "handoff": handoff,
                "blocking_job": blocking_job,
            },
        )

    @app.get("/api/manual-requests")
    def api_manual_requests(open_only: bool = True) -> dict[str, Any]:
        with _conn() as conn:
            rows = list_manual_requests(conn, open_only=open_only)
        return {"requests": rows}

    @app.post(
        "/api/manual-requests/{run_id}/{origin:path}/context",
        response_model=ManualContextResponse,
    )
    def api_manual_submit_context(
        run_id: str,
        origin: str,
        payload: ManualContextRequest,
    ) -> dict[str, Any]:
        forbid_anonymous("Submitting manual context")
        text = (payload.context_text or "").strip()
        if not text:
            raise HTTPException(
                status_code=400, detail="context_text cannot be empty",
            )
        if len(text) > 8000:
            raise HTTPException(
                status_code=400,
                detail="context_text too long (max 8000 chars)",
            )
        with _conn() as conn:
            mr = get_manual_request(conn, run_id, origin)
            if mr is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No manual request for run={run_id} origin={origin}",
                )
            operator = (payload.operator or "").strip() or None
            new_rev = upsert_user_context_text(
                conn, run_id, origin, text, submitted_by=operator,
            )
        return {"ok": True, "context_rev": new_rev}

    @app.post(
        "/api/manual-requests/{run_id}/{origin:path}/discard",
        response_model=ManualDiscardResponse,
    )
    def api_manual_discard(
        run_id: str,
        origin: str,
        payload: ManualDiscardRequest | None = None,
    ) -> dict[str, Any]:
        forbid_anonymous("Discarding a manual request")
        with _conn() as conn:
            mr = get_manual_request(conn, run_id, origin)
            if mr is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"No manual request for run={run_id} origin={origin}",
                )
            reason = (payload.reason if payload else "") or ""
            discarded = discard_manual_request(conn, run_id, origin, reason)
        return {"ok": True, "discarded": discarded}

    # ------------------------------------------------------------------
    # Phase 5 step 1: dsynth-progress UI adapter. Lifts the
    # www/example/progress.{html,js,css} UI and feeds it from tracker
    # data via two JSON endpoints (summary + chunked history). No
    # change to the existing /target/{target} dashboard yet.
    # ------------------------------------------------------------------

    # JSON endpoints live under /api/progress/{target}/ to stay clear
    # of the legacy /target/{target}/{cat}/{port} catch-all. The HTML
    # page is served at the canonical /target/{target} below.

    @app.get("/api/progress/{target}/summary.json")
    def progress_summary(target: str) -> dict[str, Any]:
        with _conn() as conn:
            return target_summary(conn, target)

    @app.get("/api/progress/{target}/{chunk}_history.json")
    def progress_history(target: str, chunk: str) -> Any:
        try:
            chunk_index = int(chunk)
        except ValueError:
            raise HTTPException(status_code=404, detail="Bad chunk index")
        with _conn() as conn:
            # Returns [] past the last chunk — kfiles in summary.json
            # bounds the UI's fetch range so this is rarely hit.
            return target_history_chunk(conn, target, chunk_index)

    @app.get("/api/progress/build/{run_id}/summary.json")
    def progress_build_summary(run_id: int) -> dict[str, Any]:
        with _conn() as conn:
            summary = run_summary(conn, run_id)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"Unknown build run: {run_id}")
        return summary

    @app.get("/api/progress/build/{run_id}/{chunk}_history.json")
    def progress_build_history(run_id: int, chunk: str) -> Any:
        try:
            chunk_index = int(chunk)
        except ValueError:
            raise HTTPException(status_code=404, detail="Bad chunk index")
        with _conn() as conn:
            return run_history_chunk(conn, run_id, chunk_index)

    @app.get("/target/{target}", response_class=HTMLResponse)
    def dashboard_target(request: RequestType, target: str) -> Any:
        """The newest run on one target, live.

        The header is server-rendered from the run this resolves now;
        summary.json carries run_id so the page notices when a newer run
        starts underneath it. The <base> tag pins run.js' relative JSON
        fetches to the progress API root.
        """
        with _conn() as conn:
            run = latest_run_for_target(conn, target)
        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "title": target,
                "target": target,
                "run": run,
                "progress_base": f"/api/progress/{target}/",
            },
        )

    @app.get("/target/{target}/{cat}/{port}", response_class=HTMLResponse)
    def dashboard_port_detail(
        request: RequestType, target: str, cat: str, port: str
    ) -> Any:
        origin = f"{cat}/{port}"
        with _conn() as conn:
            rows = get_port_status(conn, target=target, origin=origin)
            if not rows:
                raise HTTPException(
                    status_code=404, detail=f"Unknown port status: {target} {origin}"
                )
            # The repair side of the same origin. list_issues orders by
            # times_seen, so the first row is the problem this port is best
            # known for. Its stored state only -- `regressed` is derived
            # from occurrences this page does not load.
            issues = list_issues(conn, target=target, origin=origin, limit=5)
            return templates.TemplateResponse(
                request,
                "port_detail.html",
                {
                    "title": f"{origin} {target}",
                    "target": target,
                    "origin": origin,
                    "status": rows[0],
                    "history": get_port_history(conn, target, origin, limit=20),
                    "issues": issues,
                },
            )

    @app.get("/builds", response_class=HTMLResponse)
    def dashboard_builds(
        request: RequestType,
        target: str | None = None,
        build_type: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> Any:
        with _conn() as conn:
            runs = list_build_runs(
                conn, target=target, build_type=build_type, limit=limit
            )
            compare_links = _resolve_compare_links(runs)
            options = build_filter_options(conn)
            return templates.TemplateResponse(
                request,
                "builds.html",
                {
                    "title": "Run history",
                    "runs": runs,
                    "compare_links": compare_links,
                    "target": target,
                    "build_type": build_type,
                    "target_options": options["targets"],
                    "build_type_options": options["build_types"],
                },
            )

    @app.get("/builds/compare", response_class=HTMLResponse)
    def dashboard_build_compare(
        request: RequestType, a: int | None = None, b: int | None = None
    ) -> Any:
        """Origin-level delta between two runs.

        a and b are optional so the section nav can reach this page with
        nothing chosen yet; it then renders the pickers and says so instead
        of rejecting the request.
        """
        with _conn() as conn:
            runs = list_build_runs(conn, limit=60)
            compare = None
            if a is not None and b is not None:
                try:
                    compare = compare_builds(conn, a, b)
                except ValueError as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
            return templates.TemplateResponse(
                request,
                "build_compare.html",
                {
                    "title": "Build Compare",
                    "compare": compare,
                    "runs": runs,
                    "run_a": a,
                    "run_b": b,
                },
            )

    @app.get("/builds/{run_id}", response_class=HTMLResponse)
    def dashboard_build_detail(request: RequestType, run_id: int) -> Any:
        """One run, live. The same view as /target/{target}, pinned to a run
        instead of following the newest. Unknown ids 404 here rather than at
        the first JSON fetch."""
        try:
            with _conn() as conn:
                build = get_build_run(conn, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "title": f"Build {run_id}",
                "target": str(build["target"]),
                "run": build,
                "progress_base": f"/api/progress/build/{run_id}/",
            },
        )

    @app.get("/diff", response_class=HTMLResponse)
    def dashboard_diff(
        request: RequestType,
        a: str | None = None,
        b: str | None = None,
    ) -> Any:
        with _conn() as conn:
            targets = get_target_summary(conn)
            diff_payload = get_diff(conn, a, b) if a and b else None
            return templates.TemplateResponse(
                request,
                "diff.html",
                {
                    "title": "Target Diff",
                    "targets": targets,
                    "target_a": a,
                    "target_b": b,
                    "diff": diff_payload,
                },
            )


def _resolve_compare_links(runs: list[dict[str, Any]]) -> dict[int, int]:
    links: dict[int, int] = {}
    by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in runs:
        key = (str(run["target"]), str(run["build_type"]))
        by_group.setdefault(key, []).append(run)
    for group_runs in by_group.values():
        for index, run in enumerate(group_runs[:-1]):
            older_run = group_runs[index + 1]
            links[int(run["id"])] = int(older_run["id"])
    return links
