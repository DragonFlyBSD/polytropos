"""put_file and edit_file never overwrite a payload file another line reads.

poly-7pwa.29. The env builds one line, so a ``dragonfly/`` or ``diffs/``
file that an @any op or another line's op reads must not change: nothing
here can show the change is right for the others (poly-7pwa.27, row 5a).
install_patches already writes the line's own copy; a hand-written patch
through either write tool used to land on the shared file.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dportsv3.agent import worker

HEAD = 'port devel/thing\ntype port\nreason "fixture"\n'
PORT = "/work/DeltaPorts/ports/devel/thing"
SHARED = HEAD + (
    "target @any\n"
    "file materialize dragonfly/patch-a -> dragonfly/patch-a\n"
    "patch apply diffs/Makefile.diff\n"
    "target @main\n"
    "file materialize dragonfly/@main/patch-b -> dragonfly/patch-b\n"
    "file materialize dragonfly/patch-c -> dragonfly/patch-c\n"
)
PATCH = "--- a/x.c\n+++ b/x.c\n@@ -1,1 +1,1 @@\n-old\n+new\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A port on @main whose job started with SHARED and its payload."""
    deltaports = tmp_path / "writable" / "work" / "DeltaPorts"
    port = deltaports / "ports" / "devel" / "thing"
    for rel in ("dragonfly/patch-a", "dragonfly/@main/patch-b",
                "dragonfly/patch-c", "diffs/Makefile.diff"):
        (port / rel).parent.mkdir(parents=True, exist_ok=True)
        (port / rel).write_text(PATCH)
    (port / "overlay.dops").write_text(SHARED)
    monkeypatch.setattr(worker, "env_paths", lambda e: SimpleNamespace(
        deltaports=deltaports, writable=tmp_path / "writable"))
    state = SimpleNamespace(committed="disk", head=SHARED, extra=set())
    started = {f"ports/devel/thing/{p.relative_to(port)}"
               for p in port.rglob("*") if p.is_file()}

    def committed_paths(e, rels):
        if state.committed is None:
            return None
        return {r for r in rels if r in started or r in state.extra}

    monkeypatch.setattr(worker, "_committed_paths", committed_paths)
    monkeypatch.setattr(worker, "_head_text", lambda e, rel: state.head)
    worker.set_env_target("payload-env", "@main")
    yield SimpleNamespace(name="payload-env", port=port, state=state)
    worker.set_env_target("payload-env", None)


def _put(env, rel, content=PATCH.replace("new", "newer")):
    return worker.put_file(env.name, f"{PORT}/{rel}", content)


def _edit(env, rel):
    return worker.edit_file(env.name, f"{PORT}/{rel}", "+new", "+newer")


@pytest.mark.parametrize("write", [_put, _edit], ids=["put_file", "edit_file"])
def test_a_patch_every_line_reads_is_not_overwritten(env, write):
    result = write(env, "dragonfly/patch-a")
    assert result["blocked_by"] == "shared_payload"
    assert "`@any`" in result["error"]
    # This line's own copy, which the folder installs after the shared
    # file: no op to add (poly-u9n4).
    assert f"{PORT}/dragonfly/@main/patch-a" in result["error"]
    assert "overlay.dops needs no edit" in result["error"]
    assert "file materialize" not in result["error"]
    assert (env.port / "dragonfly/patch-a").read_text() == PATCH


@pytest.mark.parametrize("rel", [
    "dragonfly/@main/patch-b",   # this line's own folder
    "dragonfly/patch-c",         # read only by this line's block
    "dragonfly/@main/patch-new",  # the job writes it, in its own folder
    "Makefile.local",            # not a payload lane
], ids=["own-folder", "only-this-line", "new-file", "outside-the-lanes"])
def test_what_only_this_line_reads_can_be_written(env, rel):
    assert _put(env, rel).get("ok", True) is not False


def test_a_shared_file_the_job_wrote_is_its_own(env):
    """Not in the job's HEAD: written by this job, so no line built it."""
    env.port.joinpath("overlay.dops").write_text(
        SHARED + "target @any\n"
        "file materialize dragonfly/patch-z -> dragonfly/patch-z\n")
    assert worker.put_file(env.name, f"{PORT}/dragonfly/patch-z",
                           PATCH).get("ok", True) is not False
    assert _edit(env, "dragonfly/patch-z").get("ok", True) is not False


def test_a_new_flat_file_on_a_port_with_an_overlay_is_refused(env):
    """poly-u9n4: the folder is the install, so a new flat file would reach
    every line, and this port's new work is this line's alone (.27 row 2)."""
    result = _put(env, "dragonfly/patch-new")
    assert result["blocked_by"] == "flat_payload"
    assert f"{PORT}/dragonfly/@main/patch-new" in result["error"]
    assert not (env.port / "dragonfly/patch-new").exists()


def test_a_new_flat_file_an_op_reads_is_the_ops_to_scope(env):
    env.port.joinpath("overlay.dops").write_text(
        SHARED + "target @main\n"
        "file materialize dragonfly/patch-new -> dragonfly/patch-new\n")
    assert _put(env, "dragonfly/patch-new").get("ok", True) is not False


def _started_with(env, monkeypatch, overlay, files):
    """The job's HEAD: ``overlay`` and the port-relative ``files``."""
    prefix = "ports/devel/thing/"
    head = {prefix + "overlay.dops", *(prefix + f for f in files)}

    def committed_paths(e, rels):
        return {r for r in rels
                if r in head or any(h.startswith(r + "/") for h in head)}

    monkeypatch.setattr(worker, "_committed_paths", committed_paths)
    env.state.head = overlay
    env.port.joinpath("overlay.dops").write_text(overlay)


def test_a_fresh_port_takes_a_new_flat_file(env, monkeypatch):
    """Row 1: no ops and no dragonfly/ files when the job started."""
    _started_with(env, monkeypatch, HEAD + "target @any\n", [])
    assert _put(env, "dragonfly/patch-new").get("ok", True) is not False


def test_a_header_with_payload_is_not_fresh(env, monkeypatch):
    """Q4: a header-only overlay whose folder already installs patches on
    every line has been built with, so new work stays on this line."""
    _started_with(env, monkeypatch, HEAD + "target @any\n",
                  ["dragonfly/patch-a"])
    assert _put(env, "dragonfly/patch-new")["blocked_by"] == "flat_payload"


def test_a_diff_under_patch_apply_cannot_be_copied_around(env):
    """A second copy cannot replace a `patch apply` on one line."""
    result = _edit(env, "diffs/Makefile.diff")
    assert result["blocked_by"] == "shared_payload"
    assert "`patch apply`" in result["error"]
    assert "later op in the `target @main` block" in result["error"]


def test_a_list_reading_this_lines_folder_has_no_copy_of_its_own(env):
    text = HEAD + ("target @2026Q3,@main\n"
                   "file materialize dragonfly/@main/patch-b -> dragonfly/patch-b\n")
    env.port.joinpath("overlay.dops").write_text(text)
    env.state.head = text
    result = _put(env, "dragonfly/@main/patch-b")
    assert "`@2026Q3`" in result["error"]
    assert "no copy of it is `@main`'s alone" in result["error"]


def test_editing_the_overlay_first_does_not_open_the_file(env):
    """The job's HEAD still has the @any op, so every other line reads it."""
    env.port.joinpath("overlay.dops").write_text(
        SHARED.replace("file materialize dragonfly/patch-a -> dragonfly/patch-a\n", ""))
    assert _put(env, "dragonfly/patch-a")["blocked_by"] == "shared_payload"


def test_when_git_cannot_say_the_file_counts_as_committed(env):
    env.state.committed = None
    assert _put(env, "dragonfly/patch-a")["blocked_by"] == "shared_payload"


def test_install_patches_counts_a_patch_apply_reader_too():
    from dportsv3.engine.api import build_plan

    plan = build_plan(SHARED, None).plan
    assert worker._read_on_another_line(plan, "@main", "diffs/Makefile.diff")
    assert not worker._read_on_another_line(plan, "@main", "dragonfly/patch-c")


def test_another_lines_copy_points_at_this_lines_own(env):
    """lang/rust's shape: @2026Q3 and @main each read their own copy. A
    @main job editing @2026Q3's is sent to @main's, not told to add an op
    @main already has (which would leave a dead op)."""
    text = HEAD + (
        "target @2026Q3\n"
        "file materialize dragonfly/@2026Q3/patch-b -> dragonfly/patch-b\n"
        "target @main\n"
        "file materialize dragonfly/@main/patch-b -> dragonfly/patch-b\n")
    (env.port / "dragonfly/@2026Q3").mkdir()
    (env.port / "dragonfly/@2026Q3/patch-b").write_text(PATCH)
    env.port.joinpath("overlay.dops").write_text(text)
    env.state.head = text
    env.state.extra.add("ports/devel/thing/dragonfly/@2026Q3/patch-b")
    result = _put(env, "dragonfly/@2026Q3/patch-b")
    assert "`@2026Q3`" in result["error"]
    assert f"Edit {PORT}/dragonfly/@main/patch-b instead" in result["error"]
    assert "overlay.dops needs no edit" in result["error"]


def test_an_override_this_line_already_has_is_where_the_edit_goes(env):
    text = SHARED + ("target @main\n"
                     "file materialize dragonfly/@main/patch-a -> dragonfly/patch-a\n")
    env.port.joinpath("overlay.dops").write_text(text)
    env.state.head = text
    result = _edit(env, "dragonfly/patch-a")
    assert f"Edit {PORT}/dragonfly/@main/patch-a instead" in result["error"]


def test_a_diffs_file_that_is_materialized_gets_the_copy_advice(env):
    text = SHARED + "target @any\nfile materialize diffs/x.diff -> files/x.diff\n"
    (env.port / "diffs/x.diff").write_text(PATCH)
    env.port.joinpath("overlay.dops").write_text(text)
    env.state.head = text
    env.state.extra.add("ports/devel/thing/diffs/x.diff")
    result = _put(env, "diffs/x.diff")
    assert "`patch apply`" not in result["error"]
    assert "`file materialize diffs/@main/x.diff -> files/x.diff`" in result["error"]


def test_a_dotted_path_is_the_same_file(env):
    result = worker.put_file(
        env.name, f"{PORT}/./dragonfly//patch-a", PATCH.replace("new", "x"))
    assert result["blocked_by"] == "shared_payload"

