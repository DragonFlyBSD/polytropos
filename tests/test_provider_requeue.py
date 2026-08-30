"""A transient provider failure requeues the job instead of killing it.

poly-4av. Nine jobs died in one run because the provider was briefly
unreachable — six patch jobs on a bare 404 the SDK will not retry, and
three more after its ~1.5s ladder ran out. None of them had anything
wrong with them.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import runner
from dportsv3.agent.lifecycle import TRANSITIONS, JobEvent, JobState
from dportsv3.agent.step import StepCtx, StepOutcome, _reroute_if_transient


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
    monkeypatch.setattr(runner, "_setting_int",
                        lambda k, d: {"runner.llm_retry_backoff_seconds": 30,
                                      "runner.llm_retry_backoff_max_seconds": 900}[k])
    assert runner._llm_backoff_seconds(1) == 30
    assert runner._llm_backoff_seconds(2) == 60
    assert runner._llm_backoff_seconds(3) == 120
    # capped, and a silly tally cannot build a huge intermediate
    assert runner._llm_backoff_seconds(50) == 900


def test_no_failures_means_no_wait():
    assert runner._llm_backoff_seconds(0) == 0


# --- rerouting the outcome ---------------------------------------------------

def _ctx(should_requeue):
    return StepCtx(job_id="j1", job={}, should_requeue=should_requeue)


def _failed(transient: bool) -> StepOutcome:
    return StepOutcome(
        status="failed",
        next_event=JobEvent.PATCH_GAVE_UP,
        extra_events=[JobEvent.ESCALATE_MANUAL],
        detail={"status_str": "boom", "error": True, "transient": transient},
    )


def test_a_transient_failure_becomes_a_requeue():
    out = _reroute_if_transient(_ctx(lambda jid: True), _failed(True))
    assert out.next_event is JobEvent.PROVIDER_UNAVAILABLE
    assert out.detail["requeued"] is True


def test_the_requeue_drops_the_closing_events():
    """extra_events describe a job that is ending. This one is not."""
    out = _reroute_if_transient(_ctx(lambda jid: True), _failed(True))
    assert out.extra_events == []


def test_a_real_failure_is_left_alone():
    out = _reroute_if_transient(_ctx(lambda jid: True), _failed(False))
    assert out.next_event is JobEvent.PATCH_GAVE_UP


def test_a_spent_retry_budget_lets_the_job_die():
    out = _reroute_if_transient(_ctx(lambda jid: False), _failed(True))
    assert out.next_event is JobEvent.PATCH_GAVE_UP


def test_an_unbound_callback_keeps_the_old_behaviour():
    """Nothing requeues unless the runner wired the tally in."""
    out = _reroute_if_transient(_ctx(None), _failed(True))
    assert out.next_event is JobEvent.PATCH_GAVE_UP


def test_a_broken_tally_does_not_eat_the_job():
    def _boom(jid):
        raise RuntimeError("db gone")

    out = _reroute_if_transient(_ctx(_boom), _failed(True))
    assert out.next_event is JobEvent.PATCH_GAVE_UP


def test_a_successful_outcome_is_never_rerouted():
    ok = StepOutcome(status="success", next_event=JobEvent.PATCH_OK,
                     detail={"transient": True})
    assert _reroute_if_transient(_ctx(lambda jid: True), ok) is ok


# --- the columns the tally lives in ------------------------------------------

def test_the_jobs_table_carries_the_retry_state():
    """Both must also be in MIGRATIONS, or an existing state.db has a
    schema the code reads and the file does not have."""
    from dportsv3.db import schema

    assert "retry_count" in schema.SCHEMA
    assert "next_eligible_at TEXT" in schema.SCHEMA
    migrations = " ".join(schema.MIGRATIONS)
    assert "jobs ADD COLUMN retry_count" in migrations
    assert "jobs ADD COLUMN next_eligible_at" in migrations
