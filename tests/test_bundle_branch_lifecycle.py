"""Branch naming and base-branch resolution in worker.

``_resolve_bundle_base_branch`` reads the env's ``origin/HEAD``
symbolic-ref, caches per env, and falls back to ``master`` on failure.
``_branch_name_for`` / ``_verify_branch_name_for`` decide what a job's
branch is called.

The checkout/drop lifecycle this file used to cover is gone: builds no
longer take turns on branches in a shared checkout, so there is nothing
to switch onto or restore. Each job gets a worktree instead — see
``tests/test_dev_env_job_worktree.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dportsv3.agent import worker


@pytest.fixture(autouse=True)
def _clear_caches():
    worker._BUNDLE_BASE_BRANCH_CACHE.clear()
    yield
    worker._BUNDLE_BASE_BRANCH_CACHE.clear()


def _exec_recorder(scripts: dict[str, tuple[int, str, str]]):
    """Return (calls, fake_exec). ``scripts`` maps a substring of
    the shell command to (rc, stdout, stderr). The first matching
    substring wins. Unmatched commands return rc=0 with empty
    streams (safe default for "this call wasn't load-bearing").
    """
    calls: list[str] = []

    def _fake(env, *argv, **kwargs):
        cmd = argv[-1] if argv else ""
        calls.append(cmd)
        for needle, (rc, out, err) in scripts.items():
            if needle in cmd:
                return SimpleNamespace(
                    returncode=rc, stdout=out, stderr=err,
                )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return calls, _fake


# --- _resolve_bundle_base_branch -------------------------------------


def test_base_branch_reads_the_checked_out_branch(monkeypatch):
    """The authority is what ports-main actually has checked out.

    dev-env clones it with --single-branch --branch <deltaports_branch>,
    so its HEAD is the branch the env was built on.
    """
    calls, fake = _exec_recorder({
        "rev-parse": (0, "2026Q3\n", ""),
    })
    monkeypatch.setattr(worker, "_exec", fake)

    assert worker._resolve_bundle_base_branch("e1") == "2026Q3"
    # Cached on second call — no extra subprocess.
    assert worker._resolve_bundle_base_branch("e1") == "2026Q3"
    assert sum("rev-parse" in c for c in calls) == 1


def test_base_branch_reads_ports_main_not_the_symlink(monkeypatch):
    """PORTS_DIR points into a job's worktree while one runs, so plumbing
    must address the real checkout."""
    calls, fake = _exec_recorder({"rev-parse": (0, "master\n", "")})
    monkeypatch.setattr(worker, "_exec", fake)

    worker._resolve_bundle_base_branch("e1")
    cmd = " ".join(calls)
    assert worker.PORTS_MAIN_DIR in cmd
    assert f"-C {worker.PORTS_DIR}" not in cmd


def test_base_branch_refuses_when_it_cannot_be_read(monkeypatch):
    """No silent default. This is the regression: the old code sent any
    failure through '|| echo master', so every env resolved to master
    whether or not that was its branch."""
    calls, fake = _exec_recorder({"rev-parse": (128, "", "fatal: not a git repo")})
    monkeypatch.setattr(worker, "_exec", fake)

    assert worker._resolve_bundle_base_branch("e1") is None


def test_base_branch_refuses_on_detached_head(monkeypatch):
    """rev-parse prints the literal 'HEAD' when detached, and a branch name
    is required — the worktree is created with `-b <new> <base>`."""
    calls, fake = _exec_recorder({"rev-parse": (0, "HEAD\n", "")})
    monkeypatch.setattr(worker, "_exec", fake)

    assert worker._resolve_bundle_base_branch("e1") is None


def test_base_branch_refuses_on_empty_output(monkeypatch):
    calls, fake = _exec_recorder({"rev-parse": (0, "\n", "")})
    monkeypatch.setattr(worker, "_exec", fake)

    assert worker._resolve_bundle_base_branch("e1") is None


@pytest.mark.parametrize("bad", [(128, "", "boom"), (0, "HEAD\n", ""), (0, "\n", "")])
def test_failed_resolution_is_not_cached(monkeypatch, bad):
    """Caching a failure is what pinned a wrong base for a whole process.

    Detached HEAD is the case that matters: "HEAD" is a truthy string, so
    unlike an empty result it would survive in the cache and be handed back
    as a branch name on every later call.
    """
    calls, fake = _exec_recorder({"rev-parse": bad})
    monkeypatch.setattr(worker, "_exec", fake)
    assert worker._resolve_bundle_base_branch("e1") is None
    assert "e1" not in worker._BUNDLE_BASE_BRANCH_CACHE

    calls2, fake2 = _exec_recorder({"rev-parse": (0, "main\n", "")})
    monkeypatch.setattr(worker, "_exec", fake2)
    assert worker._resolve_bundle_base_branch("e1") == "main"


def test_worktree_creation_refuses_without_a_base(monkeypatch):
    """The two halves compose: no base means no worktree, and under
    poly-m7o no worktree means the job is retired rather than run."""
    monkeypatch.setattr(worker, "_resolve_bundle_base_branch", lambda env: None)
    ran = []
    monkeypatch.setattr(worker, "_exec", lambda *a, **k: ran.append(a))

    result = worker.create_job_worktree("e1", "b1", "patch")

    assert result["ok"] is False
    assert "base branch" in result["error"]
    assert ran == [], "touched the env despite having no base"


def test_base_branch_per_env_cache(monkeypatch):
    calls, fake = _exec_recorder({
        "rev-parse": (0, "master\n", ""),
    })
    monkeypatch.setattr(worker, "_exec", fake)

    worker._resolve_bundle_base_branch("env-a")
    worker._resolve_bundle_base_branch("env-b")
    # Two separate envs, two separate cache entries → two
    # subprocess invocations.
    assert sum("rev-parse" in c for c in calls) == 2


# --- checkout_bundle_branch ------------------------------------------


# --- drop_bundle_branch ----------------------------------------------


def test_branch_name_strips_job_suffix(monkeypatch):
    """Defensive: caller passes a job filename by accident.
    Strip ``.job`` from the bundle name so the branch isn't
    ``bundle/<...>.job``."""
    assert worker._branch_name_for("b-abc.job") == "bundle/b-abc"
    assert worker._branch_name_for("b-abc") == "bundle/b-abc"


# --- verify branch: checkout_verify_branch / drop_verify_branch ------


def test_verify_branch_name_is_suffixed():
    assert worker._verify_branch_name_for("b-abc") == "bundle/b-abc-verify"
    assert worker._verify_branch_name_for("b-abc.job") == "bundle/b-abc-verify"


