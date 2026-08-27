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
def client(seeded: Path, evidence_root: Path,
           monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DPORTSV3_ARTIFACT_ROOT", str(evidence_root))
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


def test_the_rest_of_the_entry_shape_is_unchanged(client: TestClient) -> None:
    """dsynth-progress' own field set — the UI is lifted, not rewritten."""
    entry = _entries(client)["editors/vim"]
    assert set(entry) == {"entry", "elapsed", "ID", "result", "origin",
                          "info", "duration"}
    assert entry["result"] == "built"
    assert entry["info"] == "9.2.0738"


# --- the link actually resolves --------------------------------------------

def test_the_link_the_ui_builds_serves_the_log(client: TestClient) -> None:
    """The whole point: follow exactly what progress.js constructs and
    get the bytes, rather than the 404 every row used to give."""
    bundle_id = _entries(client)["graphics/jpeg-turbo"]["bundle_id"]
    url = f"/api/bundles/{bundle_id}/artifacts/logs/full.log.gz"

    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    assert gzip.decompress(resp.content) == LOG_BODY


def test_the_old_relative_link_is_gone_from_the_ui() -> None:
    """Guard against the lifted form coming back: it resolved against the
    page's <base> to /api/progress/build/<x>.log and never existed."""
    js = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
          / "static" / "progress.js").read_text()
    assert "'___'" not in js
    assert "'../'" not in js
    assert "/api/bundles/" in js


def test_the_link_is_root_relative() -> None:
    """The page sets <base href="/api/progress/...">, which rewrites
    relative URLs. A root-relative path is immune to it."""
    js = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
          / "static" / "progress.js").read_text()
    start = js.index("function logfile(")
    body = js[start:js.index("}", start)]
    assert "'/api/bundles/'" in body


def test_only_failed_rows_get_a_link_in_the_renderer() -> None:
    """built used to render a link and nothing else. It renders the
    version now — successes have no log in the tracker to link to."""
    js = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
          / "static" / "progress.js").read_text()
    start = js.index("function infoHTML(")
    body = js[start:js.index("\n}", start)]
    assert body.count("logfile(") == 1
    failed_branch = body[body.index("if (result === 'failed')"):]
    assert "logfile(" in failed_branch
