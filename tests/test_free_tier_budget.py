"""A free model gets no token budget — poly-w3s.

The per-attempt budget exists to bound SPEND. On a free endpoint there
is nothing to bound, and it was the only thing stopping the patch loop
from finishing: measured on the same harness, DeepSeek bills 3,836
tokens per turn against nemotron's 15,744 — 4.1x more on prompts half
the size, because NIM's cache covers a fixed early chunk while
DeepSeek's tracks the growing prefix. At ASSIST's 120k that is ~7.6
turns against ~31, and all twelve patch attempts died there mid-work.
"""

from __future__ import annotations

from dportsv3 import settings
from dportsv3.agent import steps
from dportsv3.agent.policy import Tier


ASSIST = Tier(name="ASSIST", max_iterations=4, max_tokens=120_000)


def test_the_budget_stands_by_default(monkeypatch):
    """A paid model must be unaffected — this flag is opt-in."""
    monkeypatch.setattr(steps.settings, "get", lambda p: False)
    assert steps._lift_budget_if_free(ASSIST).max_tokens == 120_000


def test_a_free_model_loses_its_budget(monkeypatch):
    """0 is how the loop already spells unbounded: both checks in
    tool_loop read `if max_tokens and ...`."""
    monkeypatch.setattr(steps.settings, "get", lambda p: True)
    assert steps._lift_budget_if_free(ASSIST).max_tokens == 0


def test_the_attempt_cap_survives(monkeypatch):
    """Only the token budget goes. Drop max_iterations too and
    "unlimited" would mean exactly that."""
    monkeypatch.setattr(steps.settings, "get", lambda p: True)
    lifted = steps._lift_budget_if_free(ASSIST)
    assert lifted.max_iterations == 4
    assert lifted.name == "ASSIST"


def test_the_original_tier_is_not_mutated(monkeypatch):
    """The tier comes from the shared policy table — mutating it would
    lift the budget for every later job in the process."""
    monkeypatch.setattr(steps.settings, "get", lambda p: True)
    steps._lift_budget_if_free(ASSIST)
    assert ASSIST.max_tokens == 120_000


def test_the_turn_cap_still_bounds_a_free_run(monkeypatch):
    """With no token budget, the turn cap is what terminates an attempt.

    Asserts the behaviour rather than a literal: patch.run resolves
    runner.max_tool_turns and passes it down, overriding attempt_loop's
    own default of 12. Pinning the number here made the test fail when
    the number changed, which is not the property that matters
    (poly-lvw)."""
    from dportsv3.agent import attempt_loop, patch

    seen = {}

    def fake_run(payload, **kw):
        seen.update(kw)
        raise SystemExit  # far enough — the argument is what is under test

    monkeypatch.setattr(attempt_loop, "run", fake_run)
    try:
        patch.run("payload", tier=ASSIST, env="e", model="m")
    except SystemExit:
        pass

    assert seen["max_tool_turns"] == settings.get("runner.max_tool_turns")
    assert seen["max_tool_turns"] > 12, (
        "must override attempt_loop's own default, or a free run stops early"
    )


def test_an_explicit_turn_cap_still_wins(monkeypatch):
    """The manual harnesses and other tests pass their own."""
    from dportsv3.agent import attempt_loop, patch

    seen = {}
    monkeypatch.setattr(attempt_loop, "run",
                        lambda p, **kw: seen.update(kw) or None)
    patch.run("payload", tier=ASSIST, env="e", model="m", max_tool_turns=7)
    assert seen["max_tool_turns"] == 7


def test_the_setting_is_declared_and_off():
    from dportsv3 import settings

    assert settings.schema().has("llm.patch.free_tier")
    assert settings.get("llm.patch.free_tier") is False
