"""PORTREVISION is bumped relative to the line's upstream, never pinned.

poly-7pwa.12 found that an absolute ``mk set PORTREVISION`` pins every
build line it applies to: compose re-seeds the port from upstream and
applies the op again, so each later upstream bump on that line is
reverted. In 2026-09 ``graphics/gdal`` carried ``mk set PORTREVISION "3"``
in ``@any`` while upstream ``2026Q3`` had 2 and ``main`` had none. Scoping
the op to one build line, the first fix, only moved the pin.

poly-7pwa.18. ``mk bump PORTREVISION`` adds to whatever the line's
upstream Makefile says, on every compose, so the playbook teaches that and
nothing else.
"""

from __future__ import annotations

from dportsv3.paths import AGENT_PLAYBOOKS_DIR

FLOW = AGENT_PLAYBOOKS_DIR / "flow-patch.md"
HEADING = "## Bumping PORTREVISION"


def _flat(path) -> str:
    return " ".join(path.read_text().split())


def _raw_section() -> str:
    text = FLOW.read_text()
    assert HEADING in text, f"heading moved: {HEADING!r}"
    return HEADING + text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _section() -> str:
    return " ".join(_raw_section().split())


def test_the_playbook_no_longer_says_keep_portrevision_in_any():
    text = _flat(FLOW)
    assert "Keep PORTREVISION bumps in" not in text


def test_the_section_teaches_the_relative_bump():
    section = _section()
    assert "mk bump PORTREVISION" in section
    assert "Do not write mk set PORTREVISION" in section


def test_the_section_scopes_the_bump_with_its_change():
    section = _section()
    assert "in the same target block as the change it accounts for" in section


def test_an_existing_op_is_left_to_an_operator():
    """Replacing an existing op can lower a revision already shipped."""
    section = _section()
    assert "leave that op alone" in section


def test_scoping_no_longer_carries_a_portrevision_rule():
    """The old rule, scope it to one line and never @any, now contradicts."""
    text = FLOW.read_text()
    scoping = text.split("## Scoping", 1)[1].split("\n## ", 1)[0]
    assert "PORTREVISION" not in scoping


def test_the_section_stays_small():
    """It rides in every patch prompt, so it is the rule and no more."""
    assert len(_raw_section().encode("utf-8")) <= 720


def test_the_worked_example_no_longer_claims_mk_set_fails_when_absent():
    """The same false premise appeared in the ordering example's aside."""
    text = _flat(FLOW)
    assert "the `@any` op fails outright under the default" not in text
