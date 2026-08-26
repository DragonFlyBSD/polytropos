"""WS5 — the issue state model (tracker.issue_state).

Pins the read-side projection that sits above `fix_state`:

- lifecycle badge per effective state (regressed loud, muted/resolved toned)
  — the derivation that produces `regressed` from a `resolved` row lives in
  test_issue_regression_derived.py; these cases feed the effective state
  directly, which is the contract the badge and the gate are written to;
- the issue-action gate + UI surface (mute↔unmute, resolve↔reopen);
- the bucket rule combining lifecycle (surfacing) with the *actionable
  occurrence* (the action band), including the subtle terminal-latest-
  but-open cases;
- systemic-first worklist ordering.
"""

from __future__ import annotations

import itertools

import pytest

from dportsv3.tracker import issue_state as I


def _occ(bundle_id, resolution, verification=None, ts="2026-07-25T00:00:00Z",
         state=None):
    d = {"bundle_id": bundle_id, "resolution": resolution,
         "verification_status": verification, "ts_utc": ts}
    if state:
        d["state"] = state
    return d


def _issue(state, *, key="k", origin="ftp/curl", target="@2026Q3",
           times_seen=5, occurrences=None):
    """An issue row that projects to `state`.

    `regressed` is derived, never stored (C3): the row says `resolved` and
    carries a boundary its occurrences are past. Asking for it here builds
    that shape rather than a row the DB would reject, so the cases below stay
    honest about what they are testing.
    """
    row = {
        "issue_key": key, "state": state, "origin": origin, "target": target,
        "times_seen": times_seen,
        "first_seen_at": "2026-07-20T00:00:00Z",
        "last_seen_at": "2026-07-25T00:00:00Z",
        "occurrences": occurrences or [],
    }
    if state == "regressed":
        # No green head: these occurrences carry no build ordinal, so the
        # boundary is resolved_at and every one of them is past it.
        row["state"] = "resolved"
        row["resolved_at"] = "2026-07-01T00:00:00Z"
    return row


def _projected(state):
    """An issue row that projects to `state`.

    `regressed` is derived, never stored (C3), so the only row that can carry
    it is a resolved one with an occurrence past its known-good boundary.
    Building it here keeps these cases on shapes the DB can actually hold.
    """
    if state != "regressed":
        return {"state": state}
    return {"state": "resolved", "green_head_run_id": 7,
            "resolved_at": "2026-07-25T00:00:00Z",
            "occurrences": [{"bundle_id": "b", "build_run_id": 8,
                             "ts_utc": "2026-07-26T00:00:00Z"}]}


# --- lifecycle projection --------------------------------------------------


@pytest.mark.parametrize("state,key,pill", [
    ("unresolved", "unresolved", "total"),
    ("regressed", "regressed", "failed"),   # loud
    ("resolved", "resolved", "built"),
    ("muted", "muted", "ignored"),
])
def test_issue_status_projection(state, key, pill):
    st = I.issue_status(_projected(state))
    assert (st.key, st.pill) == (key, pill)


def test_issue_status_unknown_state():
    assert I.issue_status({"state": "bogus"}).key == "unknown"
    assert I.issue_status({}).key == "unknown"


def test_issue_status_pill_classes_are_known():
    known = {"built", "failed", "skipped", "total", "ignored"}
    for state in ["unresolved", "regressed", "resolved", "muted", None]:
        assert I.issue_status(_projected(state)).pill in known


# --- action gate + surface -------------------------------------------------


def _expected_allowed(action, state):
    if action == "mute":
        return state in ("unresolved", "regressed")
    if action == "unmute":
        return state == "muted"
    if action == "resolve":
        return state != "resolved"
    if action == "reopen":
        # also from `resolving`: an accepted fix awaiting its confirm build
        # can be pulled back out of the delivery path
        return state in ("resolved", "resolving")
    if action in ("build", "cancel-build"):
        # a confirm build proves an ACCEPTED fix; `resolving` is that state
        return state == "resolving"
    raise AssertionError(action)


@pytest.mark.parametrize("action", sorted(I.ISSUE_ACTION_ALLOWED))
@pytest.mark.parametrize("state", ["unresolved", "regressed", "resolved", "muted", "resolving"])
def test_issue_action_gate(action, state):
    assert I.issue_action_allowed(action, state) == _expected_allowed(action, state)


def test_issue_action_unknown_refused():
    assert I.issue_action_allowed("frobnicate", "unresolved") is False


def test_issue_actions_surface_matches_gate():
    for state in ["unresolved", "regressed", "resolved", "muted", "resolving"]:
        acts = I.issue_actions(_projected(state))
        assert acts == {
            "can_mute": I.issue_action_allowed("mute", state),
            "can_unmute": I.issue_action_allowed("unmute", state),
            "can_resolve": I.issue_action_allowed("resolve", state),
            "can_reopen": I.issue_action_allowed("reopen", state),
            "can_build": I.issue_action_allowed("build", state),
            "can_cancel_build": I.issue_action_allowed("cancel-build", state),
        }


# --- actionable occurrence -------------------------------------------------


def test_actionable_occurrence_is_newest():
    occs = [
        _occ("old", "agent_gave_up", ts="2026-07-24T00:00:00Z"),
        _occ("new", "agent_fixed", "verified", ts="2026-07-25T09:00:00Z"),
        _occ("mid", "triage_failed", ts="2026-07-24T12:00:00Z"),
    ]
    assert I.actionable_occurrence(occs)["bundle_id"] == "new"


def test_actionable_occurrence_empty_is_none():
    assert I.actionable_occurrence([]) is None


# --- bucket rule (lifecycle × actionable occurrence) -----------------------


@pytest.mark.parametrize("state,occ,expected", [
    # open issue, band from the actionable occurrence
    ("unresolved", _occ("b", "agent_fixed", "verified"), "ready"),
    ("unresolved", _occ("b", "agent_fixed"), "verify"),
    ("unresolved", _occ("b", "agent_gave_up"), "decide"),
    ("unresolved", _occ("b", "operator_owned"), "owned"),
    ("regressed", _occ("b", "agent_fixed", "verified"), "ready"),
    # latest occurrence in-flight → not actionable yet
    ("unresolved", _occ("b", None, state="patching"), None),
    # terminal-per-occurrence but issue still open:
    # Accepted fix but the issue is OPEN (not `resolving`): delivery was
    # abandoned, or its confirm build came back red (A3). Needs a decision —
    # it must not read as "awaiting delivery" (that left an unfixed issue
    # looking as good as fixed) nor vanish from the worklist.
    ("unresolved", _occ("b", "accepted", "verified"), "decide"),
    ("unresolved", _occ("b", "rejected"), "decide"),           # try again
    ("unresolved", _occ("b", "discarded"), "decide"),
    # lifecycle-terminal issues
    ("resolved", _occ("b", "merged"), "done"),
    ("muted", _occ("b", "agent_gave_up"), "muted"),
    # operator reopened a resolved issue whose latest occurrence merged:
    # the merge is spent, the problem's back open → needs a fresh attempt
    ("unresolved", _occ("b", "merged"), "decide"),
])
def test_issue_bucket(state, occ, expected):
    issue = _issue(state, occurrences=[occ])
    assert I.issue_bucket(issue, [occ]) == expected


def test_open_issue_without_occurrences_needs_a_look():
    assert I.issue_bucket(_issue("unresolved", occurrences=[]), []) == "decide"


def test_bucket_uses_latest_not_any_occurrence():
    """A stale verified attempt does not keep a since-rejected issue in
    'ready' — the newest occurrence wins."""
    old = _occ("old", "agent_fixed", "verified", ts="2026-07-24T00:00:00Z")
    new = _occ("new", "rejected", ts="2026-07-25T00:00:00Z")
    issue = _issue("unresolved", occurrences=[old, new])
    assert I.issue_bucket(issue, [old, new]) == "decide"


# --- group projection + worklist ------------------------------------------


def test_issue_group_shape_and_rollup():
    occs = [
        _occ("a", "agent_gave_up", ts="2026-07-25T00:30:00Z"),
        _occ("b", "agent_gave_up", ts="2026-07-25T11:00:00Z"),
        _occ("c", "triage_failed", ts="2026-07-25T00:32:00Z"),
    ]
    issue = _issue("regressed", times_seen=9, occurrences=occs)
    g = I.issue_group(issue, occs)
    assert g["state"] == "regressed"
    assert g["regressed"] is True and g["systemic"] is True
    assert g["times_seen"] == 9           # from the issue row, not len(occs)
    assert g["count"] == 3
    # newest-first: b (11:00) > c (00:32) > a (00:30)
    assert [o["bundle_id"] for o in g["occurrences"]] == ["b", "c", "a"]
    assert g["latest"]["bundle_id"] == "b"
    rollup = {r["label"]: r["n"] for r in g["rollup"]}
    assert rollup == {"agent gave up": 2, "triage failed": 1}
    # regressed + verified? no — latest is gave_up → decide band
    assert g["bucket"] == "decide"


def test_build_issue_worklist_buckets_and_orders():
    issues = [
        _issue("unresolved", key="k1", origin="a/a", times_seen=1,
               occurrences=[_occ("x", "agent_gave_up", ts="2026-07-25T01:00:00Z")]),
        _issue("unresolved", key="k2", origin="b/b", times_seen=7,
               occurrences=[_occ("y", "agent_gave_up", ts="2026-07-25T00:00:00Z")]),
        _issue("unresolved", key="k3", origin="c/c", times_seen=2,
               occurrences=[_occ("z", "agent_fixed", "verified")]),
        _issue("muted", key="k4", origin="d/d",
               occurrences=[_occ("w", "agent_gave_up")]),
        _issue("resolved", key="k5", origin="e/e",
               occurrences=[_occ("v", "merged")]),
        # in-flight latest → omitted entirely
        _issue("unresolved", key="k6", origin="f/f",
               occurrences=[_occ("u", None, state="patching")]),
    ]
    wl = I.build_issue_worklist(issues)
    # decide bucket: systemic-first (k2 times_seen=7 before k1 times_seen=1)
    assert [g["issue_key"] for g in wl["decide"]] == ["k2", "k1"]
    assert [g["issue_key"] for g in wl["ready"]] == ["k3"]
    assert [g["issue_key"] for g in wl["muted"]] == ["k4"]
    assert [g["issue_key"] for g in wl["done"]] == ["k5"]
    # k6 (in-flight) is not operator-actionable → in no bucket
    everywhere = {g["issue_key"] for b in wl.values() for g in b}
    assert "k6" not in everywhere


def test_worklist_sections_cover_every_bucket():
    section_keys = {k for k, _l, _c in I.ISSUE_WORKLIST_SECTIONS}
    # every non-None bucket issue_bucket can emit is a real section
    assert {"ready", "verify", "decide", "owned", "done", "muted"} <= section_keys
    # and an occurrence's own bucket (from fix_state, reused for the
    # actionable occurrence) is always a valid issue-worklist section.
    from dportsv3.tracker import fix_state
    assert set(fix_state._WORKLIST_BUCKET.values()) <= section_keys


# --- DeltaPorts-sh7: issues knocked off the happy path must have a landing ---


def test_red_confirm_build_lands_in_decide():
    """A3 reopens the issue but leaves the bundle `accepted`. That must read as
    `decide`, not `delivering` — an unfixed issue must not look as good as
    fixed."""
    occ = _occ("b", "accepted", "verified")
    issue = _issue("unresolved", occurrences=[occ])
    issue["reopened_by"] = "runner-reconcile"
    assert I.issue_bucket(issue, [occ]) == "decide"


def test_operator_reopened_bundle_stays_visible():
    """Reopening a bundle blanks its resolution. The issue must stay in the
    worklist (it previously fell out entirely via bucket None)."""
    occ = _occ("b", None, "verified")          # reopened: resolution -> NULL
    issue = _issue("unresolved", occurrences=[occ])
    assert I.issue_bucket(issue, [occ]) == "decide"

    worklist = I.build_issue_worklist([issue])
    visible = [g for groups in worklist.values() for g in groups]
    assert len(visible) == 1, "operator-reopened issue vanished from worklist"


def test_runner_owned_occurrence_still_hidden():
    """The complement: work a job is actively doing stays out of the operator
    worklist. bucket None is reserved for genuinely runner-owned work."""
    for job_state in ("patching", "confirming"):
        occ = _occ("b", None, None)
        occ["state"] = job_state
        issue = _issue("unresolved", occurrences=[occ])
        assert I.issue_bucket(issue, [occ]) is None, job_state
