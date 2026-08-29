"""A retry needs enough budget to be worth starting (poly-5e1).

Measured on hardware 2026-08-29, devel/glib20::

    [attempt_start] attempt 2/4 (tokens used 111554/120000)
    ... 5 turns ...
    tool_loop: token budget exhausted after turn 5 (12018 >= 8446 billable)
    attempt_loop: budget exhausted after attempt 2 (123572 >= 120000)

Attempt 2 started with 8,446 billable and spent 12,018. It overran on
the turn it was always going to overrun on, and the spend bought
nothing: attempts inherit no findings, so all five turns went on
re-reading files attempt 1 had already read (both patch files,
materialize_dports, env_verify) — 71% of it model output re-reasoning
from scratch.

The old gate was ``remaining <= 0``, which only refuses an attempt with
literally nothing left. The floor is a fraction of the whole budget
rather than an absolute: the budget is per-tier, so the "too small to
bother" point scales with it.

Grounding for 25%: on the same run the cheapest path to the first real
tool call (make_extract on turn 4) had already cost 37,227 billable of
120,000 — about 31%. A retry that cannot reach that far only
re-establishes context before dying.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import attempt_loop


# --- the floor itself -------------------------------------------------------

def test_the_floor_scales_with_the_budget() -> None:
    """Not an absolute: a tier with a bigger allowance implies bigger
    attempts, so the point below which a retry is pointless moves too."""
    assert attempt_loop._min_attempt_budget(120_000) == 30_000
    assert attempt_loop._min_attempt_budget(40_000) == 10_000


def test_the_measured_case_is_refused() -> None:
    """8,446 left of 120,000 — the exact numbers from the host."""
    assert 8_446 < attempt_loop._min_attempt_budget(120_000)


def test_the_floor_is_overridable(set_setting, monkeypatch) -> None:
    set_setting("runner.min_attempt_budget_fraction", float("0.5"))
    assert attempt_loop._min_attempt_budget(120_000) == 60_000


@pytest.mark.parametrize("bad", ["", "abc", "not-a-number"])
def test_a_malformed_override_falls_back(set_setting, monkeypatch, bad) -> None:
    """A typo in an operator's env must not disable the gate or crash
    a running patch loop."""
    set_setting("runner.min_attempt_budget_fraction", bad)
    assert attempt_loop._min_attempt_budget(120_000) == 30_000


@pytest.mark.parametrize("value,expected", [("-1", 0), ("2.0", 120_000)])
def test_an_out_of_range_override_is_clamped(set_setting, 
    monkeypatch, value, expected
) -> None:
    """0 disables the gate, 1.0 allows only a full budget. Anything
    outside that is meaningless rather than an error."""
    set_setting("runner.min_attempt_budget_fraction", value)
    assert attempt_loop._min_attempt_budget(120_000) == expected


# --- how it is applied ------------------------------------------------------

class _Usage:
    def __init__(self, billable=0):
        self.billable_tokens = billable
        self.total_tokens = billable
        self.cached_tokens = 0

    def add(self, other):
        self.billable_tokens += other.billable_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens


class _Tier:
    def __init__(self, budget, iterations):
        self.max_tokens = budget
        self.max_iterations = iterations


def _drive(monkeypatch, *, budget, cost, iterations=4):
    """Run the loop with a stub tool_loop of fixed cost per attempt."""
    calls: list[int] = []

    class _Response:
        text = "attempt produced no verified fix"
        tool_calls: list = []

    def fake_run(messages, **kw):
        calls.append(1)
        return (_Response(), _Usage(cost), False)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)
    monkeypatch.setattr(attempt_loop, "Usage", _Usage)
    result = attempt_loop.run(
        "payload",
        tier=_Tier(budget, iterations),
        env="env",
        model="m",
    )
    return result, len(calls)


def test_a_retry_below_the_floor_never_starts(monkeypatch) -> None:
    """The measured shape: attempt 1 eats most of the budget, and what
    is left is under the floor. Attempt 2 must not run at all."""
    result, attempts_run = _drive(monkeypatch, budget=120_000, cost=111_554)
    assert attempts_run == 1, "attempt 2 started on a budget it could not finish in"
    assert result.status == "budget-exhausted"


def test_a_retry_above_the_floor_still_runs(monkeypatch) -> None:
    """The gate must not disable retries generally — a cheap first
    attempt leaves room for a real second one."""
    result, attempts_run = _drive(monkeypatch, budget=120_000, cost=20_000)
    assert attempts_run > 1


def test_attempt_one_runs_even_when_it_cannot_afford_the_floor(
    monkeypatch,
) -> None:
    """The first attempt is what the budget was granted for. Gating it
    would leave a small-budget tier unable to do anything at all."""
    result, attempts_run = _drive(monkeypatch, budget=1_000, cost=900,
                                  iterations=4)
    assert attempts_run == 1
    assert result.status == "budget-exhausted"


def test_the_reasoning_is_recorded_with_the_constant() -> None:
    import inspect

    src = inspect.getsource(attempt_loop)
    head = src[:src.index("MIN_ATTEMPT_BUDGET_FRACTION = ")]
    for phrase in ("37,227", "measured"):
        assert phrase in head, f"the rationale does not mention {phrase!r}"
