"""WS3 — the issue writer (artifact_store, single writer).

An occurrence's ingest find-or-creates the fingerprinted issue it
belongs to and rolls it up. The invariants pinned here:

- first occurrence for a key **creates** the issue (`unresolved`,
  `times_seen=1`, first/last/latest stamped);
- a later occurrence bumps `times_seen` and, when it is the newest by
  timestamp, advances `last_seen_at` + `latest_bundle_id`;
- a **re-upsert of the same bundle_id** (status touch) does NOT bump
  rollups — rollups count occurrences, not writes;
- an arriving occurrence NEVER moves the issue's state (C3): it rolls up
  and, when it lands past a resolved fix's known-good boundary, emits
  `issue_regressed` as a notification — the badge itself is derived on read;
- a failure with no fingerprint lands on a per-`(target, origin)`
  fallback issue, distinct from any fingerprinted issue;
- `bundles.issue_key` links the occurrence and is set once at birth.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from dportsv3.artifact_store import ArtifactStore
from dportsv3.db.schema import init_db
from dportsv3.fingerprint import compute_fingerprint, issue_key

ERR = "/tmp/build.xy/work/foo.c:12:3: error: 'X' undeclared\n"
TARGET = "@2026Q3"
ORIGIN = "ftp/curl"


@pytest.fixture
def store():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    s = ArtifactStore.__new__(ArtifactStore)
    s.conn = conn
    s._lock = threading.Lock()
    return s


def _up(store, bundle_id, *, errors=None, ts="2026-07-25T00:00:00Z", origin=ORIGIN,
        run_id="r1", build_run_id=""):
    payload = {
        "run_id": run_id, "profile": "p", "ts_utc": ts, "bundle_id": bundle_id,
        "origin": origin, "flavor": "", "result": "failure", "target": TARGET,
        "build_run_id": build_run_id,
    }
    if errors is not None:
        payload["errors_text"] = errors
    store.upsert_run_bundle(payload)


def _issue(store, key):
    return store.conn.execute(
        "SELECT * FROM issues WHERE issue_key = ?", (key,)
    ).fetchone()


def _key(fingerprint):
    return issue_key(TARGET, ORIGIN, fingerprint)


def test_first_occurrence_creates_issue(store):
    _up(store, "b1", errors=ERR, ts="2026-07-25T00:00:00Z")
    row = _issue(store, _key(compute_fingerprint(ERR)))
    assert row["state"] == "unresolved"
    assert row["times_seen"] == 1
    assert row["first_seen_at"] == "2026-07-25T00:00:00Z"
    assert row["last_seen_at"] == "2026-07-25T00:00:00Z"
    assert row["latest_bundle_id"] == "b1"
    # the occurrence links up
    linked = store.conn.execute(
        "SELECT issue_key FROM bundles WHERE bundle_id = 'b1'"
    ).fetchone()[0]
    assert linked == _key(compute_fingerprint(ERR))


def test_second_occurrence_rolls_up_and_advances_pointer(store):
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR, ts="2026-07-25T00:00:00Z")
    _up(store, "b2", errors=ERR, ts="2026-07-25T05:00:00Z")
    row = _issue(store, key)
    assert row["times_seen"] == 2
    assert row["last_seen_at"] == "2026-07-25T05:00:00Z"
    assert row["latest_bundle_id"] == "b2"
    assert row["first_seen_at"] == "2026-07-25T00:00:00Z"  # unchanged


def test_reupsert_same_bundle_does_not_double_count(store):
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR, ts="2026-07-25T00:00:00Z")
    # Same bundle_id again (e.g. a later status touch), no fresh errors.
    _up(store, "b1", ts="2026-07-25T00:00:00Z")
    assert _issue(store, key)["times_seen"] == 1


def _issue_events(store):
    return [r[0] for r in store.conn.execute(
        "SELECT type FROM events WHERE type LIKE 'issue_%' ORDER BY id"
    ).fetchall()]


def test_occurrence_past_the_boundary_notifies_without_moving_state(store):
    """The fix came back — but the row stays `resolved`.

    Regression is on the build-observation axis and derived on read (C3), so
    the writer's only job here is the rollup plus the notification.
    """
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR)
    store.conn.execute(
        "UPDATE issues SET state='resolved', resolved_at='2026-07-25T01:00:00Z' WHERE issue_key=?",
        (key,),
    )
    store.conn.commit()
    _up(store, "b2", errors=ERR, ts="2026-07-25T09:00:00Z")
    row = _issue(store, key)
    assert row["state"] == "resolved"
    assert row["times_seen"] == 2
    assert _issue_events(store) == ["issue_created", "issue_regressed"]


def test_occurrence_before_the_boundary_does_not_notify(store):
    """An occurrence that predates the resolution is the old failure being
    reported late, not a fix that came back."""
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR)
    store.conn.execute(
        "UPDATE issues SET state='resolved', resolved_at='2026-07-25T08:00:00Z' WHERE issue_key=?",
        (key,),
    )
    store.conn.commit()
    _up(store, "b2", errors=ERR, ts="2026-07-25T03:00:00Z")
    assert _issue(store, key)["state"] == "resolved"
    assert _issue_events(store) == ["issue_created"]


def test_boundary_uses_the_build_ordinal_over_the_clock(store):
    """With a Green-Head watermark and a build ordinal on the occurrence, the
    ordinals decide — even when the timestamps say the opposite.

    This is the whole point of the watermark: the build hosts and the store
    do not share a clock, so a skewed ts_utc must not be able to invent or
    hide a regression.
    """
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR, run_id="r1", build_run_id=7)
    store.conn.execute(
        "UPDATE issues SET state='resolved', resolved_at='2026-07-25T08:00:00Z', "
        "green_head_run_id=7 WHERE issue_key=?",
        (key,),
    )
    store.conn.commit()
    # Build 9 is past the watermark, but its clock reads BEFORE the resolve.
    _up(store, "b2", errors=ERR, ts="2026-07-25T03:00:00Z",
        run_id="r2", build_run_id=9)
    assert _issue_events(store) == ["issue_created", "issue_regressed"]


def test_occurrence_at_the_boundary_is_not_a_regression(store):
    """The watermark records the build that was current when the fix was
    proven, so that build itself is not past it — only a strictly later one
    is."""
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR, run_id="r1", build_run_id=7)
    store.conn.execute(
        "UPDATE issues SET state='resolved', resolved_at='2026-07-25T01:00:00Z', "
        "green_head_run_id=7 WHERE issue_key=?",
        (key,),
    )
    store.conn.commit()
    _up(store, "b2", errors=ERR, ts="2026-07-25T09:00:00Z",
        run_id="r2", build_run_id=7)
    assert _issue_events(store) == ["issue_created"]


def test_resolving_issue_is_left_to_its_confirm_build(store):
    """A farm build still failing while the confirm build is in flight is the
    unfixed port being observed, not a fix that came back. The writer used to
    rewrite the state to `regressed` and drop the deliverable here."""
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR)
    store.conn.execute(
        "UPDATE issues SET state='resolving', delivery_bundle_id='b1' "
        "WHERE issue_key=?", (key,),
    )
    store.conn.commit()
    _up(store, "b2", errors=ERR, ts="2026-07-25T09:00:00Z")
    row = _issue(store, key)
    assert (row["state"], row["delivery_bundle_id"]) == ("resolving", "b1")
    assert row["times_seen"] == 2
    assert _issue_events(store) == ["issue_created"]


def test_build_ordinal_is_recorded_on_the_run(store):
    """The link C3 needs: the tracker build_runs ordinal the hook sent,
    stored on the occurrence's run."""
    _up(store, "b1", errors=ERR, run_id="r9", build_run_id=42)
    assert store.conn.execute(
        "SELECT build_run_id FROM runs WHERE run_id='r9'"
    ).fetchone()[0] == 42


def test_a_later_upsert_without_an_ordinal_keeps_the_recorded_one(store):
    """Tracking dropping out mid-run must not erase the linkage a previous
    occurrence established."""
    _up(store, "b1", errors=ERR, run_id="r9", build_run_id=42)
    _up(store, "b2", errors=ERR, run_id="r9", build_run_id="")
    assert store.conn.execute(
        "SELECT build_run_id FROM runs WHERE run_id='r9'"
    ).fetchone()[0] == 42


def test_muted_issue_stays_muted_but_counts(store):
    key = _key(compute_fingerprint(ERR))
    _up(store, "b1", errors=ERR)
    store.conn.execute(
        "UPDATE issues SET state='muted', muted_at='m', muted_by='op' WHERE issue_key=?",
        (key,),
    )
    store.conn.commit()
    _up(store, "b2", errors=ERR, ts="2026-07-25T09:00:00Z")
    row = _issue(store, key)
    assert row["state"] == "muted"          # silent — no surfacing/auto-triage
    assert row["times_seen"] == 2           # but still counted
    # muting must not spuriously emit a regression
    assert not store.conn.execute(
        "SELECT 1 FROM events WHERE type='issue_regressed'"
    ).fetchone()


def test_late_arriving_occurrence_keeps_newest_pointer(store):
    key = _key(compute_fingerprint(ERR))
    _up(store, "b_new", errors=ERR, ts="2026-07-25T12:00:00Z")
    _up(store, "b_old", errors=ERR, ts="2026-07-24T00:00:00Z")  # older ts
    row = _issue(store, key)
    assert row["times_seen"] == 2
    assert row["latest_bundle_id"] == "b_new"
    assert row["last_seen_at"] == "2026-07-25T12:00:00Z"


def test_no_fingerprint_lands_on_fallback_issue(store):
    _up(store, "bn", errors="", ts="2026-07-25T00:00:00Z")  # empty → no signature
    fallback = _key(None)
    row = _issue(store, fallback)
    assert row is not None
    assert row["times_seen"] == 1
    assert fallback != _key(compute_fingerprint(ERR))  # distinct from fp issue
    assert store.conn.execute(
        "SELECT issue_key FROM bundles WHERE bundle_id='bn'"
    ).fetchone()[0] == fallback


def test_no_origin_writes_no_issue(store):
    # Degenerate payload with empty origin: bundle recorded, no issue row
    # (issues.origin is NOT NULL).
    store.upsert_run_bundle({
        "run_id": "r1", "profile": "p", "ts_utc": "2026-07-25T00:00:00Z",
        "bundle_id": "b0", "origin": "", "flavor": "", "result": "failure",
        "target": TARGET, "errors_text": ERR,
    })
    assert store.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    assert store.conn.execute(
        "SELECT issue_key FROM bundles WHERE bundle_id='b0'"
    ).fetchone()[0] is None


def test_compact_hook_timestamp_normalized_to_iso(store):
    """The dsynth hooks stamp the compact ``YYYYmmdd-HHMMSSZ`` form; the
    writer canonicalizes ``ts_utc`` to ISO so it is directly comparable
    with the issue's ISO ``*_at`` timestamps (and parseable by
    ``relative_age``). Storing it raw made ``ts_utc > resolved_at`` on
    unmute a lexicographic no-op — this pins the fix at the boundary."""
    iso = "2026-07-26T08:00:00+00:00"
    _up(store, "b1", errors="cc: error: boom\n", ts="20260726-080000Z")
    assert store.conn.execute(
        "SELECT ts_utc FROM bundles WHERE bundle_id='b1'"
    ).fetchone()["ts_utc"] == iso
    # first/last_seen derive from ts_utc, so they land ISO too.
    issue = _issue(store, _key(compute_fingerprint("cc: error: boom\n")))
    assert (issue["first_seen_at"], issue["last_seen_at"]) == (iso, iso)
    # An already-ISO stamp (tests, other clients) passes through unchanged.
    _up(store, "b2", errors="cc: error: boom\n", ts="2026-07-26T09:00:00+00:00")
    assert store.conn.execute(
        "SELECT ts_utc FROM bundles WHERE bundle_id='b2'"
    ).fetchone()["ts_utc"] == "2026-07-26T09:00:00+00:00"
