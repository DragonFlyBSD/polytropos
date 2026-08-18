"""Build-confirmed resolution (C1/C2 + A1-A4).

`resolved` means the fix was BUILT and proven, not that a PR merged. This pins
the whole loop:

- C1: the `resolving` transition records desired build state as a monotonic
  generation counter;
- C2: a level-triggered reconcile re-derives what needs a build, claims it
  single-flight, and is the sole queue writer;
- A1/A2/A3: a produced verdict advances the counter and moves the issue —
  green to `resolved` with a Green-Head watermark, red back to `unresolved`;
- A4: a single green is provisional; N consecutive greens confirm;
- the defect fixes: stale/out-of-order verdicts, the green tally leaking past
  the state guard, a failed verdict write reported as success, duplicate
  builds across a restart, and unbounded could-not-run retries.

The build itself is never executed here — `_record_confirm_verdict` is fed a
green/red directly, exactly as the real dispatch does once dsynth returns.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from dportsv3.agent import runner
from dportsv3.db.schema import init_db
from dportsv3.tracker import delivery_sync
from dportsv3.tracker.agentic_queries import issues_needing_build
from dportsv3.tracker.routes import issue_actions

NOW = "2026-08-18T00:00:00Z"
TARGET = "@main"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "state.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_db(c)
    # The runner talks to state.db through module globals.
    monkeypatch.setattr(runner, "_state_db_conn", c)
    monkeypatch.setattr(runner, "_state_db_lock", threading.Lock())
    yield c
    c.close()


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "queue"
    (q / "pending").mkdir(parents=True)
    return q


def add_issue(conn, key="k1", *, state="unresolved", origin="x/y",
              bundle="b1", requested=0, confirmed=0, building=None,
              greens=0, failures=0, with_bundle=True):
    conn.execute(
        "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
        "delivery_bundle_id, requested_build_generation, "
        "last_confirmed_build_generation, building_generation, "
        "confirm_green_count, confirm_failure_count, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, TARGET, origin, f"fp-{key}", state,
         bundle if state == "resolving" else None,
         requested, confirmed, building, greens, failures, NOW),
    )
    if with_bundle:
        conn.execute(
            "INSERT INTO bundles(bundle_id, origin, target, issue_key, "
            "ts_utc, result, resolution, verification_status) "
            "VALUES(?,?,?,?,?,'failure','accepted','verified')",
            (bundle, origin, TARGET, key, NOW),
        )
    conn.commit()


def add_build_run(conn, run_id: int, target: str = TARGET) -> None:
    # finished_at is set: `uq_build_runs_active` allows only ONE unfinished run
    # per (target, build_type), and these are historical farm builds anyway —
    # the Green-Head watermark is taken from the newest ordinal.
    conn.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, "
        "finished_at) VALUES(?,?, 'test', ?, ?)", (run_id, target, NOW, NOW),
    )
    conn.commit()


def row(conn, key="k1"):
    return conn.execute(
        "SELECT * FROM issues WHERE issue_key = ?", (key,)).fetchone()


def confirm_jobs(queue: Path) -> list[str]:
    return sorted(p.name for p in (queue / "pending").glob("*confirm.job"))


# --- C1: build intent ------------------------------------------------------


def test_accept_records_build_intent(conn):
    """The `resolving` transition bumps the desired-build generation in the
    same statement, so a crash leaves old-or-new, never a half-written intent."""
    add_issue(conn, state="unresolved", with_bundle=True)
    state = issue_actions.mark_issue_resolving(
        conn, "k1", bundle_id="b1", now=NOW, actor="operator")
    conn.commit()
    r = row(conn)
    assert state == "resolving"
    assert (r["state"], r["requested_build_generation"]) == ("resolving", 1)


def test_re_accept_bumps_generation_not_a_second_request(conn):
    """A counter self-dedups: re-accepting bumps it rather than queueing a
    second command."""
    add_issue(conn, state="unresolved")
    for _ in range(2):
        issue_actions.mark_issue_resolving(
            conn, "k1", bundle_id="b1", now=NOW, actor="operator")
    conn.commit()
    assert row(conn)["requested_build_generation"] == 2


def test_request_confirm_build_seam(conn):
    """The standalone operator-facing bump (the seam a future 'start build
    from tracker' endpoint reuses) leaves the issue state alone."""
    add_issue(conn, state="resolved")
    gen = issue_actions.request_confirm_build(
        conn, "k1", now=NOW, actor="operator")
    conn.commit()
    assert gen == 1
    assert row(conn)["state"] == "resolved"


# --- the reconcile feed ----------------------------------------------------


@pytest.mark.parametrize("state,requested,confirmed,building,expected", [
    ("resolving", 1, 0, None, True),    # wants a build
    ("resolving", 1, 1, None, False),   # already confirmed
    ("resolving", 1, 0, 1, False),      # in flight (single-flight)
    ("resolving", 2, 1, 1, True),       # newer intent than the in-flight build
    ("unresolved", 1, 0, None, False),  # not in the delivery path
    ("muted", 1, 0, None, False),
    ("resolved", 1, 0, None, False),
])
def test_feed_predicate(conn, state, requested, confirmed, building, expected):
    add_issue(conn, state=state, requested=requested, confirmed=confirmed,
              building=building)
    keys = [i["issue_key"] for i in issues_needing_build(conn)]
    assert (keys == ["k1"]) is expected


# --- C2: reconcile ---------------------------------------------------------


def test_reconcile_enqueues_once_and_claims(conn, queue):
    add_issue(conn, state="resolving", requested=1)
    runner.process_build_requests(queue)
    jobs = confirm_jobs(queue)
    assert len(jobs) == 1
    body = (queue / "pending" / jobs[0]).read_text()
    assert "type=confirm" in body
    assert "issue_key=k1" in body
    assert "requested_build_generation=1" in body
    assert row(conn)["building_generation"] == 1


def test_reconcile_is_single_flight(conn, queue):
    """A second pass while the build is in flight must not enqueue again."""
    add_issue(conn, state="resolving", requested=1)
    runner.process_build_requests(queue)
    runner.process_build_requests(queue)
    assert len(confirm_jobs(queue)) == 1


def test_reconcile_skips_issue_without_a_deliverable(conn, queue):
    add_issue(conn, state="resolving", requested=1, with_bundle=False)
    conn.execute("UPDATE issues SET delivery_bundle_id = NULL")
    conn.commit()
    runner.process_build_requests(queue)
    assert confirm_jobs(queue) == []


def test_failed_claim_never_enqueues(conn, queue, monkeypatch):
    """i8n: the claim gates the enqueue. A DB write that fails (lock
    contention) must not leave an unmarked issue for the next pass to
    duplicate."""
    add_issue(conn, state="resolving", requested=1)
    monkeypatch.setattr(runner, "_claim_issue_build",
                        lambda *a, **k: False)
    runner.process_build_requests(queue)
    assert confirm_jobs(queue) == []


def test_enqueue_failure_releases_the_claim(conn, queue, monkeypatch):
    """So the level-triggered feed re-derives instead of parking the issue."""
    add_issue(conn, state="resolving", requested=1)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "enqueue_confirm_build_job", boom)
    runner.process_build_requests(queue)
    assert row(conn)["building_generation"] is None
    assert [i["issue_key"] for i in issues_needing_build(conn)] == ["k1"]


def test_restart_does_not_duplicate_a_queued_job(conn, queue):
    """i8n: clearing markers alone left the queued job claimable AND let the
    feed enqueue a fresh one — two builds for one request."""
    add_issue(conn, state="resolving", requested=1)
    runner.process_build_requests(queue)
    assert len(confirm_jobs(queue)) == 1

    runner._reset_building_markers(queue)          # runner restart
    assert confirm_jobs(queue) == []               # queued job discarded
    assert row(conn)["building_generation"] is None

    runner.process_build_requests(queue)           # feed re-derives
    assert len(confirm_jobs(queue)) == 1           # exactly one, not two


# --- A2: green resolves ----------------------------------------------------


def test_green_resolves_with_green_head(conn):
    add_build_run(conn, 41)
    add_build_run(conn, 42)
    add_issue(conn, state="resolving", requested=1, building=1, greens=1)
    out = runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    r = row(conn)
    assert out == "recorded"
    assert r["state"] == "resolved"
    assert r["resolved_at"]
    assert r["green_head_run_id"] == 42        # newest ordinal = the boundary
    assert r["last_confirmed_build_generation"] == 1
    assert r["building_generation"] is None
    assert issues_needing_build(conn) == []


@pytest.mark.parametrize("state", ["muted", "unresolved", "resolved"])
def test_green_does_not_override_a_non_resolving_issue(conn, state):
    add_issue(conn, state=state, requested=1, building=1)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    r = row(conn)
    assert r["state"] == state                 # untouched
    # ...but the verdict is still recorded, so the loop does not re-enqueue.
    assert r["last_confirmed_build_generation"] == 1


def test_merge_no_longer_resolves_an_issue_awaiting_its_build(conn):
    """A2 moves the authority from the merge event to the build."""
    add_issue(conn, state="resolving", requested=1)
    out = delivery_sync.resolve_issue_for_bundle(
        conn, "b1", now_iso=NOW, source="merge")
    conn.commit()
    assert out is None
    assert row(conn)["state"] == "resolving"


def test_merge_still_resolves_an_issue_no_build_owns(conn):
    add_issue(conn, state="unresolved")
    delivery_sync.resolve_issue_for_bundle(
        conn, "b1", now_iso=NOW, source="merge")
    conn.commit()
    assert row(conn)["state"] == "resolved"


# --- A3: red reopens -------------------------------------------------------


def test_red_reopens_and_clears_delivery(conn):
    add_issue(conn, state="resolving", requested=1, building=1)
    conn.execute("UPDATE issues SET resolved_at = ?", (NOW,))
    conn.commit()
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=False, requested_by="test", target=TARGET,
        verdict_detail="dsynth stage failed (dsynth_exit=1)")
    r = row(conn)
    assert r["state"] == "unresolved"
    assert r["resolved_at"] is None
    assert r["delivery_bundle_id"] is None
    assert r["reopened_by"] == "runner-test"
    # generation advanced -> a persistently-red fix is not re-enqueued
    assert issues_needing_build(conn) == []


# --- A4: a single green is provisional -------------------------------------


def test_first_green_is_provisional_and_requests_another_build(conn):
    add_build_run(conn, 7)
    add_issue(conn, state="resolving", requested=1, building=1)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    r = row(conn)
    assert r["state"] == "resolving"           # NOT resolved yet
    assert r["confirm_green_count"] == 1
    # a second INDEPENDENT build is requested (distinct generation)
    assert r["requested_build_generation"] == 2
    assert [i["issue_key"] for i in issues_needing_build(conn)] == ["k1"]


def test_two_consecutive_greens_resolve(conn):
    add_build_run(conn, 7)
    add_issue(conn, state="resolving", requested=1, building=1)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    conn.execute("UPDATE issues SET building_generation = 2")
    conn.commit()
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 2, ok=True, requested_by="test", target=TARGET)
    r = row(conn)
    assert r["state"] == "resolved"
    assert r["confirm_green_count"] == 0       # reset for the next cycle
    assert r["green_head_run_id"] == 7


def test_red_after_a_provisional_green_reopens_and_resets(conn):
    """Confirmation must be CONSECUTIVE: green/red never resolves."""
    add_issue(conn, state="resolving", requested=1, building=1)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    conn.execute("UPDATE issues SET building_generation = 2")
    conn.commit()
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 2, ok=False, requested_by="test", target=TARGET)
    r = row(conn)
    assert r["state"] == "unresolved"
    assert r["confirm_green_count"] == 0


def test_threshold_of_one_resolves_on_the_first_green(conn, monkeypatch):
    monkeypatch.setenv("DP_CONFIRM_GREEN_THRESHOLD", "1")
    add_build_run(conn, 3)
    add_issue(conn, state="resolving", requested=1, building=1)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    assert row(conn)["state"] == "resolved"


def test_green_tally_does_not_leak_past_the_state_guard(conn):
    """The threshold was silently defeatable: the tally was bumped
    unconditionally but reset only on a successful resolve, so a green
    returning after an operator reopened the issue left the count parked —
    and a later accept resolved on its FIRST green."""
    add_issue(conn, state="resolving", requested=1, building=1, greens=1)
    conn.execute("UPDATE issues SET state = 'unresolved'")  # operator reopened
    conn.commit()
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    assert row(conn)["confirm_green_count"] == 0


# --- verdict bookkeeping defects -------------------------------------------


def test_stale_verdict_does_not_walk_the_counter_backwards(conn):
    """Two confirm jobs can exist for one issue and finish out of order."""
    add_issue(conn, state="resolving", requested=6, building=6)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 6, ok=False, requested_by="test", target=TARGET)
    assert row(conn)["last_confirmed_build_generation"] == 6

    out = runner._record_confirm_verdict(
        Path("/tmp"), "k1", 5, ok=False, requested_by="test", target=TARGET)
    assert out == "stale"
    assert row(conn)["last_confirmed_build_generation"] == 6


def test_malformed_generation_is_rejected(conn):
    """A job file without a usable generation parses to 0; it must not reset
    the issue's counter to 0."""
    add_issue(conn, state="resolving", requested=3, confirmed=2, building=3)
    out = runner._record_confirm_verdict(
        Path("/tmp"), "k1", 0, ok=True, requested_by="test", target=TARGET)
    assert out == "stale"
    assert row(conn)["last_confirmed_build_generation"] == 2


def test_late_verdict_does_not_clear_a_newer_builds_marker(conn):
    add_issue(conn, state="resolving", requested=7, confirmed=5, building=7)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 6, ok=True, requested_by="test", target=TARGET)
    assert row(conn)["building_generation"] == 7


def test_verdict_write_failure_reports_error_and_writes_nothing(conn):
    """A failed verdict write used to be reported as success, stranding the
    issue in `resolving` with no verdict while the job moved to done/."""
    add_issue(conn, state="resolving", requested=1, building=1)
    conn.execute("DROP TABLE events")           # non-sqlite3.Error path too
    conn.commit()
    out = runner._record_confirm_verdict(
        Path("/tmp"), "k1", 1, ok=True, requested_by="test", target=TARGET)
    assert out == "error"
    r = row(conn)
    assert r["state"] == "resolving"            # untouched
    assert r["last_confirmed_build_generation"] == 0
    # the connection must be left usable — no dangling transaction
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("ROLLBACK")


# --- yt7: could-not-run failures are bounded -------------------------------


def test_failed_build_retries_without_a_runner_restart(conn):
    add_issue(conn, state="resolving", requested=1, building=1)
    exhausted = runner._record_confirm_failure(
        Path("/tmp"), "k1", reason="no dev-env available")
    assert exhausted is False
    r = row(conn)
    assert r["building_generation"] is None      # marker released
    assert r["confirm_failure_count"] == 1
    assert [i["issue_key"] for i in issues_needing_build(conn)] == ["k1"]


def test_unbuildable_fix_gives_up_instead_of_parking_forever(conn):
    add_issue(conn, state="resolving", requested=1, building=1)
    for _ in range(2):
        assert runner._record_confirm_failure(
            Path("/tmp"), "k1", reason="empty changes.diff") is False
    assert runner._record_confirm_failure(
        Path("/tmp"), "k1", reason="empty changes.diff") is True
    r = row(conn)
    assert r["state"] == "unresolved"            # handed to a human
    assert r["confirm_failure_count"] == 0
    assert issues_needing_build(conn) == []


def test_a_produced_verdict_resets_the_failure_tally(conn):
    """Unrelated transient failures must not accumulate into a false give-up."""
    add_issue(conn, state="resolving", requested=2, confirmed=1, building=2,
              failures=2)
    runner._record_confirm_verdict(
        Path("/tmp"), "k1", 2, ok=False, requested_by="test", target=TARGET)
    assert row(conn)["confirm_failure_count"] == 0
