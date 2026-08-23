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


def test_base_branch_reads_origin_head(monkeypatch):
    calls, fake = _exec_recorder({
        "symbolic-ref": (0, "origin/main\n", ""),
    })
    monkeypatch.setattr(worker, "_exec", fake)

    assert worker._resolve_bundle_base_branch("e1") == "main"
    # Cached on second call — no extra subprocess.
    assert worker._resolve_bundle_base_branch("e1") == "main"
    assert sum("symbolic-ref" in c for c in calls) == 1


def test_base_branch_fallback_to_master(monkeypatch):
    """When the symbolic-ref isn't set, the shell command's
    ``|| echo master`` fallback fires and we get master."""
    calls, fake = _exec_recorder({
        "symbolic-ref": (0, "master\n", ""),  # echo master path
    })
    monkeypatch.setattr(worker, "_exec", fake)

    # The wrapper command echoes "master" when symbolic-ref fails;
    # the function should pass that through after the origin/ strip.
    assert worker._resolve_bundle_base_branch("e1") == "master"


def test_base_branch_per_env_cache(monkeypatch):
    calls, fake = _exec_recorder({
        "symbolic-ref": (0, "origin/main\n", ""),
    })
    monkeypatch.setattr(worker, "_exec", fake)

    worker._resolve_bundle_base_branch("env-a")
    worker._resolve_bundle_base_branch("env-b")
    # Two separate envs, two separate cache entries → two
    # subprocess invocations.
    assert sum("symbolic-ref" in c for c in calls) == 2


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


