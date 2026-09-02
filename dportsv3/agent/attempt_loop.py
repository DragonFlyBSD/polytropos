"""Budget-bounded retry loop around tool_loop for the patch flow.

One ``run(...)`` is one patch *job*: up to ``tier.max_iterations``
attempts, each itself a full multi-turn tool_loop conversation. Each
attempt starts fresh from [system, user] — we do **not** extend the
prior attempt's growing history, because tool-call traces compound
fast and the budget would melt by attempt 3 otherwise. Between
attempts we append a small failure-context message describing what
went wrong, so the LLM knows it's on a retry.

Stops when:
- the LLM emits Rebuild Proof JSON with ``rebuild_ok=true`` → success
- ``usage.total_tokens >= tier.max_tokens`` → budget-exhausted
- ``attempt == tier.max_iterations`` without success → needs-help
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import llm, prompts, tool_loop, worker
from .llm import Usage

log = logging.getLogger(__name__)


@dataclass
class AttemptInfo:
    attempt: int  # 1-indexed
    tokens: int
    rebuild_ok: bool
    proof: dict | None = None  # parsed Rebuild Proof JSON for this attempt


@dataclass
class PatchResult:
    status: str  # "success" | "needs-help" | "budget-exhausted"
    final_text: str
    usage: Usage = field(default_factory=Usage)
    attempts: list[AttemptInfo] = field(default_factory=list)
    proof: dict | None = None  # the final/winning Rebuild Proof JSON (if any)
    #: Last ``dsynth_test`` result seen, or None if the gate was never
    #: run. Recorded by the harness rather than read out of the model's
    #: report, because a turn-capped attempt never writes one (poly-qkp).
    gate_ok: bool | None = None
    #: False when the loop stopped on the turn or token cap instead of
    #: the model going text-only, i.e. ``final_text`` is the commentary
    #: that accompanied the last tool call, not a report.
    report_complete: bool = True


_PROOF_BLOCK_RE = re.compile(
    r"##\s*Rebuild Proof\s*\(JSON\)\s*\n+```(?:json)?\s*\n(.*?)\n```",
    re.IGNORECASE | re.DOTALL,
)


def _parse_rebuild_proof(text: str) -> dict | None:
    """Extract the final ``## Rebuild Proof (JSON)`` block, if present."""
    matches = _PROOF_BLOCK_RE.findall(text)
    if not matches:
        return None
    raw = matches[-1].strip()
    try:
        proof = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("attempt_loop: rebuild_proof JSON parse failed: %s", exc)
        return None
    if not isinstance(proof, dict):
        return None
    return proof


#: Smallest share of the total budget an attempt after the first may
#: start on. Below this a retry cannot reach useful work: on the
#: measured glib20 run the cheapest path to the first real tool call
#: (make_extract, turn 4) had already cost 37,227 billable of 120,000 —
#: about 31% — and a retry that cannot get that far only re-establishes
#: context before dying. Overridable for tiers whose attempts are
#: cheaper than the ones this was measured on.
MIN_ATTEMPT_BUDGET_FRACTION = 0.25


def _min_attempt_budget(budget: int) -> int:
    """Billable tokens a retry must have available to be worth starting."""
    try:
        from dportsv3 import settings  # noqa: PLC0415
        frac = float(settings.get("runner.min_attempt_budget_fraction"))
    except Exception:  # noqa: BLE001 — a bad value must not stop a retry
        frac = MIN_ATTEMPT_BUDGET_FRACTION
    frac = min(max(frac, 0.0), 1.0)
    return int(budget * frac)


def _failure_context_message(attempt_idx: int, prev_text: str) -> dict:
    """Build the user message that nudges the LLM into a retry."""
    snippet = prev_text[-2000:] if len(prev_text) > 2000 else prev_text
    parts = [
        f"Previous attempt #{attempt_idx} did not succeed.\n",
        f"Tail of your prior response:\n```\n{snippet}\n```\n",
    ]
    parts.append(
        "Inspect what went wrong, adjust your approach, and try again. "
        "If you've tried the same idea twice and it failed both times, "
        "describe the obstacle in your Patch Log and stop — don't burn "
        "the budget thrashing."
    )
    return {"role": "user", "content": "\n".join(parts)}


def run(
    payload: str,
    *,
    tier,  # dportsv3.agent.policy.Tier
    env: str,
    model: str,
    api_base: str | None = None,
    api_key: str | None = None,
    custom_llm_provider: str | None = None,
    timeout: int = 600,
    max_tool_turns: int = 12,
    on_event=None,
    origin: str | None = None,
    system_prompt: str | None = None,
    tool_whitelist: set[str] | frozenset[str] | None = None,
    proof_parser=None,
    is_success=None,
    session_dump=None,
    reasoning: str | None = None,
) -> PatchResult:
    """Run the patch flow for one bundle, returning a structured PatchResult.

    ``system_prompt`` defaults to ``prompts.PATCH_SYSTEM`` and can be
    overridden by callers that reuse the same attempt-loop / tool-loop
    infrastructure with a different system prompt.
    """
    base_messages = [
        {"role": "system", "content": system_prompt or prompts.PATCH_SYSTEM},
        {"role": "user", "content": payload},
    ]

    total_usage = Usage()
    attempts: list[AttemptInfo] = []
    prev_text = ""
    final_text = ""
    winning_proof: dict | None = None
    # Defaults for the needs-help return, which sits outside the attempt
    # loop: an attempt that raises before these are rebound must not turn
    # into a NameError on the way out.
    gate_ok: bool | None = None
    report_complete = True

    iterations = max(1, int(getattr(tier, "max_iterations", 1) or 1))
    budget = int(getattr(tier, "max_tokens", 0) or 0)

    for attempt_idx in range(1, iterations + 1):
        # Each attempt starts from a fresh message list, so anything the
        # tools elided as "you already have this" has to be forgotten —
        # otherwise a retry is refused content it never saw.
        worker.reset_attempt_caches()
        # The scratch on disk has to be forgotten too: genpatch-out and
        # the port's WRKDIR are shared, and an attempt that inherits
        # them diffs against the previous attempt's edits (poly-dq5).
        workspace = worker.reset_attempt_workspace(
            env, origin, attempt_idx=attempt_idx
        )
        if on_event is not None:
            try:
                on_event({
                    "type": "attempt_workspace_reset",
                    "attempt": attempt_idx,
                    **workspace,
                })
            except Exception:
                pass  # callback must never break the loop
        if attempt_idx == 1:
            messages = list(base_messages)
        else:
            messages = list(base_messages) + [
                _failure_context_message(attempt_idx - 1, prev_text)
            ]

        # Remaining tokens this attempt is allowed to consume.
        # Budget on billable (uncached) tokens, not total — re-sending a
        # cached prefix every turn shouldn't burn the budget for no new work.
        remaining = (budget - total_usage.billable_tokens) if budget else 0
        log.info(
            "attempt_loop: starting attempt %d/%d (billable used so far: %d / %d, remaining %d)",
            attempt_idx, iterations, total_usage.billable_tokens, budget, remaining,
        )

        if budget and remaining <= 0:
            log.warning("attempt_loop: budget already exhausted before attempt %d", attempt_idx)
            return PatchResult(
                gate_ok=gate_ok,
                report_complete=report_complete,
                status="budget-exhausted",
                final_text=final_text,
                usage=total_usage,
                attempts=attempts,
                proof=None,
            )

        # A retry needs enough budget to be worth starting. Measured on
        # devel/glib20: attempt 2 began with 8,446 billable remaining,
        # spent 12,018 over five turns re-reading files attempt 1 had
        # already read, and overran on the turn it was always going to
        # overrun on. Nothing it did could have survived.
        #
        # The floor is a fraction of the whole budget rather than an
        # absolute, because the budget is per-tier: a bigger allowance
        # implies bigger attempts, so the "too small to bother" point
        # scales with it. Attempt 1 is never gated — it is the attempt
        # the budget was granted for.
        if budget and attempt_idx > 1 and remaining < _min_attempt_budget(budget):
            log.warning(
                "attempt_loop: not starting attempt %d — %d billable left is "
                "below the %d floor; spending it would re-derive context "
                "rather than finish the work",
                attempt_idx, remaining, _min_attempt_budget(budget),
            )
            return PatchResult(
                gate_ok=gate_ok,
                report_complete=report_complete,
                status="budget-exhausted",
                final_text=final_text,
                usage=total_usage,
                attempts=attempts,
                proof=None,
            )

        if on_event is not None:
            try:
                on_event({
                    "type": "attempt_start",
                    "attempt": attempt_idx,
                    "iterations": iterations,
                    # `tokens_used_so_far` is the number the budget gate
                    # actually enforces on (billable = uncached prompt +
                    # completion). Reporting total here made the display
                    # show the re-billed cached prefix (millions) while
                    # the gate compared billable (thousands) — alarming
                    # and wrong. Carry total + cached alongside for the
                    # UI breakdown.
                    "tokens_used_so_far": total_usage.billable_tokens,
                    "total_tokens_so_far": total_usage.total_tokens,
                    "cached_tokens_so_far": total_usage.cached_tokens,
                    "budget": budget,
                })
            except Exception:
                pass

        # Watch the event stream for the two things the model cannot be
        # relied on to report: whether it ran the acceptance gate, and
        # why the loop ended. Both are facts the harness already has.
        seen: dict = {}

        def _observe(ev, _seen=seen):
            if ev.get("type") == "tool_call" and ev.get("tool") == "dsynth_test":
                res = ev.get("result")
                if isinstance(res, dict) and "rebuild_ok" in res:
                    _seen["gate_ok"] = bool(res.get("rebuild_ok"))
            elif ev.get("type") == "loop_stop":
                _seen["stop_reason"] = ev.get("reason")
            if on_event is not None:
                on_event(ev)

        try:
            response, attempt_usage, rebuild_ok_seen = tool_loop.run(
                messages,
                model=model,
                env=env,
                api_base=api_base,
                api_key=api_key,
                custom_llm_provider=custom_llm_provider,
                timeout=timeout,
                max_turns=max_tool_turns,
                max_tokens=remaining,
                on_event=_observe,
                attempt_idx=attempt_idx,
                tool_whitelist=tool_whitelist,
                reasoning=reasoning,
            )
        except tool_loop.EnvironmentBlocked as blocked:
            # A tool reported something no further agent work can clear.
            # Stop here rather than letting the model improvise around
            # it: the alternative is a full budget spent producing a
            # plausible-looking fix for a port that could not be built
            # in this environment either way.
            if blocked.usage is not None:
                total_usage.add(blocked.usage)
            if session_dump is not None:
                try:
                    session_dump(attempt_idx, messages)
                except Exception as exc:
                    log.warning(
                        "attempt_loop: session_dump failed on blocked "
                        "attempt %d: %s", attempt_idx, exc)
            log.warning(
                "attempt_loop: attempt %d ended by %s: %s",
                attempt_idx, blocked.tool, blocked.reason)
            return PatchResult(
                # Whatever the gate said before the block is still worth
                # keeping. report_complete stays True: unlike a capped
                # attempt, final_text below is a complete account the
                # harness wrote itself.
                gate_ok=seen.get("gate_ok"),
                status="environment-blocked",
                final_text=(
                    "## Environment\n\n"
                    f"Stopped: `{blocked.tool}` reported a condition this "
                    f"agent cannot clear.\n\n{blocked.reason}\n\n"
                    "No patch was attempted. This needs an operator to fix "
                    "the environment, not a different fix for the port."
                ),
                usage=total_usage,
                attempts=attempts,
                proof=None,
            )
        total_usage.add(attempt_usage)
        prev_text = response.text or ""
        final_text = prev_text

        # Optional full-session dump (gated by DP_HARNESS_DUMP_SESSION
        # at the callback's construction site). messages is the final
        # state of this attempt's conversation; the callback persists
        # it to the bundle. Best-effort: any failure inside the
        # callback is swallowed so the loop never derails.
        if session_dump is not None:
            try:
                session_dump(attempt_idx, messages)
            except Exception as exc:
                log.warning(
                    "attempt_loop: session_dump failed on attempt %d: %s",
                    attempt_idx, exc,
                )

        # Step 20: the success criterion is configurable. Patch
        # uses _parse_rebuild_proof + proof.rebuild_ok==True;
        # convert passes a Conversion-Proof parser + an existence
        # predicate. Without this, attempt_loop would always retry
        # convert attempts even after a clean proof.
        _parse = proof_parser or _parse_rebuild_proof
        _ok = is_success or (
            lambda p: bool(p and p.get("rebuild_ok") is True)
        )
        proof = _parse(prev_text)
        rebuild_ok = bool(_ok(proof))

        # Proof-block orphan rescue: the LLM may have run out of budget
        # before it could emit ``## Rebuild Proof (JSON)``, even though
        # a ``dsynth_build`` tool call already returned rebuild_ok=true
        # earlier in the attempt. Lift the success from the structured
        # tool result and synthesize a minimal proof dict so downstream
        # writes ``proposed_fix.md`` (the success artifact) instead of
        # ``manual_handoff.md`` (the escalation artifact). Gated on the
        # default success predicate — convert's custom is_success keys
        # on different fields, so the rebuild_ok signal is meaningless
        # there.
        if not rebuild_ok and rebuild_ok_seen and is_success is None:
            log.info(
                "attempt_loop: rebuild_ok=true seen via tool result but no "
                "proof block in assistant text; synthesizing proof for "
                "attempt %d",
                attempt_idx,
            )
            proof = {"rebuild_ok": True, "source": "tool_result"}
            rebuild_ok = True

        gate_ok = seen.get("gate_ok")
        # text_only is the only stop that produced a real report; a turn
        # or token cap leaves final_text as mid-attempt commentary.
        report_complete = seen.get("stop_reason") == "text_only"
        if not report_complete:
            log.warning(
                "attempt_loop: attempt %d ended on %s, not a text-only "
                "response — final_text is not a report",
                attempt_idx, seen.get("stop_reason") or "an unknown stop",
            )

        attempts.append(
            AttemptInfo(
                attempt=attempt_idx,
                tokens=attempt_usage.total_tokens,
                rebuild_ok=rebuild_ok,
                proof=proof,
            )
        )

        if on_event is not None:
            try:
                on_event({
                    "type": "attempt_end",
                    "attempt": attempt_idx,
                    "rebuild_ok": rebuild_ok,
                    "tokens": attempt_usage.total_tokens,
                })
            except Exception:
                pass

        if rebuild_ok:
            log.info("attempt_loop: success on attempt %d", attempt_idx)
            winning_proof = proof
            return PatchResult(
                gate_ok=gate_ok,
                report_complete=report_complete,
                status="success",
                final_text=final_text,
                usage=total_usage,
                attempts=attempts,
                proof=winning_proof,
            )

        if budget and total_usage.billable_tokens >= budget:
            log.warning(
                "attempt_loop: budget exhausted after attempt %d (%d >= %d billable)",
                attempt_idx, total_usage.billable_tokens, budget,
            )
            return PatchResult(
                gate_ok=gate_ok,
                report_complete=report_complete,
                status="budget-exhausted",
                final_text=final_text,
                usage=total_usage,
                attempts=attempts,
                proof=proof,
            )

    log.info("attempt_loop: needs-help after %d attempts", iterations)
    return PatchResult(
        gate_ok=gate_ok,
        report_complete=report_complete,
        status="needs-help",
        final_text=final_text,
        usage=total_usage,
        attempts=attempts,
        proof=attempts[-1].proof if attempts else None,
    )
