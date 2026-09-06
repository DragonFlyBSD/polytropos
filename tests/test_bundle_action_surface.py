"""What the bundle page offers, against what the worklist asks for.

The worklist routes an occurrence to a band -- ready / verify / decide /
owned -- and the detail page is where the operator acts on it. A bundle the
worklist sends to "needs a decision" while the page offers no way to make
one is a dead end, and that is what triage_failed was (poly-kp60).

The invariant below is the general form of that bug; the rest pin the
particular lanes, including the two actions that stay off and why.
"""

from __future__ import annotations

import pytest

from dportsv3.tracker import fix_state as fs

# Every (resolution, verification_status, job state) an occurrence can hold,
# named the way an operator would describe it.
SHAPES = [
    ("never triaged, job working it", {"resolution": None, "state": "patching"}),
    ("never triaged, nobody on it", {"resolution": None}),
    ("triage could not run", {"resolution": "triage_failed"}),
    ("agent gave up", {"resolution": "agent_gave_up"}),
    ("agent out of budget", {"resolution": "agent_budget_exhausted"}),
    ("escalated to a human", {"resolution": "escalated_manual"}),
    ("fix proposed", {"resolution": "agent_fixed"}),
    ("fix verified", {"resolution": "agent_fixed",
                      "verification_status": "verified"}),
    ("verify failed", {"resolution": "agent_fixed",
                       "verification_status": "verification_failed"}),
    ("operator owns it", {"resolution": "operator_owned"}),
    ("accepted", {"resolution": "accepted"}),
    ("rejected", {"resolution": "rejected"}),
    ("discarded", {"resolution": "discarded"}),
    ("merged upstream", {"resolution": "merged"}),
]

CAPABILITIES = (
    "can_verify", "can_accept", "can_reject", "can_take_over",
    "can_discard", "can_retry", "can_reopen", "can_release",
)


def _bundle(**over):
    row = {"resolution": None, "verification_status": None,
           "target": "@main", "origin": "lang/rust"}
    row.update(over)
    return row


def _offered(bundle):
    acts = fs.bundle_actions(bundle)
    return {name for name in CAPABILITIES if acts[name]}


# --- the invariant -------------------------------------------------------


@pytest.mark.parametrize(("name", "over"), SHAPES, ids=[s[0] for s in SHAPES])
def test_a_bundle_the_worklist_bands_can_be_acted_on(name, over) -> None:
    """If the worklist puts an occurrence in a band, the page must offer at
    least one thing to do with it. Only in_progress is exempt: while a job
    works it the operator genuinely has nothing to do, and it is the one
    status the worklist leaves unbanded."""
    bundle = _bundle(**over)
    bucket = fs.worklist_bucket(bundle)

    if bucket is None:
        assert fs.fix_status(bundle).key == "in_progress"
        assert fs.bundle_actions(bundle)["show"] is False
        return

    assert _offered(bundle), f"{name} buckets to {bucket} with nothing to do"
    assert fs.bundle_actions(bundle)["show"] is True


@pytest.mark.parametrize(("name", "over"), SHAPES, ids=[s[0] for s in SHAPES])
def test_the_surface_never_offers_what_the_gate_refuses(name, over) -> None:
    """bundle_actions is deliberately narrower than ACTION_ALLOWED. Wider
    would mean a button that 409s."""
    bundle = _bundle(**over)
    acts = fs.bundle_actions(bundle)
    for capability, action in (
        ("can_verify", "verify"), ("can_accept", "accept"),
        ("can_reject", "reject"), ("can_take_over", "take-over"),
        ("can_discard", "discard"), ("can_retry", "retry"),
        ("can_reopen", "reopen"), ("can_release", "release"),
    ):
        if acts[capability]:
            assert fs.action_allowed(
                action, bundle["resolution"], bundle["verification_status"]
            ), f"{name}: surfaced {action} the gate refuses"


# --- the untriaged lane --------------------------------------------------


def test_a_triage_failure_offers_the_untriaged_lane() -> None:
    """Re-run the triage, take it over by hand, or drop it. The bundle is a
    real port failure that nothing has looked at yet."""
    assert _offered(_bundle(resolution="triage_failed")) == {
        "can_retry", "can_take_over", "can_discard",
    }


def test_a_triage_failure_is_not_offered_a_verify() -> None:
    """verify replays analysis/changes.diff and triage is what produces one,
    so run_verify_fix raises on the 404 and again on an empty diff. The
    action gate permits it; offering it would queue a job that cannot
    succeed."""
    assert fs.action_allowed("verify", "triage_failed", None) is True
    assert fs.bundle_actions(_bundle(resolution="triage_failed"))[
        "can_verify"] is False


def test_a_triage_failure_is_not_offered_a_reject() -> None:
    """Rejecting a fix that was never produced is meaningless. The gate
    permits it, and this is exactly the narrowing bundle_actions is for."""
    assert fs.action_allowed("reject", "triage_failed", None) is True
    assert fs.bundle_actions(_bundle(resolution="triage_failed"))[
        "can_reject"] is False


def test_a_bundle_nobody_is_working_gets_the_same_lane() -> None:
    """A NULL resolution with no live job -- reopened, or a job that died
    without writing one -- is untriaged for the same reason and the worklist
    already routes it to the same band."""
    bundle = _bundle(resolution=None)

    assert fs.fix_status(bundle).key == "unknown"
    assert fs.worklist_bucket(bundle) == "decide"
    assert _offered(bundle) == {"can_retry", "can_take_over", "can_discard"}


def test_a_bundle_a_job_is_working_is_left_alone() -> None:
    """The resolution is NULL in both cases; only the job state separates
    them, so the surface has to read it."""
    bundle = _bundle(resolution=None, state="patching")

    assert fs.fix_status(bundle).key == "in_progress"
    assert fs.bundle_actions(bundle)["show"] is False


def test_take_over_needs_somewhere_to_stake() -> None:
    """take-over opens an origin_skip_flags row for (target, origin); with
    neither there is nothing to stake."""
    assert fs.bundle_actions(
        _bundle(resolution="triage_failed", target="", origin=""),
    )["can_take_over"] is False


# --- the two sets are answering different questions ----------------------


def test_the_failure_statistics_do_not_absorb_infra() -> None:
    """FAILURE_RESOLUTIONS is "how did the agent's attempt end", and it
    feeds the counts. Widening the ACTION lane must not widen that -- the
    poly-h6c lesson was that counting infra as failure overstated it 3.1x."""
    assert "triage_failed" not in fs.FAILURE_RESOLUTIONS
    assert "triage_failed" in fs.UNTRIAGED_RESOLUTIONS
    assert fs.FAILURE_RESOLUTIONS < fs.UNTRIAGED_RESOLUTIONS


def test_the_gate_lets_an_operator_take_over_an_untriaged_bundle() -> None:
    for resolution in (None, "triage_failed", "agent_gave_up"):
        assert fs.action_allowed("take-over", resolution, None) is True
        assert fs.action_allowed("discard", resolution, None) is True


def test_an_anonymous_reader_is_offered_nothing() -> None:
    """Not a blocked operator: no panel at all, on every shape."""
    for _, over in SHAPES:
        acts = fs.bundle_actions(_bundle(**over), can_operate=False)
        assert acts["show"] is False
        assert not any(acts[name] for name in CAPABILITIES)
