"""WS7 — issue-level operator endpoints (mute / unmute / resolve / reopen).

Two layers: the transition helpers (pure over a write connection) and the
HTTP routes (gate → 404/409, single-writer BEGIN IMMEDIATE). The subtle
piece is unmute, which recomputes unresolved-vs-regressed from the issue's
occurrences rather than guessing.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import get_issue
from dportsv3.tracker.routes import issue_actions as IA
from dportsv3.tracker.server import create_app


# --- transition helpers (no HTTP) ------------------------------------------


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.close()
    return str(p)


def _seed_issue(db, key="k", *, state="unresolved", resolved_at=None,
                origin="ftp/curl", target="@2026Q3"):
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO issues (issue_key, target, origin, fingerprint, state,
              times_seen, first_seen_at, last_seen_at, resolved_at, updated_at)
           VALUES (?, ?, ?, 'fp', ?, 1, '2026-07-25T00:00:00Z',
              '2026-07-25T00:00:00Z', ?, '2026-07-25T00:00:00Z')""",
        (key, target, origin, state, resolved_at),
    )
    conn.commit()
    conn.close()


def _seed_occurrence(db, bundle_id, key, ts, origin="ftp/curl", target="@2026Q3"):
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO bundles (bundle_id, run_id, origin, flavor, ts_utc,
              result, target, issue_key, last_seen_at)
           VALUES (?, 'r', ?, '', ?, 'failure', ?, ?, ?)""",
        (bundle_id, origin, ts, target, key, ts),
    )
    conn.commit()
    conn.close()


def _state(db, key):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return get_issue(conn, key)["state"]
    finally:
        conn.close()


def _write(db):
    w = sqlite3.connect(db, isolation_level=None)
    w.row_factory = sqlite3.Row
    return w


def test_mute_and_resolve_helpers(db):
    _seed_issue(db, "k", state="unresolved")
    w = _write(db)
    try:
        assert IA.mute_issue(w, "k", now="t1", actor="op") == "muted"
        assert _state(db, "k") == "muted"
    finally:
        w.close()
    w = _write(db)
    try:
        assert IA.resolve_issue(w, "k", now="t2", actor="op") == "resolved"
    finally:
        w.close()
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT resolved_at, muted_at FROM issues WHERE issue_key='k'").fetchone()
    conn.close()
    assert row["resolved_at"] == "t2"


def test_unmute_without_prior_resolution_is_unresolved(db):
    _seed_issue(db, "k", state="muted", resolved_at=None)
    w = _write(db)
    try:
        assert IA.unmute_issue(w, "k", now="t", actor="op") == "unresolved"
    finally:
        w.close()


def test_unmute_restores_the_resolution_it_was_muted_from(db):
    # Muted while carrying a resolved_at → it was resolved-then-regressed, so
    # unmute puts the resolution axis back and leaves the `regressed` badge to
    # the projection. Unmute no longer answers "regressed?" itself (C3).
    _seed_issue(db, "k", state="muted", resolved_at="2026-07-25T03:00:00Z")
    _seed_occurrence(db, "b_after", "k", "2026-07-25T04:00:00Z")
    w = _write(db)
    try:
        assert IA.unmute_issue(w, "k", now="t", actor="op") == "resolved"
    finally:
        w.close()


def test_unmute_without_a_resolution_is_unresolved(db):
    # Never resolved (reopen clears resolved_at), so there is nothing to
    # restore — it goes back to the open state it was muted from.
    _seed_issue(db, "k", state="muted", resolved_at=None)
    _seed_occurrence(db, "b1", "k", "2026-07-25T01:00:00Z")
    w = _write(db)
    try:
        assert IA.unmute_issue(w, "k", now="t", actor="op") == "unresolved"
    finally:
        w.close()


def test_reopen_clears_resolution_history(db):
    _seed_issue(db, "k", state="resolved", resolved_at="2026-07-25T03:00:00Z")
    w = _write(db)
    try:
        assert IA.reopen_issue(w, "k", now="t9", actor="alice") == "unresolved"
    finally:
        w.close()
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT resolved_at, reopened_at, reopened_by FROM issues WHERE issue_key='k'"
    ).fetchone()
    conn.close()
    assert row["resolved_at"] is None
    assert row["reopened_at"] == "t9" and row["reopened_by"] == "alice"


# --- HTTP endpoints --------------------------------------------------------


@pytest.fixture
def client(db):
    with TestClient(create_app(db)) as c:
        yield c, db


def test_endpoint_mute_then_gate_blocks_remute(client):
    c, db = client
    _seed_issue(db, "k", state="unresolved")
    r = c.post("/api/issues/k/mute", json={"actor": "tester"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "issue_key": "k", "state": "muted",
                        "actor": "tester"}
    # already muted → mute gate refuses
    assert c.post("/api/issues/k/mute").status_code == 409


def test_endpoint_unknown_issue_404(client):
    c, _db = client
    assert c.post("/api/issues/ghost/resolve").status_code == 404


@pytest.mark.parametrize("state,action,ok", [
    ("unresolved", "resolve", True),
    ("unresolved", "reopen", False),   # not resolved → nothing to reopen
    ("unresolved", "unmute", False),   # not muted
    ("resolved", "reopen", True),
    ("resolved", "mute", False),       # can't mute a resolved issue
    ("muted", "unmute", True),
    ("muted", "resolve", True),
])
def test_endpoint_gate_matches_state(client, state, action, ok):
    c, db = client
    _seed_issue(db, "k", state=state)
    r = c.post(f"/api/issues/k/{action}")
    assert (r.status_code == 200) == ok
    if not ok:
        assert r.status_code == 409


def test_endpoint_default_actor(client):
    c, db = client
    _seed_issue(db, "k", state="unresolved")
    r = c.post("/api/issues/k/resolve")
    assert r.json()["actor"] == "operator"


# --- build controls (DeltaPorts-p0q) ---------------------------------------


def _gens(db, key="k"):
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT requested_build_generation q, last_confirmed_build_generation c,"
        " building_generation b FROM issues WHERE issue_key = ?", (key,)
    ).fetchone()
    conn.close()
    return r["q"], r["c"], r["b"]


def test_endpoint_build_requests_a_confirm_build(client):
    c, db = client
    _seed_issue(db, "k", state="resolving")
    r = c.post("/api/issues/k/build", json={"actor": "tester"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["actor"] == "tester"
    assert body["requested_build_generation"] == 1
    # desired state is now ahead of confirmed -> the runner will build it
    q, conf, _ = _gens(db)
    assert q > conf


def test_endpoint_build_is_gated_to_resolving(client):
    """A confirm build proves an accepted fix; anywhere else the intent would
    be recorded but never acted on, because the feed only reads `resolving`."""
    c, db = client
    for state in ("unresolved", "resolved", "muted"):
        _seed_issue(db, f"k-{state}", state=state)
        r = c.post(f"/api/issues/k-{state}/build")
        assert r.status_code == 409, state


def test_endpoint_build_unknown_issue_is_404(client):
    c, _ = client
    assert c.post("/api/issues/nope/build").status_code == 404


def test_endpoint_repeated_build_requests_do_not_stack_jobs(client):
    """Two clicks bump the counter; single-flight lives in the runner claim, so
    this must not be able to create two builds — only a newer request."""
    c, db = client
    _seed_issue(db, "k", state="resolving")
    c.post("/api/issues/k/build")
    c.post("/api/issues/k/build")
    q, conf, _ = _gens(db)
    assert (q, conf) == (2, 0)


def test_endpoint_cancel_stops_the_pending_build(client):
    c, db = client
    _seed_issue(db, "k", state="resolving")
    c.post("/api/issues/k/build")
    r = c.post("/api/issues/k/build/cancel", json={"actor": "tester"})
    assert r.status_code == 200
    q, conf, building = _gens(db)
    assert q == conf          # nothing left to build
    assert building is None   # in-flight marker dropped
    # the issue keeps its accepted fix; it is simply not building
    assert r.json()["state"] == "resolving"


def test_cancelled_build_drops_out_of_the_reconcile_feed(client):
    from dportsv3.tracker.agentic_queries import issues_needing_build
    c, db = client
    _seed_issue(db, "k", state="resolving")
    c.post("/api/issues/k/build")
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    assert [i["issue_key"] for i in issues_needing_build(conn)] == ["k"]
    c.post("/api/issues/k/build/cancel")
    assert issues_needing_build(conn) == []
    conn.close()


def test_build_actions_emit_events(client):
    c, db = client
    _seed_issue(db, "k", state="resolving")
    c.post("/api/issues/k/build", json={"actor": "alice"})
    c.post("/api/issues/k/build/cancel", json={"actor": "bob"})
    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    types = [r["type"] for r in conn.execute(
        "SELECT type FROM events ORDER BY id")]
    conn.close()
    assert "issue_build_requested" in types
    assert "issue_build_cancelled" in types
