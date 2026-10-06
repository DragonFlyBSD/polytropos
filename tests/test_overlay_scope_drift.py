"""An attempt must learn, before its next try, that its edits missed.

poly-7pwa.1. Scope is inherited from the last ``target`` block above an op,
so appending lands in whatever block is last -- ``@main`` for both
multi-target ports in DeltaPorts. A job on another build line writes its
op, ``validate_dops`` passes, compose omits it, the build fails exactly as
before, and every remaining attempt repeats the mistake.

THE RULE IS "NOTHING LANDED", NOT "SOMETHING LANDED ELSEWHERE". Authoring
ops for another build line is the convention, and the first version of this
check flagged three legitimate shapes -- a ``target @2026Q3,@main`` block
(which the engine expands into one op per target), a per-target split
authored in one attempt, and an edit to the op that already references a
patch. Those fixtures are here because they are the ones that matter.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from dportsv3.agent.scope_check import UNREADABLE, scope_drift

HEAD = 'port x/y\ntype port\nreason "fixture"\n'

#: Ends in a @main block, as ports-mgmt/pkg and lang/rust both do.
BASE = HEAD + 'target @any\nmk set A "1"\ntarget @main\nmk set B "2"\n'

#: The defect: appended at EOF, so it inherits @main.
APPENDED = BASE + "file materialize dragonfly/patch-NEW -> dragonfly/patch-NEW\n"


# --------------------------------------------------------------------------
# The finding
# --------------------------------------------------------------------------


def test_an_appended_op_is_reported_on_another_build_line():
    drift = scope_drift(BASE, APPENDED, "@2026Q3")
    assert drift.ok is False
    assert len(drift.stranded) == 1
    assert "@main" in drift.stranded[0]
    assert "dragonfly/patch-NEW" in drift.stranded[0]


def test_the_same_append_is_fine_on_the_block_it_landed_in():
    """Accidentally correct is still correct -- do not cry wolf."""
    assert scope_drift(BASE, APPENDED, "@main").ok is True


def test_the_note_tells_the_agent_what_to_do_about_it():
    note = scope_drift(BASE, APPENDED, "@2026Q3").note()
    assert "did not reach @2026Q3" in note
    # A finding with no remedy costs an attempt to act on.
    assert "`target @2026Q3` block" in note
    # poly-7pwa.27: the port has an overlay, so the remedy is this line's
    # block, never @any -- other lines build that overlay untested.
    assert "@any" not in note
    assert "get_effective_overlay" in note


def test_the_message_names_the_mechanism_and_the_remedy():
    msg = scope_drift(BASE, APPENDED, "@2026Q3").message()
    assert "last `target` block" in msg
    assert "Move them into the `target @2026Q3` block" in msg


# --------------------------------------------------------------------------
# What must NOT be reported: the convention working
# --------------------------------------------------------------------------


def test_a_comma_list_block_is_not_drift():
    """`target @2026Q3,@main` expands to ONE OP PER TARGET.

    So one correctly authored line becomes two ops and the non-matching
    half looks stranded. lang/rust really carries such a block, and the
    first version of this check flagged it on BOTH build lines.
    """
    before = HEAD + 'target @2026Q3,@main\nmk add MAKE_ENV "LDVER=ld.bfd"\n'
    after = before + 'mk add MAKE_ENV "LD_LIBRARY_PATH=/x"\n'
    assert scope_drift(before, after, "@2026Q3").ok is True
    assert scope_drift(before, after, "@main").ok is True


def test_a_per_target_split_authored_in_one_attempt_is_not_drift():
    """Half of a correct split is always "for the other target"."""
    before = HEAD + 'target @any\nmk set A "1"\n'
    after = before + (
        'target @2026Q3\nmk set V "1.96.1"\n'
        'target @main\nmk set V "1.98.1"\n'
    )
    assert scope_drift(before, after, "@2026Q3").ok is True
    assert scope_drift(before, after, "@main").ok is True


def test_editing_another_targets_op_alongside_an_effective_one_is_not_drift():
    """poly-7pwa.13's re-cut edits the op that references the patch."""
    before = HEAD + (
        "target @2026Q3\nfile materialize dragonfly/@2026Q3/p -> dragonfly/p\n"
        "target @main\nfile materialize dragonfly/@main/p -> dragonfly/p\n"
    )
    after = before.replace(
        "dragonfly/@2026Q3/p -> dragonfly/p", "dragonfly/@2026Q3/p2 -> dragonfly/p"
    ).replace("dragonfly/@main/p -> dragonfly/p", "dragonfly/@main/p2 -> dragonfly/p")
    assert scope_drift(before, after, "@2026Q3").ok is True


def test_pre_existing_other_target_ops_are_never_reported():
    """lang/rust has 29 by design; the finding is about ADDED ops."""
    other = HEAD + 'target @main\nmk set B "2"\nmk set C "3"\n'
    assert scope_drift(other, other, "@2026Q3").ok is True


def test_an_op_added_in_the_right_block_is_not_reported():
    after = HEAD + (
        'target @any\nmk set A "1"\n'
        'target @2026Q3\nmk set NEW "yes"\n'
        'target @main\nmk set B "2"\n'
    )
    assert scope_drift(BASE, after, "@2026Q3").ok is True


def test_an_op_added_in_any_scope_is_not_reported():
    after = HEAD + (
        'target @any\nmk set A "1"\nmk set C "3"\ntarget @main\nmk set B "2"\n'
    )
    assert scope_drift(BASE, after, "@2026Q3").ok is True


def test_inserting_a_line_above_an_op_does_not_make_it_look_new():
    """Op ids encode ordinal position, so identity must exclude them."""
    after = HEAD + (
        'target @any\nmk set A "1"\nmk set ZZ "first"\n'
        'target @main\nmk set B "2"\n'
    )
    assert scope_drift(BASE, after, "@2026Q3").ok is True


def test_a_removed_op_is_not_reported_as_added():
    shrunk = HEAD + 'target @any\nmk set A "1"\n'
    assert scope_drift(BASE, shrunk, "@2026Q3").ok is True


def test_a_duplicated_line_counts_once_not_twice():
    """Multiset counting: adding a second identical op adds one op."""
    after = BASE + 'mk set B "2"\n'
    drift = scope_drift(BASE, after, "@2026Q3")
    assert len(drift.stranded) == 1


# --------------------------------------------------------------------------
# No baseline means no finding
# --------------------------------------------------------------------------


def test_a_brand_new_overlay_is_not_judged():
    """The bootstrap flow writes the header AFTER the snapshot.

    Judging against "nothing" reported every other-target op in a freshly
    and correctly authored two-target overlay -- the canonical shape for
    exactly the ports this epic is about.
    """
    drift = scope_drift(None, APPENDED, "@2026Q3")
    assert drift.ok is True
    assert drift.unavailable is not None


def test_an_unreadable_overlay_is_distinguished_from_an_absent_one():
    """One failed read must not read as "the file was empty before"."""
    drift = scope_drift(UNREADABLE, APPENDED, "@2026Q3")
    assert drift.ok is True
    assert "could not be read" in (drift.unavailable or "")


def test_an_unparseable_overlay_defers_to_validate_dops():
    drift = scope_drift(BASE, HEAD + "this is not dops\n", "@2026Q3")
    assert drift.unavailable is not None
    assert drift.message() == ""
    assert drift.note() == ""


def test_no_target_is_unavailable_not_a_finding():
    assert scope_drift(BASE, APPENDED, "").unavailable is not None


def test_a_deleted_overlay_is_not_a_finding():
    assert scope_drift(BASE, None, "@2026Q3").ok is True


def test_a_payload_key_named_target_cannot_mask_the_scope(monkeypatch):
    """Identity is read from PlanOp fields, not from to_dict()'s flattening.

    to_dict() spreads the payload over id/target/kind, so a payload key
    named `target` would overwrite the op's scope and make the op read as
    universally effective -- a silent false negative in the core predicate.
    """
    from dportsv3.engine import api
    from dportsv3.engine.models import Plan, PlanOp, PlanResult

    def fake_build_plan(text, path):
        ops = []
        if text == APPENDED:
            ops = [PlanOp(id="op-0001-mk.var.set", target="@main",
                          kind="mk.var.set",
                          payload={"target": "@any", "var": "X"})]
        return PlanResult(ok=True, plan=Plan(port="x/y", ops=ops))

    monkeypatch.setattr(api, "build_plan", fake_build_plan)
    drift = scope_drift(BASE, APPENDED, "@2026Q3")
    assert drift.ok is False
    assert drift.stranded[0].startswith("scope @main")


def test_the_remedy_survives_the_retry_prompt_cap():
    """The remedy comes first, so the per-note cap never cuts it off."""
    from dportsv3.agent import attempt_loop

    names = [f"patch-library_std_src_sys_pal_unix_mod{i}.rs" for i in range(5)]
    after = BASE + "".join(
        f"file materialize dragonfly/{n} -> dragonfly/{n}\n" for n in names)
    note = scope_drift(BASE, after, "@2026Q3").note()
    shown = note[:attempt_loop._MAX_NOTE_CHARS]
    assert "`target @2026Q3` block" in shown
    assert "get_effective_overlay" in shown


# --------------------------------------------------------------------------
# The real tree is the fixture that matters
# --------------------------------------------------------------------------


_PROBE = 'mk set POLY_SCOPE_PROBE "x"'


def test_a_real_comma_list_block_is_not_flagged(multi_line_ports):
    """An op added inside a real `target A,B` block applies on A and B."""
    checked = 0
    for port in multi_line_ports:
        text = port.overlay.read_text()
        for m in re.finditer(r"^target\s+(@\S*,\S*)\s*$", text, re.M):
            after = text[:m.end()] + "\n" + _PROBE + text[m.end():]
            for line in m.group(1).split(","):
                assert scope_drift(text, after, line).ok is True, (
                    port.origin, line)
            checked += 1
    if not checked:
        pytest.skip("no multi-line port has a comma-list block")


def test_an_eof_append_to_a_real_overlay_is_flagged_on_the_other_lines(
        multi_line_ports):
    """The actual defect: an append lands in whatever block is last."""
    flagged = 0
    for port in multi_line_ports:
        text = port.overlay.read_text()
        sep = "" if text.endswith("\n") else "\n"
        after = text + sep + _PROBE + "\n"
        for line in port.lines:
            ok = scope_drift(text, after, line).ok
            assert ok is ("@any" in port.last_lines
                          or line in port.last_lines), (port.origin, line)
            flagged += not ok
    if not flagged:
        pytest.skip("no multi-line port ends in a scoped block")


# --------------------------------------------------------------------------
# It has to reach the next attempt, not just a log
# --------------------------------------------------------------------------


def _drive_attempts(tmp_path, monkeypatch, edits, iterations):
    """Run attempt_loop.run with a stub tool loop; return the retry messages.

    ``edits`` maps an attempt number to the overlay text that attempt
    writes. Every attempt fails, so each one after the first opens with
    the harness's retry message, which is what is returned.
    """
    from dportsv3.agent import attempt_loop, worker

    port = tmp_path / "DeltaPorts" / "ports" / "x" / "y"
    port.mkdir(parents=True)
    overlay = port / "overlay.dops"
    overlay.write_text(BASE)
    retries: list[str] = []

    def fake_run(messages, **kw):
        idx = kw["attempt_idx"]
        if idx > 1:
            retries.append(messages[-1]["content"])
        if idx in edits:
            overlay.write_text(edits[idx])
        return (SimpleNamespace(text="no fix", tool_calls=[]),
                attempt_loop.Usage(), False)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)
    monkeypatch.setattr(attempt_loop, "_current_diff", lambda *a: "")
    monkeypatch.setattr(worker, "reset_attempt_workspace", lambda *a, **k: {})
    monkeypatch.setattr(worker, "reset_attempt_caches", lambda: None)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda env: SimpleNamespace(deltaports=tmp_path / "DeltaPorts"),
    )
    worker.set_env_target("loop-env", "@2026Q3")
    try:
        attempt_loop.run(
            "payload",
            tier=SimpleNamespace(max_tokens=0, max_iterations=iterations),
            env="loop-env", model="m", origin="x/y",
        )
    finally:
        worker.set_env_target("loop-env", None)
    return retries


def test_the_next_attempt_carries_one_current_scope_note(tmp_path, monkeypatch):
    """The finding is state, not an event: shown once, and gone once fixed.

    Attempt 1 strands an op, attempt 2 does nothing, attempt 3 moves the
    op under `target @any`, attempt 4 does nothing. Appending the note to
    the agent's notes showed it 1, 2, 2 times, so attempt 4 was told to
    move an op it had already moved.
    """
    fixed = HEAD + (
        'target @any\nmk set A "1"\n'
        "file materialize dragonfly/patch-NEW -> dragonfly/patch-NEW\n"
        'target @main\nmk set B "2"\n'
    )
    retries = _drive_attempts(tmp_path, monkeypatch,
                              {1: APPENDED, 3: fixed}, iterations=4)
    assert [r.count("SCOPE:") for r in retries] == [1, 1, 0]
    assert "did not reach @2026Q3" in retries[0]
    assert "Notes recorded in earlier attempts" not in retries[0]
    assert (retries[0].index("## Found by the harness after that attempt")
            < retries[0].index("SCOPE:"))


def test_the_note_helper_never_raises(monkeypatch):
    """A note is not worth failing an attempt over."""
    from dportsv3.agent import attempt_loop

    monkeypatch.setattr(
        attempt_loop, "_overlay_text", lambda *a: (_ for _ in ()).throw(RuntimeError)
    )
    assert attempt_loop._scope_drift_note("e", "x/y", BASE) == ""


def test_the_runner_still_records_it_for_the_operator(tmp_path, monkeypatch):
    from dportsv3.agent import steps, worker

    port = tmp_path / "DeltaPorts" / "ports" / "x" / "y"
    port.mkdir(parents=True)
    (port / "overlay.dops").write_text(APPENDED)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda env: SimpleNamespace(deltaports=tmp_path / "DeltaPorts"),
    )
    worker.set_env_target("drift-env", "@2026Q3")

    logged: list[tuple] = []
    events: list[tuple] = []
    services = SimpleNamespace(
        log=lambda root, level, msg: logged.append((level, msg)),
        activity_log=lambda root, kind, msg, **kw: events.append((kind, msg, kw)),
    )
    try:
        steps._report_scope_drift(
            services, SimpleNamespace(job_id="j1"), tmp_path, "drift-env",
            baseline={"x/y": BASE},
        )
    finally:
        worker.set_env_target("drift-env", None)

    assert any("@main" in msg for _, msg in logged), logged
    assert events and events[0][0] == "overlay_scope_drift"
    assert events[0][2]["extra"]["stranded"]


def test_a_broken_reporter_says_so_instead_of_vanishing(tmp_path, monkeypatch):
    """A silently dead check is worse than no check."""
    from dportsv3.agent import scope_check, steps

    def broken(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(scope_check, "scope_drift", broken)
    logged: list[tuple] = []
    services = SimpleNamespace(
        log=lambda root, level, msg: logged.append((level, msg)),
        activity_log=lambda *a, **k: None,
    )
    steps._report_scope_drift(
        services, SimpleNamespace(job_id="j1"), tmp_path, "drift-env",
        baseline={"x/y": BASE},
    )
    assert any(level == "WARN" and "scope-drift report failed" in msg
               for level, msg in logged), logged
