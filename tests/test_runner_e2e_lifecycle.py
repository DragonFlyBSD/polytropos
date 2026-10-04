"""End-to-end lifecycle integration tests for the runner.

Phase 1 Step 4. Drives ``process_job`` directly with stubbed LLM
results and a throwaway sqlite DB, asserting the lifecycle event
sequence and final state match expectations.

Coverage:
- Happy triage path: AUTO tier classification → TRIAGE_OK,
  auto-enqueued patch job exists in pending/, jobs.state == TRIAGED.
- MANUAL escalation: triage classification → MANUAL tier → TRIAGE_OK
  + ESCALATE_MANUAL, final state ESCALATED.
- Reap orphans on startup: a pre-existing PATCHING job is
  transitioned to DEAD with retire_reason="runner_restart".
- env_broken: when the cached health probe (Phase 2) shows broken,
  completion routes to ENV_BROKEN regardless of job_type and the
  job ends DEAD with retire_reason="env_broken".
- invalidate_health_cache clears entries.
- _looks_env_suspicious heuristic recognizes the known stderr
  patterns that trigger a forced re-probe.
- Operator context: a MANUAL promotion or a patch-cap pardon happens
  only on context recorded in the job's own run, and the prompt of
  the patch job it enqueues carries that context (poly-7pwa.17).

The runner's LLM calls (``triage.run`` / ``patch.run``) and the dev-env
worker boundary are stubbed via monkeypatch. No real network, no
real chroot, no real dsynth.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dportsv3.agent import lifecycle, runner
from dportsv3.agent.llm import Usage
from dportsv3.db.schema import init_db as init_state_db


# --- Test stubs ---------------------------------------------------------------


@dataclass
class _StubTriageResult:
    """Mirror of dportsv3.agent.triage.TriageResult for stubbing."""
    text: str = ""
    classification: str = "plist-error"
    confidence: str = "high"
    snippet_rounds: int = 0
    usage: Usage = field(default_factory=Usage)


@dataclass
class _StubAttempt:
    attempt: int = 1
    tokens: int = 1000
    rebuild_ok: bool = True


@dataclass
class _StubPatchResult:
    """Mirror of dportsv3.agent.attempt_loop.PatchResult for stubbing."""
    status: str = "success"
    final_text: str = ""
    usage: Usage = field(default_factory=Usage)
    attempts: list = field(default_factory=lambda: [_StubAttempt()])
    proof: dict | None = field(
        default_factory=lambda: {"origin": "foo/bar", "rebuild_ok": True}
    )


# --- Fixtures -----------------------------------------------------------------


@pytest.fixture
def queue_env(set_setting, tmp_path, monkeypatch):
    """A fully-wired throwaway queue + state.db + runner module state.

    Yields a dict with the queue_root, the open state-db connection,
    and helpers. Resets runner module globals on teardown.
    """
    # Queue directories
    queue_root = tmp_path / "queue"
    for sub in ("pending", "inflight", "done", "failed"):
        (queue_root / sub).mkdir(parents=True)

    # state.db sits one level up from queue_root per get_state_db_path()
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_state_db(conn)

    # Wire the runner's module-level connection at the throwaway DB.
    monkeypatch.setattr(runner, "_state_db_conn", conn, raising=False)
    # Drop any health cache state from previous tests.
    runner.invalidate_health_cache()

    # Stub the health probe — without an active env the runner skips
    # the probe; with it (set below), the decision engine probes on
    # every triage. Default the stub to "ready" so the happy-path
    # tests don't accidentally route to skip; individual tests that
    # want a broken env plant a broken EnvHealth into _health_cache.
    from dportsv3.agent import health as health_mod
    monkeypatch.setattr(
        health_mod, "check",
        lambda env, only=None: health_mod.EnvHealth(
            env=env, status="ready", probed_at="2026-05-21T00:00:00Z",
        ),
    )

    # Stub artifact-store HTTP calls (no server in tests).
    monkeypatch.setattr(runner, "artifact_store_put",
                        lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(runner, "artifact_get",
                        lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(runner, "bundle_artifact_list",
                        lambda *a, **kw: [], raising=False)
    # Stub the tracker history endpoint too (returns no prior bundles).
    monkeypatch.setattr(runner, "port_bundle_history",
                        lambda *a, **kw: [], raising=False)

    # Required env vars; values don't matter because we stub the LLM call.
    set_setting("llm.triage.model", "test/stub-triage")
    set_setting("llm.patch.model", "test/stub-patch")
    monkeypatch.setattr(runner, "_CLI_ENV_DEFAULT", "test-env")

    yield {"queue_root": queue_root, "conn": conn, "db_path": db_path}

    conn.close()


def _drop_synthetic_job(
    queue_env: dict,
    job_id: str = "20260520-test-foo_bar-1.job",
    *,
    job_type: str = "triage",
    bundle_dir: Path | None = None,
    extra_fields: dict | None = None,
) -> Path:
    """Write a .job file to pending/ and register the row via
    _register_new_job so the lifecycle test mirrors production flow."""
    queue_root = queue_env["queue_root"]
    job_path = queue_root / "pending" / job_id
    fields = {
        "type": job_type,
        "created_ts_utc": "20260520-100000Z",
        "profile": "test",
        "origin": "foo/bar",
        "flavor": "",
        "bundle_id": "foo_bar-20260520-100000Z",
        "target": "@test",
    }
    if bundle_dir is not None:
        fields["bundle_dir"] = str(bundle_dir)
    if extra_fields:
        fields.update(extra_fields)
    job_path.write_text("\n".join(f"{k}={v}" for k, v in fields.items()) + "\n")
    # Mirror the production HOOK_ENQUEUED transition.
    runner._register_new_job(job_id, metadata=fields)
    return job_path


def _make_bundle_dir(tmp_path: Path) -> Path:
    """Synthetic on-disk bundle dir so the harness's bundle_dir check passes."""
    bdir = tmp_path / "bundle"
    (bdir / "analysis").mkdir(parents=True)
    (bdir / "logs").mkdir(parents=True)
    (bdir / "logs" / "errors.txt").write_text("synthetic build error\n")
    (bdir / "meta.txt").write_text("origin=foo/bar\n")
    return bdir


# --- Tests --------------------------------------------------------------------


def test_full_triage_path_hands_off_and_finishes(queue_env, tmp_path, monkeypatch):
    """AUTO tier triage: patch job auto-enqueued, and the triage row that
    enqueued it reaches DONE.

    It used to stop at TRIAGED. Nothing transitions a triage again on the
    auto_patch route -- the patch job is a separate row -- so the completed
    triage sat there until a restart swept it to DEAD/runner_restart, which
    is why triage had reached DONE zero times and dead overstated real
    failure by about 3.1x."""
    conn = queue_env["conn"]
    bdir = _make_bundle_dir(tmp_path)
    job_path = _drop_synthetic_job(queue_env, bundle_dir=bdir)
    job_id = job_path.name

    # Stub the LLM call: plist-error + high confidence → AUTO tier.
    from dportsv3.agent import triage as triage_module
    monkeypatch.setattr(triage_module, "run",
                        lambda *a, **kw: _StubTriageResult(
                            text="## Classification\nplist-error\n\n## Confidence\nhigh\n",
                            classification="plist-error",
                            confidence="high",
                        ))

    # Move job to inflight (the runner does this via claim_next_job_batch
    # in production; we shortcut here since we're testing process_job).
    inflight_path = queue_env["queue_root"] / "inflight" / job_id
    job_path.rename(inflight_path)
    runner._apply_transition(job_id, lifecycle.JobEvent.CLAIM)

    runner.process_job(queue_env["queue_root"], inflight_path, [],
                       dry_run=False, playbooks_dir=None)

    hist = lifecycle.history(conn, job_id)
    events = [r["event_name"] for r in hist]
    assert events == [
        "hook_enqueued",
        "claim",
        "triage_start",
        "triage_ok",
        "triage_handoff",
    ], events
    assert lifecycle.current(conn, job_id) == lifecycle.JobState.DONE

    # Patch job auto-enqueued: one new pending .job file, and a jobs
    # row in QUEUED state with type=patch.
    pending = list((queue_env["queue_root"] / "pending").glob("*.job"))
    assert len(pending) == 1
    patch_job_id = pending[0].name
    assert "patch" in patch_job_id
    assert lifecycle.current(conn, patch_job_id) == lifecycle.JobState.QUEUED


def test_a_handed_off_triage_is_not_reaped_as_an_orphan(queue_env, tmp_path,
                                                        monkeypatch):
    """The invariant the measured data violated: a triage that produced a
    classification must not end as DEAD/runner_restart. reap_orphans sweeps
    _INFLIGHT_STATES, which still contains TRIAGED on purpose -- a triage
    that died before handing off is genuinely incomplete -- so what keeps a
    finished one safe is that it is no longer sitting in that state."""
    conn = queue_env["conn"]
    bdir = _make_bundle_dir(tmp_path)
    job_path = _drop_synthetic_job(queue_env, bundle_dir=bdir)
    job_id = job_path.name

    from dportsv3.agent import triage as triage_module
    monkeypatch.setattr(triage_module, "run",
                        lambda *a, **kw: _StubTriageResult(
                            text="## Classification\nplist-error\n\n"
                                 "## Confidence\nhigh\n",
                            classification="plist-error",
                            confidence="high",
                        ))
    inflight_path = queue_env["queue_root"] / "inflight" / job_id
    job_path.rename(inflight_path)
    runner._apply_transition(job_id, lifecycle.JobEvent.CLAIM)
    runner.process_job(queue_env["queue_root"], inflight_path, [],
                       dry_run=False, playbooks_dir=None)

    lifecycle.reap_orphans(conn)

    assert lifecycle.current(conn, job_id) == lifecycle.JobState.DONE
    row = conn.execute("SELECT retire_reason FROM jobs WHERE job_id = ?",
                       (job_id,)).fetchone()
    assert row["retire_reason"] != "runner_restart"


def test_a_triage_that_died_before_handing_off_is_still_reaped(queue_env):
    """The safety net the fix must not remove. TRIAGED stays in
    _INFLIGHT_STATES so a triage that classified and then crashed before
    enqueueing its patch job is still swept on restart."""
    job_path = _drop_synthetic_job(queue_env)
    job_id = job_path.name
    conn = queue_env["conn"]

    runner._apply_transition(job_id, lifecycle.JobEvent.CLAIM)
    runner._apply_transition(job_id, lifecycle.JobEvent.TRIAGE_START)
    runner._apply_transition(job_id, lifecycle.JobEvent.TRIAGE_OK)
    assert lifecycle.current(conn, job_id) == lifecycle.JobState.TRIAGED

    lifecycle.reap_orphans(conn)

    assert lifecycle.current(conn, job_id) == lifecycle.JobState.DEAD


def test_triage_manual_escalates(queue_env, tmp_path, monkeypatch):
    """missing-dep classification → MANUAL tier → ESCALATED end state."""
    conn = queue_env["conn"]
    bdir = _make_bundle_dir(tmp_path)
    job_path = _drop_synthetic_job(queue_env, bundle_dir=bdir)
    job_id = job_path.name

    from dportsv3.agent import triage as triage_module
    monkeypatch.setattr(triage_module, "run",
                        lambda *a, **kw: _StubTriageResult(
                            text="## Classification\nmissing-dep\n\n## Confidence\nhigh\n",
                            classification="missing-dep",
                            confidence="high",
                        ))

    inflight_path = queue_env["queue_root"] / "inflight" / job_id
    job_path.rename(inflight_path)
    runner._apply_transition(job_id, lifecycle.JobEvent.CLAIM)

    runner.process_job(queue_env["queue_root"], inflight_path, [],
                       dry_run=False, playbooks_dir=None)

    events = [r["event_name"] for r in lifecycle.history(conn, job_id)]
    assert events == [
        "hook_enqueued",
        "claim",
        "triage_start",
        "triage_ok",
        "escalate_manual",
    ], events
    assert lifecycle.current(conn, job_id) == lifecycle.JobState.ESCALATED

    row = conn.execute("SELECT retire_reason FROM jobs WHERE job_id = ?",
                       (job_id,)).fetchone()
    assert row["retire_reason"] == "escalated_manual"

    # No patch job enqueued.
    pending = list((queue_env["queue_root"] / "pending").glob("*.job"))
    assert pending == []


def test_reap_orphans_on_startup(queue_env):
    """A PATCHING-stuck job is transitioned to DEAD by reap_orphans."""
    conn = queue_env["conn"]
    # Walk a synthetic job to PATCHING using the lifecycle directly —
    # mirrors what a previous runner instance would have left behind.
    jid = "orphaned-job"
    for ev in [
        lifecycle.JobEvent.HOOK_ENQUEUED,
        lifecycle.JobEvent.CLAIM,
        lifecycle.JobEvent.TRIAGE_START,
        lifecycle.JobEvent.TRIAGE_OK,
        lifecycle.JobEvent.PATCH_START,
    ]:
        lifecycle.apply(conn, jid, ev, actor="prior-runner")
    assert lifecycle.current(conn, jid) == lifecycle.JobState.PATCHING

    # Simulate the runner-startup reap.
    n = lifecycle.reap_orphans(conn, actor="runner-test")
    assert n == 1

    assert lifecycle.current(conn, jid) == lifecycle.JobState.DEAD
    row = conn.execute(
        "SELECT retire_reason FROM jobs WHERE job_id = ?", (jid,)
    ).fetchone()
    assert row["retire_reason"] == "runner_restart"

    # The latest event is REAP_ORPHAN.
    hist = lifecycle.history(conn, jid)
    assert hist[-1]["event_name"] == "reap_orphan"
    assert hist[-1]["actor"] == "runner-test"


def test_env_broken_routes_to_dead(queue_env):
    """When the cached health probe shows broken, completion routes
    to ENV_BROKEN regardless of job_type and final state is DEAD
    with retire_reason=env_broken.
    """
    from dportsv3.agent import health as health_mod
    import time as _time

    conn = queue_env["conn"]
    # Plant a "broken" probe in the cache directly. ``time.monotonic``
    # is what _probe_health reads, so use that for the cache timestamp.
    broken = health_mod.EnvHealth(
        env="test-env",
        status="broken",
        checks=[health_mod.HealthCheck(
            name="python_runtime", status="broken",
            detail="missing py311 packages",
            operator_action="pkg install py311-...",
        )],
        operator_action="pkg install py311-...",
    )
    runner._health_cache["test-env"] = (_time.monotonic(), broken)

    # Phase 5 Step 4: _completion_events_for retired. The cached-
    # health-broken override now lives inside each Step's run() —
    # exercised via the orchestrator-driven happy path (separate
    # tests). Here we just confirm the cache flag is observable.
    assert runner._cached_health_broken() is True

    # End-to-end: walk a job to PATCHING, then fire ENV_BROKEN; the
    # job should land DEAD with retire_reason="env_broken".
    jid = "env-broken-job"
    for ev in [
        lifecycle.JobEvent.HOOK_ENQUEUED,
        lifecycle.JobEvent.CLAIM,
        lifecycle.JobEvent.TRIAGE_START,
        lifecycle.JobEvent.TRIAGE_OK,
        lifecycle.JobEvent.PATCH_START,
    ]:
        lifecycle.apply(conn, jid, ev)
    lifecycle.apply(conn, jid, lifecycle.JobEvent.ENV_BROKEN,
                    detail={"reason": "missing py311 packages"})

    assert lifecycle.current(conn, jid) == lifecycle.JobState.DEAD
    row = conn.execute(
        "SELECT retire_reason FROM jobs WHERE job_id = ?", (jid,)
    ).fetchone()
    assert row["retire_reason"] == "env_broken"


def test_invalidate_health_cache_clears_all(queue_env):
    """invalidate_health_cache() with no arg drops every env's entry."""
    import time as _time
    from dportsv3.agent import health as health_mod
    runner._health_cache["a"] = (_time.monotonic(), health_mod.EnvHealth(env="a", status="ready"))
    runner._health_cache["b"] = (_time.monotonic(), health_mod.EnvHealth(env="b", status="broken"))
    assert runner._cached_health_broken() is True
    runner.invalidate_health_cache()
    assert runner._cached_health_broken() is False


def test_looks_env_suspicious_detects_known_sentinels():
    """The heuristic that triggers cache invalidation mid-job."""
    assert runner._looks_env_suspicious({
        "ok": False, "stderr_tail": "dportsv3: missing DragonFly packages required...",
    }) is True
    assert runner._looks_env_suspicious({
        "ok": False, "stderr_tail": "compile error: foo.c:42",
    }) is False
    # ok=True is never suspicious
    assert runner._looks_env_suspicious({
        "ok": True, "stderr_tail": "missing DragonFly packages",
    }) is False
    assert runner._looks_env_suspicious(None) is False


# --- Operator context is keyed on the job's run (poly-7pwa.17) ------------------


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _plant_run(conn, run_id: str, target: str | None = "@test"):
    conn.execute("INSERT OR IGNORE INTO runs (run_id, profile, target) "
                 "VALUES (?, 'test', ?)", (run_id, target))
    conn.commit()


def _plant_context(conn, run_id: str, when: str, origin="foo/bar",
                   text="link against the system zstd"):
    _plant_run(conn, run_id)
    conn.execute("INSERT INTO user_context (run_id, origin, "
                 "context_text, updated_at, context_rev) "
                 "VALUES (?, ?, ?, ?, 1)", (run_id, origin, text, when))
    conn.execute("INSERT INTO user_context_history (run_id, origin, "
                 "context_rev, submitted_at, text) "
                 "VALUES (?, ?, 1, ?, ?)", (run_id, origin, when, text))
    conn.commit()


def _plant_gave_up_patch(conn, job_id: str, when: str):
    conn.execute("INSERT INTO jobs (job_id, origin, target, type, "
                 "state, retire_reason, last_transition_at) "
                 "VALUES (?, 'foo/bar', '@test', 'patch', 'dead', "
                 "'patch_gave_up', ?)", (job_id, when))
    conn.commit()


def _triage_in_run(queue_env, tmp_path, monkeypatch, run_id: str,
                   classification: str = "missing-dep"):
    """missing-dep is MANUAL in the default policy. Returns (the
    patch job it enqueued, or None; the bundle dir)."""
    from dportsv3.agent import triage as triage_module
    monkeypatch.setattr(triage_module, "run",
                        lambda *a, **kw: _StubTriageResult(
                            classification=classification,
                            confidence="high"))
    queue_root = queue_env["queue_root"]
    bdir = _make_bundle_dir(tmp_path)
    job_path = _drop_synthetic_job(queue_env, bundle_dir=bdir,
                                   extra_fields={"run_id": run_id})
    inflight = queue_root / "inflight" / job_path.name
    job_path.rename(inflight)
    runner._apply_transition(job_path.name, lifecycle.JobEvent.CLAIM)
    runner.process_job(queue_root, inflight, [],
                       dry_run=False, playbooks_dir=None)
    pending = sorted((queue_root / "pending").glob("*-patch.job"))
    assert len(pending) <= 1
    if not pending:
        return None, bdir
    return runner.parse_job_file(pending[0]), bdir


def _prompt_carries_context(patch_job, bdir) -> bool:
    payload = runner.build_patch_payload(bdir, None, patch_job)
    return "## User Context" in payload


def test_a_retry_in_its_context_s_run_launches_with_the_context(
        queue_env, tmp_path, monkeypatch):
    """The retry-with-context loop: the operator answered run-1's
    request, and run-1's retriage launches with the answer in its
    prompt."""
    _plant_context(queue_env["conn"], "run-1", _ago(minutes=5))
    patch_job, bdir = _triage_in_run(queue_env, tmp_path, monkeypatch,
                                     "run-1")
    assert patch_job is not None
    assert patch_job["run_id"] == "run-1"
    assert _prompt_carries_context(patch_job, bdir)


@pytest.mark.parametrize("context_age, gave_up_age", [
    ({"hours": 30}, None),
    ({"days": 400}, {"days": 399}),
], ids=["previous-run", "a-year-old-row-after-the-window-rolled-over"])
def test_another_run_s_context_launches_nothing(queue_env, tmp_path,
                                                monkeypatch, context_age,
                                                gave_up_age):
    """A failure in a new run cannot carry an earlier run's text, so
    that text does not promote it: the port is asked about again, in
    the new run."""
    conn = queue_env["conn"]
    _plant_context(conn, "run-1", _ago(**context_age))
    if gave_up_age is not None:
        _plant_gave_up_patch(conn, "old-patch.job", _ago(**gave_up_age))
    patch_job, _ = _triage_in_run(queue_env, tmp_path, monkeypatch, "run-2")
    assert patch_job is None
    runs = [r["run_id"] for r in conn.execute(
        "SELECT run_id FROM user_context_requests WHERE origin = 'foo/bar'")]
    assert runs == ["run-2"]


def test_a_retry_that_waited_past_the_window_still_launches(
        queue_env, tmp_path, monkeypatch, set_setting):
    """A retry waits while another job of its port is active on its
    line; the context it was enqueued for still counts when it runs."""
    set_setting("runner.attempt_window_hours", 2)
    _plant_context(queue_env["conn"], "run-1", _ago(hours=3))
    patch_job, bdir = _triage_in_run(queue_env, tmp_path, monkeypatch,
                                     "run-1")
    assert patch_job is not None
    assert _prompt_carries_context(patch_job, bdir)


@pytest.mark.parametrize("job_run, pardoned", [
    ("run-1", True),
    ("run-2", False),
])
def test_the_patch_cap_is_pardoned_only_in_the_context_s_run(
        queue_env, tmp_path, monkeypatch, set_setting, job_run, pardoned):
    """Context newer than the last gave-up attempt forgives the cap,
    but only for a job whose prompt will carry it."""
    set_setting("runner.max_patch_attempts", 3)
    set_setting("runner.attempt_window_hours", 2)
    conn = queue_env["conn"]
    _plant_run(conn, "run-2")
    for n, minutes in enumerate((60, 61, 62)):
        _plant_gave_up_patch(conn, f"gave-up-{n}.job", _ago(minutes=minutes))
    _plant_context(conn, "run-1", _ago(minutes=30))
    patch_job, bdir = _triage_in_run(queue_env, tmp_path, monkeypatch,
                                     job_run, classification="compile-error")
    assert (patch_job is not None) is pardoned
    if pardoned:
        assert _prompt_carries_context(patch_job, bdir)


def test_a_run_with_no_recorded_target_still_counts_for_its_own_jobs(
        queue_env, tmp_path, monkeypatch):
    """runs.target is never read: a run with none still promotes its
    own jobs, whose prompts carry its context."""
    conn = queue_env["conn"]
    _plant_run(conn, "run-legacy", target=None)
    _plant_context(conn, "run-legacy", _ago(minutes=5))
    patch_job, bdir = _triage_in_run(queue_env, tmp_path, monkeypatch,
                                     "run-legacy")
    assert patch_job is not None
    assert _prompt_carries_context(patch_job, bdir)


def test_context_for_another_origin_in_this_run_launches_nothing(
        queue_env, tmp_path, monkeypatch):
    """Context is keyed (run_id, origin): another port's answer in the
    same run does not promote this one."""
    _plant_context(queue_env["conn"], "run-1", _ago(minutes=5),
                   origin="other/port")
    patch_job, _ = _triage_in_run(queue_env, tmp_path, monkeypatch, "run-1")
    assert patch_job is None
