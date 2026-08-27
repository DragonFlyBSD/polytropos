"""The failure hook must not lose the bundle when the tracker is absent.

`dportsv3-hooks.conf` states the contract: with no `DPORTSV3_TRACKER_URL`
"the tracker_* helpers short-circuit and hooks only do artifact-store
work." `hook_pkg_failure` did not honour it. It called
`tracker_load_config` up front — for `DPORTSV3_TRACKER_TARGET`, nothing
more — and that function soft-fails with `exit 0` when `DPORTSV3_BIN` is
unset or unusable. So on any host where the tool was not reachable, every
failed build was recorded nowhere and the hook exited looking successful.
That was the state of every dev-env chroot, which is where the tool is
least reachable and where failures are most worth keeping.

These run the hook. The bug lived in the difference between what the
script says and what executing it does.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest


def _hooks_dir() -> Path:
    from dports_dev_env.hooks import repo_hook_source
    return repo_hook_source()


_HOOKS = _hooks_dir()


@pytest.fixture
def store_url(ingest_server):
    """The hooks post to whatever serves /v1/; see conftest."""
    return ingest_server


@pytest.fixture
def client(tmp_path):
    """The store client as an executable, the way the hook invokes it.

    It is a venv console script now, so its own shebang points into
    whichever venv the tests run from. Wrapping it keeps the hook's
    "$ARTIFACT_STORE_CLIENT" contract — an executable path — without
    depending on this checkout being installed anywhere.
    """
    path = tmp_path / "artifact-store-client"
    path.write_text(
        f"#!/bin/sh\nexec {sys.executable} -m dportsv3.artifact_store_client \"$@\"\n"
    )
    path.chmod(0o755)
    return path


def _run_failure_hook(tmp_path, url, client, **env_extra):
    """Fire hook_pkg_failure for a failed editors/vim, and return the run."""
    logs = tmp_path / "logs"
    ports = tmp_path / "ports" / "editors" / "vim"
    ports.mkdir(parents=True)
    (ports / "Makefile").write_text("PORTNAME=\tvim\n")
    logs.mkdir()
    (logs / "editors___vim.log").write_text(
        "===>  Building for vim-9.1\n"
        "buffer.c:120:5: error: use of undeclared identifier 'nosuchsym'\n"
        "*** Error code 1\n"
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
        "RESULT": "failure",
        "ORIGIN": "editors/vim",
        "FLAVOR": "",
        "PKGNAME": "vim-9.1",
        "PROFILE": "2026Q3-editors_vim",
        "DIR_LOGS": str(logs),
        "DIR_PORTS": str(tmp_path / "ports"),
        "ARTIFACT_STORE_URL": url,
        "ARTIFACT_STORE_CLIENT": str(client),
        # No conf file: the "tracker was never set up" case exactly.
        "DPORTSV3_HOOKS_CONFIG": str(tmp_path / "absent.conf"),
    }
    env.update(env_extra)
    return subprocess.run([str(_HOOKS / "hook_pkg_failure")],
                          capture_output=True, text=True, env=env)


def test_an_unconfigured_tracker_still_records_the_failure(
    tmp_path, store_url, client
):
    url, store = store_url
    done = _run_failure_hook(tmp_path, url, client)
    assert done.returncode == 0, done.stderr

    rows = store.conn.execute(
        "SELECT bundle_id, origin, result, target FROM bundles"
    ).fetchall()
    assert len(rows) == 1, f"no bundle recorded: {done.stderr}"
    assert rows[0]["origin"] == "editors/vim"
    assert rows[0]["result"] == "failure"


def test_the_evidence_survives_too(tmp_path, store_url, client):
    """A bundle row with no artifacts under it is not evidence. The
    distilled errors text is what triage reads first."""
    url, store = store_url
    done = _run_failure_hook(tmp_path, url, client)
    assert done.returncode == 0, done.stderr

    relpaths = {r["relpath"] for r in store.conn.execute(
        "SELECT relpath FROM artifact_refs").fetchall()}
    assert "logs/errors.txt" in relpaths, relpaths
    assert "port/Makefile" in relpaths, relpaths


def test_the_job_is_enqueued_with_the_target(tmp_path, store_url, client):
    """`DPORTSV3_TRACKER_TARGET` is derived by the config half, which is
    the half that has to keep running. Losing it leaves jobs.target NULL
    and the job invisible under every target filter in the UI."""
    url, _ = store_url
    done = _run_failure_hook(tmp_path, url, client)
    assert done.returncode == 0, done.stderr

    pending = sorted((tmp_path / "logs" / "evidence" / "queue" / "pending").iterdir())
    assert len(pending) == 1, pending
    body = pending[0].read_text()
    assert "target=@2026Q3-editors_vim" in body, body
    assert "origin=editors/vim" in body, body


def test_a_broken_dportsv3_bin_does_not_cost_the_bundle(
    tmp_path, store_url, client
):
    """The specific shape seen in a chroot: the conf names a tracker and a
    tool, and the tool is not there. Before the split this exited 0 from
    inside tracker_load_config, three lines before the upsert."""
    url, store = store_url
    conf = tmp_path / "hooks.conf"
    conf.write_text(
        "DPORTSV3_TRACKER_URL=http://127.0.0.1:1\n"
        f"DPORTSV3_BIN={tmp_path / 'nowhere' / 'dportsv3'}\n"
    )
    done = _run_failure_hook(tmp_path, url, client,
                             DPORTSV3_HOOKS_CONFIG=str(conf))
    assert done.returncode == 0, done.stderr

    rows = store.conn.execute("SELECT origin FROM bundles").fetchall()
    assert len(rows) == 1, done.stderr


def test_the_full_log_arrives_as_bytes_not_as_a_path(tmp_path, store_url, client):
    """put-fs recorded a path the store had to open itself, which fails
    the moment the hook and the store are not the same filesystem — and
    in a chroot they never are. The compressed log is a blob now."""
    import gzip

    url, store = store_url
    done = _run_failure_hook(tmp_path, url, client)
    assert done.returncode == 0, done.stderr

    row = store.conn.execute(
        "SELECT backend, sha256, fs_path, kind, size FROM artifact_refs "
        "WHERE relpath = 'logs/full.log.gz'"
    ).fetchone()
    assert row is not None, "the full log was not stored at all"
    assert row["backend"] == "blob"
    assert row["fs_path"] is None
    assert row["kind"] == "gzip"
    assert row["size"] > 0

    from dportsv3.artifact_store import blob_path
    stored = blob_path(store.blob_root, row["sha256"])
    assert stored.is_file()
    assert b"nosuchsym" in gzip.decompress(stored.read_bytes())


def test_no_artifact_is_left_pointing_at_a_path(tmp_path, store_url, client):
    """Nothing the hook writes may be fs-backed: the tracker serving the
    UI is not guaranteed to share a filesystem with the builder."""
    url, store = store_url
    assert _run_failure_hook(tmp_path, url, client).returncode == 0
    backends = {
        r["backend"]
        for r in store.conn.execute("SELECT DISTINCT backend FROM artifact_refs")
    }
    assert backends == {"blob"}
