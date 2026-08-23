"""B3: a crashed runner self-cleans on restart.

Both sweeps are decidable only because the runner holds an exclusive lock
(poly-bg1) and has claimed nothing yet — so nothing they touch can be
live. See poly-tr8.
"""
from __future__ import annotations

import pytest

from dportsv3.agent import runner as runner_mod
from dportsv3.agent import worker as worker_mod


@pytest.fixture
def queue_root(tmp_path):
    for sub in ("pending", "inflight", "done", "failed"):
        (tmp_path / sub).mkdir()
    return tmp_path


# --- stranded inflight files --------------------------------------------

def test_moves_stranded_jobs_to_failed(queue_root) -> None:
    (queue_root / "inflight" / "a.job").write_text('{"origin":"x11/a"}')
    (queue_root / "inflight" / "b.job").write_text('{"origin":"x11/b"}')

    moved = runner_mod.sweep_stranded_inflight(queue_root)

    assert moved == ["a.job", "b.job"]
    assert not list((queue_root / "inflight").glob("*.job"))
    assert {p.name for p in (queue_root / "failed").glob("*.job")} == {"a.job", "b.job"}


def test_preserves_the_payload(queue_root) -> None:
    """Moved, not deleted — the payload is the only record of the job."""
    (queue_root / "inflight" / "a.job").write_text('{"origin":"x11/a"}')
    runner_mod.sweep_stranded_inflight(queue_root)
    assert (queue_root / "failed" / "a.job").read_text() == '{"origin":"x11/a"}'


def test_writes_an_error_note(queue_root) -> None:
    (queue_root / "inflight" / "a.job").write_text("{}")
    runner_mod.sweep_stranded_inflight(queue_root)
    note = queue_root / "failed" / "a.job.error"
    assert note.exists() and "stranded" in note.read_text()


def test_leaves_pending_alone(queue_root) -> None:
    """Queued work is claimable and must survive a restart untouched."""
    (queue_root / "pending" / "queued.job").write_text("{}")
    assert runner_mod.sweep_stranded_inflight(queue_root) == []
    assert (queue_root / "pending" / "queued.job").exists()


def test_no_inflight_dir_is_not_an_error(tmp_path) -> None:
    assert runner_mod.sweep_stranded_inflight(tmp_path) == []


# --- stale worktrees ----------------------------------------------------

def test_reports_removed_worktrees(queue_root, monkeypatch) -> None:
    monkeypatch.setattr(
        worker_mod, "sweep_job_worktrees",
        lambda env, keep=(): {"ok": True, "removed": ["job-patch-a", "job-verify-b"]})
    assert runner_mod.sweep_stale_worktrees(queue_root, "env1") == [
        "job-patch-a", "job-verify-b"]


def test_no_env_means_no_sweep(queue_root, monkeypatch) -> None:
    """Never rm -rf on a guess about which env we own."""
    called = []
    monkeypatch.setattr(worker_mod, "sweep_job_worktrees",
                        lambda env, keep=(): called.append(env) or {"ok": True})
    assert runner_mod.sweep_stale_worktrees(queue_root, "") == []
    assert called == []


def test_failure_is_reported_not_raised(queue_root, monkeypatch) -> None:
    monkeypatch.setattr(
        worker_mod, "sweep_job_worktrees",
        lambda env, keep=(): {"ok": False, "error": "link restore failed"})
    assert runner_mod.sweep_stale_worktrees(queue_root, "env1") == []


def test_exception_is_contained(queue_root, monkeypatch) -> None:
    """A broken sweep must not stop the runner from starting."""
    def _boom(env, keep=()):
        raise RuntimeError("chroot gone")
    monkeypatch.setattr(worker_mod, "sweep_job_worktrees", _boom)
    assert runner_mod.sweep_stale_worktrees(queue_root, "env1") == []


# --- the shell the sweep runs -------------------------------------------

def test_sweep_restores_the_link_before_removing(monkeypatch) -> None:
    """The link may point at a directory about to be removed, so it has to
    move first or it is left dangling."""
    order = []

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(worker_mod, "_point_ports_link",
                        lambda env, target: order.append(f"link:{target}") or _P())
    monkeypatch.setattr(worker_mod, "_exec",
                        lambda *a, **k: order.append("sweep") or _P())
    worker_mod.sweep_job_worktrees("env1")
    assert order == ["link:ports-main", "sweep"]


def test_sweep_aborts_when_the_link_cannot_be_restored(monkeypatch) -> None:
    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "read-only"

    ran = []
    monkeypatch.setattr(worker_mod, "_point_ports_link", lambda env, target: _Fail())
    monkeypatch.setattr(worker_mod, "_exec", lambda *a, **k: ran.append("sweep"))
    result = worker_mod.sweep_job_worktrees("env1")
    assert result["ok"] is False
    assert ran == [], "removed worktrees with the link still pointing into them"


def test_keep_list_reaches_the_script_quoted(monkeypatch) -> None:
    seen = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(worker_mod, "_point_ports_link", lambda env, target: _P())
    monkeypatch.setattr(worker_mod, "_exec",
                        lambda *a, **k: seen.update(script=a[3]) or _P())
    worker_mod.sweep_job_worktrees("env1", keep=("job-patch-live", "odd name"))

    script = seen["script"]
    # shlex.quote leaves a shell-safe name bare and quotes one that isn't,
    # which is the behaviour worth pinning — not a particular spelling.
    assert "job-patch-live" in script
    assert "'odd name'" in script
    # the rm stays inside the job- namespace
    assert "for d in job-*" in script
    assert 'rm -rf "/work/$d"' in script


def test_sweep_script_removes_only_stale_worktrees(tmp_path) -> None:
    """Run the script the sweep actually builds against real directories.

    Everything else here stubs the chroot. This one executes the generated
    shell, because the risk in this change is the shell — an over-broad
    glob or a keep-list that silently fails to match is an rm -rf on a live
    tree, and no amount of asserting on the string catches that.
    """
    import subprocess
    from unittest.mock import patch

    root = tmp_path / "work"
    root.mkdir()
    for name in ("job-patch-a", "job-verify-b", "job-patch-live",
                 "ports-main", "DeltaPorts"):
        (root / name).mkdir()
    (root / "job-patch-a" / "marker").write_text("x")

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    seen = {}
    with patch.object(worker_mod, "_point_ports_link", lambda e, t: _P()), \
         patch.object(worker_mod, "_exec",
                      lambda *a, **k: seen.update(s=a[3]) or _P()):
        worker_mod.sweep_job_worktrees("env1", keep=("job-patch-live",))

    # Retarget the chroot-absolute paths at the tmp tree; git calls become
    # no-ops since there is no repo here.
    script = (seen["s"]
              .replace("cd /work", f"cd {root}")
              .replace('"/work/$d"', f'"{root}/$d"')
              .replace("git -C /work/ports-main worktree prune", "true")
              .replace("git -C /work/ports-main worktree remove --force", "true"))

    p = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True)

    assert p.returncode == 0, p.stderr
    removed = [l.split(" ", 1)[1] for l in p.stdout.splitlines()
               if l.startswith("removed ")]
    assert removed == ["job-patch-a", "job-verify-b"]
    assert sorted(x.name for x in root.iterdir()) == [
        "DeltaPorts", "job-patch-live", "ports-main"]
