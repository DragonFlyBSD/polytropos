"""The build page's logfile link (poly-zjf).

dsynth-progress linked each row to ``../<origin>___<port>.log``, a file
sitting beside its own static HTML report. Lifted into the tracker that
resolves — through the page's ``<base href="/api/progress/build/1/">``
— to ``/api/progress/build/<x>.log``, which nothing serves. Every link
on the page 404'd, for failures and successes alike.

The log is a blob on the failure's evidence bundle now, so the link is
built from ``bundle_id``. Successes upload nothing at all, so they get
no link rather than a broken one.
"""

from __future__ import annotations

import gzip
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.artifact_store import ArtifactStore
from dportsv3.tracker.db import init_db
from dportsv3.tracker.server import create_app

BUILD_RUN = 1
LOG_BODY = b"===>  Building for jpeg-turbo-3.1.4.1\ncc: error: unknown argument\n"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def evidence_root(tmp_path: Path) -> Path:
    root = tmp_path / "logs" / "evidence"
    root.mkdir(parents=True)
    return root


@pytest.fixture()
def seeded(evidence_root: Path) -> Path:
    """One build run: a failure with a bundle, a failure whose evidence
    never landed, and a success. The shapes the UI has to tell apart."""
    db_path = evidence_root / "state.db"
    conn = init_db(db_path)
    now = _now()
    conn.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, total_expected)"
        " VALUES (?, '@2026Q3', 'test', ?, 3)", (BUILD_RUN, now))
    conn.execute(
        "INSERT INTO runs(run_id, profile, build_run_id, target)"
        " VALUES ('run-1', '2026Q3', ?, '@2026Q3')", (BUILD_RUN,))
    conn.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, target)"
        " VALUES ('bnd-jpeg', 'run-1', 'graphics/jpeg-turbo', ?, 'failure', '@2026Q3')",
        (now,))
    conn.executemany(
        """INSERT INTO build_results
           (build_run_id, origin, version, result, recorded_at, status)
           VALUES (?, ?, ?, ?, ?, 'recorded')""",
        [
            (BUILD_RUN, "graphics/jpeg-turbo", "3.1.4.1", "failure", now),
            (BUILD_RUN, "lang/rust", "1.96.1", "failure", now),        # no bundle
            (BUILD_RUN, "editors/vim", "9.2.0738", "success", now),
        ])
    conn.commit()
    conn.close()

    store = ArtifactStore.from_evidence_root(evidence_root)
    store.put_blob("bnd-jpeg", "logs/full.log.gz", gzip.compress(LOG_BODY), "gzip")
    store.conn.close()
    return db_path


@pytest.fixture()
def client(set_setting, seeded: Path, evidence_root: Path,
           monkeypatch: pytest.MonkeyPatch) -> TestClient:
    set_setting("paths.artifact_root", str(evidence_root))
    app = create_app(seeded)
    with TestClient(app) as test_client:
        yield test_client


def _entries(client: TestClient) -> dict[str, dict]:
    resp = client.get(f"/api/progress/build/{BUILD_RUN}/01_history.json")
    assert resp.status_code == 200, resp.text
    return {e["origin"]: e for e in resp.json()}


# --- what the payload carries ----------------------------------------------

def test_a_failure_with_evidence_carries_its_bundle(client: TestClient) -> None:
    assert _entries(client)["graphics/jpeg-turbo"]["bundle_id"] == "bnd-jpeg"


def test_a_success_carries_no_bundle(client: TestClient) -> None:
    """Nothing is uploaded for a successful build, so there is no log to
    point at and the UI must not offer one."""
    assert "bundle_id" not in _entries(client)["editors/vim"]


def test_a_failure_whose_evidence_never_landed_carries_no_bundle(
    client: TestClient,
) -> None:
    """The hook can fail to upload. Better no link than a dead one."""
    assert "bundle_id" not in _entries(client)["lang/rust"]


def test_the_entry_shape_is_dsynths_plus_what_the_tracker_knows(
    client: TestClient,
) -> None:
    """dsynth-progress' own field set, and the two fields the tracker adds
    because it has them and dsynth did not: the evidence bundle a failure
    produced, and when the row was recorded.

    recorded_at was selected by the chunk query all along and then dropped
    before the entry was built, which left a "Recorded" column with nothing
    to fill it (poly-0e02.4).
    """
    entry = _entries(client)["editors/vim"]
    assert set(entry) == {"entry", "elapsed", "ID", "result", "origin",
                          "info", "duration", "recorded_at"}
    assert entry["result"] == "built"
    assert entry["info"] == "9.2.0738"
    assert entry["recorded_at"]


def test_the_fields_dsynth_had_and_the_tracker_cannot_measure_stay_empty(
    client: TestClient,
) -> None:
    """elapsed, ID and duration describe a builder-slot model this tracker
    does not have. They are emitted so the lifted progress.js keeps working
    and must never be filled in with something plausible."""
    entry = _entries(client)["editors/vim"]

    assert entry["elapsed"] == ""
    assert entry["duration"] == ""
    assert entry["ID"] == "00"


# --- the link actually resolves --------------------------------------------

def test_the_link_the_ui_builds_shows_the_log_in_the_browser(
    client: TestClient,
) -> None:
    """The whole point: follow exactly what progress.js constructs and
    read the log, rather than the 404 every row used to give — or the
    .gz download that pointing at the raw endpoint produced."""
    bundle_id = _entries(client)["graphics/jpeg-turbo"]["bundle_id"]
    url = f"/agentic/bundles/{bundle_id}/artifacts/logs/full.log.gz"

    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    assert "unknown argument" in resp.text
    assert "raw download" not in resp.text


def test_the_raw_endpoint_still_serves_the_real_gzip(client: TestClient) -> None:
    """The viewer decompresses for display only. The bytes on the wire
    are still gzip, and anything scripting against them depends on it."""
    bundle_id = _entries(client)["graphics/jpeg-turbo"]["bundle_id"]
    resp = client.get(f"/api/bundles/{bundle_id}/artifacts/logs/full.log.gz")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert gzip.decompress(resp.content) == LOG_BODY


def test_a_corrupt_gzip_reports_instead_of_crashing(
    client: TestClient, evidence_root: Path,
) -> None:
    """A truncated upload must not 500 the page."""
    store = ArtifactStore.from_evidence_root(evidence_root)
    store.put_blob("bnd-jpeg", "logs/broken.log.gz", b"\x1f\x8btruncated", "gzip")
    store.conn.close()
    resp = client.get("/agentic/bundles/bnd-jpeg/artifacts/logs/broken.log.gz")
    assert resp.status_code == 200, resp.text


def _run_js() -> str:
    return (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
            / "static" / "run.js").read_text()


def test_the_old_relative_link_is_gone_from_the_ui() -> None:
    """Guard against the lifted form coming back: it resolved against the
    page's <base> to /api/progress/build/<x>.log and never existed.

    progress.js became run.js in UI-3; the trap it guards is the same one,
    because run.html still sets <base>."""
    js = _run_js()

    assert "'___'" not in js
    assert '"___"' not in js
    assert "'../'" not in js
    assert '"../"' not in js
    assert "/agentic/bundles/" in js


def test_the_evidence_links_are_root_relative() -> None:
    """The page sets <base href="/api/progress/...">, which rewrites
    relative URLs. A root-relative path is immune to it."""
    js = _run_js()
    start = js.index("function rowNode(")
    body = js[start:js.index("\n  }", start)]

    for href in re.findall(r'link\(\s*\n?\s*"([^"]*)"', body):
        assert href.startswith("/"), href
    assert body.count('"/agentic/bundles/"') == 2   # the log and the bundle


def test_only_a_row_with_evidence_gets_a_link() -> None:
    """A success uploads nothing, so there is no log to point at. The
    renderer must not offer one -- it used to render a link and no version
    for every built row."""
    js = _run_js()
    start = js.index("function rowNode(")
    body = js[start:js.index("\n  }", start)]

    guard = body.index("if (r.bundle_id)")
    else_branch = body.index("} else {", guard)

    assert body.index('"/agentic/bundles/"') > guard
    assert body.rindex('"/agentic/bundles/"') < else_branch
    assert "—" in body[else_branch:]
