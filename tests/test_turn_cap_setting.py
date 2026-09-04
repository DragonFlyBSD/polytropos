"""poly-lvw: the turn cap is a setting, and it is what ends attempts.

Measured on comms/hamlib, the same port run either side of an unrelated
change that altered the agent's behaviour and the run's outcome:

    OLD:  A1 30  A2 30  A3 30  A4 30  -> patch_gave_up
    NEW:  A1 30  A2 30  A3 30  A4 27  -> agent_fixed

Seven of eight attempts consumed exactly the cap; the eighth used 27
because it finished. A model with turns remaining uses them, so 30 was
not a margin rarely reached — it decided when nearly every attempt
ended, while ~150k of a 600k budget went unspent.

These tests pin that the cap is declared, reachable, and actually
threaded — and that the token budget still bounds spend, so raising it
did not unbound the loop.
"""
from __future__ import annotations

import pytest

from dportsv3 import settings
from dportsv3.agent import attempt_loop, patch
from dportsv3.agent.policy import Tier


ASSIST = Tier(name="ASSIST", max_iterations=4, max_tokens=600_000)


def _capture(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(attempt_loop, "run",
                        lambda p, **kw: seen.update(kw) or None)
    return seen


# --- declared, not hidden ----------------------------------------------------


def test_the_cap_is_a_declared_setting():
    """It was a bare default on patch.py, invisible to `config show`,
    unchangeable without a deploy — while every other bound in this loop
    was already a setting."""
    assert settings.schema().has("runner.max_tool_turns")


def test_the_default_lets_an_attempt_reach_its_budget():
    """~2.7k billable per turn measured, and ASSIST grants ~150k per
    attempt (600k / 4 iterations). A cap that cannot spend that is what
    left 150k unspent on a run that hit the cap three times."""
    assert settings.get("runner.max_tool_turns") >= 50


# --- actually threaded -------------------------------------------------------


def test_patch_run_resolves_the_setting(monkeypatch):
    seen = _capture(monkeypatch)
    patch.run("payload", tier=ASSIST, env="e", model="m")
    assert seen["max_tool_turns"] == settings.get("runner.max_tool_turns")


def test_it_overrides_attempt_loops_own_default(monkeypatch):
    """attempt_loop.run defaults to 12 for direct callers. If patch.run
    ever stopped passing a value, every attempt would silently lose more
    than half its turns."""
    seen = _capture(monkeypatch)
    patch.run("payload", tier=ASSIST, env="e", model="m")
    assert seen["max_tool_turns"] > 12


def test_an_explicit_argument_still_wins(monkeypatch):
    seen = _capture(monkeypatch)
    patch.run("payload", tier=ASSIST, env="e", model="m", max_tool_turns=7)
    assert seen["max_tool_turns"] == 7


def test_a_changed_setting_is_picked_up_without_reimport(monkeypatch, set_setting):
    """Resolved per call, not frozen as a default argument at import."""
    seen = _capture(monkeypatch)
    set_setting("runner.max_tool_turns", 99)
    patch.run("payload", tier=ASSIST, env="e", model="m")
    assert seen["max_tool_turns"] == 99


# --- raising it did not unbound the loop -------------------------------------


def test_the_token_budget_still_stops_an_attempt_first(monkeypatch):
    """The point of the change is that the loop should stop on SPEND,
    not below it. A budget smaller than one attempt must still win over
    a large turn cap."""
    calls: list[int] = []

    class _Usage:
        def __init__(self, billable=0):
            self.billable_tokens = billable
            self.total_tokens = billable
            self.cached_tokens = 0

        def add(self, other):
            self.billable_tokens += other.billable_tokens
            self.total_tokens += other.total_tokens
            self.cached_tokens += other.cached_tokens

    class _Response:
        text = "no fix"
        tool_calls: list = []

    def fake_tool_loop(messages, **kw):
        calls.append(kw["max_turns"])
        return (_Response(), _Usage(200_000), False)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_tool_loop)
    monkeypatch.setattr(attempt_loop, "Usage", _Usage)
    result = attempt_loop.run(
        "payload",
        tier=Tier(name="ASSIST", max_iterations=4, max_tokens=150_000),
        env="e", model="m", max_tool_turns=500,
    )
    assert result.status == "budget-exhausted"
    assert len(calls) == 1, "the budget must stop it, not the turn cap"


@pytest.mark.parametrize("name", ["max_iterations", "max_tokens"])
def test_the_other_two_bounds_are_untouched(name):
    """Three bounds hold this loop. This change moves one of them."""
    tiers = settings.get("policy.tiers")
    assert name in tiers["ASSIST"]
