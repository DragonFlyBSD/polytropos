"""B1: the ports tree an env exposes is a symlink, so a job can swap the
tree underneath it without anything learning a second name for it."""

from __future__ import annotations

import pytest


def test_ports_link_is_relative(tmp_path):
    """It is followed from inside the chroot (/work/...) and from the host
    (<writable>/work/...). An absolute target resolves under only one."""
    from dports_dev_env.builder import link_ports_tree
    from dports_dev_env.layout import PORTS_RELATIVE

    (tmp_path / "work").mkdir()
    link_ports_tree(tmp_path)
    link = tmp_path / PORTS_RELATIVE

    assert link.is_symlink()
    target = link.readlink()
    assert not target.is_absolute(), f"target must be relative, got {target}"
    assert str(target) == "ports-main"


def test_ports_link_resolves_under_a_different_prefix(tmp_path):
    """The whole point: move the tree, the link still resolves."""
    from dports_dev_env.builder import link_ports_tree
    from dports_dev_env.layout import PORTS_MAIN_RELATIVE, PORTS_RELATIVE

    root = tmp_path / "a"
    (root / PORTS_MAIN_RELATIVE / "ports").mkdir(parents=True)
    (root / PORTS_MAIN_RELATIVE / "ports" / "marker").write_text("here")
    link_ports_tree(root)
    assert (root / PORTS_RELATIVE / "ports" / "marker").read_text() == "here"

    moved = tmp_path / "b"
    root.rename(moved)
    assert (moved / PORTS_RELATIVE / "ports" / "marker").read_text() == "here"


def test_ports_link_is_repointable(tmp_path):
    """A job repoints it to its worktree and back at job end."""
    from dports_dev_env.builder import link_ports_tree
    from dports_dev_env.layout import PORTS_RELATIVE

    (tmp_path / "work" / "job-abc").mkdir(parents=True)
    link_ports_tree(tmp_path)
    link_ports_tree(tmp_path, "job-abc")

    assert (tmp_path / PORTS_RELATIVE).readlink().name == "job-abc"

    link_ports_tree(tmp_path)
    assert (tmp_path / PORTS_RELATIVE).readlink().name == "ports-main"


def test_plumbing_targets_the_real_checkout_not_the_link():
    """Fetching or resetting through the symlink would land in whichever tree
    a job currently holds."""
    from dports_dev_env.layout import PORTS_MAIN_RELATIVE, PORTS_RELATIVE
    from dports_dev_env.update import ENV_REPOS

    targets = {rel for _, rel in ENV_REPOS}
    assert PORTS_MAIN_RELATIVE in targets
    assert PORTS_RELATIVE not in targets


def capture_exec(monkeypatch):
    """Record the shell commands worker would run in the chroot."""
    import subprocess

    from dportsv3.agent import worker

    calls: list[str] = []

    def fake_exec(env, *argv, **kw):
        calls.append(argv[-1] if argv else "")
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    monkeypatch.setattr(worker, "_resolve_bundle_base_branch", lambda env: "master")
    return calls


def test_worker_layout_agrees_with_dev_env():
    """worker spells the chroot paths itself rather than importing dev-env
    (module docstring: dev-env is driven through its CLI). Pin them together
    so the two copies cannot drift apart silently."""
    from dports_dev_env import layout
    from dportsv3.agent import worker

    assert worker.PORTS_DIR == layout.PORTS_DIR
    assert worker.PORTS_MAIN_DIR == layout.PORTS_MAIN_DIR


def test_worktree_name_is_a_relative_sibling(monkeypatch):
    """The symlink target must be relative, so the worktree has to sit
    directly under /work beside the main checkout."""
    from dportsv3.agent import worker

    name = worker._job_worktree_name("abc123", "patch")
    assert name == "job-patch-abc123"
    assert "/" not in name

    assert worker._job_worktree_name("a/b c", "verify") == "job-verify-a_b_c"
    assert worker._job_worktree_name("abc.job", "patch") == "job-patch-abc"


def test_patch_worktree_reuses_the_bundle_branch(monkeypatch):
    """A bundle's convert->patch chain keeps its commits; -B would discard
    them."""
    calls = capture_exec(monkeypatch)
    from dportsv3.agent import worker

    r = worker.create_job_worktree("e", "abc", "patch")

    assert r["ok"] and r["branch"] == "bundle/abc"
    add = [c for c in calls if "worktree add" in c][0]
    assert "-b 'bundle/abc'" in add or "-b bundle/abc" in add
    assert "-B" not in add


def test_verify_worktree_is_recut_from_base(monkeypatch):
    """Verify replays the complete changes.diff on clean base and must not
    inherit the agent's commits."""
    calls = capture_exec(monkeypatch)
    from dportsv3.agent import worker

    r = worker.create_job_worktree("abc", "abc", "verify")

    assert r["branch"] == "bundle/abc-verify"
    add = [c for c in calls if "worktree add" in c][0]
    assert "-B" in add


def test_create_points_the_link_at_the_worktree(monkeypatch):
    calls = capture_exec(monkeypatch)
    from dportsv3.agent import worker

    worker.create_job_worktree("e", "abc", "patch")

    link = [c for c in calls if "ln -s" in c][-1]
    assert "ln -s job-patch-abc" in link
    assert "ln -s /work/job-patch-abc" not in link, "target must be relative"


def test_destroy_restores_the_link_before_removing(monkeypatch):
    """Nothing may be left addressing a directory being removed."""
    calls = capture_exec(monkeypatch)
    from dportsv3.agent import worker

    worker.destroy_job_worktree("e", "abc", "patch")

    link_at = next(i for i, c in enumerate(calls) if "ln -s ports-main" in c)
    remove_at = next(i for i, c in enumerate(calls) if "worktree remove" in c)
    assert link_at < remove_at


def test_destroy_keeps_the_bundle_branch_by_default(monkeypatch):
    """The bundle branch carries commits a later job in the chain wants;
    the throwaway verify branch does not."""
    calls = capture_exec(monkeypatch)
    from dportsv3.agent import worker

    worker.destroy_job_worktree("e", "abc", "patch")
    assert not any("branch -D" in c for c in calls)

    calls.clear()
    worker.destroy_job_worktree("e", "abc", "verify", drop_branch=True)
    assert any("branch -D" in c for c in calls)


def test_dirty_check_refuses_to_guess_when_git_cannot_answer(tmp_path, monkeypatch):
    """It used to return [] on any git failure, which reads as "clean" — and
    the pre-replay caller refuses the replay precisely because a dirty tree
    makes the verify verdict meaningless. Answering "clean" when we do not
    know inverts that check."""
    import subprocess

    from dports_dev_env import cli
    from dports_dev_env.errors import CommandError

    class _Runner:
        def __init__(self, root_dir):
            pass

        def run(self, argv, **kw):
            return subprocess.CompletedProcess(argv, 128, "", "not a git repository")

    monkeypatch.setattr("dports_dev_env.chroot.ChrootRunner", _Runner)

    with pytest.raises(CommandError, match="could not determine whether"):
        cli._port_dirty_paths(tmp_path, "devel/foo")
