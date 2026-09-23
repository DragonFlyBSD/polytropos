"""What the agent is doing right now, as one line pinned to the page.

The header said ``patching`` and stopped. The only "attempt" on the job
page was the summary card's *Recent attempts 0/3 failures in last 2h*,
which is port_attempt_summary over ``runner.attempt_window_hours`` -- the
PORT's retry budget across jobs, not this job's position in its own run.
Two different things with near-identical names, and the one an operator
wants could only be found by scrolling the stream for the last
attempt_start row (poly-qqx9.3).

THE DENOMINATORS ARE STORED, NOT SETTINGS. attempt_start carries
``iterations`` and ``budget`` alongside the attempt number, and the
dispatcher persists the whole event. Reading
``runner.max_patch_attempts`` at render time would misreport every job
that ran under a different setting.

ABSENT IS NOT ZERO. No tool running, no attempt structure, no budget
recorded: each renders as nothing rather than as a 0, which is
poly-0e02's rule from the farm-telemetry correction and applies here
unchanged.
"""

from __future__ import annotations

from typing import Any

#: The tier that never runs the agent. A MANUAL job has no loop, so it
#: gets no bar rather than a bar full of zeroes.
_NO_AGENT_TIER = "MANUAL"


def now_bar(
    cards: list[dict[str, Any]],
    attempt_extra: dict[str, Any] | None = None,
    decision_extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One bar's worth of state, or None when there is nothing to pin.

    ``cards`` is the newest-first stream from
    ``group_activity_into_cards``; ``attempt_extra`` and
    ``decision_extra`` are the job's newest attempt_start and decision
    rows, queried rather than read out of the windowed stream because a
    long attempt's start row sits outside it.

    Returns ``{attempt, attempts_total, turn, tool, spend, budget,
    tier}``, any of which may be None -- the template renders a missing
    value as absent.
    """
    attempt_extra = attempt_extra or {}
    decision_extra = decision_extra or {}

    tier = decision_extra.get("tier")
    if tier == _NO_AGENT_TIER:
        return None

    newest_turn = next((c for c in cards if c.get("kind") == "turn"), None)
    if newest_turn is None:
        return None            # verify, confirm: no turns, nothing to pin

    return {
        # The attempt number comes off the turn itself (every llm_turn and
        # tool row carries it); only the denominator needs the start row.
        "attempt": newest_turn.get("attempt") or attempt_extra.get("attempt"),
        "attempts_total": attempt_extra.get("iterations"),
        "turn": newest_turn.get("turn"),
        "tool": _running_tool(cards),
        "spend": _spend(cards, attempt_extra),
        "budget": attempt_extra.get("budget") or None,
        "tier": tier,
    }


def _running_tool(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The tool dispatched with no completion row yet, if there is one.

    Only the newest turn can hold one: the loop dispatches serially and
    an older turn's call is finished by definition. Looking further back
    would report a tool the runner abandoned when it died as running
    forever.
    """
    newest_turn = next((c for c in cards if c.get("kind") == "turn"), None)
    for tool in (newest_turn or {}).get("tools") or []:
        if tool.get("running"):
            return {
                "name": tool.get("name"),
                "args": tool.get("args") or {},
                "since": tool.get("ts"),
            }
    return None


def _spend(
    cards: list[dict[str, Any]], attempt_extra: dict[str, Any],
) -> int | None:
    """Billable tokens spent by the JOB, to compare against the budget.

    TWO SCOPES, AND THEY ARE NOT THE SAME ONE. ``budget`` on attempt_start
    is ``tier.max_tokens``, which attempt_loop enforces across the whole
    job: its gate is ``budget - total_usage.billable_tokens`` and
    total_usage accumulates over every attempt (attempt_loop:613). But a
    turn's ``cumulative_billable_tokens`` comes from tool_loop's own
    accumulator, created fresh per call -- so it counts THIS ATTEMPT
    only. Showing one against the other reports attempt 3's spend against
    the job's budget, which on a third attempt understates by roughly
    two attempts' worth.

    So the job's spend is what the attempt opened with
    (``tokens_used_so_far``, the billable figure the gate itself uses)
    plus what this attempt has spent since.

    Billable throughout, never the provider total: the total re-bills the
    cached prefix every turn and reads up to 21x high (poly-9t9). A turn
    written before the field existed has no cumulative billable figure
    and no honest substitute, so the bar falls back to the attempt's
    opening number, or shows nothing at all.
    """
    opened_with = attempt_extra.get("tokens_used_so_far")
    this_attempt = None
    for card in cards:
        if card.get("kind") != "turn":
            continue
        cum = card.get("cumulative_billable_tokens")
        if cum is not None:
            this_attempt = int(cum)
        break
    if opened_with is None and this_attempt is None:
        return None
    return int(opened_with or 0) + int(this_attempt or 0)
