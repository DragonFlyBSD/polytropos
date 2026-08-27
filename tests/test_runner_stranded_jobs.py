"""Claiming work whose .job file this runner cannot see (poly-b2r).

A hook writes its job file into the queue root *it* can see. Inside a
dev-env chroot that is /work/dsynth/logs/evidence/queue, which the host
runner cannot resolve, so the file lands where nothing reads it and the
failure sits queued forever — measured on x6, three real failures, host
queue empty.

Everything else already crossed the boundary over HTTP: the jobs row
arrived, the bundle reached the store with its artifacts. Only locating
the work depended on a shared filesystem.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dportsv3.agent import runner as rm
from dportsv3.db.schema import init_db


@pytest.fixture()
def queue_root(tmp_path: Path) -> Path:
    root = tmp_path / "queue"
    for sub in ("pending", "inflight", "done", "failed"):
        (root / sub).mkdir(parents=True)
    return root


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    connection = sqlite3.connect(str(tmp_path / "state.db"))
    connection.row_factory = sqlite3.Row
    init_db(connection)
    monkeypatch.setattr(rm, "_state_db_conn", connection)
    yield connection
    connection.close()


def _seed(conn: sqlite3.Connection, *, job_id: str, bundle_id: str,
          origin: str = "devel/glib20", flavor: str = "",
          run_id: str = "run-1", profile: str = "2026Q3",
          state: str = "queued", job_type: str = "triage",
          created: str = "2026-08-27T10:00:00Z") -> None:
    """A hook-created job exactly as the HTTP half leaves it: rows in the
    DB, and jobs.path naming a queue root on the producer's side."""
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, profile, target) VALUES (?, ?, '@2026Q3')",
        (run_id, profile))
    conn.execute(
        "INSERT OR IGNORE INTO bundles (bundle_id, run_id, origin, flavor, target)"
        " VALUES (?, ?, ?, ?, '@2026Q3')",
        (bundle_id, run_id, origin, flavor))
    conn.execute(
        """INSERT INTO jobs (job_id, state, type, origin, flavor,
                             created_ts_utc, target, bundle_id, path)
           VALUES (?, ?, ?, ?, ?, ?, '@2026Q3', ?, ?)""",
        (job_id, state, job_type, origin, flavor, created, bundle_id,
         f"/work/dsynth/logs/evidence/queue/pending/{job_id}"))
    conn.commit()


# --- the bug ---------------------------------------------------------------

def test_a_job_with_no_local_file_is_claimed(queue_root, conn) -> None:
    _seed(conn, job_id="j1.job", bundle_id="b1")
    assert not list((queue_root / "pending").iterdir()), "precondition: empty queue"

    batch = rm.claim_next_job_batch(queue_root)
    assert batch is not None, "the stranded job was not picked up"
    lead, siblings = batch
    assert lead.name == "j1.job"
    assert lead.parent == queue_root / "inflight"
    assert siblings == []


def test_the_claim_moves_the_row_out_of_queued(queue_root, conn) -> None:
    _seed(conn, job_id="j1.job", bundle_id="b1")
    rm.claim_next_job_batch(queue_root)
    state = conn.execute("SELECT state FROM jobs WHERE job_id = 'j1.job'").fetchone()[0]
    assert state != "queued"


def test_the_materialized_job_carries_what_the_worker_needs(queue_root, conn) -> None:
    _seed(conn, job_id="j1.job", bundle_id="b1", origin="devel/glib20",
          run_id="run-7", profile="2026Q3-editors_vim")
    lead, _ = rm.claim_next_job_batch(queue_root)
    meta = rm.parse_job_file(lead)

    assert meta["type"] == "triage"
    assert meta["origin"] == "devel/glib20"
    assert meta["bundle_id"] == "b1"
    assert meta["target"] == "@2026Q3"
    assert meta["run_id"] == "run-7"
    assert meta["profile"] == "2026Q3-editors_vim"     # joined via bundles -> runs
    assert meta["snippet_round"] == "0"
    assert meta["has_snippets"] == "false"


def test_the_producers_path_is_not_carried_over(queue_root, conn) -> None:
    """jobs.path is the queue path the *hook* saw. That is the chroot
    path this whole change exists to stop trusting, and an absent
    bundle_dir is what makes the worker materialize over HTTP."""
    _seed(conn, job_id="j1.job", bundle_id="b1")
    lead, _ = rm.claim_next_job_batch(queue_root)
    meta = rm.parse_job_file(lead)
    assert "path" not in meta
    assert "bundle_dir" not in meta
    assert "/work/dsynth" not in lead.read_text()


# --- what it must not disturb ----------------------------------------------

def test_a_job_the_file_queue_already_has_is_left_alone(queue_root, conn) -> None:
    """On one host the hook writes both the file and the row. Claiming
    from the DB as well would process every failure twice."""
    _seed(conn, job_id="j1.job", bundle_id="b1")
    (queue_root / "pending" / "j1.job").write_text(
        "type=triage\norigin=devel/glib20\nprofile=2026Q3\nbundle_id=b1\n")

    lead, _ = rm.claim_next_job_batch(queue_root)
    assert lead.parent == queue_root / "inflight"
    assert not (queue_root / "pending" / "j1.job").exists(), "file path should win"
    # And the DB path must not have produced a second copy.
    assert len(list((queue_root / "inflight").iterdir())) == 1


def test_an_inflight_file_is_not_reclaimed(queue_root, conn) -> None:
    """Mid-race, or after a transition write failed: the row can still
    read 'queued' while the file is already being worked."""
    _seed(conn, job_id="j1.job", bundle_id="b1")
    (queue_root / "inflight" / "j1.job").write_text("type=triage\n")
    assert rm.claim_next_job_batch(queue_root) is None


def test_non_triage_jobs_are_not_derived(queue_root, conn) -> None:
    """patch/verify/confirm are created by the runner on this host and
    carry ~14 fields the jobs table does not model. Deriving one from
    the DB would silently drop them."""
    for jt in ("patch", "verify", "confirm"):
        _seed(conn, job_id=f"{jt}.job", bundle_id=f"b-{jt}", job_type=jt)
    assert rm.claim_next_job_batch(queue_root) is None


def test_a_job_already_out_of_queued_is_not_derived(queue_root, conn) -> None:
    for state in ("claimed", "inflight", "done", "failed"):
        _seed(conn, job_id=f"{state}.job", bundle_id=f"b-{state}", state=state)
    assert rm.claim_next_job_batch(queue_root) is None


def test_the_file_queue_still_wins_when_both_have_work(queue_root, conn) -> None:
    _seed(conn, job_id="from-db.job", bundle_id="b-db")
    (queue_root / "pending" / "from-file.job").write_text(
        "type=triage\norigin=devel/x\nprofile=2026Q3\nbundle_id=b-file\n")
    lead, _ = rm.claim_next_job_batch(queue_root)
    assert lead.name == "from-file.job"


# --- batching and races ----------------------------------------------------

def test_siblings_are_claimed_together(queue_root, conn) -> None:
    """Same (type, profile, origin, flavor) — one port failing in several
    bundles is one triage, as it is on the file path."""
    for i in (1, 2, 3):
        _seed(conn, job_id=f"j{i}.job", bundle_id=f"b{i}",
              created=f"2026-08-27T10:0{i}:00Z")
    lead, siblings = rm.claim_next_job_batch(queue_root)
    assert lead.name == "j1.job"
    assert sorted(s.name for s in siblings) == ["j2.job", "j3.job"]

    meta = rm.parse_job_file(lead)
    assert meta["origin"] == "devel/glib20"
    states = dict(conn.execute("SELECT job_id, state FROM jobs").fetchall())
    assert all(s != "queued" for s in states.values()), states


def test_a_different_origin_is_not_a_sibling(queue_root, conn) -> None:
    _seed(conn, job_id="j1.job", bundle_id="b1", origin="devel/glib20")
    _seed(conn, job_id="j2.job", bundle_id="b2", origin="lang/rust",
          created="2026-08-27T10:05:00Z")
    lead, siblings = rm.claim_next_job_batch(queue_root)
    assert lead.name == "j1.job"
    assert siblings == []


def test_a_different_profile_is_not_a_sibling(queue_root, conn) -> None:
    """Two builders working the same port are not one triage."""
    _seed(conn, job_id="j1.job", bundle_id="b1", run_id="r1", profile="2026Q3")
    _seed(conn, job_id="j2.job", bundle_id="b2", run_id="r2", profile="2026Q4",
          created="2026-08-27T10:05:00Z")
    lead, siblings = rm.claim_next_job_batch(queue_root)
    assert lead.name == "j1.job"
    assert siblings == []


def test_two_runners_cannot_both_claim_the_same_job(queue_root, conn) -> None:
    """The claim is lifecycle.apply under BEGIN IMMEDIATE, so the state
    machine itself is the mutex: the second caller gets an illegal
    transition out of 'claimed' and moves on."""
    _seed(conn, job_id="j1.job", bundle_id="b1")

    first = rm.claim_next_job_batch(queue_root)
    assert first is not None
    (queue_root / "inflight" / "j1.job").unlink()   # the other runner's disk

    assert rm.claim_next_job_batch(queue_root) is None


def test_oldest_first(queue_root, conn) -> None:
    _seed(conn, job_id="late.job", bundle_id="b-late", origin="lang/rust",
          created="2026-08-27T12:00:00Z")
    _seed(conn, job_id="early.job", bundle_id="b-early", origin="devel/glib20",
          created="2026-08-27T08:00:00Z")
    lead, _ = rm.claim_next_job_batch(queue_root)
    assert lead.name == "early.job"


def test_nothing_anywhere_returns_none(queue_root, conn) -> None:
    assert rm.claim_next_job_batch(queue_root) is None


def test_a_job_whose_bundle_never_arrived_is_skipped(queue_root, conn) -> None:
    """The join is inner on bundles: without the bundle there is nothing
    to triage, and the runner would have no artifacts to materialize."""
    conn.execute(
        """INSERT INTO jobs (job_id, state, type, origin, created_ts_utc, bundle_id)
           VALUES ('orphan.job', 'queued', 'triage', 'devel/x',
                   '2026-08-27T10:00:00Z', 'b-missing')""")
    conn.commit()
    assert rm.claim_next_job_batch(queue_root) is None


def test_a_job_with_no_run_row_is_left_queued(queue_root, conn, tmp_path) -> None:
    """profile joins through runs and _job_dedup_key needs it. Claiming
    without one would hand the worker a job missing a field the hook
    always writes; leave it queued and say so instead."""
    conn.execute(
        "INSERT INTO bundles (bundle_id, run_id, origin, target)"
        " VALUES ('b1', 'run-missing', 'devel/x', '@2026Q3')")
    conn.execute(
        """INSERT INTO jobs (job_id, state, type, origin, created_ts_utc, bundle_id)
           VALUES ('j1.job', 'queued', 'triage', 'devel/x',
                   '2026-08-27T10:00:00Z', 'b1')""")
    conn.commit()

    assert rm.claim_next_job_batch(queue_root) is None
    state = conn.execute("SELECT state FROM jobs WHERE job_id='j1.job'").fetchone()[0]
    assert state == "queued"
    assert "no runs.profile" in (queue_root / "runner.log").read_text()
