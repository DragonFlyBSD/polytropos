"""A patch job leaves the env matching the repository, however it ended.

poly-at76. multimedia/libx264 failed on ironman with a rejected hunk in
x264's dragonfly/patch-common_cpu.c, and built green under ``dbuild`` in
the acemagic env. The env's composed x264 carried a re-cut patch and a
CFLAGS line DeltaPorts never had: a dead patch job's last attempt, left
there by its own post-job reset, which composed the job's worktree.

``worker.reset_port`` now composes the main checkout
(test_reset_port_workdir_clean.py). These pin the step side: the reset
covers every origin the job may have dirtied, runs on the harness-raised
path too, and cannot turn into the failure an operator reads.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from dportsv3.agent import steps, worker
from dportsv3.agent.step import StepCtx


def _ctx(tmp_path):
    return StepCtx(
        job_id="job-x", job={"origin": "multimedia/libx264"},
        queue_root=tmp_path, bundle_dir=tmp_path,
    )


def _services(rows, **over):
    def _log(queue_root, stage, msg, **kw):
        rows.append((stage, msg))

    svc = SimpleNamespace(activity_log=_log)
    for k, v in over.items():
        setattr(svc, k, v)
    return svc


def _fake_reset(seen, result=None):
    def _reset(env, origin, also=()):
        seen.append((origin, list(also)))
        return result or {"ok": True, "baseline_compose_ok": True}
    return _reset


def test_the_origins_the_job_started_with_are_reset(tmp_path, monkeypatch):
    """The agent can move MASTERDIR or PATCHDIR. Resetting only what the
    relation names afterwards leaves the master it edited first dirty."""
    seen, rows = [], []
    monkeypatch.setattr(worker, "reset_port", _fake_reset(seen))
    monkeypatch.setattr(worker, "invariant_origins",
                        lambda env, o: [o, "multimedia/x264-new"])

    steps._reset_after_patch(
        _services(rows), _ctx(tmp_path), env="main",
        origin="multimedia/libx264", started_with=["multimedia/x264"],
    )

    assert seen == [("multimedia/libx264",
                     ["multimedia/x264", "multimedia/x264-new"])]
    assert rows[0][0] == "patch_post_reset"
    assert "ports/multimedia/x264/" in rows[0][1]
    assert "ports/multimedia/x264-new/" in rows[0][1]


def test_an_origin_named_twice_is_reset_once(tmp_path, monkeypatch):
    seen, rows = [], []
    monkeypatch.setattr(worker, "reset_port", _fake_reset(seen))
    monkeypatch.setattr(worker, "invariant_origins",
                        lambda env, o: [o, "multimedia/x264"])

    steps._reset_after_patch(
        _services(rows), _ctx(tmp_path), env="main",
        origin="multimedia/libx264", started_with=["multimedia/x264"],
    )

    assert seen == [("multimedia/libx264", ["multimedia/x264"])]


def test_a_failed_relation_probe_still_resets_what_the_job_started_with(
        tmp_path, monkeypatch):
    seen, rows = [], []
    monkeypatch.setattr(worker, "reset_port", _fake_reset(seen))

    def _boom(env, o):
        raise TimeoutError("make -V timed out")

    monkeypatch.setattr(worker, "invariant_origins", _boom)

    steps._reset_after_patch(
        _services(rows), _ctx(tmp_path), env="main",
        origin="multimedia/libx264", started_with=["multimedia/x264"],
    )

    assert seen == [("multimedia/libx264", ["multimedia/x264"])]


def test_the_log_row_says_whether_the_baseline_composed(tmp_path, monkeypatch):
    """The old row said plain success while the tree contradicted the
    repo (poly-15l). The compose verdict is what an operator needs."""
    seen, rows = [], []
    monkeypatch.setattr(worker, "reset_port", _fake_reset(
        seen, {"ok": True, "baseline_compose_ok": False}))
    monkeypatch.setattr(worker, "invariant_origins", lambda env, o: [o])

    steps._reset_after_patch(
        _services(rows), _ctx(tmp_path), env="main", origin="devel/foo",
    )

    assert seen == [("devel/foo", [])]
    assert rows == [("patch_post_reset",
                     "reset ports/devel/foo/ to baseline after patch "
                     "(baseline_compose_ok=False)")]


def test_a_raising_reset_is_a_row_not_an_exception(tmp_path, monkeypatch):
    rows = []

    def _boom(env, origin, also=()):
        raise RuntimeError("env unreachable")

    monkeypatch.setattr(worker, "reset_port", _boom)
    monkeypatch.setattr(worker, "invariant_origins", lambda env, o: [o])

    steps._reset_after_patch(
        _services(rows), _ctx(tmp_path), env="main", origin="devel/foo",
    )

    assert rows[0][0] == "patch_post_reset_failed"
    assert "env unreachable" in rows[0][1]


def test_a_broken_activity_log_cannot_escape(tmp_path, monkeypatch):
    """On the raise path this runs inside the except; raising here would
    replace the provider error the operator needs."""
    def _boom(*a, **kw):
        raise RuntimeError("queue root vanished")

    monkeypatch.setattr(worker, "reset_port", _fake_reset([]))
    monkeypatch.setattr(worker, "invariant_origins", lambda env, o: [o])

    steps._reset_after_patch(
        _services([], activity_log=_boom), _ctx(tmp_path),
        env="main", origin="devel/foo",
    )


# --- both ways out of the agent ---------------------------------------------

def _except_block() -> str:
    """The patch step's api_error handler, verbatim."""
    src = inspect.getsource(steps)
    start = src.index('f"Harness patch failed for {origin}')
    return src[start:src.index("duration_ms = int((time.time() - start)", start)]


def test_the_raise_path_resets_too():
    """It returned before the reset, leaving the tree as the agent's last
    materialize_dports wrote it, and the WRKDIR for the next job's first
    attempt, which does not clean it."""
    block = _except_block()
    assert "_reset_after_patch(" in block
    # After the rescue, which captures the worktree; before the outcome,
    # which may requeue.
    assert block.index("_rescue_work_on_raise") < block.index("_reset_after_patch(")
    assert block.index("_reset_after_patch(") < block.index("outcome = _err(")


def test_the_result_path_resets_after_the_diff_is_captured():
    src = inspect.getsource(steps.PatchAttemptStep.run)
    after = src[src.index("duration_ms = int((time.time() - start)"):]
    assert "_reset_after_patch(" in after
    assert after.index("services.write_changes_diff(") < after.index(
        "_reset_after_patch(")
    assert "started_with=also_origins" in after


def test_the_step_no_longer_calls_reset_port_itself():
    """One place decides what is reset and what the row says."""
    src = inspect.getsource(steps.PatchAttemptStep.run)
    assert "reset_port(" not in src
