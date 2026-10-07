"""A shared op that fails on this line goes to an operator, not the agent.

poly-7pwa.27, row 6. An op another build line also reads -- under
``target @any``, or a ``target`` list naming it -- that fails to compose on
the job's line cannot be fixed by an op of this line's (``@any`` runs
first, and the compose has already failed), and changing it changes a line
nothing here builds.

That failure is there before the agent starts: the patch preflight composes
the port and used to refuse the job as ``patch_gave_up``, which counts
toward the retry cap and ends, three failures later, in a handoff that
names neither the op nor the choice. The live shape is the six overlays
split by hand on 2026-10-05. The job now escalates at once, naming the op.

poly-7pwa.30, the other half: an op only this line reads that fails is the
agent's to fix (row 3), so the job starts on it. Anything that is not a
failing op still refuses.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dportsv3.agent import manual_handoff, steps

HEAD = 'port devel/x\ntype port\nreason "fixture"\n'


def _row(op_id, target, code="E_APPLY_MISSING_SUBJECT"):
    return {"id": op_id, "kind": "text.replace_once", "target": target,
            "status": "failed",
            "diagnostics": [{"code": code, "message": "pattern not found"}]}


def _worker(tmp_path, overlay, *rows, report_ok=True, origin="devel/x",
            stage_errors=(), port_extra=None):
    """A worker double: one failing origin, its overlay, its report.

    Every other origin composes clean. No relation probe: the preflight
    hands over the origin set it resolved before composing.
    """
    port = tmp_path / "DeltaPorts" / "ports" / origin
    port.mkdir(parents=True, exist_ok=True)
    (port / "overlay.dops").write_text(overlay)

    def report_for(env, o):
        if not report_ok:
            return {"ok": False, "report": None}
        failed = list(rows) if o == origin else []
        errors = list(stage_errors) if o == origin else []
        return {"ok": not (failed or errors), "report": {
            "stages": [{"name": "apply_semantic_ops", "errors": errors}],
            "ports": [{"origin": o, "dops_failed_op_results": failed,
                       **(port_extra or {})}]}}

    return SimpleNamespace(
        materialize_dports_with_report=report_for,
        env_paths=lambda env: SimpleNamespace(
            deltaports=tmp_path / "DeltaPorts"),
    )


ANY = HEAD + 'target @any\ntext replace-once file Makefile from "a" to "b"\n'
LIST = HEAD + ('target @2026Q3,@main\n'
               'text replace-once file Makefile from "a" to "b"\n')
OWN = HEAD + ('target @main\n'
              'text replace-once file Makefile from "a" to "b"\n')


def _ids(text):
    from dportsv3.engine.api import build_plan

    return {op.target: op.id for op in build_plan(text, None).plan.ops}


def _sort(w, origins=("devel/x",)):
    return steps._preflight_failures(w, "env", list(origins))


def test_a_failing_any_op_is_shared(tmp_path):
    w = _worker(tmp_path, ANY, _row(_ids(ANY)["@any"], "@any"))
    failures = _sort(w)
    (found,) = failures.shared
    assert "target @any" in found
    assert "E_APPLY_MISSING_SUBJECT" in found
    assert failures.own == failures.other == ()


def test_a_target_list_naming_another_line_is_shared(tmp_path):
    """One op per line, so the failing row is this line's -- but its twin
    runs on @2026Q3, and changing it changes that line too."""
    w = _worker(tmp_path, LIST, _row(_ids(LIST)["@main"], "@main"))
    assert len(_sort(w).shared) == 1


def test_a_slaves_master_overlay_is_checked(tmp_path):
    """A slave's DragonFly ops live in its master's overlay."""
    w = _worker(tmp_path, ANY, _row(_ids(ANY)["@any"], "@any"),
                origin="devel/x-master")
    (found,) = _sort(w, ["devel/x", "devel/x-master"]).shared
    assert found.startswith("devel/x-master:")


def test_an_op_only_this_line_reads_is_the_agents(tmp_path):
    w = _worker(tmp_path, OWN, _row(_ids(OWN)["@main"], "@main"))
    failures = _sort(w)
    (found,) = failures.own
    assert "target @main" in found and "E_APPLY_MISSING_SUBJECT" in found
    assert failures.shared == failures.other == ()


def test_the_same_op_in_another_lines_own_block_is_this_lines(tmp_path):
    """After `migrate branch-line` (poly-7pwa.25) @2026Q4 holds a copy of
    @main's op: same text, but changing @main's leaves @2026Q4's alone."""
    text = OWN + 'target @2026Q4\ntext replace-once file Makefile from "a" to "b"\n'
    w = _worker(tmp_path, text, _row(_ids(text)["@main"], "@main"))
    failures = _sort(w)
    assert failures.shared == () and len(failures.own) == 1


@pytest.mark.parametrize("make", [
    lambda tmp: _worker(tmp, ANY, _row("op-x", "@any"), report_ok=False),
    lambda tmp: _worker(tmp, ANY, stage_errors=["E_COMPOSE_APPLY_FAILED: x"]),
    lambda tmp: _worker(tmp, OWN, _row(_ids(OWN)["@main"], "@main"),
                        stage_errors=["E_COMPOSE_SPECIAL_PATCH_FAILED: Mk"]),
    lambda tmp: _worker(tmp, OWN, _row("op-9999-text-replace-once", "@main")),
    lambda tmp: _worker(tmp, OWN, _row(_ids(OWN)["@main"], "@main"),
                        port_extra={"errors": 2}),
    lambda tmp: _worker(tmp, OWN, _row(_ids(OWN)["@main"], "@main"),
                        port_extra={"oracle_failures": 1}),
    lambda tmp: _worker(tmp, OWN, _row(_ids(OWN)["@main"], "@main",
                                       code="E_APPLY_UNKNOWN_KIND")),
], ids=["report-unreadable", "no-op-rows", "another-error-beside",
        "row-matches-no-op", "rolled-back", "oracle-failed", "tool-skew"])
def test_anything_but_a_failing_op_leaves_the_tree_unknown(tmp_path, make):
    """Then the preflight refuses as it always did."""
    assert _sort(make(tmp_path)).other


def test_the_agent_is_told_which_of_its_ops_fail():
    rows = tuple(f"devel/x: op-{n:04d}-text-replace-once (...)" for n in range(10))
    brief = steps._own_ops_brief("@main", rows)
    assert "## This line's own ops fail to compose" in brief
    assert "`@main`" in brief and "op-0007" in brief
    assert "op-0008" not in brief and "and 2 more" in brief
    assert "`dsynth_build` refuses until `materialize_dports` succeeds" in brief
    assert len(steps._own_ops_brief("@main", rows[:1])) < 600


# --- what the operator gets -------------------------------------------------


def test_the_handoff_asks_for_the_decision():
    ctx = manual_handoff.HandoffCtx(
        origin="devel/x", target="@main",
        reason=manual_handoff.REASON_PATCH_SCOPE_DECISION,
        reason_detail="devel/x: op-0001 (text.replace_once, target @any)",
    )
    body = manual_handoff.render_handoff(ctx)
    assert "shared op fails on this build line" in body
    assert "before the agent ran" in body
    assert "drop the op everywhere" in body
    assert "other build lines' `target` blocks" in body
    assert "op-0001" in body


@pytest.mark.parametrize("target", ["@main", ""])
def test_the_question_names_the_line_or_says_this_one(target):
    ctx = manual_handoff.HandoffCtx(
        origin="devel/x", target=target,
        reason=manual_handoff.REASON_PATCH_SCOPE_DECISION)
    body = manual_handoff.render_handoff(ctx)
    assert ("`@main`" if target else "this build line") in body
