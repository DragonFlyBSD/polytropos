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
    #: Provider-reported total, which re-counts the cached prefix every
    #: turn. Kept because it is the honest wire number, but it is not
    #: the cost -- see ``billable_tokens`` (poly-0g0).
    tokens: int
    rebuild_ok: bool
    proof: dict | None = None  # parsed Rebuild Proof JSON for this attempt
    #: Uncached prompt + completion: what this attempt actually cost and
    #: what the budget gate counted. Defaults to 0 for callers that
    #: construct an AttemptInfo without it.
    billable_tokens: int = 0


@dataclass
class PatchResult:
    status: str  # "success" | "needs-help" | "budget-exhausted"
    final_text: str
    usage: Usage = field(default_factory=Usage)
    attempts: list[AttemptInfo] = field(default_factory=list)
    proof: dict | None = None  # the final/winning Rebuild Proof JSON (if any)
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


#: Tools whose call means "the previous attempt looked at this".
#: ``grep`` is deliberately not here — its ``path`` is usually a whole
#: tree, so listing it as a file read is misleading, and the pattern is
#: the half worth carrying. See ``_searches``.
_READ_TOOLS = ("get_file", "list_dir", "get_effective_overlay")
#: Tools whose call means "the previous attempt changed this".
_WRITE_TOOLS = ("put_file", "edit_file", "apply_intent", "install_patches",
                "genpatch", "make_patch")
#: Tools that prove or disprove a fix.
_PROOF_TOOLS = ("dsynth_build", "dsynth_test")

#: Cap on the carried diff. Measured diffs run 0.3-2.2KB, so this
#: rarely bites; when it does the message says so.
_MAX_CARRIED_DIFF = 4000


def _targets(tool_log: list[dict], tools: tuple[str, ...], limit: int) -> list[str]:
    """Distinct ``path``/``origin`` arguments the previous attempt passed.

    Capped at ``limit`` and, when it truncates, says so — a list the
    model reads as exhaustive when it is not would send it looking for
    the missing entries, which is the re-derivation this exists to stop.
    """
    out: list[str] = []
    for ev in tool_log:
        if ev.get("tool") not in tools:
            continue
        args = ev.get("args") or {}
        target = args.get("path") or args.get("relpath") or args.get("origin")
        if not target:
            continue
        target = str(target)
        if target not in out:
            out.append(target)
    if len(out) > limit:
        extra = len(out) - limit
        return out[:limit] + [f"… and {extra} more"]
    return out


def _searches(tool_log: list[dict], limit: int = 8) -> list[str]:
    """What the previous attempt grepped for, pattern first.

    Exact-arg repeats of ``grep`` are rare (2% of retry calls) because
    the model rewords the pattern between attempts — searching
    ``glib-2.86.4`` and then ``glib-2.86`` over the same tree. Carrying
    the patterns is what stops that; carrying the paths would not.
    """
    out: list[str] = []
    for ev in tool_log:
        if ev.get("tool") != "grep":
            continue
        args = ev.get("args") or {}
        pattern = args.get("pattern")
        if not pattern:
            continue
        entry = f"{pattern!r} under {args.get('path', '?')}"
        if entry not in out:
            out.append(entry)
    if len(out) > limit:
        return out[:limit] + [f"… and {len(out) - limit} more"]
    return out


#: ``loop_stop`` reasons, in words the model can act on. A bare
#: ``turn_cap`` tells it nothing; "ran out of tool turns" tells it to be
#: more direct this time.
_STOP_REASONS = {
    "turn_cap": "it ran out of tool turns before reaching a conclusion",
    "token_budget": "it ran out of token budget",
    "text_only": "it finished and wrote a report, but the fix was not proven",
}


def _last_proof_failure(tool_log: list[dict]) -> str:
    """The error tail of the last build the previous attempt ran, if any."""
    for ev in reversed(tool_log):
        if ev.get("tool") not in _PROOF_TOOLS:
            continue
        result = ev.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("rebuild_ok") is True or result.get("ok") is True:
            return ""
        tail = (
            result.get("error")
            or result.get("stderr_tail")
            or result.get("stdout_tail")
            or result.get("summary")
            or ""
        )
        return str(tail)[-600:].strip()
    return ""


def _current_diff(env: str | None, origin: str | None) -> str | None:
    """The overlay diff that survived into this retry.

    ``reset_attempt_workspace`` clears the WRKDIR and genpatch-out but
    never touches ``ports/<origin>/``, so a retry inherits the previous
    attempt's edits. Measured: the diff grows across attempts (0b, then
    1694b, 2116b, 2204b on one four-attempt job). The model could not
    tell, so it spent a turn on ``emit_diff`` rediscovering it —
    handing it over costs the harness a pure read and no turn at all.

    Returns ``None`` when the diff could not be read at all, which is
    not the same as an empty diff: claiming "nothing changed" on a
    failed read would contradict the tool log right above it.
    """
    if not env or not origin:
        return None
    try:
        from . import worker  # noqa: PLC0415 — avoid an import cycle at module load
        result = worker.emit_diff(env, origin, "")
    except Exception as exc:  # noqa: BLE001 — a retry must not die on its own preamble
        log.warning("attempt_loop: could not read the carried diff: %s", exc)
        return None
    if not isinstance(result, dict) or result.get("ok") is False:
        return None
    diff = str(result.get("diff") or "")
    if len(diff) > _MAX_CARRIED_DIFF:
        # Say so rather than handing over a diff that ends mid-hunk and
        # reads as complete.
        return diff[:_MAX_CARRIED_DIFF] + "\n… diff truncated, run `emit_diff` for the rest"
    return diff


def _failure_context_message(
    attempt_idx: int,
    prev_text: str,
    *,
    env: str | None = None,
    origin: str | None = None,
    tool_log: list[dict] | None = None,
    stop_reason: str | None = None,
) -> dict:
    """Build the user message that opens a retry.

    A retry is a continuation, not a restart, but nothing told the model
    that: it re-derived state the previous attempt already had, and
    ~52% of a retry's file reads repeated a read an earlier attempt had
    already made (poly-5e1). This message hands over what the harness
    knows — what was looked at, what was changed, why the build failed,
    and the diff still on disk — so the retry opens where the last one
    stopped instead of re-walking it.

    Built by the harness rather than asked of the model on purpose: 73
    of 89 measured attempts ended on a turn or token cap and never got
    a turn in which to write a handoff note.

    On inlining the diff, against poly-9u2 ("a prior attempt is a
    record, not a recipe"): that bead is about a *different bundle's*
    diff, reproduced under ``Rebuild Status: success``, which the agent
    copied. Three things separate this from that. The diff here belongs
    to this job and is still on disk; it is labelled as a hypothesis
    that failed, not as a success; and the model reaches it anyway by
    calling ``emit_diff`` on turn 2, so withholding it buys no safety
    and costs a turn. The residual anchoring risk is real, which is
    what the closing section exists to counter — keep them together.
    """
    tool_log = tool_log or []
    parts = [f"Previous attempt #{attempt_idx} did not succeed.\n"]

    if stop_reason:
        parts.append(
            f"It stopped because {_STOP_REASONS.get(stop_reason, stop_reason)}.\n"
        )

    read = _targets(tool_log, _READ_TOOLS, limit=20)
    searched = _searches(tool_log)
    changed = _targets(tool_log, _WRITE_TOOLS, limit=12)
    if read or searched or changed:
        parts.append("## What that attempt already did\n")
        if read:
            parts.append("Looked at:\n" + "".join(f"- {t}\n" for t in read))
        if searched:
            parts.append("Searched for:\n" + "".join(f"- {t}\n" for t in searched))
        if changed:
            parts.append("Changed:\n" + "".join(f"- {t}\n" for t in changed))

    failure = _last_proof_failure(tool_log)
    if failure:
        parts.append(f"Its last build failed with:\n```\n{failure}\n```\n")

    diff = _current_diff(env, origin)
    parts.append("## What survived into this attempt\n")
    parts.append(
        "The scratch was reset — the WRKDIR under /work/obj and "
        "/work/genpatch-out are gone, so re-run `make_extract` and "
        "`materialize_dports` when you need them. The overlay under "
        "`ports/<origin>/` was **not** reset.\n"
    )
    if diff:
        parts.append(f"Its current diff:\n```diff\n{diff}\n```\n")
    elif diff == "" and not changed:
        parts.append(
            "The previous attempt left no changes on disk at all, so "
            "there is nothing to build on — start from the port as it "
            "stands.\n"
        )
    else:
        # Either the diff could not be read, or it read empty while the
        # tool log shows writes. Both mean "look for yourself" — never
        # assert that nothing changed over the top of the Changed list.
        parts.append(
            "The overlay diff could not be read here — run `emit_diff` "
            "to see the current state before you change anything.\n"
        )

    snippet = prev_text[-2000:] if len(prev_text) > 2000 else prev_text
    if snippet:
        parts.append(f"## Tail of your prior response\n```\n{snippet}\n```\n")

    # The point of handing the diff over is to save the retry from
    # re-deriving it — not to commit the retry to defending it. Say so
    # explicitly, or carrying the work forward just buys more thrash:
    # one measured job ran three attempts producing a 0-byte diff
    # before the fourth wrote anything.
    subject = "That diff is" if diff else "The approach above is"
    parts.append(
        "## Before you continue\n"
        "Treat everything above as evidence, not as progress. "
        f"{subject} a hypothesis that has now failed — it is not a "
        "foundation you have to defend, and you are not obliged to "
        "build on it. "
        "Reverting it and trying something genuinely different is a "
        "legitimate move, and often the right one.\n\n"
        "Ask first: is this approach failing because of a detail I got "
        "wrong, or because it was never going to work? If the same idea "
        "has now failed the same way twice, it is the second — change "
        "the idea rather than the details.\n\n"
        "And if you conclude that no patch can fix this here — the "
        "breakage is upstream, in the environment, or in a dependency — "
        "write that in your Patch Log and stop. A clear account of why "
        "the obvious fix does not work is a genuinely better result "
        "than a third variation on it: an operator can act on the "
        "first and cannot act on the second. Stopping with a good "
        "explanation is a success, not a give-up."
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
    # What the previous attempt looked at, changed and proved. Carried
    # so the retry's opening message can hand it over instead of making
    # the model re-derive it (poly-5e1). Deliberately only the
    # immediately-previous attempt: the diff below accumulates across
    # all of them, but every attempt's tool log would grow the message
    # without bound.
    prev_tools: list[dict] = []
    prev_stop: str | None = None
    final_text = ""
    winning_proof: dict | None = None
    # Default for the needs-help return, which sits outside the attempt
    # loop: an attempt that raises before it is rebound must not turn
    # into a NameError on the way out.
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
                _failure_context_message(
                    attempt_idx - 1,
                    prev_text,
                    env=env,
                    origin=origin,
                    tool_log=prev_tools,
                    stop_reason=prev_stop,
                )
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

        # Watch the event stream for why the loop ended — a fact the
        # harness has and a turn-capped attempt never gets to report.
        seen: dict = {}
        this_attempt_tools: list[dict] = []

        def _observe(ev, _seen=seen, _tools=this_attempt_tools):
            if ev.get("type") == "loop_stop":
                _seen["stop_reason"] = ev.get("reason")
            if ev.get("type") == "tool_call":
                _tools.append(ev)
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
                # report_complete stays True: unlike a capped attempt,
                # final_text below is a complete account the harness
                # wrote itself.
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
        prev_tools = this_attempt_tools
        prev_stop = seen.get("stop_reason")

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
                billable_tokens=attempt_usage.billable_tokens,
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
                    # `tokens` is the provider total, which re-counts the
                    # cached prefix every turn. The attempt_start above
                    # reports billable, so a display that reads one field
                    # from each shows the same attempt 19x apart. Carry
                    # both, named for what they are (poly-0g0).
                    "tokens": attempt_usage.total_tokens,
                    "billable_tokens": attempt_usage.billable_tokens,
                    "cached_tokens": attempt_usage.cached_tokens,
                })
            except Exception:
                pass

        if rebuild_ok:
            log.info("attempt_loop: success on attempt %d", attempt_idx)
            winning_proof = proof
            return PatchResult(
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
                report_complete=report_complete,
                status="budget-exhausted",
                final_text=final_text,
                usage=total_usage,
                attempts=attempts,
                proof=proof,
            )

    log.info("attempt_loop: needs-help after %d attempts", iterations)
    return PatchResult(
        report_complete=report_complete,
        status="needs-help",
        final_text=final_text,
        usage=total_usage,
        attempts=attempts,
        proof=attempts[-1].proof if attempts else None,
    )
