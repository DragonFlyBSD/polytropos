"""Ids that cannot be URLs are refused at the door (poly-13ku).

A bundle, run or job id is a URL path segment. ``request.url_for``
refuses a value with a path separator outright, and it does so while
COMPOSING the page -- so one such stored row does not break its own row,
it breaks every page that links to it. Measured on one bundle: the Builds
dashboard, the occurrences list, and any issue, job or run page that
mentions it. Most of the repair workspace, from one row, and not
repairable from the UI because the pages that would let an operator find
it are among the ones that fail.

Every writer that ships already sanitises -- the dsynth hooks through
sanitize_component, the runner through origin.replace("/", "_"),
issue_key through a hexdigest -- so this guards what is left: the
artifact_store CLI, which takes --bundle-id verbatim, an ingest port with
no authentication, and any future change to those four.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from dportsv3.artifact_store import ArtifactStore
from dportsv3.db.identifiers import (
    is_safe_identifier,
    repair_unsafe_identifiers,
    require_identifier,
    stored_unsafe_identifiers,
)
from dportsv3.db.schema import init_db


@pytest.fixture
def store() -> ArtifactStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    st = ArtifactStore.__new__(ArtifactStore)
    st.conn = conn
    st._lock = threading.Lock()
    return st


def _payload(**over):
    row = {"run_id": "run-1", "bundle_id": "devel_tiff-20260901-120000Z",
           "origin": "devel/tiff", "ts_utc": "2026-09-01T12:00:00Z",
           "result": "failure", "target": "@main"}
    row.update(over)
    return row


# --- the rule -------------------------------------------------------------


@pytest.mark.parametrize("value", [
    # Exactly what the shipped minters produce.
    "devel_tiff-20260901-120000Z",          # hook_pkg_failure
    "devel_tiff@lite-20260901-120000Z",     # ...with a flavor
    "run-2026Q3-20260901-120000Z-4821",     # hook_run_start
    "20260901-120000Z-main-devel_tiff-91",  # runner.enqueue
    "9f2a17c4b0e83d51",                     # issue_key, a hexdigest
])
def test_what_the_minters_make_is_accepted(value) -> None:
    assert is_safe_identifier(value)


@pytest.mark.parametrize("value", [
    "devel/tiff", "a\\b", "a?b", "a#b", "a%b", "a b", "a\nb", "", None, 7,
])
def test_what_cannot_be_a_url_is_refused(value) -> None:
    assert not is_safe_identifier(value)


def test_the_error_names_the_character(store) -> None:
    """The caller sent it; telling them which byte is the whole job of
    the message."""
    with pytest.raises(ValueError, match="path separator"):
        require_identifier("bundle_id", "devel/tiff")
    with pytest.raises(ValueError, match="a space"):
        require_identifier("bundle_id", "devel tiff")


def test_an_empty_id_says_it_is_required(store) -> None:
    with pytest.raises(ValueError, match="required"):
        require_identifier("bundle_id", "")


# --- the door -------------------------------------------------------------


def test_the_store_accepts_what_the_hook_sends(store) -> None:
    store.upsert_run_bundle(_payload())

    row = store.conn.execute("SELECT bundle_id FROM bundles").fetchone()
    assert row["bundle_id"] == "devel_tiff-20260901-120000Z"


def test_the_store_refuses_a_bundle_id_that_is_not_a_url(store) -> None:
    with pytest.raises(ValueError, match="path separator"):
        store.upsert_run_bundle(_payload(bundle_id="devel/tiff"))

    count = store.conn.execute("SELECT COUNT(*) FROM bundles").fetchone()
    assert count[0] == 0


def test_the_store_refuses_a_run_id_that_is_not_a_url(store) -> None:
    with pytest.raises(ValueError, match="run_id"):
        store.upsert_run_bundle(_payload(run_id="run/1"))


def test_a_bundle_with_no_run_is_still_allowed(store) -> None:
    """run_id is nullable -- an occurrence can arrive without one."""
    store.upsert_run_bundle(_payload(run_id=None))

    count = store.conn.execute("SELECT COUNT(*) FROM bundles").fetchone()
    assert count[0] == 1


def test_put_blob_is_guarded_too(store, tmp_path) -> None:
    """The artifact_store CLI takes --bundle-id verbatim, and it can
    create the row it uploads against."""
    with pytest.raises(ValueError, match="path separator"):
        store.put_blob("devel/tiff", "logs/x.log", b"data", None)


# --- the HTTP door --------------------------------------------------------


def test_ingest_answers_400_and_not_500(tmp_path, set_setting) -> None:
    """The caller sent bad input. A 500 would say the tracker broke."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dportsv3.tracker.server import create_app

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    set_setting("paths.artifact_root", str(evidence))
    path = evidence / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.close()

    with TestClient(create_app(path)) as client:
        resp = client.post("/v1/bundles/upsert", json=_payload(
            bundle_id="devel/tiff"))

    assert resp.status_code == 400
    assert "path separator" in resp.text


# --- what is already stored -----------------------------------------------


def test_a_legacy_row_is_found_rather_than_left_invisible() -> None:
    """The guard stops new ones. A DB written before it can hold them,
    and they are invisible until a page tries to link to one -- at which
    point the page that would let you find them is among the ones that
    fail."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    # Written the way an older build could: straight into the table.
    conn.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target) "
        "VALUES ('devel/tiff', 'devel/tiff', 't1', 'failure', '@main')")
    conn.execute(
        "INSERT INTO jobs(job_id, origin, state, created_ts_utc) "
        "VALUES ('bad/job', 'devel/tiff', 'queued', 't1')")
    conn.commit()

    found = stored_unsafe_identifiers(conn)

    assert ("bundles", "bundle_id", "devel/tiff") in found
    assert ("jobs", "job_id", "bad/job") in found


def test_a_clean_database_reports_nothing() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target) "
        "VALUES ('devel_tiff-1', 'devel/tiff', 't1', 'failure', '@main')")
    conn.commit()

    assert stored_unsafe_identifiers(conn) == []


def test_the_check_survives_a_database_that_predates_a_table() -> None:
    """Startup must not die on it."""
    conn = sqlite3.connect(":memory:")

    assert stored_unsafe_identifiers(conn) == []


def test_startup_names_a_legacy_row(tmp_path, caplog) -> None:
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dportsv3.tracker.server import create_app

    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target) "
        "VALUES ('devel/tiff', 'devel/tiff', 't1', 'failure', '@main')")
    db.commit()
    db.close()

    import logging
    with caplog.at_level(logging.ERROR):
        with TestClient(create_app(path)):
            pass

    assert any("cannot be a URL path segment" in r.message
               for r in caplog.records)
    assert any("UPDATE bundles SET" in r.getMessage() for r in caplog.records)


# --- the repair -----------------------------------------------------------


def _bricked_db(path):
    """A tracker holding one row an older build could have written."""
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    bad = "bnd/with/slash"
    db.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, "
        "total_expected) VALUES (1, '@main', 'release', 't0', 5)")
    db.execute(
        "INSERT INTO runs(run_id, target, build_run_id) "
        "VALUES ('r1', '@main', 1)")
    db.execute(
        "INSERT INTO build_results(build_run_id, origin, version, result, "
        "recorded_at, status) VALUES (1, 'devel/x', '1.0', 'failure', 't1', "
        "'recorded')")
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "first_seen_at, last_seen_at, updated_at) VALUES ('i1', '@main', "
        "'devel/x', 'unresolved', 1, 't0', 't1', 't1')")
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
        "target, issue_key) VALUES (?, 'r1', 'devel/x', 't1', 'failure', "
        "'@main', 'i1')", (bad,))
    db.execute(
        "INSERT INTO jobs(job_id, bundle_id, origin, state, created_ts_utc, "
        "target, type) VALUES ('j1', ?, 'devel/x', 'queued', 't1', '@main', "
        "'triage')", (bad,))
    db.execute(
        "INSERT INTO activity_log(ts, bundle_id, stage, message) "
        "VALUES ('t1', ?, 'triage', 'started')", (bad,))
    db.commit()
    db.close()
    return bad


#: The pages the bead measured, plus the pipeline drawer, which is newer
#: than the bead and fails the same way.
BRICKED = ["/", "/agentic/bundles", "/agentic/issues/i1",
           "/agentic/jobs/j1", "/agentic/runs/r1",
           "/pipeline?stage=failures"]


def _failing(path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dportsv3.tracker.server import create_app

    import logging
    logging.disable(logging.CRITICAL)
    try:
        with TestClient(create_app(path),
                        raise_server_exceptions=False) as client:
            return [p for p in BRICKED if client.get(p).status_code >= 500]
    finally:
        logging.disable(logging.NOTSET)


def test_one_legacy_row_breaks_every_page_that_links_to_it(tmp_path) -> None:
    """Not its own row -- url_for raises while composing, so the failure
    is the whole page. This is the condition the guard exists for, and it
    is worth pinning so the repair below has something to prove."""
    path = tmp_path / "bricked.db"
    _bricked_db(path)

    assert _failing(path) == BRICKED


def test_the_repair_gives_those_pages_back(tmp_path) -> None:
    path = tmp_path / "bricked.db"
    _bricked_db(path)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    plan = repair_unsafe_identifiers(conn, dry_run=False)
    conn.close()

    assert plan[0]["old"] == "bnd/with/slash"
    assert plan[0]["new"] == "bnd_with_slash"
    assert _failing(path) == []


def test_the_repair_carries_every_reference(tmp_path) -> None:
    """A rename that left jobs or the activity log pointing at the old id
    would trade a broken page for an orphaned row."""
    path = tmp_path / "bricked.db"
    _bricked_db(path)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    repair_unsafe_identifiers(conn, dry_run=False)
    rows = {
        "bundles": conn.execute(
            "SELECT bundle_id FROM bundles").fetchone()[0],
        "jobs": conn.execute("SELECT bundle_id FROM jobs").fetchone()[0],
        "activity": conn.execute(
            "SELECT bundle_id FROM activity_log").fetchone()[0],
    }
    conn.close()

    assert set(rows.values()) == {"bnd_with_slash"}


def test_a_dry_run_writes_nothing(tmp_path) -> None:
    """Renaming an id touches up to nine tables. That is a decision
    somebody takes having read what it would do."""
    path = tmp_path / "bricked.db"
    _bricked_db(path)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    plan = repair_unsafe_identifiers(conn)          # dry by default
    still = conn.execute("SELECT bundle_id FROM bundles").fetchone()[0]
    conn.close()

    assert plan[0]["updated"]                       # says what it would do
    assert still == "bnd/with/slash"                # ...and did not


def test_a_collision_is_refused_rather_than_merged(tmp_path) -> None:
    """Two bad ids can sanitize to one. Silently merging them would fuse
    two occurrences of two different failures."""
    path = tmp_path / "collide.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target) "
        "VALUES ('a_b', 'devel/x', 't1', 'failure', '@main')")
    db.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target) "
        "VALUES ('a/b', 'devel/y', 't1', 'failure', '@main')")
    db.commit()
    db.close()

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    plan = repair_unsafe_identifiers(conn, dry_run=False)
    survived = {r[0] for r in conn.execute("SELECT bundle_id FROM bundles")}
    conn.close()

    assert plan[0]["collision"] is True
    assert survived == {"a_b", "a/b"}


def test_the_repaired_id_looks_like_one_the_hook_would_make() -> None:
    """Same rule the dsynth hooks use, so a repaired id reads like every
    other id rather than like a repair."""
    from dportsv3.db.identifiers import safe_form

    assert safe_form("devel/tiff-20260901Z") == "devel_tiff-20260901Z"
    assert safe_form("a b?c") == "abc"
    assert safe_form("///") == "___"
