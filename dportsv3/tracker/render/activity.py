"""Fold a job's activity_log rows into the cards the job-detail page shows.

A patch job's activity is a flat firehose — every llm_turn, tool_start,
tool result and decision in one long list, spanning multiple retry
attempts. The page used to render that firehose twice: a flat table while
the job ran, and an accordion of attempt groups once it was done. Neither
answered "what is the agent doing" without reading, because a turn's
sentence, its tool calls and their verdicts were separate rows.

So the unit is the TURN, not the row. One card per model turn, carrying
the model's own sentence and its tool calls nested inside as rows with
name, args, elapsed and outcome. Attempt boundaries are cards too, so
attempt structure survives without an accordion (poly-qqx9.4).

Pure and unit-tested; the template does the formatting, this decides the
structure and the verdicts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# A tool row's stage is "tool:<name>" — the completion row does not carry
# the name in extra, only tool_start does.
_TOOL_PREFIX = "tool:"


def _extra(row: dict[str, Any]) -> dict[str, Any]:
    e = row.get("extra")
    return e if isinstance(e, dict) else {}


def _billable(extra: dict[str, Any]) -> int:
    """This turn's billable (uncached) tokens.

    Never the provider total: that re-bills cache reads, and summing it
    made the same attempt read 19x apart on one screen (poly-0g0). Rows
    written before the field existed fall back to their total so old
    jobs don't read 0.
    """
    billable = extra.get("billable_tokens")
    if billable is None:
        billable = extra.get("total_tokens") or 0
    return int(billable or 0)


def _tool_state(ok: Any, running: bool) -> str:
    """ok is True / False / None — and None means NO VERDICT, not failure.

    steps.py:238 writes None whenever the tool returned something that
    isn't a dict, which is not the tool saying it failed. Colouring that
    red invents a failure the job never had.
    """
    if running:
        return "run"
    if ok is False:
        return "bad"
    if ok is True:
        return "ok"
    return "info"


def _turn_state(tools: list[dict[str, Any]], text_only: bool) -> str:
    """The card's verdict, carried by one glyph: the worst thing in it."""
    states = {t["state"] for t in tools}
    if "bad" in states:
        return "bad"
    if "run" in states:
        return "run"
    if "ok" in states:
        return "ok"
    if states:
        return "info"          # tools ran, none of them reported a verdict
    return "ok" if text_only else "info"


def _elapsed_seconds(start_ts: str | None, end_ts: str | None) -> int | None:
    """Wall clock between two activity_log timestamps, or None."""
    if not start_ts or not end_ts:
        return None
    try:
        t0 = datetime.fromisoformat(str(start_ts))
        t1 = datetime.fromisoformat(str(end_ts))
    except ValueError:
        return None
    return max(0, int((t1 - t0).total_seconds()))


def group_activity_into_cards(
    activity: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fold activity rows into ordered cards, newest first.

    ``activity`` may arrive in any order (the detail route passes
    newest-first); grouping is chronological by ``id`` and the result is
    reversed at the end, so the newest card is first — the running turn
    is what the operator came to see.

    Three kinds, all full-width entries in one stream:

    - ``turn``     — an ``llm_turn`` row with its tool calls nested in
      ``tools``. ``state`` is the verdict glyph: ok / bad / run / info.
    - ``boundary`` — ``attempt_start`` / ``attempt_end``. The end card
      carries what the old accordion header carried: outcome, billable
      tokens, tool count, turn count and the attempt's wall clock.
    - ``stage``    — everything else (decision, observations_masked,
      api_error, and every verify/confirm operational stage) as a
      one-line entry. verify and confirm jobs have no turns at all, so
      this is the whole stream for them: a checklist rather than an
      empty "Turns" heading.

    Rows are joined to their turn by ``(extra.attempt, extra.turn)`` —
    both event shapes carry them from the same loop variables, and
    tool_loop emits the llm_turn before it dispatches. A tool row whose
    turn was cut off by the row limit (or filtered away) synthesizes a
    turn card so its calls still group.
    """
    rows = sorted(activity, key=lambda a: (a.get("id") or 0))
    cards: list[dict[str, Any]] = []
    turns: dict[tuple[Any, Any], dict[str, Any]] = {}
    open_turn: dict[str, Any] | None = None
    # Per-attempt accumulators, so the attempt_end card can say what the
    # attempt cost without a second pass.
    attempts: dict[Any, dict[str, Any]] = {}

    def _acc(attempt: Any) -> dict[str, Any]:
        return attempts.setdefault(
            attempt,
            {"tokens": 0, "n_tools": 0, "n_turns": 0, "start_ts": None,
             "saw_start": False},
        )

    def _new_turn(attempt: Any, turn: Any, row: dict[str, Any] | None) -> dict[str, Any]:
        extra = _extra(row or {})
        card = {
            "kind": "turn",
            "key": f"t-{attempt}-{turn}",
            "state": "info",
            "attempt": attempt,
            "turn": turn,
            "ts": (row or {}).get("ts"),
            "id": (row or {}).get("id") or 0,
            "text": str(extra.get("text") or ""),
            "text_only": bool(extra.get("text_only")),
            "total_tokens": int(extra.get("total_tokens") or 0),
            "billable_tokens": _billable(extra) if row is not None else 0,
            "cumulative_billable_tokens": extra.get("cumulative_billable_tokens"),
            "tools_requested": list(extra.get("tools_requested") or []),
            # A card synthesized from tool rows alone has no sentence and
            # no cost — say so rather than printing zeros as if measured.
            "partial": row is None,
            "tools": [],
        }
        # A turn with no tool calls never reaches the tool branches below,
        # so settle its verdict here: a text-only turn is the model's final
        # answer, which is a pass, not an unknown.
        card["state"] = _turn_state([], card["text_only"])
        cards.append(card)
        turns[(attempt, turn)] = card
        return card

    def _turn_for(extra: dict[str, Any]) -> dict[str, Any] | None:
        """The card a tool row belongs to, synthesizing one if need be."""
        attempt, turn = extra.get("attempt"), extra.get("turn")
        if turn is None:
            return open_turn
        card = turns.get((attempt, turn))
        if card is not None:
            return card
        return _new_turn(attempt, turn, None)

    for a in rows:
        stage = a.get("stage") or ""
        extra = _extra(a)

        if stage == "attempt_start":
            attempt = extra.get("attempt")
            acc = _acc(attempt)
            acc["start_ts"] = a.get("ts")
            acc["saw_start"] = True
            cards.append({
                "kind": "boundary", "edge": "start", "state": "bound",
                "key": f"as-{a.get('id') or 0}",
                "attempt": attempt, "ts": a.get("ts"),
                "id": a.get("id") or 0,
                "iterations": extra.get("iterations"),
                "budget": extra.get("budget"),
                "tokens_used_so_far": extra.get("tokens_used_so_far"),
                "message": a.get("message") or "",
            })
            open_turn = None
            continue

        if stage == "attempt_end":
            attempt = extra.get("attempt")
            acc = _acc(attempt)
            ok = extra.get("rebuild_ok")
            cards.append({
                "kind": "boundary", "edge": "end",
                "state": "ok" if ok is True else ("bad" if ok is False else "bound"),
                "key": f"ae-{a.get('id') or 0}",
                "attempt": attempt, "ts": a.get("ts"),
                "id": a.get("id") or 0,
                "rebuild_ok": ok,
                "tokens": acc["tokens"], "n_tools": acc["n_tools"],
                "n_turns": acc["n_turns"],
                "elapsed_s": _elapsed_seconds(acc["start_ts"], a.get("ts")),
                # These are summed over the rows IN HAND. When the
                # attempt's own start row fell outside the fetched
                # window they count part of an attempt, so the card must
                # not print them as the attempt's total -- it read "66
                # turns" on an attempt of 75. poly-qqx9.9 replaces them
                # with figures queried over the whole attempt.
                "partial": not acc["saw_start"],
                "message": a.get("message") or "",
            })
            open_turn = None
            continue

        if stage.endswith("llm_turn"):
            open_turn = _new_turn(extra.get("attempt"), extra.get("turn"), a)
            acc = _acc(extra.get("attempt"))
            acc["tokens"] += _billable(extra)
            acc["n_turns"] += 1
            continue

        if stage == "tool_start":
            card = _turn_for(extra)
            if card is None:
                cards.append(_stage_card(a, stage))
                continue
            card["tools"].append({
                "state": "run", "running": True,
                "name": extra.get("tool") or "?",
                "args": extra.get("args") or {},
                "call_id": extra.get("call_id"),
                "ok": None, "duration_ms": None, "summary": None,
                "id": a.get("id") or 0, "ts": a.get("ts"),
            })
            card["state"] = _turn_state(card["tools"], card["text_only"])
            continue

        if stage.startswith(_TOOL_PREFIX):
            card = _turn_for(extra)
            if card is None:
                cards.append(_stage_card(a, stage))
                continue
            name = stage[len(_TOOL_PREFIX):]
            entry = _pending_tool(card, extra.get("call_id"), name)
            if entry is None:
                entry = {
                    "state": "run", "running": True, "name": name,
                    "args": {}, "call_id": extra.get("call_id"),
                    "ok": None, "duration_ms": None, "summary": None,
                    "id": a.get("id") or 0, "ts": a.get("ts"),
                }
                card["tools"].append(entry)
            ok = extra.get("ok")
            entry.update({
                "running": False, "ok": ok,
                "state": _tool_state(ok, running=False),
                "duration_ms": a.get("duration_ms"),
                "summary": a.get("message") or "",
                "args": extra.get("args") or entry["args"],
                "error": extra.get("error"),
                "stderr_tail": extra.get("stderr_tail"),
                "stdout_tail": extra.get("stdout_tail"),
                "rc": extra.get("rc"),
                "id": a.get("id") or entry["id"],
            })
            card["state"] = _turn_state(card["tools"], card["text_only"])
            _acc(extra.get("attempt") if extra.get("attempt") is not None
                 else card.get("attempt"))["n_tools"] += 1
            continue

        cards.append(_stage_card(a, stage))

    cards.reverse()
    return cards


def _pending_tool(
    card: dict[str, Any], call_id: Any, name: str,
) -> dict[str, Any] | None:
    """The tool_start this completion closes.

    call_id is the pairing (poly-qqx9.2). Rows written before it fall
    back to the first still-running entry of the same name, which is
    correct as long as the loop dispatches serially — it does.
    """
    if call_id:
        for t in card["tools"]:
            if t.get("call_id") == call_id:
                return t
    for t in card["tools"]:
        if t.get("running") and t.get("name") == name:
            return t
    return None


#: Keys a stage card renders somewhere else, or that mean nothing to a
#: reader: the diagnostics have their own disclosure, and the rest is
#: plumbing.
_STAGE_FIELD_SKIP = frozenset({
    "error", "diag_tail", "stderr_tail", "stdout_tail", "rc", "ok",
    "call_id", "args", "tool", "text", "type",
})
_STAGE_FIELD_CAP = 8


def _stage_fields(extra: dict[str, Any]) -> dict[str, Any]:
    """The scalar extras worth showing beside a stage row's message.

    A decision row's action / tier / classification / confidence was
    visible in the flat table and would otherwise be lost with it: those
    four are most of why anyone reads a decision row at all. Generalized
    rather than special-cased, so observations_masked and the workspace
    reset carry their numbers too.
    """
    out: dict[str, Any] = {}
    for key, value in (extra or {}).items():
        if key in _STAGE_FIELD_SKIP or value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, str) and len(value) > 80:
            continue
        out[key] = value
        if len(out) >= _STAGE_FIELD_CAP:
            break
    return out


def _stage_card(row: dict[str, Any], stage: str) -> dict[str, Any]:
    """One operational row as a compact entry in the same stream."""
    extra = _extra(row)
    failed = (
        stage.endswith("_failed") or stage in ("api_error", "error")
        or extra.get("ok") is False
    )
    return {
        "kind": "stage",
        "key": f"s-{row.get('id') or 0}",
        "state": "bad" if failed else "info",
        "stage": stage,
        "ts": row.get("ts"),
        "id": row.get("id") or 0,
        "duration_ms": row.get("duration_ms"),
        "message": row.get("message") or "",
        "fields": _stage_fields(extra),
        "error": extra.get("error"),
        "diag_tail": extra.get("diag_tail"),
        "stderr_tail": extra.get("stderr_tail"),
        "stdout_tail": extra.get("stdout_tail"),
        "rc": extra.get("rc"),
    }


#: How many turns the job-detail page shows before sending the reader to
#: the transcript. Five is what fits above the working-tree band without
#: scrolling; at 442 events in one attempt (the poly-up2f exemplar) a
#: full stream is the mess the cards replaced.
TURN_WINDOW = 5


def count_turns(cards: list[dict[str, Any]]) -> int:
    """How many turn cards the stream holds, windowed or not."""
    return sum(1 for c in cards if c.get("kind") == "turn")


def window_cards(
    cards: list[dict[str, Any]], turns: int = TURN_WINDOW,
) -> list[dict[str, Any]]:
    """The newest ``turns`` turn cards, with what sits between them.

    ``cards`` is newest-first, so this is a prefix: walk until the turn
    count is reached and stop. Boundary and stage cards inside that span
    come along — they are what makes the span readable — and the ones
    below it do not.

    THE COST THIS EXISTS FOR. A poly-up2f-shaped job (4 attempts x 75
    turns, 908 activity rows) rendered in 33.07 ms and 535,651 bytes
    when the page fetched 500 rows and rendered every one of them.
    Windowed to five turns, with the flat table moved to the transcript:
    4.65 ms and 50,235 bytes. 7x the speed, 10x less HTML, on the page an
    operator leaves open while a job runs. The transcript itself costs
    41.40 ms for the same job, which is the point -- it is paid by
    someone who asked for it.

    Measured with 20 renders after 3 warm-ups, TestClient on seeded
    sqlite -- the shape shell_facts uses, for the same reason: a cost
    paid on every view is worth writing down.

    One exception to the prefix: an operator note that has not been
    delivered yet pins to the top whatever the window holds (poly-
    qqx9.11). A note that scrolls out of its own window before the agent
    has read it is the failure that surface exists to prevent. Once
    delivered it takes its place in sequence and falls out like any
    other card.
    """
    def _queued_note(card: dict[str, Any]) -> bool:
        return card.get("kind") == "note" and not card.get("delivered")

    pinned = [c for c in cards if _queued_note(c)]
    if turns <= 0:
        return pinned
    out: list[dict[str, Any]] = []
    seen = 0
    for card in cards:
        if seen >= turns:
            break
        if _queued_note(card):
            continue            # already pinned above; don't show it twice
        out.append(card)
        if card.get("kind") == "turn":
            seen += 1
    return pinned + out


#: Tools whose output is worth tailing while they run. Everything else
#: returns in milliseconds and has nothing to say in the meantime.
TAILABLE_TOOLS = frozenset({"dsynth_build", "dsynth_test"})


def running_tailable_tool(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The long-running tool whose log is worth showing, if one is going.

    Only the newest turn can hold a running call -- the loop dispatches
    serially -- and only dsynth takes long enough for a tail to mean
    anything.
    """
    newest = next((c for c in cards if c.get("kind") == "turn"), None)
    for tool in (newest or {}).get("tools") or []:
        if tool.get("running") and tool.get("name") in TAILABLE_TOOLS:
            return tool
    return None


def attach_tool_tail(
    cards: list[dict[str, Any]], tail: dict[str, Any] | None,
) -> None:
    """Hang a build's last lines on the tool row that is producing them.

    In the row, not in a pane: a tail only exists while one tool runs,
    and when the tool finishes the row collapses to its duration and rc
    (poly-qqx9.8). Mutates the card so the template still depends on
    nothing but ``cards``.
    """
    tool = running_tailable_tool(cards)
    if tool is not None and tail:
        tool["tail"] = tail
