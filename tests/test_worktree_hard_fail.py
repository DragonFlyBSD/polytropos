"""No isolated worktree, no job.

Until B1 a failed checkout was soft-fail: the job carried on against the
shared tree, losing only isolation. Post-B1 the fallback is whatever
PORTS_DIR last points at — after a crashed predecessor, another job's
worktree, with commits landing on that job's branch. See poly-m7o.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import dportsv3.db.schema as schema
from dportsv3.agent import lifecycle as L
from dportsv3.agent import runner as runner_mod


@pytest.fixture
def conn(monkeypatch) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    schema.init_db(c)
    monkeypatch.setattr(runner_mod, "_state_db_conn", c, raising=False)
    return c


# --- the lifecycle event -------------------------------------------------

def test_transitions_to_dead_from_patching(conn) -> None:
    """PATCH_START fires before the checkout, so PATCHING is the state
    the job is actually in when this fires."""
    L.apply(conn, "j1", L.JobEvent.HOOK_ENQUEUED)
    L.apply(conn, "j1", L.JobEvent.CLAIM)
    L.apply(conn, "j1", L.JobEvent.PATCH_START)
    assert L.apply(conn, "j1", L.JobEvent.WORKTREE_UNAVAILABLE) is L.JobState.DEAD
    assert conn.execute(
        "SELECT state, retire_reason FROM jobs WHERE job_id='j1'"
    ).fetchone() == ("dead", "worktree_unavailable")


def test_reason_does_not_pollute_decision_counters(conn) -> None:
    """decision.py counts failed patch attempts by retire_reason. A
    machinery failure must not look like the agent giving up, or it
    suppresses future work on the port."""
    counted = ("patch_gave_up", "patch_budget_exhausted")
    assert L._TERMINAL_REASONS[L.JobEvent.WORKTREE_UNAVAILABLE] not in counted


def test_reason_does_not_pollute_the_health_signal(conn) -> None:
    """env_broken drives the health contract and decision.py's
    defer-until-healthy. A git failure is not a broken env."""
    assert L._TERMINAL_REASONS[L.JobEvent.WORKTREE_UNAVAILABLE] != "env_broken"


# --- the helper's return contract ---------------------------------------

def _checkout(monkeypatch, tmp_path, *, result=None, raises=False):
    """Drive the helper with a stubbed worktree creation.

    Patches the attribute on the real module rather than swapping the
    module into sys.modules — the latter leaked into other tests.
    """
    from dportsv3.agent import worker

    calls = []

    def _create(env, bundle_id, kind="patch"):
        calls.append((env, bundle_id, kind))
        if raises:
            raise RuntimeError("git exploded")
        return result

    monkeypatch.setattr(worker, "create_job_worktree", _create)
    return runner_mod._checkout_bundle_branch_for_job(
        queue_root=tmp_path, job_id="j1.job", env="env1",
        bundle_id="b1", job_type="patch",
    ), calls


def test_no_env_or_bundle_is_a_no_op_success(tmp_path) -> None:
    assert runner_mod._checkout_bundle_branch_for_job(
        queue_root=tmp_path, job_id="j1.job", env=None,
        bundle_id="b1", job_type="patch") is True
    assert runner_mod._checkout_bundle_branch_for_job(
        queue_root=tmp_path, job_id="j1.job", env="env1",
        bundle_id=None, job_type="patch") is True


def test_returns_true_when_the_worktree_is_created(monkeypatch, tmp_path) -> None:
    ok, calls = _checkout(monkeypatch, tmp_path,
                          result={"ok": True, "created": True, "branch": "bundle/b1"})
    assert ok is True
    assert calls == [("env1", "b1", "patch")]


def test_returns_false_when_creation_reports_failure(monkeypatch, tmp_path) -> None:
    ok, _ = _checkout(monkeypatch, tmp_path,
                      result={"ok": False, "error": "worktree add failed"})
    assert ok is False


def test_returns_false_when_creation_raises(monkeypatch, tmp_path) -> None:
    ok, _ = _checkout(monkeypatch, tmp_path, raises=True)
    assert ok is False


# --- the call site -------------------------------------------------------

def test_patch_job_retires_instead_of_running(conn, tmp_path, monkeypatch) -> None:
    """The whole point: a job with no isolated tree must not reach the
    orchestrator."""
    for sub in ("pending", "inflight", "done", "failed"):
        (tmp_path / sub).mkdir()
    job_path = tmp_path / "inflight" / "j1.job"
    job_path.write_text("{}")
    sibling = tmp_path / "inflight" / "j2.job"
    sibling.write_text("{}")

    for jid in ("j1.job", "j2.job"):
        L.apply(conn, jid, L.JobEvent.HOOK_ENQUEUED)
        L.apply(conn, jid, L.JobEvent.CLAIM)
        L.apply(conn, jid, L.JobEvent.PATCH_START)

    monkeypatch.setattr(runner_mod, "_maybe_skip_locked_origin",
                        lambda **kw: None, raising=False)
    monkeypatch.setattr(runner_mod, "_maybe_skip_muted_issue",
                        lambda **kw: None, raising=False)
    monkeypatch.setattr(runner_mod, "_checkout_bundle_branch_for_job",
                        lambda **kw: False, raising=False)

    ran = []
    monkeypatch.setattr(runner_mod, "resolve_env",
                        lambda job: "env1", raising=False)

    class _Boom:
        def __init__(self, *a, **k): ran.append("orchestrator")

    monkeypatch.setattr("dportsv3.agent.step.Orchestrator", _Boom, raising=False)

    success, status = runner_mod.process_patch_job(
        tmp_path, job_path, [sibling],
        {"origin": "x11/foo", "bundle_id": "b1", "dev_env": "env1"},
        None, None,
    )

    assert (success, status) == (False, "worktree_unavailable")
    assert ran == [], "the orchestrator ran without an isolated tree"
    for jid in ("j1.job", "j2.job"):
        assert conn.execute(
            "SELECT state, retire_reason FROM jobs WHERE job_id=?", (jid,)
        ).fetchone() == ("dead", "worktree_unavailable")
