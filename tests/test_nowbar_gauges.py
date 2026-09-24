"""How close each budget is to its edge, on the line that names the attempt
(poly-qqx9.20).

The bar already said how MUCH: "264,110 / 400,000". What it could not say
is how CLOSE, which is the question an operator actually has, and it said
nothing at all about the context -- the other budget that ends an attempt.

Two rings, and the captions are load-bearing rather than decorative:
billable and total are both called "tokens" and differ by 21x on a real
job (poly-0g0); context occupancy is a third quantity and tracks total,
not billable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dportsv3.tracker.render.nowbar import (
    GAUGE_HOT_PCT,
    GAUGE_WARN_PCT,
    gauge,
    now_bar,
)

REPO = Path(__file__).resolve().parents[1]
CSS = (REPO / "dportsv3" / "tracker" / "static" / "progress.css").read_text()


# --- the gauge itself -----------------------------------------------------


@pytest.mark.parametrize("pct,level", [
    (0, "ok"), (74, "ok"),
    (GAUGE_WARN_PCT, "warn"), (89, "warn"),
    (GAUGE_HOT_PCT, "hot"), (100, "hot"),
])
def test_the_thresholds_are_where_they_are_said_to_be(pct, level) -> None:
    assert gauge(pct, 100)["level"] == level


def test_amber_starts_where_another_attempt_stops_being_possible() -> None:
    """75 is not a round number chosen for looks: an attempt needs a
    quarter of the budget still free to be worth starting
    (runner.min_attempt_budget_fraction), so past this point the next one
    cannot begin at all."""
    from dportsv3 import settings

    fraction = settings.get("runner.min_attempt_budget_fraction")
    assert GAUGE_WARN_PCT == round((1 - fraction) * 100)


@pytest.mark.parametrize("value,ceiling", [
    (5, None), (None, 100), (None, None), (5, 0), (5, -1), (-5, 100),
    ("x", 100), (5, "x"),
])
def test_no_denominator_means_no_gauge(value, ceiling) -> None:
    """A ring is a proportion. Without a ceiling there is nothing to be a
    proportion OF, and inventing one is the failure this guards."""
    assert gauge(value, ceiling) is None


def test_a_prompt_larger_than_the_declared_window_pins_at_full() -> None:
    """A provider can report a prompt bigger than the window we were told
    about. A ring is a proportion, not the place to discover that -- the
    figures beside it still show the real numbers."""
    g = gauge(250_000, 200_000)

    assert g["pct"] == 100 and g["level"] == "hot"
    assert g["value"] == 250_000, "the real figure survives for the caption"


# --- what the bar carries -------------------------------------------------


def _cards(prompt=None, cumulative=4_000):
    turn = {"kind": "turn", "attempt": 2, "turn": 7,
            "cumulative_billable_tokens": cumulative, "tools": []}
    if prompt is not None:
        turn["prompt_tokens"] = prompt
    return [turn]


def _extra(budget=400_000, opened=260_000):
    return {"attempt": 2, "iterations": 4, "budget": budget,
            "tokens_used_so_far": opened}


def test_the_bar_gauges_the_spend_it_already_showed(monkeypatch) -> None:
    bar = now_bar(_cards(), _extra(), {"tier": "ASSIST"})

    assert bar["spend"] == 264_000
    assert bar["spend_gauge"]["pct"] == 66
    assert bar["spend_gauge"]["level"] == "ok"


def test_context_comes_from_the_newest_turns_prompt(monkeypatch) -> None:
    """prompt_tokens counts the whole conversation resent each turn, cached
    prefix included -- occupancy, not cost."""
    monkeypatch.setattr(
        "dportsv3.tracker.render.nowbar._context_window", lambda: 200_000)
    bar = now_bar(_cards(prompt=186_300), _extra(), {"tier": "ASSIST"})

    assert bar["context"] == 186_300
    assert bar["context_gauge"]["pct"] == 93
    assert bar["context_gauge"]["level"] == "hot"


def test_an_undeclared_window_gauges_nothing_but_keeps_the_figure(
    monkeypatch,
) -> None:
    """llm.patch.context_window defaults to 0. A ceiling nobody declared is
    not one to divide by -- but the occupancy is still worth showing."""
    monkeypatch.setattr(
        "dportsv3.tracker.render.nowbar._context_window", lambda: None)
    bar = now_bar(_cards(prompt=186_300), _extra(), {"tier": "ASSIST"})

    assert bar["context"] == 186_300
    assert bar["context_gauge"] is None
    assert bar["context_window"] is None


def test_a_turn_with_no_prompt_figure_reports_no_context(monkeypatch) -> None:
    """A card synthesized from tool rows alone has no llm_turn behind it.
    Absent, not zero -- a context reported as empty mid-job is a lie."""
    monkeypatch.setattr(
        "dportsv3.tracker.render.nowbar._context_window", lambda: 200_000)
    bar = now_bar(_cards(prompt=None), _extra(), {"tier": "ASSIST"})

    assert bar["context"] is None
    assert bar["context_gauge"] is None


def test_the_newest_turn_that_has_a_prompt_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        "dportsv3.tracker.render.nowbar._context_window", lambda: 200_000)
    cards = [
        {"kind": "turn", "turn": 9, "tools": [], "partial": True},
        {"kind": "turn", "turn": 8, "prompt_tokens": 120_000, "tools": []},
    ]
    bar = now_bar(cards, _extra(), {"tier": "ASSIST"})

    assert bar["context"] == 120_000


def test_a_budget_the_tier_never_set_gauges_nothing() -> None:
    bar = now_bar(_cards(), _extra(budget=None), {"tier": "ASSIST"})

    assert bar["spend_gauge"] is None
    assert bar["spend"] is not None, "the figure still shows"


# --- the rendering --------------------------------------------------------


def test_the_percentage_is_inside_the_ring() -> None:
    """State is never colour alone in this console. A ring whose only
    signal is hue says nothing to a reader who cannot separate green from
    amber."""
    macros = (REPO / "dportsv3" / "tracker" / "templates"
              / "_macros.html").read_text()
    ring = macros[macros.index("macro gauge_ring"):]

    assert "<b>{{ g.pct }}</b>" in ring
    assert 'aria-label="{{ cap }} {{ g.pct }} percent' in ring


def test_every_ring_names_its_quantity() -> None:
    bar = (REPO / "dportsv3" / "tracker" / "templates"
           / "_now_bar.html").read_text()

    caps = re.findall(r'gauge_ring\(now\.get\(.(\w+).\),\s*"(\w+)"', bar)
    caps = [quantity for _key, quantity in caps]
    assert sorted(caps) == ["billable", "context"]


def test_the_ring_needs_no_chart_library() -> None:
    """Following .token-pie, whose own comment settled this."""
    block = CSS[CSS.index(".now-bar .gauge-ring"):]
    assert "conic-gradient" in block[:block.index("}")]


def test_a_ring_without_a_level_class_still_draws() -> None:
    """Both custom properties are defaulted on the element itself, so a
    ring that arrives unclassed is neutral and empty rather than an
    unpainted disc."""
    block = CSS[CSS.index(".now-bar .gauge-ring {"):]
    block = block[:block.index("}")]

    assert "--gauge: var(--muted);" in block
    assert "--pct: 0;" in block


def test_the_setting_is_undeclared_by_default_and_stays_so() -> None:
    """AC4 through the REAL settings read, not a monkeypatched stand-in.

    The tests above patch _context_window to choose a scenario, which
    means none of them notices a default that silently invents a ceiling.
    Mutation-checked: making the default 200,000 passes every one of them
    and fails this.
    """
    from dportsv3 import settings
    from dportsv3.tracker.render.nowbar import _context_window

    assert settings.get("llm.patch.context_window") == 0
    assert _context_window() is None


def test_an_undeclared_window_omits_the_ring_end_to_end() -> None:
    """No patching at all: default settings, a turn with a prompt figure,
    and the bar must carry the occupancy with no gauge against it."""
    bar = now_bar(_cards(prompt=186_300), _extra(), {"tier": "ASSIST"})

    assert bar["context"] == 186_300
    assert bar["context_gauge"] is None
    assert bar["spend_gauge"] is not None, "spend still gauges: it has a budget"
