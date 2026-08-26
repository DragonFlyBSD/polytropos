"""Single source of truth for an *issue's* operator-facing state, the
actions allowed on it, and how issues group into the worklist.

The issue layer sits above the occurrence layer (`fix_state`) the way a
fingerprinted problem sits above its individual failures: an issue is one
fingerprinted problem, an occurrence (bundle) is one failure of it. This module is to
the issue what `fix_state` is to the occurrence, and it is deliberately
built on top of `fix_state` rather than duplicating it — an open issue's
worklist band is derived by running the occurrence projection on its
*actionable occurrence*.

Two axes, combined here:

- **Resolution** — `issue.state` ∈ {unresolved, resolving, resolved,
  muted}: what the operator and the confirm build decided. Drives
  *surfacing*: open issues in the worklist, `resolving` (fix accepted,
  awaiting delivery) in its own band, resolved in the collapsed archive,
  muted in a collapsed muted section.
- **Build observation** — what later builds actually did. `regressed` (a
  fix that came back) lives here and is DERIVED, never stored: see
  `derived_regression`. `effective_state` folds the two axes back into the
  single value the badge, the action gate and the worklist read.
- **Actionable occurrence** — the latest occurrence by timestamp. Its
  `fix_status` (via `fix_state.worklist_bucket`) drives *which* action
  band an open issue lands in (ready / verify / decide / owned).

Pure over dicts — no DB, no HTTP. Queries (WS6) assemble the issue rows +
their occurrences; endpoints (WS7) enforce the action gate; the UI (WS9)
renders the groups.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dportsv3.tracker import fix_state

# --- Issue-state vocabulary (one definition) -------------------------------

ISSUE_UNRESOLVED = "unresolved"
ISSUE_RESOLVED = "resolved"
# Resolved · pending delivery — a fix has been accepted for the issue and is
# being (or awaiting being) delivered, but hasn't landed yet. Reached from an
# open state via accept; settles to `resolved` on merge/deliver (R3), or
# reopens if a fresh occurrence arrives before the fix lands.
ISSUE_RESOLVING = "resolving"
ISSUE_MUTED = "muted"

# The resolution axis, in full. This is what `issues.state` may hold — the
# column is CHECKed against exactly this set.
ISSUE_STORED_STATES: frozenset[str] = frozenset({
    ISSUE_UNRESOLVED, ISSUE_RESOLVING, ISSUE_RESOLVED, ISSUE_MUTED,
})

# Build-observation axis. NOT a stored state: `regressed` is what
# `effective_state` returns for a resolved issue whose fingerprint came back
# past its known-good boundary. Everything downstream (badge, action gate,
# worklist) keys off it exactly as it did when it was stored.
ISSUE_REGRESSED = "regressed"

# The two "needs attention" states — an open problem the operator may act on.
# Effective states: `regressed` reaches here derived, never read from a row.
ISSUE_OPEN_STATES: frozenset[str] = frozenset({ISSUE_UNRESOLVED, ISSUE_REGRESSED})


# --- Regression: derived from the build-observation axis (C3) ---------------


def occurrence_past_boundary(
    issue: dict[str, Any], occurrence: dict[str, Any],
) -> bool:
    """Whether ``occurrence`` happened after this issue's fix was proven.

    Two comparisons, in order of trust:

    1. **Build ordinal.** ``build_runs.id`` is monotonic per target, so
       ``occurrence.build_run_id > issue.green_head_run_id`` places the
       occurrence strictly after the confirm build's known-good watermark
       (A2). No clocks involved, so no skew.
    2. **Timestamp** — ``occurrence.ts_utc > issue.resolved_at`` — when
       either ordinal is missing. A manually resolved issue records no
       watermark, and an occurrence from a build the tracker never saw
       carries no ordinal. Degraded rather than absent; it inherits the
       cross-host skew poly-9vr owns.

    An issue with neither a watermark nor a ``resolved_at`` has no boundary
    at all, so nothing can be past it.
    """
    head = issue.get("green_head_run_id")
    ordinal = occurrence.get("build_run_id")
    if head is not None and ordinal is not None:
        return int(ordinal) > int(head)
    resolved_at = issue.get("resolved_at")
    ts = occurrence.get("ts_utc")
    return bool(resolved_at and ts and ts > resolved_at)


def derived_regression(
    issue: dict[str, Any], occurrences: list[dict[str, Any]],
) -> str | None:
    """When this issue's fix came back, or None if it hasn't.

    The single definition of ``regressed``, replacing the three that used to
    disagree: the artifact-store writer's "any occurrence on a resolved
    issue", the unmute path's timestamp compare, and the stored column each
    read back independently.

    Regression is resolved-and-red-again: only the resolution axis's
    ``resolved`` can regress. ``resolving`` deliberately cannot — a farm
    build failing while the confirm build is still in flight is the
    unfixed port still being observed, not a fix that came back, and the
    confirm verdict (A2/A3) is what settles it.

    Every occurrence of an issue carries that issue's fingerprint by
    construction (``issue_key`` hashes it), so "re-emits the fingerprint"
    needs no separate check — an occurrence past the boundary IS one.
    Returns the *earliest* crossing: when it came back, not when it was
    last seen.
    """
    if issue.get("state") != ISSUE_RESOLVED:
        return None
    crossings = [
        o.get("ts_utc") for o in occurrences
        if occurrence_past_boundary(issue, o)
    ]
    crossings = [ts for ts in crossings if ts]
    return min(crossings) if crossings else None


def effective_state(
    issue: dict[str, Any],
    occurrences: list[dict[str, Any]] | None = None,
) -> str | None:
    """The single state the operator sees: the stored resolution, overridden
    by the derived ``regressed`` read.

    Folding the two axes here is what keeps the rest of this module — badge,
    action gate, worklist bucketing — unchanged from when ``regressed`` was
    a stored value. ``occurrences`` defaults to the ones attached to the
    issue dict, so template globals can still be called with the row alone.
    """
    stored = issue.get("state")
    if stored != ISSUE_RESOLVED:
        return stored
    if occurrences is None:
        occurrences = issue.get("occurrences") or []
    return ISSUE_REGRESSED if derived_regression(issue, occurrences) else stored


def stored_states_for(effective: str) -> tuple[str, ...]:
    """The stored states that can present as ``effective`` — the SQL
    prefilter for a view that then filters exactly on
    :func:`effective_state`.

    ``regressed`` is derived from a ``resolved`` row, so both effective
    values narrow to the same stored one and the exact split happens in
    Python. Returns empty for a name that is neither, so an unknown filter
    matches nothing rather than everything.
    """
    if effective == ISSUE_REGRESSED:
        return (ISSUE_RESOLVED,)
    if effective in ISSUE_STORED_STATES:
        return (effective,)
    return ()


# --- Issue-action policy (authoritative gate; consumed by WS7 endpoints) ----

# action -> issue.state -> allowed? Mirrors the plan's operator transition
# table: mute an open issue, unmute a muted one, manually resolve anything
# not already resolved, reopen a resolved one.
ISSUE_ACTION_ALLOWED = {
    "mute": lambda s: s in ISSUE_OPEN_STATES,
    "unmute": lambda s: s == ISSUE_MUTED,
    "resolve": lambda s: s != ISSUE_RESOLVED,
    "reopen": lambda s: s in (ISSUE_RESOLVED, ISSUE_RESOLVING),
    # Build controls. A confirm build exists to prove an ACCEPTED fix, and
    # "accepted fix" is exactly what `resolving` means — so both are gated to
    # that state. It also keeps the reconcile feed honest: the feed only
    # surfaces `resolving` issues, so allowing a build request from any other
    # state would record an intent nothing would ever act on.
    "build": lambda s: s == ISSUE_RESOLVING,
    "cancel-build": lambda s: s == ISSUE_RESOLVING,
}


def issue_action_allowed(action: str, state: str | None) -> bool:
    """Authoritative state-gate for an issue-level action. Unknown action
    names are refused (True is never the default)."""
    gate = ISSUE_ACTION_ALLOWED.get(action)
    return bool(gate(state)) if gate else False


def issue_actions(issue: dict[str, Any]) -> dict[str, bool]:
    """Which issue-level controls the UI shows/enables, given its state.

    A straight mirror of the gate (unlike `bundle_actions`, the issue
    surface isn't intentionally narrowed) — mute↔unmute and resolve↔reopen
    are contextual opposites.

    Gated on the EFFECTIVE state, so a regressed issue keeps the open-issue
    controls (mute, resolve) it had when `regressed` was stored, even though
    its row now reads `resolved`.
    """
    s = effective_state(issue)
    return {
        "can_mute": issue_action_allowed("mute", s),
        "can_unmute": issue_action_allowed("unmute", s),
        "can_resolve": issue_action_allowed("resolve", s),
        "can_reopen": issue_action_allowed("reopen", s),
        "can_build": issue_action_allowed("build", s),
        "can_cancel_build": issue_action_allowed("cancel-build", s),
    }


# --- Lifecycle projection (consumed by templates for the issue badge) -------


@dataclass(frozen=True)
class IssueStatus:
    """One operator-facing lifecycle status for an issue."""
    key: str        # stable machine key
    label: str      # human text for the badge
    pill: str       # css pill class: built | failed | skipped | total | ignored


_ISSUE_STATUS: dict[str, IssueStatus] = {
    ISSUE_UNRESOLVED: IssueStatus("unresolved", "open", "total"),
    # Derived, never stored — reached only via `effective_state`.
    ISSUE_REGRESSED: IssueStatus("regressed", "regressed", "failed"),   # loud
    ISSUE_RESOLVED: IssueStatus("resolved", "resolved", "built"),
    ISSUE_RESOLVING: IssueStatus("resolving", "awaiting delivery", "total"),
    ISSUE_MUTED: IssueStatus("muted", "muted", "ignored"),
}


def issue_status(issue: dict[str, Any]) -> IssueStatus:
    """Project the issue's effective state into the badge (key/label/pill).

    Reads the occurrences attached to the issue row to decide `regressed`,
    so every caller must hand over a row that carries them — the query layer
    attaches them on all four issue reads.
    """
    return _ISSUE_STATUS.get(
        effective_state(issue), IssueStatus("unknown", "—", "total")
    )


# --- Actionable occurrence + worklist bucketing -----------------------------

# Bucket key -> (heading, pill class) in display order. Extends the
# occurrence worklist with a collapsed `muted` section; `done` here means
# "resolved" (the issue archive), not an accepted occurrence.
ISSUE_WORKLIST_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("ready", "Ready to accept", "built"),
    ("verify", "Needs verify", "skipped"),
    ("decide", "Needs a decision", "failed"),
    ("owned", "You own", "total"),
    ("delivering", "Awaiting delivery", "built"),
    ("done", "Resolved", "ignored"),
    ("muted", "Muted", "ignored"),
)


def actionable_occurrence(
    occurrences: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The occurrence the operator would act on: the newest by ``ts_utc``.

    Expects occurrences for one issue; returns None for an empty list.
    """
    if not occurrences:
        return None
    return max(occurrences, key=lambda o: (o.get("ts_utc") or ""))


def issue_bucket(
    issue: dict[str, Any], occurrences: list[dict[str, Any]],
) -> str | None:
    """The worklist bucket for an issue, or None when it isn't
    operator-actionable right now (muted issues get their own bucket).

    Lifecycle decides surfacing; the actionable occurrence decides the
    action band for an *open* issue:

    - muted → ``muted`` (collapsed section, discoverable + unmuteable);
    - resolved → ``done`` (the archive);
    - open, latest occurrence in-flight/unknown → None (runner is working
      it — nothing to do yet);
    - open, latest occurrence still actionable → its own band
      (ready/verify/decide/owned);
    - resolving (a fix was accepted) → ``delivering`` (awaiting delivery);
    - open, latest occurrence accepted → ``decide``: the issue is open while
      its fix is not in the delivery path, so delivery was abandoned or its
      confirm build failed;
    - open, latest occurrence otherwise terminal-per-occurrence
      (rejected/discarded, or a merged occurrence under a manually-reopened
      issue) → ``decide`` — the attempt is spent but the problem persists,
      so it needs a fresh one;
    - open with no occurrences at all → ``decide`` (open, needs a look).

    Reads the effective state, so a derived-regressed issue buckets as the
    open problem it is rather than landing in the resolved archive.
    """
    state = effective_state(issue, occurrences)
    if state == ISSUE_MUTED:
        return "muted"
    if state == ISSUE_RESOLVED:
        return "done"
    if state == ISSUE_RESOLVING:
        return "delivering"              # fix accepted, awaiting delivery

    act = actionable_occurrence(occurrences)
    if act is None:
        return "decide"
    occ_key = fix_state.fix_status(act).key
    occ_bucket = fix_state.worklist_bucket(act)
    if occ_bucket is None:
        return None                      # in_progress / unknown
    if occ_bucket != "done":
        return occ_bucket                # ready / verify / decide / owned
    # Occurrence is terminal-per-occurrence, but the issue is still open.
    #
    # An accepted occurrence used to read as "shipping" here, from before
    # `resolving` existed: an open issue with an accepted fix meant delivery
    # was under way. `resolving` now carries that meaning explicitly and is
    # returned above, so reaching this point with an accepted occurrence means
    # the issue is open while its fix is NOT in the delivery path — the
    # delivery was abandoned (operator reopen) or its confirm build came back
    # red (A3). Both need an operator decision; calling it "awaiting delivery"
    # left an unfixed issue looking as good as fixed.
    return "decide"                      # accepted-but-not-delivering /
                                         # rejected / discarded / reopened-merged


# Count at which an issue's recurrences read as systemic (loud, floats up).
_SYSTEMIC_THRESHOLD = 3


def issue_group(
    issue: dict[str, Any], occurrences: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project one issue + its occurrences into a render-ready group.

    Rollups (`times_seen`, `first_seen_at`, `last_seen_at`) come from the
    persisted issue row — the authoritative counters — while `count` is
    the number of occurrences actually supplied (a window may be smaller
    than lifetime `times_seen`). Occurrences are ordered newest-first and
    rolled up by distinct `fix_status` label, mirroring the old
    origin-band shape so the template stays close.
    """
    ordered = sorted(
        occurrences, key=lambda o: (o.get("ts_utc") or ""), reverse=True
    )
    rollup: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for o in ordered:
        s = fix_state.fix_status(o)
        entry = seen.get(s.label)
        if entry is None:
            entry = {"label": s.label, "cls": s.pill, "n": 0}
            seen[s.label] = entry
            rollup.append(entry)
        entry["n"] += 1

    state = effective_state(issue, ordered)
    times_seen = issue.get("times_seen") or len(ordered)
    latest = ordered[0] if ordered else None
    return {
        "issue_key": issue.get("issue_key"),
        "origin": issue.get("origin") or "—",
        "target": issue.get("target"),
        "state": state,
        "status": issue_status(issue),
        "times_seen": times_seen,
        "count": len(ordered),
        "first_seen_at": issue.get("first_seen_at"),
        "last_seen_at": issue.get("last_seen_at"),
        "regressed": state == ISSUE_REGRESSED,
        "regressed_at": derived_regression(issue, ordered),
        "muted": state == ISSUE_MUTED,
        "resolved": state == ISSUE_RESOLVED,
        "resolving": state == ISSUE_RESOLVING,
        "systemic": (times_seen or 0) >= _SYSTEMIC_THRESHOLD,
        "latest": latest,
        "latest_ts": (latest.get("ts_utc") if latest else "") or "",
        "occurrences": ordered,
        "rollup": rollup,
        "bucket": issue_bucket(issue, occurrences),
        "actions": issue_actions(issue),
    }


def build_issue_worklist(
    issues: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Bucket issues into the operator worklist by their actionable state.

    Each input issue dict carries its issue-row fields plus an
    ``occurrences`` list (the WS6 join). Returns a dict keyed by
    `ISSUE_WORKLIST_SECTIONS`; within each bucket, groups sort
    systemic-first (highest lifetime `times_seen`) then most-recent.
    Issues that aren't operator-actionable right now (bucket None) are
    omitted.
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        key: [] for key, _label, _cls in ISSUE_WORKLIST_SECTIONS
    }
    for issue in issues:
        group = issue_group(issue, issue.get("occurrences") or [])
        bucket = group["bucket"]
        if bucket:
            buckets[bucket].append(group)
    for groups in buckets.values():
        groups.sort(
            key=lambda g: (g["times_seen"] or 0, g["latest_ts"]), reverse=True
        )
    return buckets
