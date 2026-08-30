"""A transient provider failure requeues the job instead of killing it.

poly-4av. Nine jobs died in one run because the provider was briefly
unreachable — six patch jobs on a bare 404 the SDK will not retry, and
three more after its ~1.5s ladder ran out. None of them were faulty.

The tally lives in the job file, not the database: that file is what the
runner claims from and what moves between pending/ and inflight/, so the
count travels with the work and there is no second source of truth to
keep in step with it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dportsv3.agent import steps
from dportsv3.agent.lifecycle import TRANSITIONS, JobEvent, JobState
from dportsv3.agent.step import StepOutcome


# --- the state machine -------------------------------------------------------

@pytest.mark.parametrize("state", [
    JobState.CLAIMED, JobState.TRIAGING, JobState.PATCHING,
])
def test_a_provider_failure_goes_back_to_queued(state):
    """CLAIMED is in the set because the model call can raise before the
    step's own *_START event has landed."""
    assert TRANSITIONS[(state, JobEvent.PROVIDER_UNAVAILABLE)] is JobState.QUEUED


def test_a_terminal_job_cannot_be_requeued():
    for state in (JobState.DONE, JobState.DEAD, JobState.ESCALATED):
        assert (state, JobEvent.PROVIDER_UNAVAILABLE) not in TRANSITIONS


# --- the backoff -------------------------------------------------------------

def test_the_backoff_doubles_and_then_caps(monkeypatch):
    monkeypatch.setattr(steps.settings, "get",
                        lambda k: {"runner.llm_retry_backoff_seconds": 30,
                                   "runner.llm_retry_backoff_max_seconds": 900}[k])
    assert steps._retry_backoff_seconds(1) == 30
    assert steps._retry_backoff_seconds(2) == 60
    assert steps._retry_backoff_seconds(3) == 120
    # capped, and a silly tally cannot build a huge intermediate
    assert steps._retry_backoff_seconds(50) == 900


def test_no_failures_means_no_wait():
    assert steps._retry_backoff_seconds(0) == 0


# --- the tally, in the job file ----------------------------------------------

def _job(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "20260830-010101Z-2026Q3-devel_nspr-1234.job"
    p.write_text("origin=devel/nspr\ntarget=@2026Q3\n" + extra)
    return p


def test_a_failure_is_counted_and_stamped(tmp_path):
    job = _job(tmp_path)
    assert steps.record_transient_failure(job) is True
    meta = dict(l.partition("=")[::2] for l in job.read_text().splitlines() if l)
    assert meta["retry_count"] == "1"
    assert meta["retry_after"] > datetime.now(timezone.utc).isoformat()
    # the original content survives the rewrite
    assert meta["origin"] == "devel/nspr"
    assert meta["target"] == "@2026Q3"


def test_the_count_accumulates_without_duplicating_keys(tmp_path):
    job = _job(tmp_path)
    for expected in ("1", "2", "3"):
        assert steps.record_transient_failure(job) is True
        lines = [l for l in job.read_text().splitlines() if l.startswith("retry_count=")]
        assert lines == [f"retry_count={expected}"]


def test_a_spent_budget_stops_requeueing(tmp_path, monkeypatch):
    monkeypatch.setattr(steps.settings, "get",
                        lambda k: 2 if k == "runner.llm_retry_max" else 30)
    job = _job(tmp_path)
    assert steps.record_transient_failure(job) is True
    assert steps.record_transient_failure(job) is True
    assert steps.record_transient_failure(job) is False


def test_an_unreadable_job_file_does_not_requeue(tmp_path):
    """No tally means no way to stop; a job that dies is recoverable,
    a job that spins is not."""
    assert steps.record_transient_failure(tmp_path / "gone.job") is False


# --- being held back ---------------------------------------------------------

def test_a_future_stamp_holds_the_job_back():
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert steps.job_held_back({"retry_after": later}) is True


def test_an_expired_stamp_releases_it():
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert steps.job_held_back({"retry_after": past}) is False


def test_a_job_that_never_failed_is_never_held_back():
    assert steps.job_held_back({"origin": "devel/nspr"}) is False
    assert steps.job_held_back({}) is False


# --- what _err returns -------------------------------------------------------

class _Services:
    def __init__(self):
        self.notes: list[str] = []

    def write_error_note(self, job_path, msg):
        self.notes.append(msg)


def test_a_transient_failure_becomes_a_requeue(tmp_path):
    job = _job(tmp_path)
    out = steps._err("boom", _Services(), job, JobEvent.PATCH_GAVE_UP,
                     transient=True)
    assert out.next_event is JobEvent.PROVIDER_UNAVAILABLE
    assert out.detail["requeued"] is True


def test_a_real_failure_retires_the_job(tmp_path):
    out = steps._err("boom", _Services(), _job(tmp_path),
                     JobEvent.PATCH_GAVE_UP, transient=False)
    assert out.next_event is JobEvent.PATCH_GAVE_UP
    assert "requeued" not in out.detail


def test_the_job_dies_once_the_budget_is_spent(tmp_path, monkeypatch):
    monkeypatch.setattr(steps.settings, "get",
                        lambda k: 1 if k == "runner.llm_retry_max" else 30)
    job = _job(tmp_path)
    assert steps._err("boom", _Services(), job, JobEvent.TRIAGE_FAIL,
                      transient=True).next_event is JobEvent.PROVIDER_UNAVAILABLE
    assert steps._err("boom", _Services(), job, JobEvent.TRIAGE_FAIL,
                      transient=True).next_event is JobEvent.TRIAGE_FAIL


def test_a_transient_failure_is_still_a_failed_outcome(tmp_path):
    """Requeued or not, the step did not succeed — the orchestrator must
    not route it down the happy path."""
    out = steps._err("boom", _Services(), _job(tmp_path),
                     JobEvent.PATCH_GAVE_UP, transient=True)
    assert isinstance(out, StepOutcome)
    assert out.status == "failed"


def test_a_requeue_writes_no_error_note(tmp_path):
    """The note travels with the file into pending/, where it would read
    as a failed job. The job is waiting, not broken."""
    svc = _Services()
    steps._err("boom", svc, _job(tmp_path), JobEvent.PATCH_GAVE_UP,
               transient=True)
    assert svc.notes == []


def test_retiring_the_job_still_writes_the_note(tmp_path):
    svc = _Services()
    steps._err("boom", svc, _job(tmp_path), JobEvent.PATCH_GAVE_UP,
               transient=False)
    assert svc.notes == ["boom"]


def test_the_note_returns_once_the_budget_is_spent(tmp_path, monkeypatch):
    monkeypatch.setattr(steps.settings, "get",
                        lambda k: 1 if k == "runner.llm_retry_max" else 30)
    svc, job = _Services(), _job(tmp_path)
    steps._err("boom", svc, job, JobEvent.PATCH_GAVE_UP, transient=True)
    steps._err("boom", svc, job, JobEvent.PATCH_GAVE_UP, transient=True)
    assert svc.notes == ["boom"], "only the terminal failure leaves a note"
