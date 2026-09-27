"""Apply order is decided by scope, never by file position (poly-7pwa.11).

For a build on target T the engine runs every ``@any`` op first, then
every T op, then the rest. ``mk``/``text`` ops are last-wins, so an
``@any`` op written BELOW a ``@main`` op still loses on ``@main`` -- and
wins on build lines that have no block of their own writing the same
subject. An env has one target, so only one composed tree is ever built.

These tests exist because the thing that reports "what will compose do"
-- ``get_effective_overlay`` -- used to answer in file order, which names
the wrong winner in exactly the multi-target case it was built for.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dportsv3.agent import worker
from dportsv3.paths import AGENT_PLAYBOOKS_DIR
from dportsv3.engine.api import build_plan
from dportsv3.engine.models import order_ops_for_target

#: An @any op DECLARED BELOW a @main op, both writing one variable.
#: Reading the file, "from-any" is last and looks like the winner.
CROSS_SCOPE = """port x/y
type port
reason "ordering fixture"
target @main
mk set FOO "from-main"
target @any
mk set FOO "from-any"
"""


def _ops(text: str):
    result = build_plan(text, None)
    assert result.ok and result.plan is not None, result.diagnostics
    return result.plan.ops


def _values(ops):
    return [(op.target, op.payload.get("value")) for op in ops]


def test_declaration_order_is_not_apply_order():
    ops = _ops(CROSS_SCOPE)
    # The file says @main first, @any second.
    assert _values(ops) == [("@main", "from-main"), ("@any", "from-any")]
    # The engine runs @any first, so the LAST op on @main is the @main one.
    assert _values(order_ops_for_target(ops, "@main")) == [
        ("@any", "from-any"),
        ("@main", "from-main"),
    ]


def _winner(ops, target):
    """The value that lands: last-wins among the ops that actually run.

    Mismatched scopes are ordered LAST rather than dropped, so that a
    caller can report them as skipped -- which means the final element of
    the ordered list is not necessarily the winner. Cut them first.
    """
    effective = [
        op for op in order_ops_for_target(ops, target)
        if op.target in ("@any", target)
    ]
    return effective[-1].payload.get("value")


def test_the_same_file_resolves_differently_per_target():
    """The defect in one assertion: one edit, two winners."""
    ops = _ops(CROSS_SCOPE)
    assert _winner(ops, "@main") == "from-main"
    assert _winner(ops, "@2026Q3") == "from-any"


def test_a_mismatched_op_is_ordered_last_not_dropped():
    """It has to stay visible so a caller can say it was skipped."""
    ops = _ops(CROSS_SCOPE)
    assert [op.target for op in order_ops_for_target(ops, "@2026Q3")] == [
        "@any",
        "@main",
    ]


def test_mismatched_scopes_sort_last_so_a_caller_can_cut_them():
    ops = _ops(
        "port x/y\ntype port\nreason \"r\"\n"
        "target @2026Q1\nmk set A \"q1\"\n"
        "target @any\nmk set B \"any\"\n"
        "target @main\nmk set C \"main\"\n"
    )
    ordered = order_ops_for_target(ops, "@main")
    assert [op.target for op in ordered] == ["@any", "@main", "@2026Q1"]


# --------------------------------------------------------------------------
# get_effective_overlay: the agent-facing report
# --------------------------------------------------------------------------


@pytest.fixture
def env_with_overlay(tmp_path, monkeypatch):
    """A fake env whose DeltaPorts tree holds one overlay."""
    deltaports = tmp_path / "DeltaPorts"
    port = deltaports / "ports" / "x" / "y"
    port.mkdir(parents=True)
    (port / "overlay.dops").write_text(CROSS_SCOPE)
    monkeypatch.setattr(
        worker, "env_paths", lambda env: SimpleNamespace(deltaports=deltaports)
    )
    worker.set_env_target("fixture-env", "@main")
    yield "fixture-env"
    worker.set_env_target("fixture-env", None)


def test_effective_ops_come_back_in_apply_order(env_with_overlay):
    result = worker.get_effective_overlay(env_with_overlay, "x/y")
    assert result["ok"] is True, result
    got = [(op["scope"], op["value"]) for op in result["effective_ops"]]
    # NOT the file's order -- the engine's.
    assert got == [("@any", "from-any"), ("@main", "from-main")]


def test_effective_ops_carry_their_apply_index(env_with_overlay):
    result = worker.get_effective_overlay(env_with_overlay, "x/y")
    assert [op["apply_index"] for op in result["effective_ops"]] == [0, 1]


def test_the_last_op_touching_a_variable_is_that_variable_s_winner(env_with_overlay):
    """Per variable -- NOT "the last op in the list wins".

    Every op in this fixture writes FOO, so the last one is the winner.
    With ops on different variables the final element decides nothing
    about the others; see the two-variable test below.
    """
    result = worker.get_effective_overlay(env_with_overlay, "x/y")
    assert result["effective_ops"][-1]["value"] == "from-main"


def test_a_quarterly_target_sees_the_other_block_as_filtered(env_with_overlay):
    worker.set_env_target(env_with_overlay, "@2026Q3")
    result = worker.get_effective_overlay(env_with_overlay, "x/y")
    assert [op["scope"] for op in result["effective_ops"]] == ["@any"]
    assert [op["scope"] for op in result["filtered_out"]] == ["@main"]
    # Same overlay, and now the @any op is the winner.
    assert result["effective_ops"][-1]["value"] == "from-any"


# --------------------------------------------------------------------------
# The guidance the agent reads has to match the engine
# --------------------------------------------------------------------------


def _unwrapped(path: Path) -> str:
    """File text with line wrapping collapsed.

    The first version of these tests asserted on byte sequences that
    contained a hard newline and two spaces of indent. Reflowing the
    paragraph made the bug reintroducible with all assertions still
    passing -- the exact "passed because a string match moved" failure
    this repo has been bitten by before. Match claims, not layout.
    """
    return " ".join(path.read_text().split())


def test_the_playbook_no_longer_claims_plain_declaration_order():
    text = _unwrapped(AGENT_PLAYBOOKS_DIR / "flow-patch.md")
    assert "Order is by scope, not by position" in text
    # Reflow-proof: the claim, however it is wrapped.
    assert "in declaration order, each tagged with `scope`" not in text
    assert "ops play in declaration order, last-wins" not in text


def test_the_playbook_states_the_rule_and_that_only_one_target_is_built():
    text = (AGENT_PLAYBOOKS_DIR / "flow-patch.md").read_text()
    section = text.split("## Order is by scope, not by position", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "every `@any` op first" in section
    assert "apply_index" in section
    # The reason it matters, not just the rule.
    assert "one target" in section


def test_a_target_of_any_does_not_duplicate_the_universal_ops():
    """Defensive: both buckets match when target is itself "@any".

    A build target is never ``@any``, and apply gates on
    ``is_compose_target``. ``get_effective_overlay`` does not -- it takes
    whatever the runner cached -- so the helper must not hand back every
    universal op twice with a bogus apply_index.
    """
    ops = _ops(CROSS_SCOPE)
    ordered = order_ops_for_target(ops, "@any")
    assert [op.target for op in ordered] == ["@any", "@main"]


def test_the_last_effective_op_is_not_the_winner_for_every_variable():
    """Guards against over-reading the report.

    ``effective_ops[-1]`` is the last op the engine RUNS, which decides a
    variable only when every effective op writes that variable. Two
    variables, and the final element says nothing about the first.
    """
    ops = _ops(
        "port x/y\ntype port\nreason \"r\"\n"
        "target @any\nmk set FOO \"foo-any\"\n"
        "target @main\nmk set BAR \"bar-main\"\n"
    )
    ordered = [
        op for op in order_ops_for_target(ops, "@main")
        if op.target in ("@any", "@main")
    ]
    assert ordered[-1].payload.get("name") == "BAR"
    # FOO's winner is the FIRST op, not the last.
    foo = [op for op in ordered if op.payload.get("name") == "FOO"]
    assert foo[-1].payload.get("value") == "foo-any"


def test_the_tool_description_states_apply_order_not_declaration_order():
    """The description is in the model's context every turn.

    The playbook and the docstring were corrected first while this still
    said "declaration order" -- and for the convert flow, which has no
    playbook of its own, this is the only place the rule appears at all.
    """
    from dportsv3.agent import tools

    spec = next(
        t for t in tools._TOOLS
        if (t.get("name") or t.get("function", {}).get("name")) == "get_effective_overlay"
    )
    text = str(spec)
    assert "declaration order" not in text
    assert "apply_index" in text
    # And it must not advertise a field the worker renames away.
    assert "kind, target," not in text


def test_the_quickref_states_the_ordering_rule():
    # The same constant worker.dops_reference serves to the agent.
    text = " ".join(worker._DOPS_QUICKREF_PATH.read_text().split())
    assert "Scope decides execution order, not file position" in text
    assert "apply_index" in text
