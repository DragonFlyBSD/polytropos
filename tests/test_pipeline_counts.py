"""Inventory counts for the pipeline overview (poly-0e02.1).

The overview's job is "is the system healthy and moving", which only works
if every number is a real count of a named population. The counts it needs
are not all COUNT(*): `regressed` is derived on read (C3) and a worklist
band is `issue_state`'s projection. Both are computed here exactly and
over the whole population anyway -- what makes that affordable is the shape
of the SQL, not a cap.

The load-bearing assertions are the two agreement tests: the cheap answer
has to equal the answer you get by materialising every issue with its
occurrences and running the real projection. That is what catches a
projection quietly starting to read a column the GROUP BY does not carry.
"""

from __future__ import annotations

import sqlite3

import pytest

from dportsv3.db.schema import init_db
from dportsv3.tracker import fix_state, issue_state
from dportsv3.tracker.agentic_queries import (
    RUNNER_BAND,
    issue_inventory,
    issues_with_occurrences,
    regressed_issue_count,
    worklist_band_counts,
)

TARGET = "@main"


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    init_db(db)
    return db


def _issue(db, key, state, *, times_seen=1, green=None, resolved_at=None):
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
        "times_seen, first_seen_at, last_seen_at, green_head_run_id, "
        "resolved_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 't0', 't1', ?, ?, 't1')",
        (key, TARGET, f"devel/{key}", f"fp-{key}", state, times_seen,
         green, resolved_at),
    )


def _run(db, run_id, build_run_id=None):
    db.execute(
        "INSERT OR IGNORE INTO runs(run_id, target, build_run_id) "
        "VALUES (?, ?, ?)", (run_id, TARGET, build_run_id),
    )


def _occurrence(db, bundle_id, issue_key, *, ts, run_id="r-1",
                resolution=None, verification=None, job_state=None):
    _run(db, run_id)
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
        "target, issue_key, resolution, verification_status) "
        "VALUES (?, ?, ?, ?, 'failure', ?, ?, ?, ?)",
        (bundle_id, run_id, f"devel/{issue_key}", ts, TARGET, issue_key,
         resolution, verification),
    )
    if job_state is not None:
        db.execute(
            "INSERT INTO jobs(job_id, bundle_id, origin, state, "
            "created_ts_utc, target) VALUES (?, ?, ?, ?, ?, ?)",
            (f"job-{bundle_id}", bundle_id, f"devel/{issue_key}", job_state,
             ts, TARGET),
        )


# --- issue_inventory ------------------------------------------------------


def test_every_stored_state_is_counted(conn) -> None:
    for n, state in enumerate(sorted(issue_state.ISSUE_STORED_STATES)):
        for k in range(n + 1):
            _issue(conn, f"{state}-{k}", state)

    inventory = issue_inventory(conn)

    assert inventory["by_state"] == {
        "muted": 1, "resolved": 2, "resolving": 3, "unresolved": 4,
    }
    assert inventory["total"] == 10


def test_a_state_nothing_is_in_reads_as_zero(conn) -> None:
    """Zero-filled, because a missing key and a zero are different claims:
    "no issue is muted" versus "the muted count is broken"."""
    _issue(conn, "i-1", "unresolved")

    by_state = issue_inventory(conn)["by_state"]

    assert set(by_state) == set(issue_state.ISSUE_STORED_STATES)
    assert by_state["muted"] == 0


def test_an_empty_tracker_counts_zero_rather_than_failing(conn) -> None:
    inventory = issue_inventory(conn)

    assert inventory["total"] == 0
    assert inventory["systemic"] == 0
    assert set(inventory["by_state"]) == set(issue_state.ISSUE_STORED_STATES)


def test_systemic_is_the_threshold_the_worklist_uses(conn) -> None:
    """The badge and the count have to mean the same thing, so both read
    issue_state.SYSTEMIC_THRESHOLD rather than each hardcoding a number."""
    threshold = issue_state.SYSTEMIC_THRESHOLD
    _issue(conn, "quiet", "unresolved", times_seen=threshold - 1)
    _issue(conn, "loud", "unresolved", times_seen=threshold)
    _issue(conn, "louder", "unresolved", times_seen=threshold + 5)

    inventory = issue_inventory(conn)

    assert inventory["systemic"] == 2
    assert inventory["systemic_threshold"] == threshold
    assert issue_state.issue_group(
        {"issue_key": "loud", "state": "unresolved", "times_seen": threshold},
        [],
    )["systemic"]


def test_regressed_is_not_a_stored_state_in_the_histogram(conn) -> None:
    """`regressed` is derived from a `resolved` row, so it cannot appear in
    a GROUP BY. It is counted separately, and the resolved figure still
    includes the issues that came back."""
    _issue(conn, "back", "resolved", green=5, resolved_at="t1")
    _run(conn, "r-late", build_run_id=9)
    _occurrence(conn, "b-1", "back", ts="t9", run_id="r-late")

    inventory = issue_inventory(conn)

    assert "regressed" not in inventory["by_state"]
    assert inventory["by_state"]["resolved"] == 1
    assert regressed_issue_count(conn) == 1


# --- regressed_issue_count ------------------------------------------------


def test_a_later_build_ordinal_is_a_regression(conn) -> None:
    _issue(conn, "back", "resolved", green=5, resolved_at="t1")
    _run(conn, "r-late", build_run_id=6)
    _occurrence(conn, "b-1", "back", ts="t0", run_id="r-late")

    assert regressed_issue_count(conn) == 1


def test_an_earlier_build_ordinal_is_not(conn) -> None:
    """The ordinal is the trusted comparison and it wins outright: this
    occurrence's timestamp is past resolved_at, and it still is not a
    regression, because the build it came from ran before the fix was
    proven."""
    _issue(conn, "fixed", "resolved", green=5, resolved_at="t1")
    _run(conn, "r-early", build_run_id=4)
    _occurrence(conn, "b-1", "fixed", ts="t9", run_id="r-early")

    assert regressed_issue_count(conn) == 0


def test_without_an_ordinal_the_timestamp_decides(conn) -> None:
    """A manually resolved issue records no watermark, and an occurrence
    from a build the tracker never saw carries no ordinal. Degraded, not
    absent."""
    _issue(conn, "manual", "resolved", resolved_at="t5")
    _run(conn, "r-unlinked", build_run_id=None)
    _occurrence(conn, "b-1", "manual", ts="t6", run_id="r-unlinked")

    assert regressed_issue_count(conn) == 1


def test_an_issue_with_no_boundary_at_all_cannot_regress(conn) -> None:
    _issue(conn, "no-boundary", "resolved")
    _occurrence(conn, "b-1", "no-boundary", ts="t9")

    assert regressed_issue_count(conn) == 0


def test_only_a_resolved_issue_can_regress(conn) -> None:
    """The narrowing is a fact about the rule, not a copy of it:
    derived_regression returns None for every other state, so no other
    state can contribute."""
    for state in ("unresolved", "resolving", "muted"):
        _issue(conn, state, state, green=1, resolved_at="t1")
        _run(conn, f"r-{state}", build_run_id=99)
        _occurrence(conn, f"b-{state}", state, ts="t9", run_id=f"r-{state}")

    assert regressed_issue_count(conn) == 0


def test_an_issue_is_counted_once_however_often_it_came_back(conn) -> None:
    _issue(conn, "back", "resolved", green=5, resolved_at="t1")
    for n in range(4):
        _run(conn, f"r-{n}", build_run_id=6 + n)
        _occurrence(conn, f"b-{n}", "back", ts=f"t{n}", run_id=f"r-{n}")

    assert regressed_issue_count(conn) == 1


def test_the_count_agrees_with_deriving_it_per_issue(conn) -> None:
    """The agreement test for regression: the streamed count must equal
    what derived_regression says issue by issue, which is what the badge
    on every page reads."""
    _issue(conn, "back", "resolved", green=5, resolved_at="t1")
    _issue(conn, "held", "resolved", green=5, resolved_at="t1")
    _issue(conn, "manual", "resolved", resolved_at="t5")
    _issue(conn, "bare", "resolved")
    _run(conn, "r-late", build_run_id=7)
    _run(conn, "r-early", build_run_id=2)
    _occurrence(conn, "b-1", "back", ts="t2", run_id="r-late")
    _occurrence(conn, "b-2", "held", ts="t9", run_id="r-early")
    _occurrence(conn, "b-3", "manual", ts="t9", run_id="r-early")
    _occurrence(conn, "b-4", "bare", ts="t9", run_id="r-early")

    per_issue = sum(
        1 for issue in issues_with_occurrences(conn, states=("resolved",))
        if issue_state.derived_regression(issue, issue["occurrences"])
    )

    assert regressed_issue_count(conn) == per_issue == 2


# --- worklist_band_counts -------------------------------------------------


#: One issue per band, built from the (resolution, verification_status,
#: job_state) that produces it. Keep every band represented -- the
#: agreement test is only worth as much as its coverage.
BAND_CASES = [
    ("ready", {"resolution": fix_state.RESOLUTION_AGENT_FIXED,
               "verification": fix_state.VERIFIED}),
    ("ready", {"resolution": fix_state.RESOLUTION_OPERATOR_OWNED,
               "verification": fix_state.VERIFIED}),
    ("verify", {"resolution": fix_state.RESOLUTION_AGENT_FIXED}),
    ("decide", {"resolution": fix_state.RESOLUTION_AGENT_FIXED,
                "verification": fix_state.VERIFICATION_FAILED}),
    ("decide", {"resolution": fix_state.RESOLUTION_TRIAGE_FAILED}),
    ("decide", {"resolution": fix_state.RESOLUTION_AGENT_GAVE_UP}),
    ("decide", {"resolution": fix_state.RESOLUTION_AGENT_BUDGET}),
    ("decide", {"resolution": fix_state.RESOLUTION_ESCALATED}),
    ("decide", {"resolution": fix_state.RESOLUTION_ACCEPTED}),
    ("decide", {"resolution": fix_state.RESOLUTION_MERGED}),
    ("decide", {"resolution": fix_state.RESOLUTION_REJECTED}),
    ("decide", {"resolution": fix_state.RESOLUTION_DISCARDED}),
    ("decide", {}),
    ("owned", {"resolution": fix_state.RESOLUTION_OPERATOR_OWNED}),
    (RUNNER_BAND, {"job_state": "patching"}),
    (RUNNER_BAND, {"job_state": "triaging"}),
]


def _seed_every_band(db) -> dict[str, int]:
    expected: dict[str, int] = {}
    for n, (band, occurrence) in enumerate(BAND_CASES):
        key = f"i-{n:02d}"
        _issue(db, key, "unresolved")
        _occurrence(db, f"b-{n:02d}", key, ts=f"t{n:02d}", **occurrence)
        expected[band] = expected.get(band, 0) + 1
    # `confirming` is the issue's own state, not its occurrence's
    _issue(db, "i-confirm", "resolving")
    _occurrence(db, "b-confirm", "i-confirm", ts="t90", resolution="accepted")
    expected["confirming"] = 1
    # an open issue nothing has produced an occurrence for yet
    _issue(db, "i-bare", "unresolved")
    expected["decide"] += 1
    return expected


def test_each_band_is_counted(conn) -> None:
    expected = _seed_every_band(conn)

    counts = worklist_band_counts(conn)

    assert counts == {
        **{key: 0 for key, _l, _c in issue_state.ISSUE_WORKLIST_SECTIONS
           if key not in ("done", "muted")},
        RUNNER_BAND: 0,
        **expected,
    }


def test_the_grouped_count_equals_the_full_materialise(conn) -> None:
    """The load-bearing test. The counts are cheap because SQL groups by
    the four columns the projection reads and Python runs it once per
    distinct combination -- so a projection that starts reading a fifth
    column makes them silently wrong. Materialising every issue with all
    of its occurrences and running build_issue_worklist is the slow answer
    that cannot go wrong that way; they must match.
    """
    _seed_every_band(conn)
    # a second occurrence on one issue, older, in a different band: the
    # actionable occurrence is the newest and the other must not count
    _occurrence(conn, "b-old", "i-02", ts="t00",
                resolution=fix_state.RESOLUTION_OPERATOR_OWNED)

    issues = issues_with_occurrences(
        conn, states=("unresolved", "resolving"), limit=1000,
    )
    worklist = issue_state.build_issue_worklist(issues)
    expected = {
        key: len(groups) for key, groups in worklist.items()
        if key not in ("done", "muted")
    }
    expected[RUNNER_BAND] = sum(
        1 for issue in issues
        if issue_state.issue_bucket(issue, issue["occurrences"]) is None
    )

    assert worklist_band_counts(conn) == expected


def test_the_newest_occurrence_decides_the_band(conn) -> None:
    """The band comes from the actionable occurrence, and the SQL picks the
    same one issue_state.actionable_occurrence would."""
    _issue(conn, "i-1", "unresolved")
    _occurrence(conn, "b-old", "i-1", ts="t1",
                resolution=fix_state.RESOLUTION_OPERATOR_OWNED)
    _occurrence(conn, "b-new", "i-1", ts="t2",
                resolution=fix_state.RESOLUTION_AGENT_FIXED,
                verification=fix_state.VERIFIED)

    assert worklist_band_counts(conn)["ready"] == 1
    assert worklist_band_counts(conn)["owned"] == 0


def test_a_tie_on_timestamp_breaks_the_way_the_projection_breaks_it(
    conn,
) -> None:
    """Same ts_utc on two occurrences: the query orders bundle_id DESC and
    so does the feed the projection consumes, so both pick the same row."""
    _issue(conn, "i-1", "unresolved")
    _occurrence(conn, "b-a", "i-1", ts="t1",
                resolution=fix_state.RESOLUTION_OPERATOR_OWNED)
    _occurrence(conn, "b-z", "i-1", ts="t1",
                resolution=fix_state.RESOLUTION_AGENT_FIXED,
                verification=fix_state.VERIFIED)

    issues = issues_with_occurrences(conn, states=("unresolved",))
    actionable = issue_state.actionable_occurrence(issues[0]["occurrences"])

    assert actionable["bundle_id"] == "b-z"
    assert worklist_band_counts(conn)["ready"] == 1


def test_runner_owned_work_is_counted_rather_than_dropped(conn) -> None:
    """The worklist drops a None bucket because it lists what needs you.
    An inventory must not: that band is the system moving on its own, which
    is what the overview exists to show."""
    _issue(conn, "i-live", "unresolved")
    _occurrence(conn, "b-live", "i-live", ts="t1", job_state="patching")

    counts = worklist_band_counts(conn)

    assert counts[RUNNER_BAND] == 1
    assert sum(counts.values()) == 1


def test_the_archives_are_not_bands(conn) -> None:
    """resolved and muted are stored states, counted by issue_inventory.
    Counting them here as well would double-count them on the overview."""
    _issue(conn, "i-done", "resolved")
    _issue(conn, "i-muted", "muted")
    _occurrence(conn, "b-done", "i-done", ts="t1",
                resolution=fix_state.RESOLUTION_MERGED)
    _occurrence(conn, "b-muted", "i-muted", ts="t1")

    counts = worklist_band_counts(conn)

    assert sum(counts.values()) == 0
    assert "done" not in counts
    assert "muted" not in counts


def test_the_bands_are_the_projections_and_not_a_local_list(conn) -> None:
    expected = {
        key for key, _label, _cls in issue_state.ISSUE_WORKLIST_SECTIONS
        if key not in ("done", "muted")
    } | {RUNNER_BAND}

    assert set(worklist_band_counts(conn)) == expected


def test_an_empty_tracker_reports_every_band_as_zero(conn) -> None:
    assert set(worklist_band_counts(conn).values()) == {0}
