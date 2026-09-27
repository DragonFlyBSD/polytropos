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

import pathlib
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
    yield SimpleNamespace(name="scope-env", deltaports=deltaports, out=out, tmp=tmp_path)
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
    assert "patch-src_lib.rs" in note
    # It must say the existing op already stages it, or the agent adds a
    # second materialize op and poly-7pwa.3's collision follows.
    assert "needs NO edit" in note
    assert "collide" in note


def test_a_flat_port_is_untouched(env):
    """The common case. A redirect here would be the bug, inverted."""
    _overlay(env, FLAT)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-configure")
    result = _install(env)
    assert result["installed"] == ["ports/devel/thing/dragonfly/patch-configure"]
    assert "scope_note" not in result


def test_a_new_patch_follows_the_ports_convention_for_this_target(env):
    """No op names this file yet, but the port scopes its payload here."""
    _overlay(env, SCOPED)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-brand_new.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-brand_new.rs"
    ]
    # This one DOES need an op, and the note must say so and where.
    note = result.get("scope_note") or ""
    assert "dragonfly/@main/patch-brand_new.rs" in note
    assert "still needs an op" in note
    assert "file materialize" in note
    assert "`target @main` block" in note


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
    _genpatch(env, "patch-src_lib.rs")   # existing op -> "needs NO edit"
    _genpatch(env, "patch-brand_new.rs")  # new -> "still needs an op"
    note = _install(env).get("scope_note") or ""
    # Each statement must name which files it is about.
    assert "patch-src_lib.rs" in note and "patch-brand_new.rs" in note
    assert note.count("needs NO edit") <= 1


def test_an_op_that_renames_is_followed_by_destination_not_basename(env):
    """src basename may differ from dst; the dst is what identifies the file."""
    renaming = HEAD + (
        "target @main\n"
        "file materialize dragonfly/@main/src-name.c -> dragonfly/patch-x\n"
    )
    _overlay(env, renaming)
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-x")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/@main/patch-x"
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


def test_an_unparseable_overlay_falls_back_to_flat_and_says_so(env):
    _overlay(env, HEAD + "this is not dops at all\n")
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")
    result = _install(env)
    assert result["installed"] == [
        "ports/devel/thing/dragonfly/patch-src_lib.rs"
    ]
    assert "Could not read" in (result.get("scope_note") or "")


def test_a_port_with_no_overlay_falls_back_to_flat(env):
    worker.set_env_target("scope-env", "@main")
    _genpatch(env, "patch-src_lib.rs")
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/patch-src_lib.rs"
    ]


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
    assert _install(env)["installed"] == [
        "ports/devel/thing/dragonfly/patch-src_lib.rs"
    ]


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


# --------------------------------------------------------------------------
# The real overlays are the fixture that caught this
# --------------------------------------------------------------------------

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


@pytest.mark.skipif(DELTA is None, reason="DeltaPorts checkout not present")
@pytest.mark.parametrize(
    "origin,target,filename,expected",
    [
        # pkg's four libpkg patches are FLAT and @any. Sending a re-cut of
        # one into @main/ was the defect a layout guess produced.
        ("ports-mgmt/pkg", "@main", "patch-libpkg_scripts.c", "dragonfly"),
        ("ports-mgmt/pkg", "@2026Q3", "patch-libpkg_scripts.c", "dragonfly"),
        # ...and exactly one patch each is scoped, where 2.7.5 and 2.8.4 diverge.
        ("ports-mgmt/pkg", "@main", "patch-configure.def", "dragonfly/@main"),
        ("ports-mgmt/pkg", "@2026Q3", "patch-auto.def.c", "dragonfly/@2026Q3"),
        # rust's @any lane is the cargo crate patches; its own source
        # patches are scoped per target.
        ("lang/rust", "@main", "extra-libc-0.2.62", "dragonfly"),
        ("lang/rust", "@main", "patch-src_bootstrap_src_bin_main.rs",
         "dragonfly/@main"),
        ("lang/rust", "@2026Q3", "patch-src_bootstrap_src_bin_main.rs",
         "dragonfly/@2026Q3"),
    ],
)
def test_the_real_overlays_resolve_the_lane_a_human_would(
    origin, target, filename, expected, monkeypatch
):
    """Every one of these the first version of this fix got wrong but two."""
    monkeypatch.setattr(
        worker, "env_paths",
        lambda e: SimpleNamespace(
            deltaports=DELTA.parent, writable=pathlib.Path("/tmp")
        ),
    )
    worker.set_env_target("real-env", target)
    try:
        plan = worker._overlay_plan_for("real-env", origin)
        assert plan is not None, f"{origin} overlay did not plan"
        lane, _ = worker._dragonfly_payload_dir(plan, target, filename)
    finally:
        worker.set_env_target("real-env", None)
    assert lane == expected
