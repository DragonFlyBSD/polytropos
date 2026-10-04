"""Operator context belongs to the build line it was written for (poly-7pwa.10).

``has_fresh_user_context`` is not a display flag: it promotes a MANUAL
classification into an automatic patch attempt, and it forgives the
per-target patch cap. Both were keyed on "is there recent context for this
origin" across every target, while the failure count they act on is
per-target -- so guidance written against one build line launched attempts
on another.

An env builds one target, so nothing downstream could notice.

Since poly-7pwa.17 the lookup is keyed on the job's own run, the key the
prompt reads the operator's text with. A run is one dsynth invocation of
one profile, so it has one target, and every case below still holds: each
job reads from the run of its own build line.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from dportsv3.agent.decision import PortHistory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


@pytest.fixture
def db():
    """The tables PortHistory.load reads, with runs carrying the target."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, target TEXT);
        CREATE TABLE user_context (
            run_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            context_text TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, origin)
        );
        CREATE TABLE bundles (
            bundle_id TEXT PRIMARY KEY,
            origin TEXT, target TEXT, result TEXT, last_seen_at TEXT,
            error_signature TEXT
        );
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            origin TEXT, target TEXT, type TEXT, retire_reason TEXT,
            last_transition_at TEXT, last_seen_at TEXT
        );
        """
    )
    yield conn
    conn.close()


def _add_context(conn, run_id, target, origin, when):
    conn.execute("INSERT OR REPLACE INTO runs (run_id, target) VALUES (?, ?)",
                 (run_id, target))
    conn.execute(
        "INSERT INTO user_context (run_id, origin, context_text, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (run_id, origin, "do the thing", when),
    )
    conn.commit()


def _load(conn, target, origin="foo/bar", run_id=None):
    return PortHistory.load(conn, target, origin, window_hours=24,
                            run_id=run_id)


def test_context_for_this_target_counts(db):
    _add_context(db, "r-main", "@main", "foo/bar", _now())
    assert _load(db, "@main", run_id="r-main").has_fresh_user_context is True


def test_context_for_another_target_does_not_count(db):
    """The defect. Guidance for @2026Q3 must not act on @main."""
    _add_context(db, "r-q3", "@2026Q3", "foo/bar", _now())
    assert _load(db, "@main", run_id="r-main").has_fresh_user_context is False


def test_the_newest_row_does_not_leak_across_targets(db):
    """ORDER BY updated_at DESC LIMIT 1 picked the newest row overall.

    Older context on the target being built, newer context on the other:
    the old query returned the other one's timestamp and reported fresh.
    """
    _add_context(db, "r-main", "@main", "foo/bar", _hours_ago(3))
    _add_context(db, "r-q3", "@2026Q3", "foo/bar", _now())
    assert _load(db, "@main", run_id="r-main").has_fresh_user_context is True
    assert _load(db, "@2026Q3", run_id="r-q3").has_fresh_user_context is True
    # And a third build line, with context on neither, stays untouched.
    assert _load(db, "@2026Q2", run_id="r-q2").has_fresh_user_context is False


def test_context_for_another_origin_still_does_not_count(db):
    _add_context(db, "r-main", "@main", "other/port", _now())
    assert _load(db, "@main", run_id="r-main").has_fresh_user_context is False


def test_a_row_from_another_run_does_not_count_even_for_an_untargeted_job(db):
    """The key is the job's run, not the target. Under poly-7pwa.10 a
    row whose run was unknown counted for a caller asking for no target;
    it is not that caller's run, so its prompt never carries the text."""
    db.execute(
        "INSERT INTO user_context (run_id, origin, context_text, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("r-orphan", "foo/bar", "ctx", _now()),
    )
    db.commit()
    assert _load(db, "@main", run_id="r-main").has_fresh_user_context is False
    assert _load(db, "", run_id="r-main").has_fresh_user_context is False


def test_a_missing_user_context_table_degrades_to_no_context(db):
    """Per-query try/except: degrade toward NOT promoting, never toward it."""
    _add_context(db, "r-main", "@main", "foo/bar", _now())
    # Without the row above this passes for the trivial reason that the
    # query finds nothing, whatever the code does.
    db.execute("DROP TABLE user_context")
    db.commit()
    assert _load(db, "@main", run_id="r-main").has_fresh_user_context is False


@pytest.mark.parametrize("run_id", [None, ""])
def test_a_job_with_no_run_has_no_context(db, run_id):
    """No run, no row: the prompt reads nothing either."""
    _add_context(db, "r-main", "@main", "foo/bar", _now())
    assert _load(db, "@main", run_id=run_id).has_fresh_user_context is False


def _add_failed_patch(conn, origin, target, when):
    conn.execute(
        "INSERT INTO jobs (job_id, origin, target, type, retire_reason, "
        "last_transition_at) VALUES (?, ?, ?, 'patch', 'patch_gave_up', ?)",
        (f"j-{target}-{when}", origin, target, when),
    )
    conn.commit()


def test_a_failed_attempt_on_this_target_is_counted(db):
    """Guards the fixture: without a jobs table this whole branch is dead.

    failed_patch_attempts raising meant last_failed_patch_at was always
    empty, which short-circuits the freshness comparison to True -- so the
    cross-target tests above were passing through a path that never
    reached the timestamp logic at all.
    """
    _add_failed_patch(db, "foo/bar", "@main", _hours_ago(3))
    assert _load(db, "@main").failed_patch_attempts == 1
    assert _load(db, "@2026Q3").failed_patch_attempts == 0


def test_the_patch_cap_is_not_forgiven_by_the_other_build_line(db):
    """The bead's sharper effect: the count is per-target, the pardon was not.

    A patch attempt gave up on @main 3h ago. The operator's @main context
    is OLDER than that failure, so it is not fresh -- but newer context
    exists on @2026Q3. The old query took the newest row across targets,
    called it fresh, and handed @main another attempt.
    """
    _add_failed_patch(db, "foo/bar", "@main", _hours_ago(3))
    _add_context(db, "r-main", "@main", "foo/bar", _hours_ago(6))
    _add_context(db, "r-q3", "@2026Q3", "foo/bar", _hours_ago(1))

    on_main = _load(db, "@main", run_id="r-main")
    assert on_main.failed_patch_attempts == 1
    assert on_main.has_fresh_user_context is False

    # And @2026Q3's own context IS fresh there -- it has no failure at all.
    assert _load(db, "@2026Q3", run_id="r-q3").has_fresh_user_context is True


def test_context_newer_than_this_target_s_failure_is_still_fresh(db):
    """The effect must keep working where it is meant to."""
    _add_failed_patch(db, "foo/bar", "@main", _hours_ago(3))
    _add_context(db, "r-main", "@main", "foo/bar", _now())
    assert _load(db, "@main", run_id="r-main").has_fresh_user_context is True
