"""A re-cut patch must land where this build line's op reads it.

poly-7pwa.2. install_patches always wrote ``ports/<origin>/dragonfly/``.
For a port whose patches live in ``dragonfly/@<target>/`` -- lang/rust
keeps 15 under @2026Q3 and 14 under @main, pkg one under each -- the fresh
patch landed BESIDE the scoped ones at a path the target's own
``file materialize dragonfly/@main/patch-X -> dragonfly/patch-X`` does not
read. install_patches reported ok, compose staged the OLD patch, and
dsynth_build reproduced the original failure.

Same class as poly-lt5q's slave-port case ("install into a directory the
build never reads"), and the same remedy: redirect, and say so.

THE OVERLAY IS THE AUTHORITY, not the directory listing -- pkg keeps four
libpkg patches flat in @any and one scoped per target, in one file.
"""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from dportsv3.agent import worker

HEAD = 'port devel/thing\ntype port\nreason "fixture"\n'

#: ports-mgmt/pkg's REAL shape, and the fixture whose absence let the
#: first version of this fix reproduce the bug with the lanes swapped: a
#: flat @any `patch-*` op beside per-target scoped ones, in one overlay.
SCOPED = HEAD + (
    "target @any\n"
    "file materialize dragonfly/extra-crate-1.0 -> dragonfly/extra-crate-1.0\n"
    "file materialize dragonfly/patch-libpkg_flat.c"
    " -> dragonfly/patch-libpkg_flat.c\n"
    "target @2026Q3\n"
    "file materialize dragonfly/@2026Q3/patch-src_lib.rs -> dragonfly/patch-src_lib.rs\n"
    "target @main\n"
    "file materialize dragonfly/@main/patch-src_lib.rs -> dragonfly/patch-src_lib.rs\n"
)

#: Everything flat and universal -- the common case, must not be disturbed.
FLAT = HEAD + (
    "target @any\n"
    "file materialize dragonfly/patch-configure -> dragonfly/patch-configure\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A writable overlay with a genpatch-out dir, and no chroot."""
    deltaports = tmp_path / "DeltaPorts"
    (deltaports / "ports" / "devel" / "thing").mkdir(parents=True)
    out = tmp_path / "writable" / "work" / "genpatch-out"
    out.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda e: SimpleNamespace(
            deltaports=deltaports, writable=tmp_path / "writable"
        ),
    )
    # No slave relation: keep the destination on the port's own tree.
    monkeypatch.setattr(worker, "patch_origin_for", lambda e, o: o)
    # What the job started with (poly-7pwa.27). The real answer is git HEAD
    # in the chroot; here, by default, whatever is on disk when
    # install_patches runs. A test sets `committed` to a set to say exactly
    # which paths predate the job, or "unknown" for a git that cannot say.
    state = SimpleNamespace(committed=None)

    def committed_paths(e, rels):
        if state.committed == "unknown":
            return None
        if state.committed is None:
            return {r for r in rels if (deltaports / r).exists()}
        return {r for r in rels if r in state.committed}

    monkeypatch.setattr(worker, "_committed_paths", committed_paths)
    # HEAD's copy of a committed file: by default the one on disk, or the
    # text a test puts in `head`.
    state.head = {}

    def head_text(e, rel):
        if rel in state.head:
            return state.head[rel]
        f = deltaports / rel
        return f.read_text() if f.is_file() else None

    monkeypatch.setattr(worker, "_head_text", head_text)
    yield SimpleNamespace(name="scope-env", deltaports=deltaports, out=out,
                          tmp=tmp_path, state=state)
    worker.set_env_target("scope-env", None)


def _overlay(env, text):
    (env.deltaports / "ports" / "devel" / "thing" / "overlay.dops").write_text(text)


def _genpatch(env, name):
    (env.out / name).write_text(
        "--- a/src/lib.rs\n+++ b/src/lib.rs\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )


def _install(env):
    return worker.install_patches("scope-env", "devel/thing")


def _diff(path):
    return f"--- {path}.orig\n+++ {path}\n@@ -1,1 +1,1 @@\n-old\n+new\n"


def _port(env):
    return env.deltaports / "ports" / "devel" / "thing"


def test_a_recut_lands_in_the_scoped_dir_its_own_op_reads(env):
    """The defect: this used to go to a flat path nothing stages."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")

    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-src_lib.rs"
    ]
    assert (
        env.deltaports / "ports/devel/thing/dragonfly/@main/patch-src_lib.rs"
    ).is_file()
    # And NOT beside it at the flat path.
    assert not (
        env.deltaports / "ports/devel/thing/dragonfly/patch-src_lib.rs"
    ).exists()


def test_the_other_build_line_gets_its_own_directory(env):
    """Same port, same filename, same overlay -- different target."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@2026Q3")
    _genpatch(env, "patch-src_lib.rs")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/@2026Q3/patch-src_lib.rs"
    ]


def test_the_result_says_why_it_was_redirected(env):
    """The lt5q rule: redirect, and tell the model, rather than refuse."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")

    note = _install(env).get("scope_note") or ""
    assert "dragonfly/@main/patch-src_lib.rs" in note
    # It must say what already installs it needs no edit, and what an op
    # would do: the decision-1 sentences live here, not in a playbook.
    assert "needs no edit" in note
    assert "Do not add a `file materialize`" in note
    assert "never lands" in note
    assert "validate_dops rejects it" in note
    # poly-u9n4: an op that reads a file narrows the folder's scope.
    assert "takes it out of the dragonfly/ folder" in note
    assert "collide" not in note and "refused" not in note


def test_a_flat_port_is_untouched(env):
    """The common case. A redirect here would be the bug, inverted."""
    _overlay(env, FLAT)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    result = _install(env)
    assert result["installed"] == ["ports/devel/thing/dragonfly/patch-configure"]
    assert "scope_note" not in result


def test_a_new_patch_goes_to_this_lines_folder_with_no_op(env):
    """poly-7pwa.27 row 2: the port had an overlay, so the patch is this
    line's alone. It used to go flat under @any, which is how devel/glib20's
    @main patch reached @2026Q3, whose older glib it did not apply to. The
    folder installs it there (poly-u9n4), so there is no op to give."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-brand_new.rs"
    ]
    assert not (_port(env) / "dragonfly/patch-brand_new.rs").exists()
    note = result.get("scope_note") or ""
    assert "Written to @main's own folder" in note
    assert "needs no edit" in note
    assert "file materialize dragonfly/@main/patch-brand_new.rs" not in note
    assert "`target @any`" not in note


def test_a_new_patch_on_a_single_scope_overlay_also_stays_on_its_line(env):
    _overlay(env, FLAT)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-brand_new.rs"
    ]
    note = result.get("scope_note") or ""
    assert "Written to @main's own folder" in note
    assert "\ntarget @main\n" not in note
    assert "per-target blocks" not in note


def test_a_port_with_no_overlay_at_job_start_gets_a_flat_patch(env):
    """Row 1: the overlay is this job's own header, so no line was building
    with one, and the patch goes flat; the folder installs it on every line."""
    _overlay(env, FLAT)
    env.state.committed = set()
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/patch-brand_new.rs"
    ]
    note = result.get("scope_note") or ""
    assert "Written to the flat dragonfly/" in note
    assert "no overlay content when the job started" in note
    assert "file materialize" not in note


def test_a_committed_bootstrap_header_is_still_row_1(env):
    """The preflight can commit triage's header before the attempt. A header
    has no ops, so nothing was built with an overlay on any line."""
    _overlay(env, FLAT)
    env.state.head["ports/devel/thing/overlay.dops"] = HEAD + "target @any\n"
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/patch-brand_new.rs"
    ]
    assert "Written to the flat dragonfly/" in (result.get("scope_note") or "")


def test_a_header_whose_folder_has_patches_is_not_row_1(env):
    """Q4 (poly-u9n4): a header-only overlay still installs its dragonfly/
    files on every line, so a port that has them was built with them."""
    _overlay(env, HEAD + "target @any\n")
    _shared(env, "patch-old", "old\n")
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-brand_new.rs"
    ]


def test_an_unreadable_head_overlay_counts_as_ops(env):
    _overlay(env, FLAT)
    env.state.head["ports/devel/thing/overlay.dops"] = None
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-brand_new.rs"
    ]


def test_a_new_patch_on_a_bootstrapped_scoped_overlay_needs_no_block(env):
    """No op to place, so nothing to say about which block is last."""
    _overlay(env, SCOPED)
    env.state.committed = set()
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    note = _install(env).get("scope_note") or ""
    assert "Written to the flat dragonfly/" in note
    assert "last block" not in note


def test_a_recut_of_a_flat_patch_stays_flat_even_on_a_scoped_port(env):
    """THE defect the first version of this fix had, inverted.

    pkg keeps four libpkg patches flat under @any and one scoped per
    target. Guessing "this port scopes its payload for @main" from the
    presence of a scoped directory sent a re-cut of a FLAT patch into
    @main/, where no op reads it -- poly-7pwa.2 again with the lanes
    swapped, on the very port the bead names.
    """
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-libpkg_flat.c")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/patch-libpkg_flat.c"
    ]
    assert not (
        env.deltaports / "ports/devel/thing/dragonfly/@main/patch-libpkg_flat.c"
    ).exists()


def test_one_call_really_does_split_across_two_lanes(env):
    """pkg's shape in one install_patches call: one flat, one scoped."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-libpkg_flat.c")    # flat @any op reads it
    _genpatch(env, "patch-src_lib.rs")       # scoped @main op reads it
    installed = set(_install(env)["installed"])
    assert installed == {
        "ports/devel/thing/dragonfly/patch-libpkg_flat.c",
        "ports/devel/thing/dragonfly/@main/patch-src_lib.rs",
    }


def test_destination_is_always_a_string_even_when_lanes_differ(env):
    """A field that is a str on most calls and a list on some breaks callers."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-libpkg_flat.c")
    _genpatch(env, "patch-src_lib.rs")
    assert isinstance(_install(env)["destination"], str)


def test_the_note_does_not_both_forbid_and_order_an_op(env):
    """Two notes concatenated read as a contradiction."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")   # existing op -> "needs no edit"
    _genpatch(env, "patch-brand_new.rs")  # new -> "No op installs"
    note = _install(env).get("scope_note") or ""
    # Each statement must name which files it is about, and neither may
    # order an op the other forbids.
    assert "dragonfly/@main/patch-src_lib.rs" in note
    assert "dragonfly/@main/patch-brand_new.rs" in note
    assert "\ntarget @main\n" not in note


def test_a_renaming_op_gets_the_file_it_actually_reads(env):
    """src basename may differ from dst; the re-cut replaces the src."""
    renaming = HEAD + (
        "target @main\n"
        "file materialize dragonfly/@main/src-name.c -> dragonfly/patch-x\n"
    )
    _overlay(env, renaming)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-x")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/@main/src-name.c"
    ]


def test_no_cached_target_falls_back_to_flat_and_says_so(env):
    """Silence here is indistinguishable from "this port is flat".

    The agent has already paid for a genpatch call; a result identical to
    success on a flat port restores the original bug with no signal.
    """
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", None)
    _genpatch(env, "patch-src_lib.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/patch-src_lib.rs"
    ]
    assert "could not be decided" in (result.get("scope_note") or "")


@pytest.mark.parametrize("overlay", [
    (HEAD + "this is not dops at all\n").encode(),
    HEAD.encode() + b'mk set X "caf\xe9"\n',
], ids=["unparseable", "non-utf8"])
def test_an_overlay_that_does_not_plan_falls_back_to_flat_and_says_so(
        env, overlay):
    (_port(env) / "overlay.dops").write_bytes(overlay)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/patch-src_lib.rs"
    ]
    assert "does not plan" in (result.get("scope_note") or "")


def test_an_overlay_that_does_not_plan_never_overwrites_an_existing_copy(env):
    """Nothing says which lines read dragonfly/<name>, so leave it alone."""
    _overlay(env, HEAD + "this is not dops at all\n")
    shared = _port(env) / "dragonfly" / "patch-src_lib.rs"
    shared.parent.mkdir(parents=True)
    shared.write_text("shared\n")
    worker.set_env_target("scope-env", "@2026Q3")
    _genpatch(env, "patch-src_lib.rs")
    result = _install(env)
    assert shared.read_text() == "shared\n"
    assert result["installed"] == []
    assert result["not_installed"] == ["patch-src_lib.rs"]
    assert result["ok"] is False
    assert "call install_patches again" in (result.get("scope_note") or "")


def test_a_port_without_an_overlay_is_compat_and_gets_no_op(env):
    """Compat mode copies dragonfly/ as it is; overlay.dops would end that."""
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/patch-src_lib.rs"
    ]
    assert "scope_note" not in result
    assert not (_port(env) / "overlay.dops").exists()


def test_another_targets_scoped_op_does_not_redirect_this_one(env):
    """@2026Q3 scoping is no reason to put a @main patch under @2026Q3."""
    only_q3 = HEAD + (
        "target @2026Q3\n"
        "file materialize dragonfly/@2026Q3/patch-src_lib.rs"
        " -> dragonfly/patch-src_lib.rs\n"
    )
    _overlay(env, only_q3)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-src_lib.rs"
    ]
    assert "Written to @main's own folder" in (result.get("scope_note") or "")


def test_the_decision_comes_from_the_overlay_not_the_directory(env):
    """pkg keeps flat and scoped payload in one port; a listing gets it wrong.

    An existing @main/ DIRECTORY with no op naming this file, and an
    overlay that scopes nothing for @main, must still install flat.
    """
    _overlay(env, FLAT)
    (env.deltaports / "ports/devel/thing/dragonfly/@main").mkdir(parents=True)
    (
        env.deltaports / "ports/devel/thing/dragonfly/@main/leftover"
    ).write_text("stale\n")
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/patch-configure"
    ]


@pytest.mark.parametrize("src", [
    "../../../ESCAPED/patch-x",
    "ABSOLUTE",
    "files/patch-x",
    "overlay.dops",
])
def test_a_source_outside_dragonfly_is_never_followed(env, src):
    """install_patches writes; only a file under dragonfly/ is followed."""
    if src == "ABSOLUTE":
        src = str(env.tmp / "ABSOLUTE" / "patch-x")
    text = HEAD + f"target @any\nfile materialize {src} -> dragonfly/patch-x\n"
    _overlay(env, text)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-x")
    result = _install(env)
    assert result["installed"] == ["ports/devel/thing/dragonfly/patch-x"]
    written = [p for p in env.tmp.rglob("patch-x")
               if "genpatch-out" not in p.parts]
    assert written == [_port(env) / "dragonfly" / "patch-x"]
    assert (_port(env) / "overlay.dops").read_text() == text
    assert "does not read a file under dragonfly/" in (
        result.get("scope_note") or "")


def test_a_recut_genpatch_named_differently_replaces_the_file_its_op_reads(env):
    """genpatch names a re-cut after the file it changes, not after the op."""
    _overlay(env, HEAD + (
        "target @2026Q3\n"
        "file materialize dragonfly/@2026Q3/patch-src_ae_epoll.c"
        " -> dragonfly/patch-src_ae_epoll.c\n"
        "target @main\n"
        "file materialize dragonfly/@main/patch-src_ae_epoll.c"
        " -> dragonfly/patch-src_ae_epoll.c\n"
    ))
    old = _diff("src/ae_epoll.c").replace("+new", "+stale")
    for line in ("@2026Q3", "@main"):
        f = _port(env) / "dragonfly" / line / "patch-src_ae_epoll.c"
        f.parent.mkdir(parents=True)
        f.write_text(old)
    (env.out / "patch-src_ae__epoll.c").write_text(_diff("src/ae_epoll.c"))
    worker.set_env_target("scope-env", "@2026Q3")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@2026Q3/patch-src_ae_epoll.c"
    ]
    port = _port(env)
    assert (port / "dragonfly/@2026Q3/patch-src_ae_epoll.c").read_text() == (
        _diff("src/ae_epoll.c"))
    assert (port / "dragonfly/@main/patch-src_ae_epoll.c").read_text() == old
    assert not list(port.rglob("patch-src_ae__epoll.c"))
    assert "No op installs" not in (result.get("scope_note") or "")


@pytest.mark.parametrize("line", ["@main", "@2026Q3"])
def test_an_override_is_resolved_by_apply_order_not_file_order(env, line):
    """The @any block is written last, but @main's own op runs after it."""
    _overlay(env, HEAD + (
        "target @main\n"
        "file materialize dragonfly/@main/patch-x -> dragonfly/patch-x\n"
        "target @any\n"
        "file materialize dragonfly/patch-x -> dragonfly/patch-x\n"
    ))
    worker.set_env_target("scope-env", line)
    _genpatch(env, "patch-x")
    result = _install(env)
    expected = ("dragonfly/@main/patch-x" if line == "@main"
                else "dragonfly/patch-x")
    assert result["installed"] == [f"ports/devel/thing/{expected}"]
    assert "No op installs" not in (result.get("scope_note") or "")


def test_two_patches_of_one_file_are_not_guessed(env):
    _overlay(env, HEAD + (
        "target @any\n"
        "file materialize dragonfly/patch-one -> dragonfly/patch-one\n"
        "file materialize dragonfly/patch-two -> dragonfly/patch-two\n"
    ))
    port = _port(env)
    (port / "dragonfly").mkdir(parents=True)
    for name in ("patch-one", "patch-two"):
        (port / "dragonfly" / name).write_text(_diff("src/a.c"))
    (env.out / "patch-src_a.c").write_text(
        _diff("src/a.c").replace("+new", "+recut"))
    worker.set_env_target("scope-env", "@main")
    result = _install(env)
    # Neither is guessed, so it is a new patch: this line's own.
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-src_a.c"]
    for name in ("patch-one", "patch-two"):
        assert (port / "dragonfly" / name).read_text() == _diff("src/a.c")


# --------------------------------------------------------------------------
# The real overlays are the fixture that caught this
# --------------------------------------------------------------------------


def _plus_path(path):
    """The single '+++ ' path of a diff, else None (the test's own reading)."""
    import re

    found = re.findall(r"^\+\+\+ (\S+)", path.read_text(errors="replace"), re.M)
    return found[0] if len(found) == 1 else None


def _genpatch_name(path):
    """ports-mgmt/genpatch's name for a patch of ``path``."""
    return "patch-" + path.replace("_", "__").replace("/", "_")


def _reads(plan, line, port_dir):
    """dst -> src of the materialize, copy or dragonfly/ folder file that
    fills it last on ``line``. The folder is the engine helper's
    (poly-u9n4): almost no port names its patches in an op any more."""
    from dportsv3.engine.models import FolderInstall, order_ops_for_target
    from dportsv3.engine.payload import folder_installs, with_folder_installs

    out = {}
    for step in with_folder_installs(order_ops_for_target(plan.ops, line),
                                     folder_installs(port_dir, plan.ops, line)):
        if isinstance(step, FolderInstall):
            out[step.dst] = step.src
        elif (step.kind in ("file.materialize", "file.copy")
                and step.target in ("@any", line)):
            out[step.payload["dst"]] = step.payload["src"]
    return out


def _read_elsewhere(plan, line, src):
    """Does an op that is not ``line``'s alone read ``src``? (The test's
    own reading: @any, another line, or a target list naming another.)
    Or the folder: a flat file no op reads is read by every line."""
    import posixpath

    from dportsv3.engine.payload import explicit_reads, folder_scope

    if src not in explicit_reads(plan.ops) and folder_scope(src) not in (None, line):
        return True
    return any(
        op.kind == "file.materialize" and op.target != line
        and posixpath.normpath(op.payload["src"]) == src
        for op in plan.ops
    )


@pytest.mark.parametrize("naming", ["composed", "genpatch"])
def test_a_recut_lands_on_the_file_each_line_reads_on_the_real_ports(
        multi_line_ports, tmp_path, monkeypatch, naming):
    from dportsv3.engine.api import build_plan

    deltaports = tmp_path / "DeltaPorts"
    out = tmp_path / "writable" / "work" / "genpatch-out"
    out.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda e: SimpleNamespace(deltaports=deltaports,
                                  writable=tmp_path / "writable"),
    )
    monkeypatch.setattr(worker, "patch_origin_for", lambda e, o: o)
    # The whole real tree predates the job.
    monkeypatch.setattr(
        worker, "_committed_paths",
        lambda e, rels: {r for r in rels if (deltaports / r).exists()})
    monkeypatch.setattr(
        worker, "_head_text", lambda e, rel: (deltaports / rel).read_text())
    cases = 0
    try:
        for port in multi_line_ports:
            port_dir = deltaports / "ports" / port.origin
            shutil.copytree(port.overlay.parent, port_dir)
            plan = build_plan(port.overlay.read_text(), port.overlay).plan
            for line in port.lines:
                reads = _reads(plan, line, port_dir)
                headers = {}
                for dst, src in reads.items():
                    if dst.startswith("dragonfly/patch-"):
                        headers[dst] = _plus_path(port_dir / src)
                for dst, src in reads.items():
                    if not dst.startswith("dragonfly/patch-"):
                        continue
                    path = headers[dst]
                    if naming == "composed":
                        name = dst.rpartition("/")[2]
                    else:
                        if path is None or list(headers.values()).count(path) > 1:
                            continue
                        name = _genpatch_name(path)
                    for f in out.iterdir():
                        f.unlink()
                    (out / name).write_text(_diff(path or "x"))
                    worker.set_env_target("scope-env", line)
                    result = worker.install_patches(
                        "scope-env", port.origin, patches=[name])
                    # poly-7pwa.27 row 5a: a file another line reads too is
                    # never written; this line gets its own copy, named for
                    # the destination -- or nothing, when its own folder is
                    # what the other line reads.
                    expected = src
                    if _read_elsewhere(plan, line, src):
                        own = f"dragonfly/{line}/{dst.rpartition('/')[2]}"
                        expected = None if own == src else own
                    if expected is None:
                        assert result["installed"] == [], (port.origin, line, dst)
                    else:
                        assert result["installed"] == [
                            f"ports/{port.origin}/{expected}"
                        ], (port.origin, line, dst, name)
                    cases += 1
    finally:
        worker.set_env_target("scope-env", None)
    assert cases


def test_the_file_each_line_reads_is_what_apply_installs_on_the_real_ports(
        multi_line_ports, tmp_path):
    """The lane rule above, checked against the engine's own apply."""
    from dportsv3.engine.api import build_plan
    from dportsv3.engine.apply import apply_plan
    from dportsv3.engine.models import Plan
    from dportsv3.engine.payload import payload_files

    checked = 0
    for port in multi_line_ports:
        plan = build_plan(port.overlay.read_text(), port.overlay).plan
        ops = [op for op in plan.ops if op.kind == "file.materialize"]
        sentinels = tmp_path / "src" / port.origin
        for rel in ({op.payload["src"] for op in ops}
                    | set(payload_files(port.overlay.parent))):
            f = sentinels / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(rel)
        for line in port.lines:
            root = tmp_path / "out" / port.origin / line
            root.mkdir(parents=True)
            result = apply_plan(Plan(port=port.origin, ops=ops),
                                source_root=sentinels, port_root=root,
                                target=line, oracle_profile="off")
            assert result.ok, (port.origin, line)
            for dst, src in _reads(Plan(port=port.origin, ops=ops),
                                   line, sentinels).items():
                assert (root / dst).read_text() == src, (port.origin, line, dst)
                checked += 1
    assert checked


def test_no_cached_target_still_writes_over_an_existing_flat_file(env):
    """The hold rule is for an overlay that does not plan, not a missing target."""
    _overlay(env, SCOPED)
    flat = _port(env) / "dragonfly" / "patch-src_lib.rs"
    flat.parent.mkdir(parents=True)
    flat.write_text("shared\n")
    worker.set_env_target("scope-env", None)
    _genpatch(env, "patch-src_lib.rs")
    result = _install(env)
    assert result["installed"] == ["ports/devel/thing/dragonfly/patch-src_lib.rs"]
    assert "not_installed" not in result
    assert flat.read_text() != "shared\n"


# --------------------------------------------------------------------------
# poly-7pwa.27, row 5a: never over a file other build lines read
# --------------------------------------------------------------------------


def _shared(env, name="patch-configure", text="shared\n"):
    f = _port(env) / "dragonfly" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def test_a_recut_of_a_shared_patch_writes_this_lines_own_copy(env):
    """The ping-pong: a @main re-cut over the @any file broke @2026Q3, whose
    own re-cut landed on the same file and broke @main again."""
    _overlay(env, FLAT)
    shared = _shared(env)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-configure"
    ]
    assert shared.read_text() == "shared\n"
    note = result.get("scope_note") or ""
    assert "Not written over dragonfly/patch-configure" in note
    # The folder installs @main's copy after the @any op's file (poly-u9n4).
    assert "Written to @main's own folder" in note
    assert "\ntarget @main\n" not in note


def test_the_own_copy_keeps_the_destination_name(env):
    """genpatch names a re-cut after the file it changes; the copy takes
    the name the shared op installs, which is what do-patch sees."""
    _overlay(env, HEAD + (
        "target @any\n"
        "file materialize dragonfly/patch-src_ae_epoll.c"
        " -> dragonfly/patch-src_ae_epoll.c\n"
    ))
    _shared(env, "patch-src_ae_epoll.c", _diff("src/ae_epoll.c"))
    (env.out / "patch-src_ae__epoll.c").write_text(
        _diff("src/ae_epoll.c").replace("+new", "+recut"))
    worker.set_env_target("scope-env", "@2026Q3")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@2026Q3/patch-src_ae_epoll.c"
    ]
    assert "dragonfly/@2026Q3/patch-src_ae_epoll.c" in (
        result.get("scope_note") or "")


def test_once_the_own_copy_has_its_op_a_recut_replaces_it(env):
    _overlay(env, FLAT + (
        "target @main\n"
        "file materialize dragonfly/@main/patch-configure"
        " -> dragonfly/patch-configure\n"
    ))
    shared = _shared(env)
    own = _shared(env, "@main/patch-configure", "old own copy\n")
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-configure"
    ]
    assert own.read_text() != "old own copy\n"
    assert shared.read_text() == "shared\n"
    assert "No op installs" not in (result.get("scope_note") or "")
    assert "Not written over" not in (result.get("scope_note") or "")


def test_a_shared_file_this_job_wrote_is_its_own_to_replace(env):
    """Row 1: the job created the overlay and the patch, so a re-cut of it
    replaces it in place; no other line ever read it."""
    _overlay(env, FLAT)
    shared = _shared(env)
    env.state.committed = set()
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    result = _install(env)
    assert result["installed"] == ["ports/devel/thing/dragonfly/patch-configure"]
    assert shared.read_text() != "shared\n"


def test_when_git_cannot_say_nothing_shared_is_touched(env):
    """Unknown means the overlay and the files predate the job: that answer
    only ever keeps a change on this line."""
    _overlay(env, FLAT)
    shared = _shared(env)
    env.state.committed = "unknown"
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    _genpatch(env, "patch-brand_new.rs")
    result = _install(env)
    assert set(result["installed"]) == {
        "ports/devel/thing/dragonfly/@main/patch-configure",
        "ports/devel/thing/dragonfly/@main/patch-brand_new.rs",
    }
    assert shared.read_text() == "shared\n"


def test_a_target_list_reading_this_lines_folder_is_held(env):
    """No copy of a file two lines read through one op is either's alone."""
    _overlay(env, HEAD + (
        "target @2026Q3,@main\n"
        "file materialize dragonfly/@main/patch-x -> dragonfly/patch-x\n"
    ))
    both = _shared(env, "@main/patch-x", "both lines\n")
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-x")
    result = _install(env)
    assert result["installed"] == []
    assert result["not_installed"] == ["patch-x"]
    assert result["ok"] is False
    assert both.read_text() == "both lines\n"
    note = result.get("scope_note") or ""
    assert "Leave it uninstalled" in note
    assert "an operator has to give each line its own file" in note


def _composed(env, line):
    """The dragonfly/ files ``line`` composes, by name -> content."""
    from dportsv3.engine.api import apply_dsl

    dops = _port(env) / "overlay.dops"
    out = env.tmp / "out" / f"{line}-{len(list(env.tmp.glob('out/*')))}"
    out.mkdir(parents=True)
    (out / "Makefile").write_text("PORTNAME= thing\n")
    result = apply_dsl(dops.read_text(), source_path=dops, port_root=out,
                       target=line, oracle_profile="off")
    assert result.ok, [d.code for d in result.diagnostics]
    return {p.name: p.read_text() for p in (out / "dragonfly").glob("*")}


@pytest.mark.parametrize("overlay", [FLAT, SCOPED], ids=["flat", "scoped"])
def test_the_own_copy_composes_each_line_as_intended(env, overlay):
    """With no op added: @main composes its own copy, every other line keeps
    the shared file (poly-u9n4), and the overlay is untouched."""
    name = "patch-libpkg_flat.c" if overlay == SCOPED else "patch-configure"
    _overlay(env, overlay)
    for other in ("patch-configure", "patch-libpkg_flat.c", "extra-crate-1.0",
                  "@2026Q3/patch-src_lib.rs", "@main/patch-src_lib.rs"):
        if other != name:
            _shared(env, other, "other\n")
    _shared(env, name)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, name)
    _install(env)
    assert (_port(env) / "overlay.dops").read_text() == overlay
    assert _composed(env, "@main")[name] != "shared\n"
    assert _composed(env, "@2026Q3")[name] == "shared\n"


def test_an_op_in_the_lines_block_that_fills_the_name_still_needs_one(env):
    """A target list that fills the destination runs after the folder, so
    the own copy alone would lose on @main: the note gives the op, and the
    overlay with it composes @main's copy."""
    import re

    text = HEAD + (
        "target @any\n"
        "file materialize dragonfly/patch-configure -> dragonfly/patch-configure\n"
        "target @2026Q3,@main\n"
        "file materialize dragonfly/other -> dragonfly/patch-configure\n"
    )
    _overlay(env, text)
    _shared(env)
    _shared(env, "other", "both lines\n")
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-configure"
    ]
    note = result.get("scope_note") or ""
    assert "fills dragonfly/patch-configure after the folder" in note
    snippet = re.search(r"^target @main\n(?:file materialize .*\n?)+", note, re.M)
    assert snippet, note
    _overlay(env, text + snippet.group(0).rstrip("\n") + "\n")
    assert _composed(env, "@main")["patch-configure"] != "both lines\n"
    assert _composed(env, "@2026Q3")["patch-configure"] == "both lines\n"


# --- a dport is no exception (poly-7pwa.35) ---------------------------------

DPORT = 'port devel/thing\ntype dport\nreason "fixture"\ntarget @any\n'


def test_a_dport_recut_writes_this_lines_own_copy_beside_newport(env):
    """newport/ is the dport's origin and never written; its patches are in
    the sibling dragonfly/, and a re-cut there follows the same rule as for
    any port: this line's own copy, the shared file untouched."""
    _overlay(env, DPORT)
    newport = _port(env) / "newport"
    newport.mkdir()
    (newport / "Makefile").write_text("PORTNAME= thing\n")
    shared = _shared(env)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-configure"
    ]
    assert shared.read_text() == "shared\n"
    assert sorted(p.name for p in newport.iterdir()) == ["Makefile"]


def test_a_new_dport_patch_goes_to_this_lines_folder(env):
    _overlay(env, DPORT)
    (_port(env) / "newport").mkdir()
    (_port(env) / "newport" / "Makefile").write_text("PORTNAME= thing\n")
    _shared(env, "patch-old", "old\n")
    worker.set_env_target("scope-env", "@2026Q3")
    _genpatch(env, "patch-brand_new.rs")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/@2026Q3/patch-brand_new.rs"
    ]
