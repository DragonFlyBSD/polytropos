"""PORTREVISION in @any is a permanent pin, not a safe default.

poly-7pwa.12. flow-patch.md told the agent to keep PORTREVISION bumps in
``@any``, and to compute the value from the current one -- read from the
branch this job builds. ``mk set`` inserts when the variable is absent and
replaces when it is not, so that op rewrites every other build line's
PORTREVISION to this branch's number, and goes on rewriting it on every
compose forever: every future upstream bump on those lines is reverted.

MEASURED, and the first version of this fix got it wrong: ``graphics/gdal``
carries ``mk set PORTREVISION "3"`` in ``@any`` while upstream ``2026Q3``
has PORTREVISION 2 and ``main`` has none. The clobber is live. And 5085 of
5087 overlays in the tree have no non-@any block at all, so an escape clause
for "ports with no per-target blocks" exempted essentially everything --
including gdal.
"""

from __future__ import annotations

from dportsv3.paths import AGENT_PLAYBOOKS_DIR

FLOW = AGENT_PLAYBOOKS_DIR / "flow-patch.md"
HEADING = "### Scope it to the build line you are on"


def _flat(path) -> str:
    return " ".join(path.read_text().split())


def _section() -> str:
    text = FLOW.read_text()
    assert HEADING in text, f"heading moved: {HEADING!r}"
    body = text.split(HEADING, 1)[1]
    return " ".join(body.split("\n## ", 1)[0].split())


def test_the_playbook_no_longer_says_keep_portrevision_in_any():
    text = _flat(FLOW)
    assert "Keep PORTREVISION bumps in" not in text


def test_the_scoping_section_gives_the_rule_not_just_a_pointer():
    """A reader who stops before the last section must still get it.

    The Scoping section pushes @any hard for four paragraphs; deferring the
    answer 190 lines away left the rule where the decision is not made.
    """
    text = _flat(FLOW)
    scoping = text.split("## Scoping", 1)[1].split("## ", 1)[0]
    assert "scope it to the build line you are building" in scoping
    assert "never `@any`" in scoping


def test_there_is_no_escape_clause_for_single_scope_overlays():
    """5085 of 5087 overlays have no non-@any block.

    An exemption for them is an exemption for the whole tree, and it
    covered the one port where the clobber is actually happening.
    """
    section = _section()
    assert "does not arise" not in section
    assert "only scope there is" not in section


def test_the_section_states_the_permanent_pin_argument():
    """The bounded/unbounded asymmetry is what actually decides this."""
    section = _section()
    assert "permanent pin" in section
    assert "unbounded" in section
    # And the bounded half, so the trade is honest rather than one-sided.
    assert "bounded" in section


def test_the_section_names_the_live_instance():
    section = _section()
    assert "gdal" in section
    assert "3.13.1" in section and "3.13.3" in section


def test_the_claim_about_mk_set_is_narrow_and_true():
    """`mk set` CAN error -- E_APPLY_AMBIGUOUS_MATCH on a two-assignment port.

    The true, narrower claim is that it never fails because the variable is
    ABSENT. flow-patch.md:134 already documents the ambiguity refusal, so a
    blanket "never errors" contradicted the same file.
    """
    section = _section()
    assert "never fails because the variable is absent" in section
    assert "never errors and never warns" not in section


def test_the_unbumped_cost_is_stated_as_an_unchanged_version_not_a_missing_rebuild():
    """The other lines DO rebuild -- dsynth's CRC folds mtime, size, path.

    What they ship is changed content under an unchanged version string,
    which pkg never installs. "A missing rebuild" understates it.
    """
    section = _section()
    assert "unchanged version string" in section
    assert "still" in section and "rebuild" in section


def test_the_section_handles_a_pre_existing_any_op():
    """Layering a scoped bump on top leaves the pin everywhere else."""
    section = _section()
    assert "already has an `@any`" in section
    assert "Move the existing op" in section


def test_the_worked_example_no_longer_claims_mk_set_fails_when_absent():
    """The same false premise appeared in the ordering example's aside."""
    text = _flat(FLOW)
    assert "the `@any` op fails outright under the default" not in text
