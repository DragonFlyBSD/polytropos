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

import inspect
import pathlib
from types import SimpleNamespace

import pytest

from dportsv3.agent.scope_check import UNREADABLE, scope_drift

def _delta_ports() -> pathlib.Path | None:
    """The DeltaPorts ports dir, via the repo's own resolver.

    CLAUDE.md: site-owned inputs are named by --delta-root /
    $DPORTS_DELTA_ROOT and dportsv3/paths.py is the single resolver. A
    hardcoded developer path would silence these tests everywhere but one
    machine -- including this repo's own second working directory -- and
    the whole-tree sweep is the one thing standing between this check and
    a tree-wide regression.
    """
    from dportsv3.paths import resolve_delta_root

    try:
        root = resolve_delta_root() / "ports"
    except Exception:  # noqa: BLE001
        return None
    return root if root.is_dir() else None


DELTA = _delta_ports()

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
    assert "@any" in note
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


def test_a_payload_key_named_target_cannot_mask_the_scope():
    """Identity is built explicitly, not from to_dict()'s flattening.

    to_dict() spreads the payload over id/target/kind, so a future payload
    key named `target` would overwrite the op's scope and make every such
    op read as universally effective -- a silent false negative in this
    check's core predicate.
    """
    from dportsv3.agent import scope_check

    src = inspect.getsource(scope_check._key)
    assert 'k not in ("id", "target", "kind")' in src
    assert 'op.get("target"), op.get("kind")' in src


# --------------------------------------------------------------------------
# The real tree is the fixture that matters
# --------------------------------------------------------------------------


@pytest.mark.skipif(DELTA is None, reason="DeltaPorts checkout not present")
def test_a_real_comma_list_block_in_lang_rust_is_not_flagged():
    """The live instance: lang/rust's `target @2026Q3,@main` block."""
    overlay = (DELTA / "lang/rust/overlay.dops").read_text()
    assert "target @2026Q3,@main" in overlay, "fixture premise moved"
    after = overlay.replace(
        'mk add MAKE_ENV "LDVER=ld.bfd"',
        'mk add MAKE_ENV "LDVER=ld.bfd"\nmk add MAKE_ENV "LDFLAGS=-Wl,-z"',
        1,
    )
    for target in ("@2026Q3", "@main"):
        assert scope_drift(overlay, after, target).ok is True, target


@pytest.mark.skipif(DELTA is None, reason="DeltaPorts checkout not present")
def test_an_eof_append_to_a_real_overlay_is_flagged_on_the_other_line():
    """Same two ports, the actual defect."""
    for origin in ("lang/rust", "ports-mgmt/pkg"):
        overlay = (DELTA / origin / "overlay.dops").read_text()
        after = overlay + "\nmk set SOMETHING \"x\"\n"
        assert scope_drift(overlay, after, "@2026Q3").ok is False, origin
        assert scope_drift(overlay, after, "@main").ok is True, origin


# --------------------------------------------------------------------------
# It has to reach the next attempt, not just a log
# --------------------------------------------------------------------------


def test_the_attempt_loop_carries_the_finding_into_the_next_attempt():
    """The bead's cost is attempts 2..N repeating the mistake.

    `notes` is the channel already carried into the retry prompt, and the
    finding is appended to it beside the agent's own notes -- not reported
    after the loop, by which time every attempt it could have saved is
    spent.
    """
    from dportsv3.agent import attempt_loop

    src = inspect.getsource(attempt_loop.run)
    assert "overlay_before = _overlay_text(env, origin)" in src
    note_at = src.index("notes.append(scope_note)")
    baseline_at = src.index("overlay_before = _overlay_text")
    loop_at = src.index("for attempt_idx in range(")
    # Baseline before the loop; the note appended inside it.
    assert baseline_at < loop_at < note_at


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


def test_a_broken_reporter_says_so_instead_of_vanishing(tmp_path):
    """A silently dead check is worse than no check."""
    from dportsv3.agent import steps

    logged: list[tuple] = []
    services = SimpleNamespace(
        log=lambda root, level, msg: logged.append((level, msg)),
        activity_log=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    steps._report_scope_drift(
        services, SimpleNamespace(job_id="j1"), tmp_path, "drift-env",
        baseline={"x/y": BASE},
    )
    # Either nothing to report, or a WARN -- never a silent swallow of a
    # real failure.
    assert all(level == "WARN" for level, _ in logged)


def test_the_runner_snapshots_below_the_dirty_tree_refusal():
    """The baseline's trustworthiness must be LOCAL, not 54 lines away.

    It holds only because every dirty path has already returned. Read the
    baseline above that refusal and the property survives by accident,
    which a later warn-and-continue mode would silently break.
    """
    from dportsv3.agent import steps

    src = inspect.getsource(steps.PatchAttemptStep)
    snap = src.index('ctx.state["overlay_baseline"]')
    refusal = src.index("patch_preflight_dirty")
    harness = src.index("harness_patch.run(")
    assert refusal < snap < harness
