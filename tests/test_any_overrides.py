"""The reviewer is told which @any ops a fix overrode on its line.

poly-7pwa.27, row 5b. An @any op that composes on this line but is wrong
for it is overridden in this line's block; every other line keeps it. That
is the right default when only one line was built, and the wrong end state
when the shared value is wrong everywhere -- lang/rust's BOOTSTRAPS_DATE
was split by hand (DeltaPorts 251d8df7756). So each override is named on
the proposed fix, where the operator can decide to split it.
"""

from __future__ import annotations

from dportsv3.agent.proposed_fix import ProposedFixCtx, render_proposed_fix
from dportsv3.agent.scope_check import any_overrides

HEAD = 'port lang/thing\ntype port\nreason "fixture"\n'
BEFORE = HEAD + (
    "target @any\n"
    'mk set BOOTSTRAPS_DATE "1.95.0"\n'
    "mk add CONFIGURE_ARGS --with-foo\n"
    "file materialize dragonfly/patch-a -> dragonfly/patch-a\n"
)


def test_a_value_override_is_named_with_both_values():
    after = BEFORE + 'target @main\nmk set BOOTSTRAPS_DATE "1.97.1"\n'
    found = any_overrides(BEFORE, after, "@main")
    assert len(found) == 1
    assert "`@main` overrides `@any`" in found[0]
    assert '"1.95.0"' in found[0] and '"1.97.1"' in found[0]


def test_taking_back_a_token_and_dropping_a_shared_patch_are_overrides():
    after = BEFORE + (
        "target @main\n"
        "mk remove CONFIGURE_ARGS --with-foo\n"
        "file remove dragonfly/patch-a\n"
    )
    found = any_overrides(BEFORE, after, "@main")
    assert len(found) == 2
    assert any("CONFIGURE_ARGS" in f for f in found)
    assert any("dragonfly/patch-a" in f for f in found)


def test_an_own_copy_of_a_shared_patch_is_an_override():
    after = BEFORE + (
        "target @main\n"
        "file materialize dragonfly/@main/patch-a -> dragonfly/patch-a\n"
    )
    (found,) = any_overrides(BEFORE, after, "@main")
    assert "dragonfly/@main/patch-a -> dragonfly/patch-a" in found


def test_ops_that_stack_on_the_any_op_are_not_overrides():
    """mk add, mk bump, mk target append accumulate: nothing is lost, so
    telling the reviewer to split it would be noise."""
    before = BEFORE + (
        "mk bump PORTREVISION\n"
        "mk target append post-patch <<'MK'\n\t@true\nMK\n"
    )
    after = before + (
        "target @main\n"
        "mk add CONFIGURE_ARGS --with-bar\n"
        "mk bump PORTREVISION\n"
        "mk target append post-patch <<'MK'\n\t@echo main\nMK\n"
    )
    assert any_overrides(before, after, "@main") == ()


def test_removing_a_token_any_did_not_add_is_not_an_override():
    after = BEFORE + "target @main\nmk remove CONFIGURE_ARGS --with-other\n"
    assert any_overrides(BEFORE, after, "@main") == ()


def test_a_new_op_with_nothing_shared_under_it_is_not_an_override():
    after = BEFORE + 'target @main\nmk set OTHER "x"\n'
    assert any_overrides(BEFORE, after, "@main") == ()


def test_an_override_that_was_already_there_is_old_news():
    before = BEFORE + 'target @main\nmk set BOOTSTRAPS_DATE "1.97.1"\n'
    assert any_overrides(before, before, "@main") == ()


def test_another_lines_block_is_not_this_lines_override():
    after = BEFORE + 'target @2026Q3\nmk set BOOTSTRAPS_DATE "1.96.0"\n'
    assert any_overrides(BEFORE, after, "@main") == ()


def test_no_overlay_before_means_nothing_to_override():
    """Row 1: the job wrote the whole overlay, all of it in @any."""
    assert any_overrides(None, BEFORE, "@main") == ()


def test_an_overlay_that_does_not_plan_reports_nothing():
    assert any_overrides(BEFORE, HEAD + "not dops\n", "@main") == ()


def test_the_proposed_fix_names_each_override():
    body = render_proposed_fix(ProposedFixCtx(
        origin="lang/thing", target="@main",
        any_overrides=["lang/thing: `@main` overrides `@any` x with y"],
    ))
    assert "## Overrides of `@any`" in body
    assert "every other build line keeps them" in body
    assert "- lang/thing: `@main` overrides `@any` x with y" in body


def test_a_fix_without_overrides_has_no_section():
    body = render_proposed_fix(ProposedFixCtx(origin="lang/thing",
                                              target="@main"))
    assert "Overrides of" not in body
