"""delete_file drops a payload file, guarded like put_file (poly-u9n4.4).

Once the dragonfly/ folder is the install, removing a patch's op no longer
drops the patch: a file no op reads is installed on every line its folder
names. So the agent deletes the file, and the runner no longer does it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dportsv3.agent import tools, worker

HEAD = 'port devel/thing\ntype port\nreason "fixture"\n'
PORT = "/work/DeltaPorts/ports/devel/thing"
OVERLAY = HEAD + (
    "target @any\n"
    "file materialize dragonfly/patch-a -> dragonfly/patch-a\n"
    "target @main\n"
    "file materialize dragonfly/patch-c -> dragonfly/patch-c\n"
)
STARTED = ("dragonfly/patch-a", "dragonfly/patch-c", "dragonfly/patch-flat",
           "dragonfly/@main/patch-own", "dragonfly/@2026Q3/patch-q3",
           "diffs/Makefile.diff")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A port on @main whose job started with OVERLAY and STARTED."""
    deltaports = tmp_path / "writable" / "work" / "DeltaPorts"
    port = deltaports / "ports" / "devel" / "thing"
    for rel in STARTED:
        (port / rel).parent.mkdir(parents=True, exist_ok=True)
        (port / rel).write_text("patch\n")
    (port / "overlay.dops").write_text(OVERLAY)
    monkeypatch.setattr(worker, "env_paths", lambda e: SimpleNamespace(
        deltaports=deltaports, writable=tmp_path / "writable"))
    state = SimpleNamespace(head=OVERLAY, committed={
        f"ports/devel/thing/{rel}" for rel in STARTED})
    monkeypatch.setattr(worker, "_committed_paths",
                        lambda e, rels: {r for r in rels if r in state.committed})
    monkeypatch.setattr(worker, "_head_text", lambda e, rel: state.head)
    worker.set_env_target("delete-env", "@main")
    yield SimpleNamespace(name="delete-env", port=port, state=state)
    worker.set_env_target("delete-env", None)


def _delete(env, rel):
    return worker.delete_file(env.name, f"{PORT}/{rel}")


def test_this_lines_own_copy_is_deleted(env):
    assert _delete(env, "dragonfly/@main/patch-own") == {
        "ok": True, "path": f"{PORT}/dragonfly/@main/patch-own",
        "deleted": True}
    assert not (env.port / "dragonfly/@main").exists()  # left empty, gone
    assert (env.port / "dragonfly").is_dir()


@pytest.mark.parametrize("rel,who", [
    ("dragonfly/patch-flat", "`@any`"),        # the folder: every line
    ("dragonfly/patch-a", "`@any`"),           # an @any op
    ("dragonfly/@2026Q3/patch-q3", "`@2026Q3`"),  # another line's folder
])
def test_a_file_another_line_read_is_refused_with_file_remove(env, rel, who):
    result = _delete(env, rel)
    assert result["blocked_by"] == "shared_payload"
    assert who in result["error"]
    if "@2026Q3" not in rel:
        assert "`file remove dragonfly/" in result["error"]
        assert "`target @main` block" in result["error"]
    else:  # never reaches @main: nothing to drop here
        assert "does not reach `@main` at all" in result["error"]
        assert "file remove" not in result["error"]
    assert (env.port / rel).is_file()


def test_a_file_an_op_still_reads_waits_for_the_op(env):
    """@main's own op reads it: deleting first would fail the next compose."""
    result = _delete(env, "dragonfly/patch-c")
    assert result["blocked_by"] == "op_reads_it"
    assert (env.port / "dragonfly/patch-c").is_file()


def test_once_its_op_is_gone_the_file_is_this_lines_to_delete(env):
    """The job's HEAD had only @main read it, so deleting it changes no
    other line -- even though, with its op removed, the working tree's
    folder would now install it everywhere."""
    (env.port / "overlay.dops").write_text(OVERLAY.replace(
        "file materialize dragonfly/patch-c -> dragonfly/patch-c\n", ""))
    assert _delete(env, "dragonfly/patch-c")["ok"] is True
    assert not (env.port / "dragonfly/patch-c").exists()


def test_a_flat_file_this_job_wrote_is_its_own(env):
    (env.port / "dragonfly/patch-new").write_text("patch\n")
    assert _delete(env, "dragonfly/patch-new")["ok"] is True


@pytest.mark.parametrize("path", [
    f"{PORT}/overlay.dops",
    f"{PORT}/Makefile",
    "/work/DPorts/devel/thing/dragonfly/patch-a",
])
def test_only_payload_lanes_can_be_deleted(env, path):
    result = worker.delete_file(env.name, path)
    assert result["ok"] is False
    assert "only a file under" in result["error"]


def test_a_missing_file_says_so(env):
    result = _delete(env, "dragonfly/@main/patch-gone")
    assert result["ok"] is False and "no such file" in result["error"]


def test_the_agent_has_the_tool():
    assert "delete_file" in tools.names()
    spec = next(s for s in tools.schemas({"delete_file"}))
    assert spec["function"]["parameters"]["required"] == ["path"]
