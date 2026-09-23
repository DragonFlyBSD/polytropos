"""The attempt strip: where each attempt's wall clock went (poly-qqx9.6).

Two rules do most of the work. Tool time is MEASURED (duration_ms on the
row); model time is the REMAINDER, because no llm_turn row carries a
duration. And a bar may never run off its own axis, so an attempt's
length is whichever of its two clocks is longer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dportsv3.tracker.render import attempt_strip

T0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


def _ts(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _bounds(*specs):
    out = []
    for attempt, start, end, ok in specs:
        out.append({"stage": "attempt_start", "ts": _ts(start),
                    "attempt": attempt, "rebuild_ok": None})
        if end is not None:
            out.append({"stage": "attempt_end", "ts": _ts(end),
                        "attempt": attempt, "rebuild_ok": ok})
    return out


def test_an_attempt_splits_into_measured_tools_and_the_remainder():
    strip = attempt_strip(
        _bounds((1, 0, 60, False)),
        [{"attempt": 1, "tool": "dsynth_build", "ok": True, "n": 1,
          "ms": 41 * 60_000},
         {"attempt": 1, "tool": "grep", "ok": True, "n": 4, "ms": 2_000}],
        [{"attempt": 1, "n": 9, "billable": 99_364}],
    )
    row = strip["attempts"][0]
    kinds = {s["kind"]: s for s in row["segments"]}
    assert kinds["build"]["ms"] == 41 * 60_000
    assert kinds["tool"]["ms"] == 2_000
    # The remainder: an hour minus the tools. Never labelled as measured.
    assert kinds["llm"]["ms"] == 60 * 60_000 - 41 * 60_000 - 2_000
    assert kinds["llm"]["n"] == 9
    assert kinds["llm"]["billable"] == 99_364


def test_segments_always_fit_their_track():
    """Durations are measured per call; the span is two timestamps. When
    the first exceeds the second the bar must not run off its axis."""
    strip = attempt_strip(
        _bounds((1, 0, 10, False)),
        [{"attempt": 1, "tool": "dsynth_test", "ok": True, "n": 3,
          "ms": 124 * 60_000}],
        [],
    )
    row = strip["attempts"][0]
    assert row["elapsed_ms"] == 124 * 60_000
    assert row["span_ms"] == 10 * 60_000
    assert sum(s["ms"] for s in row["segments"]) == row["elapsed_ms"]
    assert sum(s["pct"] for s in row["segments"]) == 100.0


def test_every_row_is_scaled_against_the_longest_attempt():
    """Stacked rows exist to be compared; per-row scaling would make a
    6-minute attempt look like a 2-hour one."""
    strip = attempt_strip(
        _bounds((1, 0, 120, False), (2, 120, 180, True)),
        [], [],
    )
    a1, a2 = strip["attempts"]
    assert strip["scale_ms"] == 120 * 60_000
    assert a1["segments"][0]["pct"] == 100.0
    assert a2["segments"][0]["pct"] == 50.0


def test_a_failed_tool_is_its_own_kind_and_none_is_not_a_failure():
    strip = attempt_strip(
        _bounds((1, 0, 60, False)),
        [{"attempt": 1, "tool": "dsynth_build", "ok": False, "n": 1,
          "ms": 60_000},
         {"attempt": 1, "tool": "odd", "ok": None, "n": 1, "ms": 500}],
        [],
    )
    kinds = {s["kind"] for s in strip["attempts"][0]["segments"]}
    assert "fail" in kinds
    assert "tool" in kinds          # ok=None counted as an ordinary tool
    assert "build" not in kinds     # the failed dsynth reads as failed


def test_a_running_attempt_measures_to_now_and_reads_as_in_progress():
    now = T0 + timedelta(minutes=30)
    strip = attempt_strip(_bounds((3, 0, None, None)), [], [], now=now)
    row = strip["attempts"][0]
    assert row["running"] is True
    assert row["state"] == "run"
    assert row["outcome"] == "running"
    assert row["elapsed_ms"] == 30 * 60_000
    # The tool running right now has no duration until it returns, so the
    # unaccounted time is in progress -- not the model thinking.
    assert [s["kind"] for s in row["segments"]] == ["run"]


def test_outcomes_come_off_rebuild_ok():
    strip = attempt_strip(
        _bounds((1, 0, 10, True), (2, 10, 20, False), (3, 20, 30, None)),
        [], [],
    )
    assert [a["outcome"] for a in strip["attempts"]] == [
        "rebuild passed", "rebuild failed", "ended"]
    assert [a["state"] for a in strip["attempts"]] == ["ok", "bad", "info"]


def test_a_job_with_no_attempts_gets_no_strip():
    """triage, verify and confirm write no boundaries at all: absent, not
    an empty chart."""
    assert attempt_strip([], [], []) == {"attempts": [], "scale_ms": 0}


def test_an_unparseable_timestamp_does_not_crash_the_chart():
    strip = attempt_strip(
        [{"stage": "attempt_start", "ts": "nonsense", "attempt": 1},
         {"stage": "attempt_end", "ts": "nonsense", "attempt": 1,
          "rebuild_ok": True}],
        [{"attempt": 1, "tool": "grep", "ok": True, "n": 1, "ms": 900}],
        [],
    )
    row = strip["attempts"][0]
    assert row["elapsed_ms"] == 900       # the measured tools, and no more
