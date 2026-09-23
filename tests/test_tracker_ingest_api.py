"""The /v1/ ingest surface, served by the tracker after the fold (poly-g19).

These assert the wire contract the dsynth hooks and the runner already
speak — the standalone artifact-store's endpoints, unchanged apart from
the host:port they answer on.
"""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dportsv3.artifact_store import ArtifactStore, blob_path
from dportsv3.tracker.render.artifacts import resolve_artifact_path
from dportsv3.tracker.server import create_app


@pytest.fixture()
def evidence_root(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "evidence"


@pytest.fixture()
def client(set_setting, evidence_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    set_setting("paths.artifact_root", str(evidence_root))
    evidence_root.mkdir(parents=True, exist_ok=True)
    app = create_app(evidence_root / "state.db")
    with TestClient(app) as test_client:
        yield test_client


def _put(client: TestClient, bundle_id: str, relpath: str, data: bytes,
         kind: str | None = None):
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Bundle-Id": bundle_id,
        "X-Relpath": relpath,
    }
    if kind:
        headers["X-Kind"] = kind
    return client.post("/v1/artifacts/put", content=data, headers=headers)


# --------------------------------------------------------------------------
# health — the hooks gate on this before doing anything else
# --------------------------------------------------------------------------

def test_health_reports_store_paths(client: TestClient, evidence_root: Path) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["db_path"] == str(evidence_root / "state.db")
    assert body["blobstore_root"] == str(evidence_root / "blobstore")


# --------------------------------------------------------------------------
# blobs
# --------------------------------------------------------------------------

def test_put_then_get_roundtrips_exact_bytes(client: TestClient) -> None:
    data = b"\x00\x01\x02 not utf-8 \xff\xfe" * 100
    resp = _put(client, "b1", "logs/raw.bin", data)
    assert resp.status_code == 200, resp.text
    assert resp.json()["size"] == len(data)

    got = client.get("/v1/artifacts/get", params={"bundle_id": "b1", "relpath": "logs/raw.bin"})
    assert got.status_code == 200
    assert got.content == data


def test_put_accepts_gzip_payload(client: TestClient) -> None:
    """The full-log path sends gzip bytes; nothing may try to decode them."""
    blob = gzip.compress(b"configure: error: no acceptable cc found\n" * 50)
    assert _put(client, "b1", "logs/full.log.gz", blob, kind="gzip").status_code == 200
    got = client.get("/v1/artifacts/get", params={"bundle_id": "b1", "relpath": "logs/full.log.gz"})
    assert got.content == blob
    assert gzip.decompress(got.content).startswith(b"configure: error")


def test_empty_body_is_stored_not_rejected(client: TestClient) -> None:
    resp = _put(client, "b1", "port/empty", b"")
    assert resp.status_code == 200
    assert resp.json()["size"] == 0


def test_identical_bytes_dedup_to_one_object(client: TestClient, evidence_root: Path) -> None:
    data = b"same content in two bundles"
    first = _put(client, "b1", "port/Makefile", data).json()
    second = _put(client, "b2", "port/Makefile", data).json()
    assert first["sha256"] == second["sha256"]

    conn = sqlite3.connect(evidence_root / "state.db")
    blobs = conn.execute("SELECT COUNT(*) FROM blob_objects").fetchone()[0]
    refs = conn.execute("SELECT COUNT(*) FROM artifact_refs").fetchone()[0]
    conn.close()
    assert (blobs, refs) == (1, 2)


def test_put_requires_bundle_and_relpath_headers(client: TestClient) -> None:
    resp = client.post(
        "/v1/artifacts/put",
        content=b"x",
        headers={"Content-Type": "application/octet-stream", "X-Bundle-Id": "b1"},
    )
    assert resp.status_code == 400


def test_get_unknown_artifact_is_404(client: TestClient) -> None:
    resp = client.get("/v1/artifacts/get", params={"bundle_id": "nope", "relpath": "x"})
    assert resp.status_code == 404


def test_get_requires_both_params(client: TestClient) -> None:
    assert client.get("/v1/artifacts/get", params={"bundle_id": "b1"}).status_code == 400


def test_errors_keep_the_v1_body_shape(client: TestClient) -> None:
    """{"error": msg}, not FastAPI's {"detail": msg}. A forwarding relay
    passes this through verbatim."""
    resp = client.get("/v1/artifacts/get", params={"bundle_id": "nope", "relpath": "x"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "artifact not found"}
    assert "detail" not in resp.json()


# --------------------------------------------------------------------------
# one owner of the on-disk layout — the reader must agree with the writer
# --------------------------------------------------------------------------

def test_reader_and_writer_agree_on_blob_location(
    client: TestClient, evidence_root: Path,
) -> None:
    data = b"layout must not drift"
    sha = _put(client, "b1", "port/distinfo", data).json()["sha256"]

    written = blob_path(evidence_root / "blobstore", sha)
    assert written.is_file()
    assert written.read_bytes() == data

    served = resolve_artifact_path(evidence_root, {"backend": "blob", "sha256": sha})
    assert served == written


def test_resolve_artifact_path_rejects_short_sha(client: TestClient, evidence_root: Path) -> None:
    assert resolve_artifact_path(evidence_root, {"backend": "blob", "sha256": "ab"}) is None
    assert resolve_artifact_path(evidence_root, {"backend": "blob"}) is None


# --------------------------------------------------------------------------
# fs-backend refs: the write path is gone, the read path is confined
# (poly-zjtx)
# --------------------------------------------------------------------------

def test_put_fs_is_gone(client: TestClient) -> None:
    """It recorded any path that existed and /v1/artifacts/get returned
    the bytes, with no authentication on either route -- an arbitrary file
    read of anything the tracker account can open, which on a deployed
    host includes the delivery token and the chat key. Nothing in the loop
    wrote fs refs: the hook sends the full log as a blob."""
    resp = client.post("/v1/artifacts/put-fs", json={
        "bundle_id": "b1", "relpath": "logs/full.log.gz",
        "fs_path": "/etc/hosts",
    })
    assert resp.status_code == 404


def _legacy_fs_ref(evidence_root: Path, relpath: str, fs_path: Path) -> None:
    """Write the row put-fs used to write, straight into the db.

    A deployed database still holds these from before the hook moved to
    put-blob, so the read path has to keep working -- and stay confined.
    """
    conn = sqlite3.connect(str(evidence_root / "state.db"))
    conn.execute(
        "INSERT INTO artifact_refs (bundle_id, relpath, backend, fs_path, "
        "created_at) VALUES ('b1', ?, 'fs', ?, '2026-01-01T00:00:00+00:00')",
        (relpath, str(fs_path)),
    )
    conn.commit()
    conn.close()


def _get(client: TestClient, relpath: str = "logs/full.log"):
    return client.get("/v1/artifacts/get",
                      params={"bundle_id": "b1", "relpath": relpath})


def test_a_legacy_fs_ref_inside_the_logs_root_still_serves(
    client: TestClient, evidence_root: Path, tmp_path: Path,
) -> None:
    log = tmp_path / "logs" / "devel___llvm19.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"the build log")
    _legacy_fs_ref(evidence_root, "logs/full.log", log)

    resp = _get(client)
    assert resp.status_code == 200
    assert resp.content == b"the build log"


def test_an_fs_ref_outside_the_logs_root_is_refused(
    client: TestClient, evidence_root: Path, tmp_path: Path,
) -> None:
    """The credential's position: readable by this account, outside the
    tree the store serves."""
    secret = tmp_path / "delivery.token"
    secret.write_bytes(b"FORGE-TOKEN")
    _legacy_fs_ref(evidence_root, "logs/full.log", secret)

    resp = _get(client)
    assert resp.status_code == 404
    assert b"FORGE-TOKEN" not in resp.content


def test_a_symlink_out_of_the_logs_root_is_refused(
    client: TestClient, evidence_root: Path, tmp_path: Path,
) -> None:
    """Containment after realpath, not before: the row names a path inside
    the tree that resolves outside it."""
    secret = tmp_path / "delivery.token"
    secret.write_bytes(b"FORGE-TOKEN")
    link = tmp_path / "logs" / "full.log"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    _legacy_fs_ref(evidence_root, "logs/full.log", link)

    resp = _get(client)
    assert resp.status_code == 404
    assert b"FORGE-TOKEN" not in resp.content


# --------------------------------------------------------------------------
# bundles / user-context
# --------------------------------------------------------------------------

def test_bundle_upsert_creates_the_row(client: TestClient, evidence_root: Path) -> None:
    resp = client.post("/v1/bundles/upsert", json={
        "run_id": "r1", "profile": "p", "bundle_id": "b1",
        "origin": "devel/glib20", "flavor": "", "ts_utc": "2026-08-27T10:00:00Z",
    })
    assert resp.status_code == 200
    conn = sqlite3.connect(evidence_root / "state.db")
    row = conn.execute("SELECT origin FROM bundles WHERE bundle_id = 'b1'").fetchone()
    conn.close()
    assert row is not None and row[0] == "devel/glib20"


def test_bundle_upsert_requires_bundle_id(client: TestClient) -> None:
    assert client.post("/v1/bundles/upsert", json={"run_id": "r1"}).status_code == 400


def test_user_context_rejects_empty_and_overlong(client: TestClient) -> None:
    base = {"run_id": "r1", "origin": "devel/glib20"}
    assert client.post("/v1/user-context", json={**base, "context_text": "   "}).status_code == 400
    assert client.post(
        "/v1/user-context", json={**base, "context_text": "x" * 8001},
    ).status_code == 400


def test_jobs_transition_requires_job_id_and_event(client: TestClient) -> None:
    assert client.post("/v1/jobs/transition", json={"job_id": "j1"}).status_code == 400
    assert client.post("/v1/jobs/transition", json={"event": "hook_enqueued"}).status_code == 400


# --------------------------------------------------------------------------
# store construction
# --------------------------------------------------------------------------

def test_from_evidence_root_does_not_assume_a_directory_name(tmp_path: Path) -> None:
    """DPORTSV3_ARTIFACT_ROOT need not be named 'evidence' or sit one level
    under a logs root; the store must use the path it is given."""
    root = tmp_path / "somewhere" / "else"
    store = ArtifactStore.from_evidence_root(root)
    assert store.evidence_root == root
    assert store.blob_root == root / "blobstore"
    assert store.db_path == root / "state.db"
