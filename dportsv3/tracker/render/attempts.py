"""Where each attempt's wall clock went, as one segmented track per attempt.

On the llvm19 exemplar an attempt is two dsynth runs at 41 and 44 minutes
and a rounding error of model turns. That shape is the first thing a
reader needs and a reverse-chronological table cannot show it at all
(poly-qqx9.6).

Aggregated from SQL rather than from the rendered stream: the job page
fetches a bounded row window, and an attempt's own start row -- the only
place its left edge is recorded -- sits outside it on any long job.

WHAT IS MEASURED AND WHAT IS INFERRED. A tool row carries duration_ms, so
tool time is measured. An llm_turn row does not: the dispatcher never
passes one. So the model's time is the REMAINDER, wall clock minus the
tools, and it carries the loop's own overhead with it. The template says
so rather than labelling the remainder "model turns" as though it had
been timed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: Tools whose time is the point of the chart -- a dsynth run is 40+
#: minutes where everything else is milliseconds.
_LONG_TOOLS = {"dsynth_build": "build", "dsynth_test": "test"}

#: Segment order within a track. Roughly the order an attempt spends its
#: time in, and stable across attempts so two rows can be compared.
_KIND_ORDER = ("llm", "run", "tool", "build", "test", "fail")

#: Below this share of the track, a segment carries no label. It is not
#: wide enough to hold one, and the overflow does not ellipsise -- it cuts,
#: so "55.0s" became "55" sitting beside "1m52s" and read as a quantity in
#: the same unit (poly-qqx9.18). The title attribute keeps the full value
#: on every segment whatever its width, so nothing is lost by staying
#: quiet. Six mono characters at this font need roughly 50px, and the
#: track is ~1100px on the job page.
_LABEL_MIN_PCT = 5.0


def _parse(ts: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def _kind(tool: str, ok: Any) -> str:
    """What a tool's time counts as. ok is None == no verdict, not failure."""
    if ok is False:
        return "fail"
    return _LONG_TOOLS.get(tool, "tool")


def attempt_strip(
    boundaries: list[dict[str, Any]],
    tool_totals: list[dict[str, Any]],
    turn_totals: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """One row per attempt, each a list of proportional segments.

    Returns ``{"attempts": [...], "scale_ms": int}``. Every row is scaled
    against the same ``scale_ms`` -- the longest attempt -- so the rows
    are comparable to each other, which is the whole reason they are
    stacked. An empty ``attempts`` means this job type has no attempts
    (triage, verify and confirm write no boundaries at all) and the
    caller renders nothing rather than an empty chart.
    """
    opened: dict[Any, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for b in boundaries:
        attempt = b.get("attempt")
        if b.get("stage") == "attempt_start":
            row = {
                "attempt": attempt, "start_ts": b.get("ts"), "end_ts": None,
                "rebuild_ok": None, "running": True,
            }
            opened[attempt] = row
            rows.append(row)
        elif attempt in opened:
            row = opened.pop(attempt)
            row["end_ts"] = b.get("ts")
            row["rebuild_ok"] = b.get("rebuild_ok")
            row["running"] = False

    if not rows:
        return {"attempts": [], "scale_ms": 0}

    now = now or datetime.now(timezone.utc)
    tools_by_attempt: dict[Any, list[dict[str, Any]]] = {}
    for t in tool_totals:
        tools_by_attempt.setdefault(t.get("attempt"), []).append(t)
    turns_by_attempt = {t.get("attempt"): t for t in turn_totals}

    for row in rows:
        row.update(_fill(row, tools_by_attempt, turns_by_attempt, now))

    scale = max((r["elapsed_ms"] for r in rows), default=0)
    for row in rows:
        for seg in row["segments"]:
            seg["pct"] = (seg["ms"] / scale * 100) if scale else 0.0
            seg["label"] = seg["pct"] >= _LABEL_MIN_PCT
    return {"attempts": rows, "scale_ms": scale}


def _fill(
    row: dict[str, Any],
    tools_by_attempt: dict[Any, list[dict[str, Any]]],
    turns_by_attempt: dict[Any, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """One attempt's elapsed time, split into segments by kind."""
    start, end = _parse(row.get("start_ts")), _parse(row.get("end_ts"))
    if start is None:
        elapsed_ms = 0
    else:
        finish = end if end is not None else now
        elapsed_ms = max(0, int((finish - start).total_seconds() * 1000))

    buckets: dict[str, dict[str, Any]] = {}
    for t in tools_by_attempt.get(row["attempt"], []):
        kind = _kind(t.get("tool") or "", t.get("ok"))
        b = buckets.setdefault(kind, {"kind": kind, "ms": 0, "n": 0,
                                      "tools": []})
        b["ms"] += int(t.get("ms") or 0)
        b["n"] += int(t.get("n") or 0)
        if t.get("tool") and t["tool"] not in b["tools"]:
            b["tools"].append(t["tool"])

    turns = turns_by_attempt.get(row["attempt"]) or {}
    measured = sum(b["ms"] for b in buckets.values())
    # An attempt cannot have spent longer in its tools than it lasted, but
    # the two numbers come from different places -- durations are measured
    # per call, the span is two timestamps -- and a retried or resumed
    # attempt can make the sum exceed the span. Take the larger as the
    # attempt's length so the segments always fit their track; a bar that
    # runs off its own axis is a chart that lies about what it is showing.
    total_ms = max(elapsed_ms, measured)
    # Everything the tools did not account for. On a finished attempt that
    # is the model thinking plus the loop's own overhead. On a running one
    # it also holds the tool running right now, whose duration is not
    # written until it returns -- so it reads as in-progress, not as
    # thinking.
    remainder = total_ms - measured
    if remainder:
        kind = "run" if row.get("running") else "llm"
        buckets[kind] = {
            "kind": kind, "ms": remainder,
            "n": int(turns.get("n") or 0),
            "billable": int(turns.get("billable") or 0),
            "tools": [],
        }

    segments = [buckets[k] for k in _KIND_ORDER if k in buckets]
    if row.get("running"):
        state, outcome = "run", "running"
    elif row.get("rebuild_ok") is True:
        state, outcome = "ok", "rebuild passed"
    elif row.get("rebuild_ok") is False:
        state, outcome = "bad", "rebuild failed"
    else:
        state, outcome = "info", "ended"
    return {
        "elapsed_ms": total_ms,
        "span_ms": elapsed_ms,
        "segments": segments,
        "state": state,
        "outcome": outcome,
        "n_turns": int(turns.get("n") or 0),
        "billable": int(turns.get("billable") or 0),
    }
