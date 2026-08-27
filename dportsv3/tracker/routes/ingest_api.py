"""Ingest API (``/v1/*``) — the write surface hooks and builders call.

Folded in from the standalone artifact-store service: same wire contract,
same ``ArtifactStore`` class, served in-process on the tracker's port so
there is one service, one port and one auth surface.

The blob routes are sync ``def`` on purpose. FastAPI reads the body on the
event loop and then dispatches a sync handler into its threadpool, so
hashing a multi-megabyte upload and writing it to disk cannot stall the UI.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from dportsv3.artifact_store import ArtifactStore

from ._common import Body, Request, Response, RouteContext

MAX_USER_CONTEXT_CHARS = 8000


def _error(status: int, message: str) -> Any:
    """The standalone store's error body, kept byte-for-byte.

    FastAPI's HTTPException would emit {"detail": ...}; the /v1/ contract
    is {"error": ...}. No current client reads the body — urlopen raises
    on 4xx without touching it — but a forwarding relay will pass it
    through verbatim, so the shape is part of the contract, not an
    implementation detail.
    """
    return Response(
        content=json.dumps({"error": message}, indent=2),
        status_code=status,
        media_type="application/json",
    )


def register(app: Any, ctx: RouteContext) -> None:
    lock = threading.Lock()

    def _store() -> ArtifactStore:
        """The store, built on first ingest call and cached on app.state.

        Lazily, not at startup: constructing it creates the evidence tree
        and opens state.db, and the tracker's read surface has no business
        failing to boot because the blobstore is not writable yet. A
        deployment or a test may preset ``app.state.artifact_store``.
        """
        store = getattr(app.state, "artifact_store", None)
        if store is not None:
            return store
        with lock:
            store = getattr(app.state, "artifact_store", None)
            if store is None:
                store = ArtifactStore.from_evidence_root(app.state.artifact_root)
                app.state.artifact_store = store
        return store

    @app.get("/health")
    def health() -> Any:
        """The standalone store's health shape, kept verbatim: the dsynth
        hooks gate on it via ``require_artifact_store``."""
        store = _store()
        return {
            "ok": True,
            "db_path": str(store.db_path),
            "blobstore_root": str(store.blob_root),
            "full_logs_root": str(store.full_logs_root),
        }

    @app.get("/v1/artifacts/get")
    def artifacts_get(bundle_id: str = "", relpath: str = "") -> Any:
        if not bundle_id or not relpath:
            return _error(400, "bundle_id and relpath required")
        result = _store().get_artifact(bundle_id, relpath)
        if not result:
            return _error(404, "artifact not found")
        _backend, file_path = result
        if not file_path.exists():
            return _error(404, "artifact file missing")
        return Response(
            content=file_path.read_bytes(),
            media_type="application/octet-stream",
        )

    @app.post("/v1/bundles/upsert")
    def bundles_upsert(body: dict[str, Any]) -> Any:
        if not body.get("bundle_id"):
            return _error(400, "bundle_id required")
        _store().upsert_run_bundle(body)
        return {"ok": True}

    @app.post("/v1/artifacts/put")
    def artifacts_put(
        request: Request,
        data: bytes = Body(default=b"", media_type="application/octet-stream"),
    ) -> Any:
        bundle_id = request.headers.get("x-bundle-id")
        relpath = request.headers.get("x-relpath")
        kind = request.headers.get("x-kind")
        if not bundle_id or not relpath:
            return _error(400, "X-Bundle-Id and X-Relpath required")
        result = _store().put_blob(bundle_id, relpath, data or b"", kind)
        return {"ok": True, **result}

    @app.post("/v1/artifacts/put-fs")
    def artifacts_put_fs(body: dict[str, Any]) -> Any:
        bundle_id = body.get("bundle_id")
        relpath = body.get("relpath")
        fs_path = body.get("fs_path")
        if not bundle_id or not relpath or not fs_path:
            return _error(400, "bundle_id, relpath, fs_path required")
        # A pointer is only worth recording if the process that will serve
        # it can open it. Without this the store happily writes a row with
        # size NULL that 404s forever, and a caller on another host — or
        # inside a chroot — cannot tell it failed. Nothing calls this now;
        # the hook sends the compressed log as bytes.
        if not Path(fs_path).is_file():
            return _error(
                400, f"fs_path is not readable by the store: {fs_path}"
            )
        result = _store().put_fs_ref(bundle_id, relpath, fs_path, body.get("kind"))
        return {"ok": True, **result}

    @app.post("/v1/jobs/transition")
    def jobs_transition(body: dict[str, Any]) -> Any:
        if not body.get("job_id") or not body.get("event"):
            return _error(400, "job_id and event required")
        result = _store().apply_transition(body)
        if not result.get("ok"):
            return _error(400, result.get("error") or "transition rejected")
        return result

    @app.post("/v1/user-context")
    def user_context(body: dict[str, Any]) -> Any:
        run_id = body.get("run_id")
        origin = body.get("origin")
        context_text = body.get("context_text")
        if not run_id or not origin or context_text is None:
            return _error(400, "run_id, origin, context_text required")
        context_text = str(context_text).strip()
        if not context_text:
            return _error(400, "context_text cannot be empty")
        if len(context_text) > MAX_USER_CONTEXT_CHARS:
            return _error(400, f"context_text too long (max {MAX_USER_CONTEXT_CHARS} chars)")
        return {
            "ok": True,
            "context_rev": _store().upsert_user_context(run_id, origin, context_text),
        }
