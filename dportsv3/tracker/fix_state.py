"""Single source of truth for a failure bundle's operator-facing state
and the actions allowed on it.

Two concepts, previously smeared across the bundle-detail view handler
(a ~130-line inline matrix) and each of the 8 operator POST endpoints
(each re-deriving its own allowed-resolution set):

- ``fix_status(bundle)`` — the **state projection**. Folds the three raw
  vocabularies (``bundle.resolution``, ``verification_status``, and the
  in-flight ``job.state``) into one operator-facing status: a key, a
  human label, and a pill class. Templates render this instead of the
  raw columns.

- Action policy, with a deliberate two-level split:
    * ``ACTION_ALLOWED[action](resolution, verification_status)`` — the
      **authoritative gate**. This is what an operator POST endpoint
      checks: "may this action run against a bundle in this state?"
      Endpoints keep their own metadata/concurrency guards (target /
      origin / run_id present, skip-lock ownership) and their own HTTP
      shaping — those were never duplicated policy.
    * ``bundle_actions(bundle)`` — the **UI surface**. Which buttons a
      page shows/enables. Deliberately *narrower* than ``ACTION_ALLOWED``
      (e.g. take-over is authorized on a NULL-resolution bundle via the
      CLI, but the UI hides it to avoid noise on un-triaged bundles).
      Returning both, from one place, is what lets that intended
      narrowing be explicit instead of drifting silently.

Resolution vocabulary is the agent-set half from ``agent.lifecycle``
(``_EVENT_TO_RESOLUTION``) plus the operator-set half defined here; both
are re-exported as constants so nothing string-literals them again. No
legacy handling — the DB is wiped, not migrated, so there are no old
column values to tolerate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# --- Resolution vocabulary (one definition) --------------------------------

# Agent/loop-set resolutions (mirrors agent.lifecycle._EVENT_TO_RESOLUTION).
RESOLUTION_AGENT_FIXED = "agent_fixed"
RESOLUTION_AGENT_GAVE_UP = "agent_gave_up"
RESOLUTION_AGENT_BUDGET = "agent_budget_exhausted"
RESOLUTION_ESCALATED = "escalated_manual"
RESOLUTION_TRIAGE_FAILED = "triage_failed"

# Operator-set resolutions (set by the tracker's POST endpoints).
RESOLUTION_ACCEPTED = "accepted"
RESOLUTION_REJECTED = "rejected"
RESOLUTION_DISCARDED = "discarded"
RESOLUTION_OPERATOR_OWNED = "operator_owned"

# Delivery-set resolution: the bundle's PR landed upstream. Set by the
# lazy merge-reconciler (delivery_sync) or the manual delivery-status
# endpoint. Dominant + terminal — a merged PR means the codebase already
# changed, so there is nothing left to accept/reject/verify. Whatever the
# resolution was (agent_fixed after a reopen, accepted, even rejected),
# upstream reality wins.
RESOLUTION_MERGED = "merged"

# Terminal decisions — nothing acts on these except reopen.
TERMINAL_RESOLUTIONS: frozenset[str] = frozenset(
    {
        RESOLUTION_ACCEPTED, RESOLUTION_REJECTED,
        RESOLUTION_DISCARDED, RESOLUTION_MERGED,
    }
)

# "The agent tried and lost" outcomes — the take-over / discard / retry
# lane. triage_failed is deliberately NOT here (it signals infra, not a
# lost fight) and matches the pre-refactor behavior.
FAILURE_RESOLUTIONS: frozenset[str] = frozenset(
    {
        RESOLUTION_AGENT_BUDGET,
        RESOLUTION_AGENT_GAVE_UP,
        RESOLUTION_ESCALATED,
    }
)

# "No fix exists and nobody is working it" — the take-over / discard lane.
#
# Wider than FAILURE_RESOLUTIONS on purpose. That set answers "how did the
# agent's ATTEMPT end", which is what the failure statistics count; this one
# answers "what may an operator do", and the two are not the same question.
# The gates already allowed None here — an untriaged bundle nothing has
# looked at — and triage_failed IS untriaged: the only difference is that
# the machinery tried and broke. Excluding it left the worklist routing
# those bundles to "needs a decision" while the detail page offered no way
# to make one (poly-kp60).
UNTRIAGED_RESOLUTIONS: frozenset[str | None] = frozenset(
    FAILURE_RESOLUTIONS | {RESOLUTION_TRIAGE_FAILED, None}
)

VERIFIED = "verified"
VERIFICATION_FAILED = "verification_failed"


# --- Action gate (authoritative; consumed by the POST endpoints) -----------

# action name -> (resolution, verification_status) -> allowed?
# Captures ONLY the state gate each endpoint checks today; the endpoint
# keeps its metadata/concurrency guards and HTTP shaping.
ACTION_ALLOWED: dict[str, Callable[[str | None, str | None], bool]] = {
    # verify blocks only the two hard-terminal decisions (notably NOT
    # discarded — a discarded bundle can still be verified).
    "verify": lambda r, v: r not in (RESOLUTION_ACCEPTED, RESOLUTION_REJECTED),
    "accept": lambda r, v: r not in TERMINAL_RESOLUTIONS and v == VERIFIED,
    "reject": lambda r, v: r not in TERMINAL_RESOLUTIONS,
    "take-over": lambda r, v: r in UNTRIAGED_RESOLUTIONS,
    "discard": lambda r, v: r in (
        UNTRIAGED_RESOLUTIONS | {RESOLUTION_OPERATOR_OWNED}
    ),
    "retry": lambda r, v: r not in TERMINAL_RESOLUTIONS,
    "release": lambda r, v: r == RESOLUTION_OPERATOR_OWNED,
    "reopen": lambda r, v: r in TERMINAL_RESOLUTIONS,
    # Re-run the delivery side effect on a bundle whose accept
    # succeeded and whose PR did not. Accept commits the decision
    # first and delivers second, so a provider failure leaves the
    # bundle accurate (it IS accepted) and unactionable (accept is
    # terminal, and only reopen acts on terminal). This gate is
    # deliberately just the resolution: whether there is a failed
    # delivery to retry needs the newest bundle_review_requests row,
    # which this signature cannot see, so the endpoint checks it and
    # 409s -- the same allowed-vs-surface split described above
    # (poly-86t).
    "deliver": lambda r, v: r == RESOLUTION_ACCEPTED,
}


def action_allowed(
    action: str, resolution: str | None, verification_status: str | None,
    *, can_operate: bool = True,
) -> bool:
    """Authoritative state-gate for an operator action. Unknown action
    names are refused (True is never the default).

    ``can_operate`` is the audience half of the gate: an anonymous reader
    may drive nothing whatever the bundle's state. It defaults True so a
    caller that has not been taught about audiences behaves exactly as
    before, and it is checked FIRST so state can never re-permit what the
    audience forbids."""
    if not can_operate:
        return False
    gate = ACTION_ALLOWED.get(action)
    return bool(gate(resolution, verification_status)) if gate else False


# --- UI surface (consumed by the bundle-detail view) -----------------------


_NO_BUNDLE_ACTIONS: dict[str, Any] = {
    "show": False, "show_11c_group": False, "show_accept_button": False,
    "can_verify": False, "can_accept": False, "can_reject": False,
    "can_take_over": False, "can_discard": False, "can_retry": False,
    "can_reopen": False, "can_release": False,
}


def bundle_actions(
    bundle: dict[str, Any], *, can_operate: bool = True,
) -> dict[str, Any]:
    """Which operator actions the bundle-detail page shows/enables.

    With ``can_operate`` False every capability is False and ``show`` is
    False, so the page renders no action panel at all rather than a panel
    of disabled buttons — an anonymous reader is not a blocked operator.

    Pure over ``resolution`` / ``verification_status`` / ``target`` /
    ``origin`` — no DB access (the verify env-picker data is a live read
    the caller merges in). Field names + semantics are preserved exactly
    from the pre-refactor inline matrix so the template is unchanged.

    Narrower than ``ACTION_ALLOWED`` on purpose (see module docstring).
    """
    if not can_operate:
        return dict(_NO_BUNDLE_ACTIONS)
    r = bundle.get("resolution")
    v = bundle.get("verification_status")
    has_meta = bool((bundle.get("target") or "").strip()) and bool(
        (bundle.get("origin") or "").strip()
    )

    actionable = r == RESOLUTION_AGENT_FIXED
    # A triage_failed bundle is untriaged, so it belongs to the same lane:
    # re-run the triage, take it over by hand, or drop it.
    #
    # A NULL resolution splits on whether a job is live, and the resolution
    # alone cannot tell -- so ask the projection the worklist asks. While a
    # job works it the operator has nothing to do; once it does not
    # (`unknown`: reopened, or a job that died without writing a
    # resolution) the bundle is nobody's, which is why the worklist routes
    # it to "needs a decision" too.
    untriaged = r in (UNTRIAGED_RESOLUTIONS - {None}) or (
        r is None and fix_status(bundle).key == "unknown"
    )
    can_take_over = untriaged and has_meta
    can_discard = untriaged or r == RESOLUTION_OPERATOR_OWNED
    can_retry = untriaged or r in (
        RESOLUTION_OPERATOR_OWNED, RESOLUTION_AGENT_FIXED
    )
    can_reopen = r in TERMINAL_RESOLUTIONS
    # NOT triage_failed. verify replays analysis/changes.diff, and triage is
    # what produces one -- run_verify_fix raises on the 404 and again on an
    # empty diff, so offering it would queue a job that cannot succeed.
    verify_eligible = actionable or r == RESOLUTION_OPERATOR_OWNED
    can_release = r == RESOLUTION_OPERATOR_OWNED
    can_accept = verify_eligible and v == VERIFIED
    # Accept renders (possibly disabled) on the agent_fixed lane so the
    # operator sees the verify→accept path; enabled only once verified.
    show_accept_button = actionable or can_accept

    return {
        "show": (
            actionable or can_take_over or can_discard
            or can_retry or can_reopen or can_release
        ),
        "show_11c_group": actionable,   # gates the Reject group
        "show_accept_button": show_accept_button,
        "can_verify": verify_eligible,
        "can_accept": can_accept,
        "can_reject": actionable,
        "can_take_over": can_take_over,
        "can_discard": can_discard,
        "can_retry": can_retry,
        "can_reopen": can_reopen,
        "can_release": can_release,
    }


# --- State projection (consumed by templates for the status pill) ----------

# In-flight job states — a bundle with resolution=NULL whose job is still
# working. (Terminal job states done/dead/escalated always carry a
# resolution, so they resolve via the resolution branch below.)
_INFLIGHT_JOB_STATES: frozenset[str] = frozenset(
    {
        "queued", "claimed", "triaging", "triaged",
        "patching", "verifying", "converting", "verifying_fix",
        # A confirm build (build-confirmed resolution) is runner-owned work
        # exactly like a verify: while it runs the operator has nothing to do.
        "confirming",
    }
)


@dataclass(frozen=True)
class FixStatus:
    """One operator-facing status for a failure bundle."""
    key: str        # stable machine key
    label: str      # human text for the pill
    pill: str       # css pill class: built | failed | skipped | total | ignored


# (resolution -> FixStatus) for the resolutions that don't depend on
# verification_status. The verification-sensitive ones are handled first.
_RESOLUTION_STATUS: dict[str, FixStatus] = {
    RESOLUTION_AGENT_GAVE_UP: FixStatus("agent_gave_up", "agent gave up", "failed"),
    RESOLUTION_AGENT_BUDGET: FixStatus("budget_out", "budget out", "failed"),
    RESOLUTION_ESCALATED: FixStatus("escalated", "escalated", "skipped"),
    RESOLUTION_TRIAGE_FAILED: FixStatus("triage_failed", "triage failed", "failed"),
    RESOLUTION_ACCEPTED: FixStatus("accepted", "accepted", "built"),
    RESOLUTION_MERGED: FixStatus("merged", "merged upstream", "built"),
    RESOLUTION_REJECTED: FixStatus("rejected", "rejected", "failed"),
    RESOLUTION_DISCARDED: FixStatus("discarded", "discarded", "skipped"),
}


def fix_status(bundle: dict[str, Any]) -> FixStatus:
    """Project the raw (resolution, verification_status, job.state)
    columns into one operator-facing status.

    Order matters: the verification-sensitive resolutions
    (agent_fixed / operator_owned) resolve first, then the fixed-mapping
    resolutions, then the resolution=NULL in-flight/unknown fallback.
    """
    r = bundle.get("resolution")
    v = bundle.get("verification_status")

    if r == RESOLUTION_AGENT_FIXED:
        if v == VERIFIED:
            return FixStatus("verified", "verified", "built")
        if v == VERIFICATION_FAILED:
            return FixStatus("verify_failed", "verify failed", "failed")
        return FixStatus("needs_review", "agent fixed — verify", "built")

    if r == RESOLUTION_OPERATOR_OWNED:
        if v == VERIFIED:
            return FixStatus("owned_verified", "you own · verified", "built")
        return FixStatus("operator_owned", "you own this", "skipped")

    mapped = _RESOLUTION_STATUS.get(r) if r is not None else None
    if mapped is not None:
        return mapped

    # resolution is NULL: distinguish in-flight from unknown via job.state.
    state = bundle.get("state") or bundle.get("job_state")
    if state in _INFLIGHT_JOB_STATES:
        return FixStatus("in_progress", "in progress", "total")
    return FixStatus("unknown", "—", "total")


# --- Verify projection (poly-0e02.6) ---------------------------------------
#
# A verify request and a verification result live in different rows and
# neither closes the other: the runner moves the request pending -> enqueued
# (or failed) and stops, and the result posts back to `bundles` without a
# request id to close. So `enqueued` is not "running" -- it is "a job was
# created at some point", and what happened next has to be reconciled from
# three places: the request's own status, the job's state, and whether the
# bundle's verification_at postdates the request.
#
# Deriving beats writing a closing status: the post-back carries no request
# id, so closing "the newest enqueued request" would mis-close whichever
# second verify an operator started in the meantime.

VERIFY_NONE = "none"            # nobody asked, and nothing has verified
VERIFY_QUEUED = "queued"        # written; the runner has not picked it up
VERIFY_STARTING = "starting"    # enqueued, job not visible yet
VERIFY_RUNNING = "running"      # enqueued, the job is working
VERIFY_PASSED = "passed"
VERIFY_FAILED = "failed"        # ran, and the fix did not hold
VERIFY_NOT_STARTED = "not_started"  # the enqueue itself failed
VERIFY_LOST = "lost"            # the job ended without recording a result


@dataclass(frozen=True)
class VerifyState:
    """What the last verify asked for on an occurrence actually did."""
    key: str
    label: str
    pill: str
    detail: str
    env: str | None
    requested_at: str | None
    job_id: str | None


def verify_state(
    bundle: dict[str, Any], request: dict[str, Any] | None = None,
) -> VerifyState:
    """Reconcile one occurrence's verification with the request that asked
    for it.

    ``request`` is ``latest_verify_request``'s row (carrying ``job_state``),
    or None when none was ever written -- the agent's own verify path
    records a result on the bundle without going through a request.
    """
    status = bundle.get("verification_status")
    verified_at = bundle.get("verification_at")
    reason = bundle.get("verification_reason")
    exit_code = bundle.get("verification_exit_code")
    env = (request or {}).get("env")
    requested_at = (request or {}).get("requested_at")
    job_id = (request or {}).get("job_id")

    def out(key: str, label: str, pill: str, detail: str) -> VerifyState:
        return VerifyState(
            key=key, label=label, pill=pill, detail=detail, env=env,
            requested_at=requested_at, job_id=job_id,
        )

    # A result that postdates the request is that request's answer. Compared
    # by timestamp because the post-back carries no request id -- the same
    # boundary reasoning issue_state uses for regression.
    answered = bool(status) and (
        not requested_at or (verified_at or "") >= requested_at
    )
    where = f" in {env}" if env else ""

    if answered:
        if status == VERIFIED:
            return out(
                VERIFY_PASSED, f"verified{where}", "green",
                f"The fix was replayed{where or ' somewhere unrecorded'} and "
                f"the port built."
                + (f" Recorded {verified_at}." if verified_at else ""),
            )
        parts = [p for p in (
            f"dsynth exited {exit_code}" if exit_code is not None else None,
            reason,
        ) if p]
        return out(
            VERIFY_FAILED, f"verify failed{where}", "red",
            "The fix was replayed and the port still failed"
            + (": " + ": ".join(parts) if parts else ".")
            + (f" Recorded {verified_at}." if verified_at else ""),
        )

    if request is None:
        return out(
            VERIFY_NONE, "not verified", "neutral",
            "No independent verification has been run for this occurrence.",
        )

    req_status = request.get("status")
    if req_status == "failed":
        return out(
            VERIFY_NOT_STARTED, "verify never started", "red",
            f"The verify could not be enqueued: "
            f"{request.get('error') or 'no reason recorded'}.",
        )
    if req_status == "pending":
        return out(
            VERIFY_QUEUED, f"verify queued{where}", "cyan",
            f"Requested {requested_at}. The runner turns it into a job on "
            f"its next pass.",
        )

    job_state = request.get("job_state")
    if job_state is None:
        return out(
            VERIFY_STARTING, f"verify starting{where}", "cyan",
            f"Job {job_id or '?'} was created; its state has not been "
            f"recorded yet.",
        )
    if job_state in _INFLIGHT_JOB_STATES:
        return out(
            VERIFY_RUNNING, f"verifying{where}", "cyan",
            f"Job {job_id} is replaying the fix{where}. The result posts "
            f"back here when it finishes.",
        )
    return out(
        VERIFY_LOST, f"verify produced no result{where}", "amber",
        f"Job {job_id} ended in state {job_state!r} without recording a "
        f"verification. Run it again, or read the job's activity for why.",
    )


# --- Worklist bucketing ------------------------------------------------------

# fix_status.key -> worklist bucket. Only `in_progress` is unlisted: that is
# the one genuinely runner-owned state, where the operator has nothing to do
# until the job finishes. `unknown` (resolution NULL with NO live job) IS
# actionable — nobody is working it, so it needs a decision. It used to be
# unlisted too, which silently dropped an issue out of the worklist the moment
# an operator reopened its bundle (resolution -> NULL): the issue vanished from
# /agentic while still listed on /agentic/issues, with no landing place.
# Consumed by `worklist_bucket`, which the issue layer (issue_state) uses to
# bucket an issue's actionable occurrence. The old bundle-level worklist
# (build_worklist) and origin grouping (group_band_by_origin) were removed in
# the issue-model cutover — the landing groups by fingerprinted issue now.
_WORKLIST_BUCKET: dict[str, str] = {
    "verified": "ready",
    "owned_verified": "ready",
    "needs_review": "verify",
    "verify_failed": "decide",
    "agent_gave_up": "decide",
    "budget_out": "decide",
    "escalated": "decide",
    "triage_failed": "decide",
    "operator_owned": "owned",
    "unknown": "decide",
    "accepted": "done",
    "merged": "done",
    "rejected": "done",
    "discarded": "done",
}


def worklist_bucket(bundle: dict[str, Any]) -> str | None:
    """The worklist bucket for a single occurrence (ready/verify/decide/
    owned/done), or None when it isn't operator-actionable — which now means
    only ``in_progress`` (a job is actively working it).

    The issue layer buckets an issue's *actionable occurrence* through this
    exact mapping instead of re-deriving it.
    """
    return _WORKLIST_BUCKET.get(fix_status(bundle).key)
