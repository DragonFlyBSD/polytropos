"""C3 — `regressed` as a derived read, not a stored state.

The resolution axis (`issues.state` ∈ unresolved/resolving/resolved/muted)
and the build-observation axis (did a later build re-emit the fingerprint?)
used to be conflated in one column, answered independently in three places:
the artifact-store writer flipped `resolved`/`resolving` to `regressed` on any
arriving occurrence, the unmute path recomputed it from timestamps, and the
projection read the stored column back. This file pins the single rule that
replaces them, and the two consequences that follow from it — the row never
says `regressed`, and everything downstream still does.
"""

from __future__ import annotations

import sqlite3

import pytest

from dportsv3.db.schema import SCHEMA, init_db
from dportsv3.tracker import issue_state as I

HEAD = 7


def _issue(state="resolved", *, green_head=None, resolved_at=None, key="k"):
    return {"issue_key": key, "state": state, "origin": "ftp/curl",
            "target": "@2026Q3", "green_head_run_id": green_head,
            "resolved_at": resolved_at, "times_seen": 3}


def _occ(bundle_id="b", *, ordinal=None, ts="2026-07-25T00:00:00Z"):
    return {"bundle_id": bundle_id, "build_run_id": ordinal, "ts_utc": ts,
            "resolution": None, "verification_status": None}


# --- the boundary rule -----------------------------------------------------


def test_ordinal_past_the_watermark_is_a_regression():
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    assert I.occurrence_past_boundary(issue, _occ(ordinal=HEAD + 1)) is True


def test_ordinal_at_the_watermark_is_not():
    """The watermark is the build that was current when the fix was proven,
    so it is inside the known-good region, not past it."""
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    assert I.occurrence_past_boundary(issue, _occ(ordinal=HEAD)) is False


def test_ordinal_before_the_watermark_is_not():
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    assert I.occurrence_past_boundary(issue, _occ(ordinal=HEAD - 1)) is False


@pytest.mark.parametrize("ordinal,ts,expected", [
    (HEAD + 1, "2020-01-01T00:00:00Z", True),    # ordinal says after, clock before
    (HEAD - 1, "2099-01-01T00:00:00Z", False),   # ordinal says before, clock after
])
def test_the_ordinal_beats_the_clock(ordinal, ts, expected):
    """Build hosts and the store do not share a clock. When both ordinals are
    known the timestamps are not consulted at all, so skew can neither invent
    a regression nor hide one."""
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    assert I.occurrence_past_boundary(issue, _occ(ordinal=ordinal, ts=ts)) is expected


@pytest.mark.parametrize("green_head,ordinal", [
    (None, 9),      # manual resolve: no confirm build, so no watermark
    (HEAD, None),   # occurrence from a build the tracker never saw
    (None, None),
])
def test_without_both_ordinals_it_falls_back_to_timestamps(green_head, ordinal):
    issue = _issue(green_head=green_head, resolved_at="2026-07-25T00:00:00Z")
    after = _occ(ordinal=ordinal, ts="2026-07-25T09:00:00Z")
    before = _occ(ordinal=ordinal, ts="2026-07-24T09:00:00Z")
    assert I.occurrence_past_boundary(issue, after) is True
    assert I.occurrence_past_boundary(issue, before) is False


def test_no_boundary_at_all_means_nothing_is_past_it():
    """Neither a watermark nor a resolved_at: there is no known-good point to
    be after, so no occurrence can be a regression."""
    issue = _issue(green_head=None, resolved_at=None)
    assert I.occurrence_past_boundary(issue, _occ(ts="2099-01-01T00:00:00Z")) is False


# --- the derivation --------------------------------------------------------


def test_derived_regression_reports_the_earliest_crossing():
    """When it came back, not when it was last seen."""
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    occs = [
        _occ("late", ordinal=HEAD + 5, ts="2026-07-28T00:00:00Z"),
        _occ("first", ordinal=HEAD + 1, ts="2026-07-26T00:00:00Z"),
        _occ("old", ordinal=HEAD - 1, ts="2026-07-01T00:00:00Z"),
    ]
    assert I.derived_regression(issue, occs) == "2026-07-26T00:00:00Z"


def test_no_crossing_is_no_regression():
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    assert I.derived_regression(issue, [_occ(ordinal=HEAD)]) is None
    assert I.derived_regression(issue, []) is None


@pytest.mark.parametrize("state", ["unresolved", "resolving", "muted"])
def test_only_a_resolved_issue_can_regress(state):
    """`resolving` is deliberately excluded: a farm build still failing while
    the confirm build is in flight is the unfixed port being observed, and the
    confirm verdict (A2/A3) is what settles it."""
    issue = _issue(state, green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    assert I.derived_regression(issue, [_occ(ordinal=HEAD + 1)]) is None


# --- folding the axes back together ----------------------------------------


def test_effective_state_overrides_resolved_with_regressed():
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    issue["occurrences"] = [_occ(ordinal=HEAD + 1)]
    assert issue["state"] == "resolved"          # the row never says otherwise
    assert I.effective_state(issue) == "regressed"


def test_effective_state_of_a_resolved_issue_that_held():
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    issue["occurrences"] = [_occ(ordinal=HEAD)]
    assert I.effective_state(issue) == "resolved"


@pytest.mark.parametrize("state", ["unresolved", "resolving", "muted"])
def test_effective_state_passes_other_states_through(state):
    assert I.effective_state(_issue(state)) == state


def test_effective_state_defaults_to_attached_occurrences():
    """Template globals get the row alone, so the attached occurrences are the
    default source — the query layer attaches them on every issue read."""
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    issue["occurrences"] = [_occ(ordinal=HEAD + 1)]
    assert I.effective_state(issue) == I.effective_state(issue, issue["occurrences"])


# --- what the operator sees ------------------------------------------------


def _returned():
    issue = _issue(green_head=HEAD, resolved_at="2026-07-25T00:00:00Z")
    occs = [_occ("b1", ordinal=HEAD + 1, ts="2026-07-26T00:00:00Z")]
    issue["occurrences"] = occs
    return issue, occs


def test_badge_is_loud_for_a_derived_regression():
    issue, _ = _returned()
    assert I.issue_status(issue).key == "regressed"
    assert I.issue_status(issue).pill == "failed"


def test_a_derived_regression_keeps_the_open_issue_controls():
    """The row reads `resolved`, which would otherwise take mute and resolve
    away and offer reopen instead."""
    issue, _ = _returned()
    acts = I.issue_actions(issue)
    assert acts["can_mute"] and acts["can_resolve"]
    assert not acts["can_reopen"]


def test_a_derived_regression_buckets_as_an_open_problem():
    """Not into `done`, the resolved archive, where its row would put it."""
    issue, occs = _returned()
    assert I.issue_bucket(issue, occs) == "decide"


def test_issue_group_exposes_the_flag_and_the_crossing():
    issue, occs = _returned()
    group = I.issue_group(issue, occs)
    assert group["regressed"] is True
    assert group["regressed_at"] == "2026-07-26T00:00:00Z"
    assert group["state"] == "regressed"


def test_stored_states_for_narrows_the_sql_prefilter():
    assert I.stored_states_for("regressed") == ("resolved",)
    assert I.stored_states_for("resolved") == ("resolved",)
    assert I.stored_states_for("muted") == ("muted",)
    assert I.stored_states_for("bogus") == ()


def test_regressed_is_not_in_the_stored_vocabulary():
    assert "regressed" not in I.ISSUE_STORED_STATES
    assert I.ISSUE_STORED_STATES == {
        "unresolved", "resolving", "resolved", "muted"}


# --- the column ------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_db(c)
    return c


def test_the_state_column_refuses_regressed(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO issues (issue_key, origin, state, updated_at) "
            "VALUES ('k', 'ftp/curl', 'regressed', 'now')"
        )


@pytest.mark.parametrize("state", sorted(I.ISSUE_STORED_STATES))
def test_the_state_column_accepts_the_vocabulary(conn, state):
    conn.execute(
        "INSERT INTO issues (issue_key, origin, state, updated_at) "
        "VALUES (?, 'ftp/curl', ?, 'now')", (state, state),
    )


_CHECK_CLAUSE = """
                     CHECK (state IN ('unresolved', 'resolving',
                                      'resolved', 'muted'))"""


def _legacy_regressed(path, key, resolved_at):
    """A state.db as it was written before C3: the real schema, minus the
    CHECK that now keeps `regressed` out of the column."""
    legacy = SCHEMA.replace(_CHECK_CLAUSE, "")
    assert legacy != SCHEMA, "the CHECK clause moved; update _CHECK_CLAUSE"
    c = sqlite3.connect(path)
    c.executescript(legacy)
    c.execute("INSERT INTO issues (issue_key, target, origin, state, "
              "resolved_at, updated_at) VALUES (?, '@2026Q3', 'ftp/curl', "
              "'regressed', ?, 'now')", (key, resolved_at))
    c.commit()
    c.close()


def test_migration_keeps_a_real_resolution(tmp_path):
    """Resolved and then came back: the resolution axis really did say
    resolved, and the derivation reproduces the badge from the occurrences."""
    path = str(tmp_path / "state.db")
    _legacy_regressed(path, "k", "2026-07-25T00:00:00Z")
    c = sqlite3.connect(path)
    init_db(c)
    assert c.execute(
        "SELECT state FROM issues WHERE issue_key='k'").fetchone()[0] == "resolved"


def test_migration_does_not_invent_a_resolution(tmp_path):
    """No resolved_at means it was flipped out of `resolving` by an arriving
    occurrence — a fix accepted but never proved. Calling that `resolved`
    would claim a resolution that never happened."""
    path = str(tmp_path / "state.db")
    _legacy_regressed(path, "k", None)
    c = sqlite3.connect(path)
    init_db(c)
    assert c.execute(
        "SELECT state FROM issues WHERE issue_key='k'").fetchone()[0] == "unresolved"


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "state.db")
    _legacy_regressed(path, "k", "2026-07-25T00:00:00Z")
    c = sqlite3.connect(path)
    init_db(c)
    c.execute("UPDATE issues SET state='muted' WHERE issue_key='k'")
    c.commit()
    init_db(c)
    assert c.execute(
        "SELECT state FROM issues WHERE issue_key='k'").fetchone()[0] == "muted"


# --- through the real stack ------------------------------------------------


@pytest.fixture
def stack(tmp_path):
    """A DB with one resolved issue whose fingerprint came back at a later
    build ordinal, plus the app that reads it."""
    import threading

    from dportsv3.artifact_store import ArtifactStore
    from dportsv3.fingerprint import compute_fingerprint, issue_key
    from dportsv3.tracker.server import create_app

    errors = "cc: error A\n"
    target, origin = "@2026Q3", "ftp/curl"
    key = issue_key(target, origin, compute_fingerprint(errors))

    db = str(tmp_path / "state.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    store = ArtifactStore.__new__(ArtifactStore)
    store.conn = conn
    store._lock = threading.Lock()

    def up(bundle_id, ts, run_id, ordinal):
        store.upsert_run_bundle({
            "run_id": run_id, "profile": "p", "ts_utc": ts,
            "bundle_id": bundle_id, "origin": origin, "flavor": "",
            "result": "failure", "target": target, "errors_text": errors,
            "build_run_id": ordinal,
        })

    up("b1", "2026-07-25T00:00:00Z", "r1", HEAD)
    conn.execute(
        "UPDATE issues SET state='resolved', resolved_at='2026-07-25T01:00:00Z', "
        "green_head_run_id=? WHERE issue_key=?", (HEAD, key),
    )
    conn.commit()
    up("b2", "2026-07-25T09:00:00Z", "r2", HEAD + 1)
    conn.commit()
    conn.close()

    from fastapi.testclient import TestClient
    with TestClient(create_app(db)) as client:
        yield client, db, key


def _read(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def test_the_row_stays_resolved_end_to_end(stack):
    _client, db, key = stack
    with _read(db) as c:
        assert c.execute(
            "SELECT state FROM issues WHERE issue_key=?", (key,)
        ).fetchone()[0] == "resolved"


def test_get_issue_carries_the_ordinal_and_derives(stack):
    from dportsv3.tracker.agentic_queries import get_issue
    _client, db, key = stack
    with _read(db) as c:
        issue = get_issue(c, key)
    assert {o["build_run_id"] for o in issue["occurrences"]} == {HEAD, HEAD + 1}
    assert I.effective_state(issue) == "regressed"


def test_issue_for_bundle_derives_for_the_bundle_page_badge(stack):
    """The bundle page reads the issue through a different query; it must not
    show a bare `resolved` for a fix that came back."""
    from dportsv3.tracker.agentic_queries import issue_for_bundle
    _client, db, _key = stack
    with _read(db) as c:
        issue = issue_for_bundle(c, "b2")
    assert I.effective_state(issue) == "regressed"


def test_the_regressed_chip_finds_it(stack):
    """`regressed` cannot be a SQL filter any more — the view narrows to the
    stored states that could present as it, then filters exactly."""
    client, _db, key = stack
    body = client.get("/agentic/issues?state=regressed").text
    assert key in body


def test_the_resolved_chip_does_not(stack):
    """A fix that came back must not sit in the resolved archive."""
    client, _db, key = stack
    assert key not in client.get("/agentic/issues?state=resolved").text


def test_accepting_a_new_fix_works_on_a_derived_regression(stack):
    """The row says `resolved`, which mark_issue_resolving refuses; the
    derivation is what re-opens the door."""
    from dportsv3.tracker.routes.issue_actions import mark_issue_resolving
    _client, db, key = stack
    w = sqlite3.connect(db, isolation_level=None)
    w.row_factory = sqlite3.Row
    try:
        assert mark_issue_resolving(
            w, key, bundle_id="b2", now="t", actor="op") == "resolving"
    finally:
        w.close()


def test_accepting_a_fix_still_cannot_override_a_resolution_that_held(stack):
    from dportsv3.tracker.routes.issue_actions import mark_issue_resolving
    _client, db, key = stack
    w = sqlite3.connect(db, isolation_level=None)
    w.row_factory = sqlite3.Row
    try:
        # Drop the occurrence that crossed the boundary: nothing derives now,
        # so the row's `resolved` stands and the fix is refused.
        w.execute("DELETE FROM bundles WHERE bundle_id='b2'")
        assert mark_issue_resolving(
            w, key, bundle_id="b1", now="t", actor="op") == "resolved"
    finally:
        w.close()


def test_muting_a_derived_regression_round_trips(stack):
    """Mute takes the row to `muted`, losing `resolved`; unmute restores it
    from resolved_at and the derivation makes it loud again."""
    client, db, key = stack
    assert client.post(f"/api/issues/{key}/mute", json={}).status_code == 200
    with _read(db) as c:
        assert c.execute(
            "SELECT state FROM issues WHERE issue_key=?", (key,)
        ).fetchone()[0] == "muted"
    assert client.post(f"/api/issues/{key}/unmute", json={}).status_code == 200

    from dportsv3.tracker.agentic_queries import get_issue
    with _read(db) as c:
        issue = get_issue(c, key)
    assert issue["state"] == "resolved"
    assert I.effective_state(issue) == "regressed"


# --- every resolve records a boundary --------------------------------------


@pytest.fixture
def resolvable(tmp_path):
    """A DB with two finished builds on the target and one open issue."""
    db = str(tmp_path / "state.db")
    c = sqlite3.connect(db, isolation_level=None)
    c.row_factory = sqlite3.Row
    init_db(c)
    for _ in range(2):
        c.execute("INSERT INTO build_runs (target, build_type, started_at, "
                  "finished_at) VALUES ('@2026Q3', 'test', 't', 't')")
    c.execute("INSERT INTO issues (issue_key, target, origin, state, "
              "updated_at) VALUES ('k', '@2026Q3', 'ftp/curl', "
              "'unresolved', 'now')")
    c.execute("INSERT INTO bundles (bundle_id, issue_key, origin, ts_utc, "
              "resolution) VALUES ('b1', 'k', 'ftp/curl', "
              "'2026-07-25T00:00:00Z', 'accepted')")
    return c, db


def _head(conn):
    return conn.execute(
        "SELECT green_head_run_id FROM issues WHERE issue_key='k'"
    ).fetchone()[0]


def test_manual_resolve_records_the_boundary(resolvable):
    """The operator asserting a fix is fixed still sets a boundary — without
    one this issue would compare wall clocks for the rest of its life."""
    from dportsv3.tracker.routes.issue_actions import resolve_issue
    conn, _db = resolvable
    assert resolve_issue(conn, "k", now="t", actor="op") == "resolved"
    assert _head(conn) == 2


def test_merge_resolve_records_the_boundary(resolvable):
    from dportsv3.tracker.delivery_sync import resolve_issue_for_bundle
    conn, _db = resolvable
    assert resolve_issue_for_bundle(
        conn, "b1", now_iso="t", source="poll") == "k"
    assert _head(conn) == 2


def test_confirm_build_resolve_records_the_boundary(resolvable):
    from dportsv3.tracker.agentic_queries import green_head_watermark
    from dportsv3.tracker.routes.issue_actions import (
        resolve_issue_build_confirmed,
    )
    conn, _db = resolvable
    conn.execute("UPDATE issues SET state='resolving' WHERE issue_key='k'")
    assert resolve_issue_build_confirmed(
        conn, "k", now="t",
        green_head_run_id=green_head_watermark(conn, "@2026Q3"),
    ) == "resolved"
    assert _head(conn) == 2


def test_no_builds_on_the_target_means_no_boundary(resolvable):
    """Nothing to be after: the derivation falls back to timestamps rather
    than treating ordinal 0 as a watermark."""
    from dportsv3.tracker.routes.issue_actions import resolve_issue
    conn, _db = resolvable
    conn.execute("DELETE FROM build_runs")
    resolve_issue(conn, "k", now="t", actor="op")
    assert _head(conn) is None
