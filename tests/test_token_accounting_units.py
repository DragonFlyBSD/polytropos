"""poly-0g0: one unit for the headline, and never an unlabelled total.

Two quantities are both called "tokens":

    billable = uncached prompt + completion   <- what the budget counts
    total    = prompt + completion, where prompt re-counts the whole
               cached prefix EVERY turn      <- provider traffic

Measured across 14 patch jobs on 2026-09-03 they differ by 7x to 21x
(audio/cdparanoia: 86,141 against 1,817,637). Displays used to pick
whichever their event happened to carry, so one attempt read 19x apart
on a single screen and the re-billed figure reached the pull request,
the manual handoff and the agent's own context as if it were the cost.

These tests pin the unit at each boundary. They are about WHICH NUMBER,
not about formatting.
"""

from __future__ import annotations

import json

from dportsv3.agent.llm import Usage


#: A turn shaped like the real thing: a large prefix, nearly all cached.
CACHED = Usage(prompt_tokens=34_681, completion_tokens=200,
               total_tokens=34_881, cached_tokens=34_673)


def test_the_fixture_really_does_separate_the_two_units():
    """Guards the tests below: if billable ever equalled total here,
    every assertion in this file would pass vacuously."""
    assert CACHED.billable_tokens == 208
    assert CACHED.total_tokens == 34_881


# --- the audit, which every downstream reader inherits ----------------

def test_the_audit_carries_cached_and_billable():
    """runner writes patch_audit.json; the PR, the handoff, the proposed
    fix and the agent's context all read it. Dropping cached there is
    what stranded every one of them on the total."""
    import inspect
    from dportsv3.agent import runner
    src = inspect.getsource(runner)
    assert '"cached": result.usage.cached_tokens' in src
    assert '"billable": result.usage.billable_tokens' in src


def test_attempt_info_carries_billable():
    from dportsv3.agent.attempt_loop import AttemptInfo
    info = AttemptInfo(attempt=1, tokens=1000, rebuild_ok=False,
                       billable_tokens=100)
    assert info.billable_tokens == 100


def _run_one_attempt(monkeypatch, on_event):
    """Drive attempt_loop through a single failing attempt."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.llm import Response

    class _Tier:
        max_iterations = 1
        max_tokens = 120_000

    monkeypatch.setattr(attempt_loop.tool_loop, "run",
                        lambda messages, **kw: (Response(text="no proof"),
                                                CACHED, False))
    monkeypatch.setattr(attempt_loop.worker, "reset_attempt_caches",
                        lambda: None)
    monkeypatch.setattr(attempt_loop.worker, "reset_attempt_workspace",
                        lambda *a, **k: {})
    return attempt_loop.run("PAYLOAD", tier=_Tier(), env="e", model="m",
                            system_prompt="SYSTEM", on_event=on_event)


def test_attempt_end_reports_the_same_unit_as_attempt_start(monkeypatch):
    """attempt_start sends billable and attempt_end sent the provider
    total, so one attempt read 1,396,534 on one line and 72,041 on the
    next -- the case that started this bead."""
    events: list[dict] = []
    _run_one_attempt(monkeypatch, events.append)

    end = [e for e in events if e.get("type") == "attempt_end"]
    assert len(end) == 1
    assert end[0]["billable_tokens"] == CACHED.billable_tokens
    assert end[0]["cached_tokens"] == CACHED.cached_tokens
    # The provider figure stays, because the ratio between the two is
    # what a working prefix cache looks like.
    assert end[0]["tokens"] == CACHED.total_tokens


def test_the_attempt_record_keeps_both_numbers(monkeypatch):
    result = _run_one_attempt(monkeypatch, None)
    assert result.attempts[0].billable_tokens == CACHED.billable_tokens
    assert result.attempts[0].tokens == CACHED.total_tokens


# --- the pull request, which is published upstream --------------------

def _pr_body(**kw):
    from dportsv3.delivery.orchestrator import format_pr_body
    args = dict(origin="devel/foo", target="2026Q3", operator="op",
                model="m", attempts=2, verified_at=None)
    args.update(kw)
    return format_pr_body(**args)


def test_the_pr_says_which_token_count_it_is():
    body = _pr_body(tokens=86_141, tokens_kind="billable")
    assert "tokens=86141 billable" in body


def test_the_pr_qualifies_a_total_rather_than_passing_it_off_as_cost():
    body = _pr_body(tokens=1_852_319, tokens_kind="total")
    assert "tokens=1852319 total, incl. re-billed cache" in body


def test_an_unknown_kind_makes_no_claim():
    """Old callers pass no kind. Better a bare number than a wrong
    label."""
    body = _pr_body(tokens=999)
    assert "tokens=999" in body
    assert "billable" not in body


def test_the_delivery_path_prefers_billable_and_falls_back_honestly():
    """bundle_actions reads the audit and decides which number the PR
    gets. An audit written before this bead has only the total, and it
    must be labelled as one rather than published as the cost."""
    import inspect
    from dportsv3.tracker.routes import bundle_actions
    src = inspect.getsource(bundle_actions)
    assert 'tokens, tokens_kind = tu.get("billable"), "billable"' in src
    assert 'tokens, tokens_kind = tu.get("total"), "total"' in src


# --- the agent reading its own spend back -----------------------------

def _audit_summary(payload: dict) -> str:
    from dportsv3.agent.context import PriorAttemptsSection
    return PriorAttemptsSection()._audit_summary(json.dumps(payload))


def test_the_agent_is_told_its_billable_spend_first():
    """On a retry the model reasons about how much room is left. Handing
    it the re-billed total said it had spent ~14x what the budget
    counted."""
    out = _audit_summary({
        "status": "needs-help",
        "tokens_used": {"prompt": 1_831_125, "completion": 21_194,
                        "cached": 1_766_178, "billable": 86_141,
                        "total": 1_852_319},
    })
    assert "billable=86141" in out
    assert "incl. re-billed cache" in out


def test_an_audit_without_billable_still_summarizes():
    out = _audit_summary({
        "status": "needs-help",
        "tokens_used": {"prompt": 10, "completion": 2, "total": 12},
    })
    assert "total=12" in out
    assert "billable" not in out


# --- the tracker: the two cards must be comparable --------------------

def _seed_port(tmp_path, stage="llm_turn"):
    """One job, two cached turns, through the real schema."""
    import sqlite3
    from datetime import datetime, timezone
    from dportsv3.db.schema import init_db as init_state_db

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    init_state_db(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO jobs
           (job_id, state, type, origin, flavor, bundle_dir,
            created_ts_utc, path, last_seen_at, target)
           VALUES ('j', 'patching', 'patch', 'devel/foo', '', '', ?, '',
                   ?, '@2026Q3')""",
        (now, now),
    )
    for turn in (1, 2):
        conn.execute(
            """INSERT INTO activity_log
               (ts, job_id, stage, message, duration_ms, extra_json)
               VALUES (?, 'j', ?, '', NULL, ?)""",
            (now, stage, json.dumps({
                "attempt": 1, "turn": turn,
                "prompt_tokens": CACHED.prompt_tokens,
                "completion_tokens": CACHED.completion_tokens,
                "total_tokens": CACHED.total_tokens,
                "cached_tokens": CACHED.cached_tokens,
            })),
        )
    conn.commit()
    return conn


def test_the_port_card_reports_cost_not_traffic(tmp_path):
    """It showed 5,313,167 for a port that cost 395,837 -- prompt,
    completion and total, with no cached and no billable at all."""
    from dportsv3.tracker.agentic_queries.jobs import token_usage_for_port
    conn = _seed_port(tmp_path)
    usage = token_usage_for_port(conn, "devel/foo")
    assert usage["billable_tokens"] == 2 * CACHED.billable_tokens
    assert usage["cached_tokens"] == 2 * CACHED.cached_tokens
    assert usage["total_tokens"] == 2 * CACHED.total_tokens


def test_the_port_card_and_the_job_card_agree_on_one_job(tmp_path):
    """Same rows, same definition: the two cards summed different row
    sets and reported different fields, so the same port read 5.3M on
    one page and 86k on another."""
    from dportsv3.tracker.agentic_queries.jobs import (
        token_usage_for_job, token_usage_for_port,
    )
    conn = _seed_port(tmp_path)
    port = token_usage_for_port(conn, "devel/foo")
    job = token_usage_for_job(conn, "j")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "cached_tokens", "billable_tokens", "llm_turns"):
        assert port[key] == job[key], key


def test_a_namespaced_stage_reaches_both_cards(tmp_path):
    """token_usage_for_job matched LIKE '%llm_turn' and
    token_usage_for_port matched it exactly, so a convert:llm_turn row
    would land in one card and not the other."""
    from dportsv3.tracker.agentic_queries.jobs import (
        token_usage_for_job, token_usage_for_port,
    )
    conn = _seed_port(tmp_path, stage="convert:llm_turn")
    assert token_usage_for_port(conn, "devel/foo")["llm_turns"] == 2
    assert token_usage_for_job(conn, "j")["llm_turns"] == 2


def test_the_activity_group_header_sums_billable():
    """The header sits directly above a column labelled 'Cum
    (billable)'. Summing the total there put 1,396,534 above 72,041 for
    one attempt."""
    from dportsv3.tracker.render.activity import (
        group_activity_by_attempt as group_activity,
    )
    rows = [
        {"id": 1, "stage": "attempt_start", "extra": {"attempt": 1}},
        {"id": 2, "stage": "llm_turn",
         "extra": {"attempt": 1, "total_tokens": 34_881,
                   "billable_tokens": 208}},
        {"id": 3, "stage": "llm_turn",
         "extra": {"attempt": 1, "total_tokens": 34_881,
                   "billable_tokens": 208}},
    ]
    groups = group_activity(rows)
    assert groups[0]["tokens"] == 416


def test_a_row_without_billable_falls_back_to_its_total():
    """Old rows must not read 0 — that would understate rather than
    overstate, which is the worse failure for a cost display."""
    from dportsv3.tracker.render.activity import (
        group_activity_by_attempt as group_activity,
    )
    rows = [
        {"id": 1, "stage": "attempt_start", "extra": {"attempt": 1}},
        {"id": 2, "stage": "llm_turn",
         "extra": {"attempt": 1, "total_tokens": 500}},
    ]
    assert group_activity(rows)[0]["tokens"] == 500


# --- triage measured no caching at all --------------------------------

def test_triage_emits_the_cache_fields():
    """Without these every triage job claimed a 0% cache hit, and 'no
    cache' was indistinguishable from 'not measured'."""
    import inspect
    from dportsv3.agent import triage
    src = inspect.getsource(triage)
    assert '"cached_tokens": response.usage.cached_tokens' in src
    assert '"billable_tokens": response.usage.billable_tokens' in src
    assert ('"cumulative_billable_tokens": total_usage.billable_tokens'
            in src)


def test_the_triage_phase_result_carries_them_too():
    from dportsv3.agent.phase_result import TriageResult
    r = TriageResult(
        classification="patch-error", confidence="high", root_cause="x",
        evidence_excerpt="y", error_signature=None, tier="assist",
        classifier_version="1", tokens_prompt=20_536,
        tokens_completion=1_072, tokens_total=21_608, model="m",
        tokens_cached=19_000, tokens_billable=2_608,
    )
    assert r.tokens_billable == 2_608


def test_the_patch_phase_result_carries_them_too():
    from dportsv3.agent.phase_result import PatchResult
    r = PatchResult(
        rebuild_ok=False, status="needs-help", attempts=2,
        tokens_prompt=1_831_125, tokens_completion=21_194,
        tokens_total=1_852_319, tokens_cached=1_766_178,
        tokens_billable=86_141,
    )
    assert r.tokens_billable == 86_141
    assert r.tokens_cached == 1_766_178


def test_both_producers_write_the_cache_fields():
    """runner builds both phase results from result.usage; a producer
    that stops passing them leaves the artifact reporting 0% cached."""
    import inspect
    from dportsv3.agent import runner
    src = inspect.getsource(runner)
    assert src.count("tokens_cached=result.usage.cached_tokens") == 2
    assert src.count("tokens_billable=result.usage.billable_tokens") == 2


def test_the_phase_result_fields_are_defaulted_not_version_bumped():
    """A schema bump would make every artifact already in the store
    raise PhaseResultVersionMismatch. Additive-with-default keeps old
    files loadable, which load_phase_result is already built for."""
    from dportsv3.agent.phase_result import TriageResult
    r = TriageResult(
        classification="c", confidence="high", root_cause="x",
        evidence_excerpt="y", error_signature=None, tier="assist",
        classifier_version="1", tokens_prompt=1, tokens_completion=2,
        tokens_total=3, model="m",
    )
    assert (r.tokens_cached, r.tokens_billable) == (0, 0)
    assert r.schema_version == TriageResult.schema_version


# --- the operator-facing cost document --------------------------------

def test_proposed_fix_leads_with_billable():
    from dportsv3.agent import proposed_fix
    ctx = proposed_fix.ProposedFixCtx(
        origin="devel/foo", model="m",
        prompt_tokens=1_831_125, completion_tokens=21_194,
        total_tokens=1_852_319, billable_tokens=86_141,
        triage_prompt_tokens=20_536, triage_completion_tokens=1_072,
        triage_total_tokens=21_608, triage_billable_tokens=2_608,
    )
    md = proposed_fix.render_proposed_fix(ctx)
    assert "**Billable (real cost, triage + patch): 88,749**" in md
    assert "incl. re-billed cache" in md


def test_proposed_fix_without_billable_still_reports_the_total():
    from dportsv3.agent import proposed_fix
    ctx = proposed_fix.ProposedFixCtx(
        origin="devel/foo", model="m",
        prompt_tokens=10, completion_tokens=2, total_tokens=12,
        triage_total_tokens=8,
    )
    md = proposed_fix.render_proposed_fix(ctx)
    assert "Combined total (triage + patch): 20" in md


# --- the manual handoff -----------------------------------------------

def test_the_handoff_reports_billable():
    import dportsv3.agent.manual_handoff as mh

    class _Result:
        status = "needs-help"
        attempts = [1, 2]
        usage = CACHED

    ctx = mh.build_handoff_ctx(
        origin="devel/foo", reason=mh.REASON_PATCH_GAVE_UP,
        patch_result=_Result(),
    )
    assert ctx.tokens_used == CACHED.billable_tokens


def test_the_handoff_falls_back_to_total_for_a_usage_without_billable():
    """Reporting 0 tokens used would be worse than reporting the
    inflated figure."""
    import dportsv3.agent.manual_handoff as mh

    class _OldUsage:
        total_tokens = 999

    class _Result:
        status = "needs-help"
        attempts = [1]
        usage = _OldUsage()

    ctx = mh.build_handoff_ctx(
        origin="devel/foo", reason=mh.REASON_PATCH_GAVE_UP,
        patch_result=_Result(),
    )
    assert ctx.tokens_used == 999


# --- poly-cwi: the message strings, which are read, not just stored ----
#
# poly-0g0 fixed the structured fields and the templates that read them.
# The MESSAGE built alongside each row is what _activity_row.html renders
# verbatim for non-llm_turn rows, what lands in runner.log, and — for the
# context.py pair — what the model itself is told it has spent. Seven of
# them printed a count under a bare "tokens", in two different scopes.


def _dispatch(event: dict) -> str:
    """One event through the real dispatcher; return its logged message."""
    from pathlib import Path

    from dportsv3.agent.steps import PatchEventDispatcher

    entries: list[str] = []
    d = PatchEventDispatcher(
        queue_root=Path("/tmp/x"), job_id="j", origin="devel/foo",
        activity_log=lambda _r, _s, message, **kw: entries.append(message),
        looks_env_suspicious=lambda res: False,
        invalidate_health_cache=lambda: None,
        summarize_tool_call=lambda tool, args, res: "",
    )
    d(event)
    return entries[0]


def test_attempt_start_marks_its_figure_cumulative():
    """tokens_used_so_far is total_usage.billable_tokens — every attempt
    so far, not this one. attempt_end reports this attempt. Naming the
    unit alone would still invite comparing 198,207 against 76,776."""
    msg = _dispatch({"type": "attempt_start", "attempt": 3, "iterations": 4,
                     "tokens_used_so_far": 198_207, "budget": 300_000})
    assert "billable so far 198207/300000" in msg


def test_attempt_end_names_both_quantities():
    msg = _dispatch({"type": "attempt_end", "attempt": 3, "rebuild_ok": False,
                     "tokens": 1_540_341, "billable_tokens": 76_776})
    assert "billable=76776 total=1540341" in msg


def test_attempt_end_omits_billable_rather_than_filing_a_total_under_it():
    msg = _dispatch({"type": "attempt_end", "attempt": 1,
                     "rebuild_ok": False, "tokens": 1_540_341})
    assert "total=1540341" in msg
    assert "billable" not in msg


# --- what the agent is told it spent ----------------------------------

def _prior():
    from dportsv3.agent.context import PriorAttemptsSection
    return PriorAttemptsSection()


def test_the_agent_sees_billable_for_each_prior_attempt():
    """runner writes both into the attempts list; the summary rendered
    only `tokens`, so a retry was told attempt 1 cost 1,141,455 when it
    cost 121,768."""
    out = _prior()._audit_summary(json.dumps({
        "status": "budget-exhausted",
        "tokens_used": {"prompt": 1_116_299, "completion": 25_156,
                        "cached": 1_019_687, "billable": 121_768,
                        "total": 1_141_455},
        "attempts": [{"attempt": 1, "tokens": 1_141_455,
                      "billable_tokens": 121_768, "rebuild_ok": False}],
    }))
    assert "attempt=1 billable=121768 total=1141455" in out


def test_the_trace_summary_separates_cumulative_from_per_attempt():
    trace = "\n".join(json.dumps(e) for e in [
        {"type": "attempt_start", "attempt": 3,
         "tokens_used_so_far": 198_207, "budget": 300_000},
        {"type": "attempt_end", "attempt": 3, "rebuild_ok": False,
         "tokens": 1_540_341, "billable_tokens": 76_776},
    ])
    out = _prior()._tool_trace_summary(trace)
    assert "billable so far 198207/300000" in out
    # Anchored on the preceding field: a re-added "tokens=" prefix would
    # still contain "billable=76776" and slip through a bare substring.
    assert "compiled=False billable=76776" in out


def test_the_trace_summary_says_total_when_billable_is_absent():
    trace = json.dumps({"type": "attempt_end", "attempt": 1,
                        "rebuild_ok": False, "tokens": 1_540_341})
    out = _prior()._tool_trace_summary(trace)
    assert "compiled=False total=1540341" in out
    assert "billable" not in out


def test_the_completion_messages_name_both_quantities():
    """The line that closes out a phase. On alsa-plugins it read
    'status=budget-exhausted, attempts=3, tokens=4567261' beside a
    300,000 budget the job never came near — it stopped at 274,983
    billable."""
    from dportsv3.agent.steps import _usage_spend
    assert _usage_spend(CACHED) == "billable=208, total=34881"


def test_no_completion_message_prints_a_bare_provider_total():
    """Both completion messages route through _usage_spend. This is the
    exact pre-fix form; if either call site regrows it, the message is
    back to one word for two quantities."""
    import inspect
    from dportsv3.agent import steps
    src = inspect.getsource(steps)
    assert "tokens={result.usage.total_tokens}" not in src
