"""Triage is an initial investigation, not an instruction.

Measured 2026-09-01/02: three consecutive runs on the same port produced
byte-identical fixes, because triage emitted the change as literal
``REINPLACE_CMD`` lines and the patch agent transcribed them. The patch
agent was behaving exactly as told — "Apply it first", "Don't burn turns
re-investigating" — and the only escape hatches were "already tried" and
"doesn't work", neither of which fires for a suggestion that builds green
and is still wrong.

These pin the contract, by substance rather than by exact wording, so a
revert is caught without making every copy edit a test failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GEN = Path(__file__).resolve().parents[1]
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))


def _flat(text: str) -> str:
    """Collapse whitespace: these are wrapped prose, and a re-wrap must
    not break an assertion about what the prompt says."""
    import re
    return re.sub(r"\s+", " ", text)


def _patch_prompt() -> str:
    from dportsv3.agent import prompts
    return _flat(prompts.PATCH_SYSTEM)


def _triage_prompt() -> str:
    from dportsv3.agent import prompts
    return _flat(prompts.TRIAGE_SYSTEM)


def test_triage_is_told_not_to_write_the_change():
    t = _triage_prompt()
    assert "Do not write the change itself" in t
    assert "REINPLACE_CMD" in t, "the ban must name the form it keeps producing"


def test_triage_is_told_the_recipes_are_not_its_output():
    """Every playbook is flows:[triage, patch], so triage sees fix recipes.

    They are there for pattern recognition; transcribing them is what
    turned a guess into an unverified change.
    """
    t = _triage_prompt()
    assert "do not transcribe the recipe" in t.lower()


def test_triage_suggested_fix_heading_survives():
    """The runner's parsers key off the exact heading."""
    assert "## Suggested Fix" in _triage_prompt()


def test_patch_agent_is_told_triage_can_be_wrong():
    p = _patch_prompt()
    low = p.lower()
    assert "initial investigation" in low
    assert "not an instruction" in low
    assert "can be wrong" in low


def test_patch_agent_is_not_told_to_skip_verifying_triage():
    """The old wording foreclosed checking: 'Don't burn turns
    re-investigating what's already in the Triage Summary.'"""
    p = _patch_prompt()
    assert "re-investigating what's already in the Triage Summary" not in p


def test_the_automation_context_has_an_exit_for_a_wrong_but_green_fix():
    """'already tried' and 'doesn't work' both miss the case that builds."""
    from dportsv3.agent.context import AutomationContextSection
    import inspect
    src = _flat(inspect.getsource(AutomationContextSection))
    assert "wrong change" in src
    assert "builds green can still be wrong" in src
