"""Shared pytest fixtures for the dportsv3 test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_dir_search(monkeypatch):
    """Keep the host's real config out of the suite.

    Same hazard as ``_isolate_env_resolver`` below, one layer over:
    poly-mrl gave ``paths.config_dir`` a filesystem search underneath
    ``$DPORTSV3_CONFIG_DIR`` — ``<bindir>/../etc/polytropos``, then
    ``/usr/local/etc/polytropos``. Without this, any test that unsets
    that variable reads whatever the machine happens to have: green on a
    laptop with no ``/usr/local/etc/polytropos``, different on the build
    host that has one, and the difference shows up as an unrelated
    assertion failing.

    Only ``argv[0]`` is replaced — ``cli.py`` reads ``argv[1:]``. Tests
    that exercise the search set both of these to what they need, and
    win because they run after this.
    """
    from dportsv3 import paths
    monkeypatch.setattr(
        paths, "DEFAULT_CONFIG_DIR",
        Path("/nonexistent/polytropos-test-isolation/etc/polytropos"),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["/nonexistent/polytropos-test-isolation/bin/dportsv3",
         *sys.argv[1:]],
    )


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


@pytest.fixture(autouse=True)
def _reset_settings():
    """Drop the process-wide settings between tests.

    The schema is loaded once and cached, which is right for a service
    and wrong for a suite where each test points $DPORTSV3_CONFIG_DIR
    somewhere else. Resetting on both sides means a test that never
    touches settings still starts from the shipped defaults.
    """
    from dports_dev_env import config as dev_env_config
    from dportsv3 import settings
    # Two schemas, one file: the generator's and the dev-env's. Both cache,
    # so both have to be dropped or a test inherits the previous one's.
    settings.reset()
    dev_env_config.reset_schema()
    yield
    settings.reset()
    dev_env_config.reset_schema()


@pytest.fixture
def set_setting(tmp_path, monkeypatch):
    """Write settings into a throwaway config dir.

    Replaces the ``monkeypatch.setenv("DP_HARNESS_...")`` idiom: those
    variables are gone, and a test that wants a non-default value now
    says which setting it means. Accumulates, so several calls build one
    file.
    """
    import tomli_w

    from dports_dev_env import config as dev_env_config
    from dportsv3 import settings as settings_mod

    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(config_dir))
    document: dict = {}

    def _set(path: str, value) -> Path:
        node = document
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        target = config_dir / settings_mod.CONFIG_FILENAME
        target.write_bytes(tomli_w.dumps(document).encode())
        settings_mod.reset()
        dev_env_config.reset_schema()
        return target

    return _set
