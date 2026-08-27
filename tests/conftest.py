"""Shared pytest fixtures for the dportsv3 test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_env_resolver(monkeypatch):
    """Force env_resolver.list_available_envs to return () for every
    test by default.

    Why: the resolver's auto-pick step reads the host filesystem
    (`/var/cache/dports-dev/envs/`). On a developer machine with
    dev-envs configured, tests that expect "no env" would silently
    auto-pick a real env. We default to "no envs on disk" and let
    tests that exercise auto-pick supply their own value via the
    resolver's ``available_envs`` parameter (the test-friendly
    override that bypasses list_available_envs entirely).

    Also resets the runner's CLI-flag default so each test starts
    from a clean slate without per-file boilerplate.
    """
    from dportsv3.agent import env_resolver, runner
    monkeypatch.setattr(env_resolver, "list_available_envs", lambda: ())
    monkeypatch.setattr(runner, "_CLI_ENV_DEFAULT", None)
    # Reset the gate's TTL cache between tests — a value populated
    # in test A would bleed into test B for up to 1 s and silently
    # mask "the UI change reached the runner" behavior.
    monkeypatch.setattr(runner, "_GATE_RESOLVE_CACHE", None)


@pytest.fixture
def ingest_server(tmp_path):
    """The /v1/ ingest surface on a real port, the way a dsynth hook
    reaches it.

    Post-fold that surface is the tracker, so this boots the FastAPI app
    rather than the standalone store that used to own :8788. Yields
    ``(url, store)``; the store is handed in so a test can inspect the
    rows and blobs the hook produced.
    """
    import threading
    import time

    import uvicorn

    from dportsv3.artifact_store import ArtifactStore
    from dportsv3.tracker.server import create_app

    evidence = tmp_path / "store" / "evidence"
    evidence.mkdir(parents=True)
    store = ArtifactStore.from_evidence_root(evidence)
    app = create_app(store.db_path)
    app.state.artifact_root = evidence
    app.state.artifact_store = store

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("ingest server thread died during startup")
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("ingest server did not start in time")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", store
    finally:
        server.should_exit = True
        thread.join(timeout=15)
