"""A harness raise must not take the agent's work with it (poly-be6o).

www/chromium, job 20260909-194249Z-2026Q3-www_chromium-309853-patch:

    19:42:57  bundle_branch_checkout  bundle/www_chromium-...
    20:33-01:09  put_file x4 -> two finished dragonfly patches
    01:09:39  dsynth_build -> 8 h, 34,886/54,854 targets (63.6%)
    09:23:39  api_error 400 invalid_request_error
    09:23:42  bundle_branch_dropped (patch_failure)

Thirteen hours and forty-one minutes. Recovered afterwards: nothing.
The bundle held manual_handoff.md, triage.md, triage_result.json and
the *triage* session. No changes.diff, no patch session, no tool_trace.

Two causes, one shape. Everything that persists the agent's work sits
below the ``except`` in the patch step, on the success path, so the
raise skipped all of it; and ``session_dump`` is invoked after
``tool_loop.run`` returns, so a raise from inside the loop left no
transcript either. The worktree that held the edits was then destroyed
by the caller, and the agent never commits, so there was no commit to
recover.

These tests pin the salvage: capture inside the step, while the tree
still exists, and dump the conversation on the way out.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from dportsv3.agent import attempt_loop, runner, steps, worker
from dportsv3.agent.step import StepCtx


# --- _write_changes_diff: only_if_nonempty ----------------------------------

class _FakeEnvPaths:
    def __init__(self, deltaports: Path) -> None:
        self.deltaports = deltaports
        self.writable = deltaports.parent
        self.env_dir = deltaports.parent


@pytest.fixture
def _diff_env(tmp_path, monkeypatch):
    """Wire the worker calls _write_changes_diff makes, and hand back a
    setter for what the in-chroot git diff produces."""
    deltaports = tmp_path / "writable" / "work" / "DeltaPorts"
    deltaports.mkdir(parents=True)
    monkeypatch.setattr(worker, "env_paths", lambda env: _FakeEnvPaths(deltaports))
    monkeypatch.setattr(worker, "_resolve_bundle_base_branch", lambda env: "main")

    def _set(stdout=None, raises=None):
        def _fake(env, base, rel):
            if raises is not None:
                raise raises
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        monkeypatch.setattr(worker, "_git_diff_against_base", _fake)

    bundle_dir = tmp_path / "bundle-x"
    bundle_dir.mkdir()
    return SimpleNamespace(set=_set, bundle_dir=bundle_dir)


def test_a_captured_diff_is_written_and_its_size_returned(_diff_env):
    """The rescue path needs to know whether it saved anything — that
    is the difference between an activity row an operator acts on and
    one they scroll past."""
    _diff_env.set(stdout="diff --git a/x b/x\n+new\n")
    n = runner._write_changes_diff(
        _diff_env.bundle_dir, None, env="e1", origin="devel/foo",
        only_if_nonempty=True,
    )
    out = _diff_env.bundle_dir / "analysis" / "changes.diff"
    assert out.is_file()
    assert n == len(out.read_bytes()) > 0


def test_an_empty_capture_writes_nothing_under_only_if_nonempty(_diff_env):
    """A raise before the agent touched anything has nothing to file,
    and a bundle whose earlier job produced a real diff must not have
    it replaced by an empty one."""
    _diff_env.set(stdout="")
    assert runner._write_changes_diff(
        _diff_env.bundle_dir, None, env="e1", origin="devel/foo",
        only_if_nonempty=True,
    ) == 0
    assert not (_diff_env.bundle_dir / "analysis" / "changes.diff").exists()


def test_a_failed_capture_raises_rather_than_filing_a_tombstone(_diff_env):
    """On the success path a tombstone is the right answer: it tells
    the operator the shape of the failure instead of breaking delivery
    silently. On the rescue path it would overwrite real work with an
    error string — and a 0 return would be indistinguishable from
    "the agent changed nothing", which is the one answer we must not
    guess at while the tree is being deleted."""
    _diff_env.set(raises=RuntimeError("chroot gone"))
    with pytest.raises(RuntimeError, match="chroot gone"):
        runner._write_changes_diff(
            _diff_env.bundle_dir, None, env="e1", origin="devel/foo",
            only_if_nonempty=True,
        )
    assert not (_diff_env.bundle_dir / "analysis" / "changes.diff").exists()


def test_a_nonzero_git_is_a_failed_capture_not_an_empty_one(_diff_env, monkeypatch):
    """An unreachable chroot yields rc!=0 with empty stdout. Read as a
    diff that is merely empty, it becomes 'the agent changed nothing' —
    which is exactly the report that would hide the next lost night."""
    monkeypatch.setattr(
        worker, "_git_diff_against_base",
        lambda env, base, rel: SimpleNamespace(
            returncode=128, stdout="", stderr="fatal: not a git repository\n",
        ),
    )
    with pytest.raises(RuntimeError, match="128"):
        runner._write_changes_diff(
            _diff_env.bundle_dir, None, env="e1", origin="devel/foo",
            only_if_nonempty=True,
        )
    # Default path keeps its contract: a tombstone naming the failure.
    runner._write_changes_diff(
        _diff_env.bundle_dir, None, env="e1", origin="devel/foo",
    )
    body = (_diff_env.bundle_dir / "analysis" / "changes.diff").read_text()
    assert body.startswith("# failed to capture diff:")
    assert "not a git repository" in body


def test_the_success_path_still_files_a_tombstone(_diff_env):
    """only_if_nonempty is opt-in; the default behaviour that delivery
    depends on is unchanged."""
    _diff_env.set(raises=RuntimeError("chroot gone"))
    runner._write_changes_diff(
        _diff_env.bundle_dir, None, env="e1", origin="devel/foo",
    )
    body = (_diff_env.bundle_dir / "analysis" / "changes.diff").read_text()
    assert body.startswith("# failed to capture diff:")


# --- _rescue_work_on_raise --------------------------------------------------

def _rescue_ctx(tmp_path):
    ctx = StepCtx(
        job_id="job-x", job={"origin": "www/chromium", "bundle_id": "b-1"},
        queue_root=tmp_path, bundle_dir=tmp_path,
    )
    return ctx


def _services(**over):
    calls: dict = {"rows": [], "trace": None, "diff_kwargs": None}

    def _log(queue_root, stage, msg, **kw):
        calls["rows"].append((stage, msg, kw.get("extra") or {}))

    def _trace(bundle_dir, bundle_id, events):
        calls["trace"] = list(events)

    def _diff(bundle_dir, bundle_id, env, origin, **kw):
        calls["diff_kwargs"] = kw
        return 6923

    svc = SimpleNamespace(
        activity_log=_log, write_tool_trace=_trace, write_changes_diff=_diff,
    )
    for k, v in over.items():
        setattr(svc, k, v)
    return svc, calls


def _stages(calls):
    return [stage for stage, _, _ in calls["rows"]]


def test_the_rescued_diff_also_lands_where_nothing_overwrites_it(tmp_path):
    """changes.diff belongs to whichever job wrote last. A requeued
    attempt that gives up cleanly writes an empty one over it — it
    starts from base, so the rescued edits are not in its tree — and
    the salvage would be undone by the retry it enabled."""
    svc, calls = _services()
    steps._rescue_work_on_raise(
        svc, _rescue_ctx(tmp_path),
        env="2026Q3", origin="www/chromium", bundle_id="b-1",
        trace_events=[],
    )
    assert calls["diff_kwargs"]["extra_relpaths"] == (
        "analysis/rescued/job-x.diff",
    )


def test_extra_relpaths_get_the_same_bytes(_diff_env):
    """One capture, two destinations — a second git call could
    disagree with the first about a tree being torn down."""
    _diff_env.set(stdout="diff --git a/x b/x\n+new\n")
    runner._write_changes_diff(
        _diff_env.bundle_dir, None, env="e1", origin="devel/foo",
        only_if_nonempty=True,
        extra_relpaths=("analysis/rescued/job-x.diff",),
    )
    canonical = _diff_env.bundle_dir / "analysis" / "changes.diff"
    rescued = _diff_env.bundle_dir / "analysis" / "rescued" / "job-x.diff"
    assert rescued.read_bytes() == canonical.read_bytes()


def test_the_rescue_files_the_trace_and_the_diff(tmp_path):
    """Both artifacts exist by this point at no extra cost: the
    dispatcher accumulated the trace live, and the diff is one
    in-chroot git call against a tree that is about to be deleted."""
    svc, calls = _services()
    events = [{"type": "tool_call", "tool": "put_file"}]
    steps._rescue_work_on_raise(
        svc, _rescue_ctx(tmp_path),
        env="2026Q3", origin="www/chromium", bundle_id="b-1",
        trace_events=events,
    )
    assert calls["trace"] == events
    assert calls["diff_kwargs"]["only_if_nonempty"] is True
    assert "patch_work_rescued" in _stages(calls)


def test_the_rescued_row_carries_the_byte_count(tmp_path):
    """'work rescued' with no number is not evidence. The chromium job
    emitted 6,923 bytes to the model and persisted none of them."""
    svc, calls = _services()
    steps._rescue_work_on_raise(
        svc, _rescue_ctx(tmp_path),
        env="2026Q3", origin="www/chromium", bundle_id="b-1",
        trace_events=[],
    )
    row = next(r for r in calls["rows"] if r[0] == "patch_work_rescued")
    assert row[2]["diff_bytes"] == 6923
    assert "6923" in row[1]


def test_nothing_to_capture_says_so_rather_than_claiming_a_rescue(tmp_path):
    """A raise in the first minute is a different event from a raise at
    hour thirteen, and the operator reads only the activity row."""
    svc, calls = _services(
        write_changes_diff=lambda *a, **kw: 0,
    )
    steps._rescue_work_on_raise(
        svc, _rescue_ctx(tmp_path),
        env="2026Q3", origin="www/chromium", bundle_id="b-1",
        trace_events=[],
    )
    assert "patch_work_rescue_empty" in _stages(calls)
    assert "patch_work_rescued" not in _stages(calls)


def test_a_capture_failure_is_reported_not_raised(tmp_path):
    """This runs inside an except. Raising here would replace the
    provider error the operator needs with a git error they do not."""
    def _boom(*a, **kw):
        raise RuntimeError("worktree already gone")

    svc, calls = _services(write_changes_diff=_boom)
    steps._rescue_work_on_raise(
        svc, _rescue_ctx(tmp_path),
        env="2026Q3", origin="www/chromium", bundle_id="b-1",
        trace_events=[],
    )
    assert "patch_work_rescue_failed" in _stages(calls)


def test_a_trace_write_failure_does_not_cost_us_the_diff(tmp_path):
    """The trace is the cheaper artifact and it is written first;
    losing it must not take the expensive one down with it."""
    def _boom(*a, **kw):
        raise RuntimeError("artifact store unreachable")

    svc, calls = _services(write_tool_trace=_boom)
    steps._rescue_work_on_raise(
        svc, _rescue_ctx(tmp_path),
        env="2026Q3", origin="www/chromium", bundle_id="b-1",
        trace_events=[{"type": "tool_call"}],
    )
    assert calls["diff_kwargs"]["only_if_nonempty"] is True
    assert "patch_work_rescued" in _stages(calls)


def test_a_broken_activity_log_cannot_break_the_rescue(tmp_path):
    """Bookkeeping must not be able to fail the thing it books."""
    def _boom(*a, **kw):
        raise RuntimeError("queue root vanished")

    svc, calls = _services(activity_log=_boom)
    steps._rescue_work_on_raise(
        svc, _rescue_ctx(tmp_path),
        env="2026Q3", origin="www/chromium", bundle_id="b-1",
        trace_events=[{"type": "tool_call"}],
    )
    assert calls["diff_kwargs"]["only_if_nonempty"] is True


# --- where the rescue sits --------------------------------------------------

def _except_block() -> str:
    """The patch step's api_error handler, verbatim."""
    src = inspect.getsource(steps)
    # The triage step raises an api_error row too; anchor on the patch
    # one so this does not silently assert about the wrong handler.
    start = src.index('f"Harness patch failed for {origin}')
    return src[start:src.index("duration_ms = int((time.time() - start)", start)]


def test_the_rescue_runs_before_the_outcome_is_decided():
    """_err may requeue, and a requeued job restarts from base — so
    what is filed here is all that survives either way."""
    block = _except_block()
    assert block.index("_rescue_work_on_raise") < block.index("outcome = _err(")


def test_the_rescue_precedes_the_handoff_that_reads_its_output():
    """manual_handoff.md summarizes analysis/changes.diff. Written in
    this order, the document an operator picks the port up from names
    the rescued work; written in the other, it reports nothing changed
    while the diff sits in the same bundle."""
    block = _except_block()
    assert block.index("_rescue_work_on_raise") < block.index("_try_write_handoff(")


def test_nowhere_to_write_is_not_reported_as_nothing_to_save(_diff_env):
    """The rescue reads the return value as a byte count. A real diff
    with no destination returning 0 would log 'nothing to capture' over
    the loss it is supposed to catch."""
    _diff_env.set(stdout="diff --git a/x b/x\n+new\n")
    with pytest.raises(RuntimeError, match="neither bundle_id nor bundle_dir"):
        runner._write_changes_diff(
            None, None, env="e1", origin="devel/foo", only_if_nonempty=True,
        )


def test_the_rescue_lives_in_the_step_not_the_caller():
    """runner.process_job destroys the worktree immediately after
    process_patch_job returns. A capture moved out to the caller reads
    a tree that no longer exists — this is the ordering that lost the
    thirteen hours, so it is pinned rather than commented."""
    src = inspect.getsource(runner.process_job)
    drop = src.index("_drop_bundle_branch_for_job(")
    call = src.index("success, status = process_patch_job(")
    assert call < drop
    assert "_rescue_work_on_raise(" not in src


def test_the_moot_premise_does_not_come_back():
    """'on failure the branch's state is moot' conflated the agent
    failing with the harness raising. The first is moot; the second
    holds everything the agent did."""
    src = inspect.getsource(runner.process_job)
    assert "the branch's state is moot" not in src
    assert "poly-be6o" in src


# --- the transcript ---------------------------------------------------------

def test_a_raising_attempt_still_dumps_its_session():
    """The 400 came from inside tool_loop.run, so the dump below it
    never ran and the bundle carried the triage session only — leaving
    no record of what the request that died actually contained."""
    src = inspect.getsource(attempt_loop.run)
    loop = src.index("response, attempt_usage, rebuild_ok_seen = tool_loop.run(")
    tail = src[loop:]
    # Newline-anchored: an unanchored match would find the 16-space
    # `except Exception:` of some inner best-effort try instead.
    handler = tail.index("\n        except Exception:\n")
    reraise = tail.index("\n            raise\n", handler)
    assert tail[handler:reraise].count("session_dump(attempt_idx, messages)") == 1
    assert handler < reraise


def test_the_dump_does_not_swallow_the_error():
    """The caller classifies this exception (transient vs terminal) and
    that decision drives requeue-or-retire."""
    src = inspect.getsource(attempt_loop.run)
    tail = src[src.index("\n        except Exception:\n"):]
    body = tail[:tail.index("\n            raise\n") + len("\n            raise\n")]
    assert body.rstrip().endswith("raise")
    assert "return PatchResult" not in body
