"""End-to-end orchestrator parity test.

Phase 5 Step 5. Walks full job flows through ``process_job`` with
stubbed LLM + worker boundaries, asserting that the orchestrator-
driven path produces the same lifecycle event sequences the
hand-coded ``_completion_events_for`` produced pre-Phase-5.

Covers:
- Patch happy path: success → PATCH_OK + VERIFY_OK → DONE.
- Patch budget exhausted: → PATCH_BUDGET_OUT → DEAD.
- Patch gave-up: → PATCH_GAVE_UP → DEAD.
- Patch sibling fan-out: lead + sibling get identical event sequences.
- Patch precheck halt: no models set → catchall PATCH_GAVE_UP fires
  for lead + siblings.
- Triage cached-health-broken override: forces ENV_BROKEN even
  when triage itself succeeded.
- Every escalation that is not triage's MANUAL tier leaves a Manual
  Queue row (poly-7pwa.28).

The existing test_runner_e2e_lifecycle.py covers the simpler
triage paths (auto_patch enqueue, manual escalate). This file
extends coverage to the patch flow + sibling fan-out + the
precheck-halt path, all through the orchestrator.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dportsv3.agent import lifecycle, runner
from dportsv3.agent.llm import Usage
from dportsv3.db.schema import init_db as init_state_db


# --- stubs -------------------------------------------------------------------


@dataclass
class _StubAttempt:
    attempt: int = 1
    tokens: int = 1000
    rebuild_ok: bool = True
    billable_tokens: int = 100


@dataclass
class _StubPatchResult:
    status: str = "success"
    final_text: str = ""
    usage: Usage = field(default_factory=Usage)
    attempts: list = field(default_factory=lambda: [_StubAttempt()])
    proof: dict | None = field(
        default_factory=lambda: {"origin": "foo/bar", "rebuild_ok": True}
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def queue_env(set_setting, tmp_path, monkeypatch):
    """Same shape as test_runner_e2e_lifecycle but with patch-flow
    stubs wired in."""
    queue_root = tmp_path / "queue"
    for sub in ("pending", "inflight", "done", "failed"):
        (queue_root / sub).mkdir(parents=True)

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_state_db(conn)

    monkeypatch.setattr(runner, "_state_db_conn", conn, raising=False)
    runner.invalidate_health_cache()

    # Stub artifact-store & tracker network calls.
    monkeypatch.setattr(runner, "artifact_store_put", lambda *a, **kw: True)
    monkeypatch.setattr(runner, "artifact_get", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "port_bundle_history", lambda *a, **kw: [])

    # Stub the worker's env path resolution (the patch flow's
    # _write_changes_diff calls it via git -C).
    from dportsv3.agent import worker

    @dataclass
    class _Paths:
        env_dir: Path = field(default_factory=lambda: tmp_path / "env")
        writable: Path = field(default_factory=lambda: tmp_path / "env" / "writable")

        @property
        def deltaports(self) -> Path:
            return self.writable / "work" / "DeltaPorts"

    monkeypatch.setattr(worker, "env_paths", lambda env: _Paths())

    # The patch preflight (steps.py §5.1) refuses to start unless the
    # port subtree is clean. Stub it healthy — these tests exercise
    # the orchestrator event chain, not the clean-tree guard.
    monkeypatch.setattr(worker, "assert_port_clean",
                        lambda env, origin: {"ok": True})

    # poly-15l: the preflight also composes the origin before the agent
    # starts, and refuses the job if that fails — the compose tree is
    # shared across jobs, so an attempt must establish its own starting
    # state rather than inherit the previous one's. These tests exercise
    # the orchestrator event chain, not compose, so stub it succeeding.
    monkeypatch.setattr(worker, "materialize_dports",
                        lambda env, origin: {"ok": True})

    # B1 gives each job its own worktree, and since poly-m7o a job that
    # cannot get one is retired worktree_unavailable instead of running
    # against whatever the ports link points at. These tests exercise the
    # orchestrator event chain, not worktree provisioning, so hand them a
    # tree that always succeeds.
    monkeypatch.setattr(
        worker, "create_job_worktree",
        lambda env, bundle_id, kind="patch": {
            "ok": True, "branch": f"bundle/{bundle_id}", "base": "master",
            "worktree": f"/work/job-{kind}-{bundle_id}", "created": True,
        })
    monkeypatch.setattr(
        worker, "destroy_job_worktree",
        lambda env, bundle_id, kind="patch", **kw: {"ok": True})

    # Stub a healthy env so the gate doesn't pause + decide() proceeds.
    from dportsv3.agent import health as health_mod
    monkeypatch.setattr(
        health_mod, "check",
        lambda env, only=None: health_mod.EnvHealth(
            env=env, status="ready", probed_at="2026-05-21T00:00:00Z",
        ),
    )

    set_setting("llm.triage.model", "stub-triage")
    set_setting("llm.patch.model", "stub-patch")
    monkeypatch.setattr(runner, "_CLI_ENV_DEFAULT", "test-env")

    yield {"queue_root": queue_root, "conn": conn, "tmp_path": tmp_path}
    conn.close()


def _make_bundle_dir(tmp_path: Path) -> Path:
    bdir = tmp_path / "bundle"
    (bdir / "analysis").mkdir(parents=True, exist_ok=True)
    (bdir / "logs").mkdir(parents=True, exist_ok=True)
    (bdir / "logs" / "errors.txt").write_text("synthetic error\n")
    (bdir / "meta.txt").write_text("origin=foo/bar\n")
    (bdir / "analysis" / "triage.md").write_text(
        "## Classification\nplist-error\n\n"
        "## Confidence\nhigh\n\n"
        "## Suggested Fix\nadd a thing\n"
    )
    return bdir


def _drop_patch_job(queue_env: dict, *, job_id: str, bundle_dir: Path,
                    tier: str = "AUTO", extra: dict | None = None) -> Path:
    queue_root = queue_env["queue_root"]
    job_path = queue_root / "pending" / job_id
    fields = {
        "type": "patch",
        "created_ts_utc": "20260521-100000Z",
        "profile": "test",
        "origin": "foo/bar",
        "flavor": "",
        "bundle_id": f"bundle-{job_id}",
        "tier": tier,
        "dev_env": "test-env",
        "bundle_dir": str(bundle_dir),
        "target": "@test",
    }
    if extra:
        fields.update(extra)
    job_path.write_text("\n".join(f"{k}={v}" for k, v in fields.items()) + "\n")
    runner._register_new_job(job_id, metadata=fields)
    return job_path


def _claim(queue_env: dict, job_path: Path) -> Path:
    """Move .job file to inflight + fire CLAIM event."""
    inflight = queue_env["queue_root"] / "inflight" / job_path.name
    job_path.rename(inflight)
    runner._apply_transition(job_path.name, lifecycle.JobEvent.CLAIM)
    return inflight


# --- patch happy path -------------------------------------------------------


def test_patch_success_full_chain(queue_env, tmp_path, monkeypatch):
    """Stubbed patch returns status=success → PATCH_OK + VERIFY_OK
    fire → final state DONE."""
    from dportsv3.agent import patch as patch_module

    bdir = _make_bundle_dir(tmp_path)
    monkeypatch.setattr(patch_module, "run",
                        lambda *a, **kw: _StubPatchResult(status="success"))
    # C1: the success gate requires classify_dops == "converted"; the real
    # probe can't run in the test chroot, so stub it for the success chain.
    from dportsv3.agent import worker as _worker
    monkeypatch.setattr(_worker, "classify_dops", lambda env, origin: "converted")

    job_path = _drop_patch_job(queue_env, job_id="job-patch-1.job", bundle_dir=bdir)
    inflight = _claim(queue_env, job_path)
    runner.process_job(queue_env["queue_root"], inflight, [],
                       dry_run=False, playbooks_dir=None)

    events = [r["event_name"]
              for r in lifecycle.history(queue_env["conn"], "job-patch-1.job")]
    assert events == [
        "hook_enqueued", "claim", "patch_start", "patch_ok", "verify_ok",
    ], events
    assert (lifecycle.current(queue_env["conn"], "job-patch-1.job")
            == lifecycle.JobState.DONE)


def test_patch_budget_exhausted(queue_env, tmp_path, monkeypatch):
    from dportsv3.agent import patch as patch_module

    bdir = _make_bundle_dir(tmp_path)
    monkeypatch.setattr(
        patch_module, "run",
        lambda *a, **kw: _StubPatchResult(status="budget-exhausted"),
    )

    job_path = _drop_patch_job(queue_env, job_id="job-budget.job", bundle_dir=bdir)
    inflight = _claim(queue_env, job_path)
    runner.process_job(queue_env["queue_root"], inflight, [],
                       dry_run=False, playbooks_dir=None)

    events = [r["event_name"]
              for r in lifecycle.history(queue_env["conn"], "job-budget.job")]
    assert events == [
        "hook_enqueued", "claim", "patch_start", "patch_budget_out",
    ], events
    row = queue_env["conn"].execute(
        "SELECT retire_reason FROM jobs WHERE job_id = ?", ("job-budget.job",)
    ).fetchone()
    assert row["retire_reason"] == "patch_budget_exhausted"


def test_patch_gave_up(queue_env, tmp_path, monkeypatch):
    from dportsv3.agent import patch as patch_module

    bdir = _make_bundle_dir(tmp_path)
    monkeypatch.setattr(
        patch_module, "run",
        lambda *a, **kw: _StubPatchResult(status="needs-help"),
    )

    job_path = _drop_patch_job(queue_env, job_id="job-help.job", bundle_dir=bdir)
    inflight = _claim(queue_env, job_path)
    runner.process_job(queue_env["queue_root"], inflight, [],
                       dry_run=False, playbooks_dir=None)

    events = [r["event_name"]
              for r in lifecycle.history(queue_env["conn"], "job-help.job")]
    assert events == [
        "hook_enqueued", "claim", "patch_start", "patch_gave_up",
    ]


def test_a_shared_op_failing_at_preflight_escalates_before_the_agent(
        queue_env, tmp_path, monkeypatch):
    """poly-7pwa.27 row 6: an @any op fails to compose on this line. The
    operator decides; it is not the agent failing to fix the port, and no
    model is called."""
    from dportsv3.agent import patch as patch_module
    from dportsv3.agent import worker

    bdir = _make_bundle_dir(tmp_path)
    handoffs: list[dict] = []

    def no_agent(*a, **kw):
        raise AssertionError("the agent ran")

    monkeypatch.setattr(patch_module, "run", no_agent)
    monkeypatch.setattr(worker, "materialize_dports", lambda env, origin: {
        "ok": False, "origin": origin, "stderr_tail": "E_COMPOSE_APPLY_FAILED"})
    monkeypatch.setattr(worker, "invariant_origins", lambda env, o: [o])
    monkeypatch.setattr(
        worker, "materialize_dports_with_report", lambda env, o: {
            "ok": False, "report": {"ports": [{
                "origin": o, "dops_failed_op_results": [{
                    "id": "op-0001-text-replace-once",
                    "kind": "text.replace_once", "target": "@any",
                    "status": "failed",
                    "diagnostics": [{"code": "E_APPLY_MISSING_SUBJECT",
                                     "message": "pattern not found"}],
                }]}]}})
    monkeypatch.setattr(runner, "_write_manual_handoff",
                        lambda *a, **kw: handoffs.append(kw))

    job_path = _drop_patch_job(queue_env, job_id="job-scope.job",
                               bundle_dir=bdir, extra={"run_id": "run-1"})
    inflight = _claim(queue_env, job_path)
    runner.process_job(queue_env["queue_root"], inflight, [],
                       dry_run=False, playbooks_dir=None)

    events = [r["event_name"]
              for r in lifecycle.history(queue_env["conn"], "job-scope.job")]
    assert events == [
        "hook_enqueued", "claim", "patch_start", "escalate_manual",
    ], events
    assert [h["reason"] for h in handoffs] == ["patch_scope_decision"]
    assert "op-0001-text-replace-once" in handoffs[0]["reason_detail"]
    row = queue_env["conn"].execute(
        "SELECT retire_reason FROM jobs WHERE job_id = ?", ("job-scope.job",)
    ).fetchone()
    assert row["retire_reason"] != "patch_gave_up"
    assert _manual_queue(queue_env) == [
        ("run-1", "foo/bar", "bundle-job-scope.job", "pending")]


def _manual_queue(queue_env) -> list[tuple]:
    """What /agentic/manual lists: user_context_requests rows."""
    return [tuple(r) for r in queue_env["conn"].execute(
        "SELECT run_id, origin, bundle_id, status FROM user_context_requests")]


def test_a_fix_left_as_compat_artifacts_reaches_the_manual_queue(
        queue_env, tmp_path, monkeypatch):
    """poly-7pwa.28: patch_non_dops_substrate escalated with a handoff
    and no queue row, so the Manual Queue never listed it."""
    from dportsv3.agent import patch as patch_module
    from dportsv3.agent import worker

    import json
    from dataclasses import asdict

    from dportsv3.agent.phase_result import TriageResult

    bdir = _make_bundle_dir(tmp_path)
    (bdir / "analysis").mkdir(exist_ok=True)
    (bdir / "analysis" / "triage_result.json").write_text(json.dumps(asdict(
        TriageResult(classification="patch-error", confidence="high",
                     root_cause="", evidence_excerpt="", error_signature=None,
                     tier="AUTO", classifier_version="", tokens_prompt=0,
                     tokens_completion=0, tokens_total=0, model=""))))
    monkeypatch.setattr(patch_module, "run",
                        lambda *a, **kw: _StubPatchResult(status="success"))
    monkeypatch.setattr(worker, "classify_dops", lambda env, origin: "compat")

    job_path = _drop_patch_job(queue_env, job_id="job-compat.job",
                               bundle_dir=bdir, extra={"run_id": "run-1"})
    runner.process_job(queue_env["queue_root"], _claim(queue_env, job_path),
                       [], dry_run=False, playbooks_dir=None)

    assert (lifecycle.current(queue_env["conn"], "job-compat.job")
            == lifecycle.JobState.ESCALATED)
    assert _manual_queue(queue_env) == [
        ("run-1", "foo/bar", "bundle-job-compat.job", "pending")]
    # A patch job has no classification of its own: the bundle's triage.
    assert tuple(queue_env["conn"].execute(
        "SELECT classification, confidence FROM user_context_requests"
    ).fetchone()) == ("patch-error", "high")


OWN_OVERLAY = (
    'port foo/bar\ntype port\nreason "fixture"\n'
    'target @any\nmk add CFLAGS -DANY\n'
    'target @main\ntext replace-once file Makefile from "a" to "b"\n'
)


def _preflight_fails_on(monkeypatch, tmp_path, *, targets, stage_errors=(),
                        composes_after=True):
    """The preflight compose fails on the OWN_OVERLAY ops of ``targets``.

    materialize_dports fails until the agent has run, then returns
    ``composes_after``. Returns the payloads the agent was called with.
    """
    from dportsv3.agent import patch as patch_module
    from dportsv3.agent import worker
    from dportsv3.engine.api import build_plan

    port = tmp_path / "env" / "writable" / "work" / "DeltaPorts" / "ports" / "foo" / "bar"
    port.mkdir(parents=True)
    (port / "overlay.dops").write_text(OWN_OVERLAY)
    ops = {op.target: op for op in build_plan(OWN_OVERLAY, None).plan.ops}
    rows = [{"id": ops[t].id, "kind": ops[t].kind, "target": t,
             "status": "failed",
             "diagnostics": [{"code": "E_APPLY_MISSING_SUBJECT",
                              "message": "pattern not found"}]}
            for t in targets]
    payloads: list[str] = []

    def agent(payload, **kw):
        payloads.append(payload)
        return _StubPatchResult(status="success")

    monkeypatch.setattr(patch_module, "run", agent)
    monkeypatch.setattr(worker, "classify_dops", lambda env, origin: "converted")
    monkeypatch.setattr(worker, "materialize_dports", lambda env, origin: {
        "ok": bool(payloads) and composes_after, "origin": origin,
        "stderr_tail": "E_COMPOSE_APPLY_FAILED"})
    monkeypatch.setattr(worker, "invariant_origins", lambda env, o: [o])
    monkeypatch.setattr(
        worker, "materialize_dports_with_report", lambda env, o: {
            "ok": False, "report": {
                "stages": [{"name": "apply_semantic_ops", "errors": [
                    "E_COMPOSE_APPLY_FAILED: foo/bar: op(s) failed",
                    *stage_errors]}],
                "ports": [{"origin": o, "dops_failed_op_results": rows}]}})
    return ops, payloads


def _run_patch(queue_env, tmp_path, job_id):
    job_path = _drop_patch_job(queue_env, job_id=job_id,
                               bundle_dir=_make_bundle_dir(tmp_path),
                               extra={"target": "@main", "run_id": "run-1"})
    runner.process_job(queue_env["queue_root"], _claim(queue_env, job_path),
                       [], dry_run=False, playbooks_dir=None)
    return [r["event_name"]
            for r in lifecycle.history(queue_env["conn"], job_id)]


def test_an_own_op_failing_at_preflight_goes_to_the_agent(
        queue_env, tmp_path, monkeypatch):
    """poly-7pwa.30: only an op of this line's own block fails -- say an
    anchor an upstream bump removed. reapply wrote the port with every
    other op, and fixing that op is the job: the agent runs, told which."""
    ops, payloads = _preflight_fails_on(monkeypatch, tmp_path,
                                        targets=["@main"])
    events = _run_patch(queue_env, tmp_path, "job-own-op.job")
    assert events[-2:] == ["patch_ok", "verify_ok"], events
    (payload,) = payloads
    assert "## This line's own ops fail to compose" in payload
    assert ops["@main"].id in payload and "E_APPLY_MISSING_SUBJECT" in payload


def test_success_that_still_does_not_compose_is_not_done(
        queue_env, tmp_path, monkeypatch):
    """This job never had a clean compose to start from, so its claim of
    success is checked: the port must compose before the workspace reset."""
    _preflight_fails_on(monkeypatch, tmp_path, targets=["@main"],
                        composes_after=False)
    events = _run_patch(queue_env, tmp_path, "job-still-broken.job")
    assert events[-1] == "patch_gave_up", events


@pytest.mark.parametrize("targets, stage_errors, last", [
    (["@main", "@any"], (), "escalate_manual"),
    (["@main"], ("E_COMPOSE_SPECIAL_PATCH_FAILED: Mk/x.diff",), "patch_gave_up"),
], ids=["own-and-shared-escalates", "own-and-another-error-refuses"])
def test_own_ops_with_anything_else_do_not_start_the_agent(
        queue_env, tmp_path, monkeypatch, targets, stage_errors, last):
    _, payloads = _preflight_fails_on(monkeypatch, tmp_path, targets=targets,
                                      stage_errors=stage_errors)
    events = _run_patch(queue_env, tmp_path, "job-mixed.job")
    assert events[-1] == last, events
    assert payloads == []


def test_a_preflight_failure_that_is_not_an_op_is_still_refused(
        queue_env, tmp_path, monkeypatch):
    """No failing op to hand over: the tree is unknown, so the old refusal
    stands."""
    from dportsv3.agent import worker

    bdir = _make_bundle_dir(tmp_path)
    monkeypatch.setattr(worker, "materialize_dports", lambda env, origin: {
        "ok": False, "origin": origin, "stderr_tail": "boom"})
    monkeypatch.setattr(worker, "invariant_origins", lambda env, o: [o])
    monkeypatch.setattr(
        worker, "materialize_dports_with_report",
        lambda env, o: {"ok": False, "report": {"ports": []}})

    job_path = _drop_patch_job(queue_env, job_id="job-own.job", bundle_dir=bdir)
    inflight = _claim(queue_env, job_path)
    runner.process_job(queue_env["queue_root"], inflight, [],
                       dry_run=False, playbooks_dir=None)
    events = [r["event_name"]
              for r in lifecycle.history(queue_env["conn"], "job-own.job")]
    assert events[-1] == "patch_gave_up", events


# --- sibling fan-out ---------------------------------------------------------


def test_patch_sibling_fan_out(queue_env, tmp_path, monkeypatch):
    """A patch job with one sibling. Both lead and sibling should
    receive the same lifecycle events (patch_start, patch_ok,
    verify_ok)."""
    from dportsv3.agent import patch as patch_module

    bdir = _make_bundle_dir(tmp_path)
    monkeypatch.setattr(patch_module, "run",
                        lambda *a, **kw: _StubPatchResult(status="success"))
    # C1: success gate requires classify_dops == "converted" (see above).
    from dportsv3.agent import worker as _worker
    monkeypatch.setattr(_worker, "classify_dops", lambda env, origin: "converted")

    lead_path = _drop_patch_job(queue_env, job_id="lead.job", bundle_dir=bdir)
    sib_path = _drop_patch_job(queue_env, job_id="sib.job", bundle_dir=bdir)
    lead_inflight = _claim(queue_env, lead_path)
    sib_inflight = _claim(queue_env, sib_path)

    runner.process_job(queue_env["queue_root"], lead_inflight, [sib_inflight],
                       dry_run=False, playbooks_dir=None)

    lead_events = [r["event_name"]
                   for r in lifecycle.history(queue_env["conn"], "lead.job")]
    sib_events = [r["event_name"]
                  for r in lifecycle.history(queue_env["conn"], "sib.job")]

    expected = ["hook_enqueued", "claim", "patch_start", "patch_ok", "verify_ok"]
    assert lead_events == expected, lead_events
    assert sib_events == expected, sib_events

    assert (lifecycle.current(queue_env["conn"], "lead.job")
            == lifecycle.JobState.DONE)
    assert (lifecycle.current(queue_env["conn"], "sib.job")
            == lifecycle.JobState.DONE)


def test_patch_sibling_fan_out_on_failure(queue_env, tmp_path, monkeypatch):
    """Sibling should mirror the lead's failure event too."""
    from dportsv3.agent import patch as patch_module

    bdir = _make_bundle_dir(tmp_path)
    monkeypatch.setattr(
        patch_module, "run",
        lambda *a, **kw: _StubPatchResult(status="budget-exhausted"),
    )

    lead_path = _drop_patch_job(queue_env, job_id="lead-fail.job", bundle_dir=bdir)
    sib_path = _drop_patch_job(queue_env, job_id="sib-fail.job", bundle_dir=bdir)
    lead_inflight = _claim(queue_env, lead_path)
    sib_inflight = _claim(queue_env, sib_path)

    runner.process_job(queue_env["queue_root"], lead_inflight, [sib_inflight],
                       dry_run=False, playbooks_dir=None)

    expected = ["hook_enqueued", "claim", "patch_start", "patch_budget_out"]
    for jid in ("lead-fail.job", "sib-fail.job"):
        events = [r["event_name"]
                  for r in lifecycle.history(queue_env["conn"], jid)]
        assert events == expected, (jid, events)


# --- precheck halt ----------------------------------------------------------


def test_patch_precheck_halt_synthesizes_failure_event(
    queue_env, tmp_path, monkeypatch,
):
    """When precheck fails (e.g. no model env vars set), the
    orchestrator halts without firing events; the wrapper
    synthesizes PATCH_GAVE_UP for lead + siblings."""
    bdir = _make_bundle_dir(tmp_path)
    # Force precheck failure: no model env vars.
    monkeypatch.delenv("DP_HARNESS_PATCH_MODEL", raising=False)
    monkeypatch.delenv("DP_HARNESS_TRIAGE_MODEL", raising=False)

    lead_path = _drop_patch_job(queue_env, job_id="halt-lead.job", bundle_dir=bdir)
    sib_path = _drop_patch_job(queue_env, job_id="halt-sib.job", bundle_dir=bdir)
    lead_inflight = _claim(queue_env, lead_path)
    sib_inflight = _claim(queue_env, sib_path)

    runner.process_job(queue_env["queue_root"], lead_inflight, [sib_inflight],
                       dry_run=False, playbooks_dir=None)

    expected = ["hook_enqueued", "claim", "patch_start", "patch_gave_up"]
    for jid in ("halt-lead.job", "halt-sib.job"):
        events = [r["event_name"]
                  for r in lifecycle.history(queue_env["conn"], jid)]
        assert events == expected, (jid, events)
        assert (lifecycle.current(queue_env["conn"], jid)
                == lifecycle.JobState.DEAD)


# --- triage env_broken override ---------------------------------------------


def test_triage_env_broken_override_via_step(queue_env, tmp_path, monkeypatch):
    """If the cached health probe is broken, TriageStep should
    return ENV_BROKEN as its next_event regardless of decide()'s
    action. Final state: DEAD with retire_reason=env_broken."""
    import time as _time
    from dportsv3.agent import health as health_mod
    from dportsv3.agent import triage as triage_module

    # Plant a broken EnvHealth in the runner's cache.
    runner._health_cache["test-env"] = (
        _time.monotonic(),
        health_mod.EnvHealth(env="test-env", status="broken"),
    )

    bdir = _make_bundle_dir(tmp_path)

    from dataclasses import dataclass as _dc, field as _f
    from dportsv3.agent.llm import Usage as _Usage

    @_dc
    class _Stub:
        text: str = "## Classification\nplist-error\n\n## Confidence\nhigh\n"
        classification: str = "plist-error"
        confidence: str = "high"
        snippet_rounds: int = 0
        usage: _Usage = _f(default_factory=_Usage)

    monkeypatch.setattr(triage_module, "run", lambda *a, **kw: _Stub())

    queue_root = queue_env["queue_root"]
    job_path = queue_root / "pending" / "triage-env-broken.job"
    fields = {
        "type": "triage",
        "created_ts_utc": "20260521-100000Z",
        "profile": "test",
        "origin": "foo/bar",
        "bundle_id": "b-env-broken",
        "bundle_dir": str(bdir),
        "target": "@test",
    }
    job_path.write_text("\n".join(f"{k}={v}" for k, v in fields.items()) + "\n")
    runner._register_new_job("triage-env-broken.job", metadata=fields)

    inflight = _claim(queue_env, job_path)
    runner.process_job(queue_env["queue_root"], inflight, [],
                       dry_run=False, playbooks_dir=None)

    events = [r["event_name"]
              for r in lifecycle.history(queue_env["conn"], "triage-env-broken.job")]
    # decide() returns skip (env_health.status=broken from cache);
    # TriageStep emits ENV_BROKEN as next_event.
    assert "env_broken" in events
    assert (lifecycle.current(queue_env["conn"], "triage-env-broken.job")
            == lifecycle.JobState.DEAD)
    row = queue_env["conn"].execute(
        "SELECT retire_reason FROM jobs WHERE job_id = ?",
        ("triage-env-broken.job",),
    ).fetchone()
    assert row["retire_reason"] == "env_broken"


def test_a_port_that_needs_conversion_reaches_the_manual_queue(
        queue_env, tmp_path, monkeypatch):
    """poly-7pwa.28: triage's compat_needs_conversion escalated with a
    handoff and no queue row -- 20 of the live tracker's 24 missing ones."""
    import time as _time
    from dportsv3.agent import health as health_mod
    from dportsv3.agent import triage as triage_module

    runner._health_cache["test-env"] = (
        _time.monotonic(), health_mod.EnvHealth(env="test-env", status="ready"))

    @dataclass
    class _Triage:
        text: str = "## Classification\npatch-error\n\n## Confidence\nhigh\n"
        classification: str = "patch-error"
        confidence: str = "high"
        snippet_rounds: int = 0
        usage: Usage = field(default_factory=Usage)

    monkeypatch.setattr(triage_module, "run", lambda *a, **kw: _Triage())
    monkeypatch.setattr(runner, "_ensure_overlay_or_abort",
                        lambda **kw: ("abort", "compat residue"))

    bdir = _make_bundle_dir(tmp_path)
    fields = {
        "type": "triage", "created_ts_utc": "20260521-100000Z",
        "profile": "test", "origin": "foo/bar", "bundle_id": "b-convert",
        "bundle_dir": str(bdir), "target": "@test", "run_id": "run-1",
    }
    job_path = queue_env["queue_root"] / "pending" / "triage-convert.job"
    job_path.write_text("\n".join(f"{k}={v}" for k, v in fields.items()) + "\n")
    runner._register_new_job("triage-convert.job", metadata=fields)
    runner.process_job(queue_env["queue_root"], _claim(queue_env, job_path),
                       [], dry_run=False, playbooks_dir=None)

    assert (lifecycle.current(queue_env["conn"], "triage-convert.job")
            == lifecycle.JobState.ESCALATED)
    assert _manual_queue(queue_env) == [
        ("run-1", "foo/bar", "b-convert", "pending")]
    (classification,) = queue_env["conn"].execute(
        "SELECT classification FROM user_context_requests").fetchone()
    assert classification == "patch-error"
