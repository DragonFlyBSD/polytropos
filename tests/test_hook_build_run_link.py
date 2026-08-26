"""The occurrence -> build-run link the regression derivation needs (C3).

`runs.build_run_id` existed but nothing ever wrote it, so an occurrence could
not be placed against a fix's known-good boundary and the only comparison left
was wall clocks across hosts. The hook holds both ids — its own run id for the
store, and the tracker `build_runs` ordinal from `tracker start-build` — and
now sends the second with the bundle.

These run the shell rather than reading it: the failure path must survive a
missing, disabled or malformed tracker state file without losing the bundle,
and that is a property of the code executing, not of its text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


def _hooks_dir() -> Path:
    from dports_dev_env.hooks import repo_hook_source
    return repo_hook_source()


_HOOKS = _hooks_dir()


def _run_ordinal(state_file: Path | None) -> subprocess.CompletedProcess:
    script = f". {_HOOKS / 'hook_common.sh'}\n"
    if state_file is not None:
        script += f"TRACKER_STATE_FILE={state_file}\n"
    script += "tracker_run_ordinal\n"
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True)


def _ordinal(state_file: Path | None) -> str:
    """Run `tracker_run_ordinal` against a given state file.

    Asserts a clean run, stderr included: dsynth shows hook stderr to the
    operator, so a helper that yields the right answer while complaining
    about a file it chose not to read is still wrong.
    """
    done = _run_ordinal(state_file)
    assert done.returncode == 0, done.stderr
    assert done.stderr == "", done.stderr
    return done.stdout.strip()


def _state(tmp_path, body: str) -> Path:
    p = tmp_path / "state.env"
    p.write_text(body)
    return p


def test_reads_the_tracker_run_ordinal(tmp_path):
    assert _ordinal(_state(tmp_path, "RUN_ID=57\nTARGET=@2026Q3\n")) == "57"


def test_disabled_tracking_yields_nothing(tmp_path):
    assert _ordinal(_state(tmp_path, "TRACKING_DISABLED=1\nRUN_ID=57\n")) == ""


def test_missing_state_file_yields_nothing(tmp_path):
    assert _ordinal(tmp_path / "absent.env") == ""


def test_unset_state_file_yields_nothing():
    assert _ordinal(None) == ""


@pytest.mark.parametrize("body", [
    "RUN_ID=\n",
    "RUN_ID=abc\n",
    "RUN_ID=12x\n",
    "TARGET=@2026Q3\n",          # no RUN_ID at all
])
def test_a_malformed_ordinal_yields_nothing(tmp_path, body):
    """Better no link than a bogus one: the store would coerce garbage to
    NULL anyway, and the hook must not fail the bundle over it."""
    assert _ordinal(_state(tmp_path, body)) == ""


def test_the_failure_hook_sends_it():
    body = (_HOOKS / "hook_pkg_failure").read_text()
    assert re.search(r"^build_run_id=\$\(tracker_run_ordinal\)$",
                     body, re.MULTILINE)
    # Joined so a backslash continuation between the flag and the call
    # cannot hide a missing argument.
    joined = body.replace("\\\n", " ")
    assert re.search(r"artifact_store bundle-upsert\b[^\n]*"
                     r'--build-run-id "\$\{build_run_id\}"', joined)


# --- the wire, end to end --------------------------------------------------


@pytest.fixture
def live_store(tmp_path):
    """The real client binary talking to a real store over HTTP.

    The hook shells out to this client, so a payload key that the client
    never puts on the wire is invisible to every in-process test. Yields
    (run_client, store).
    """
    import threading

    from dportsv3.artifact_store import (
        ArtifactStore, ArtifactStoreServer, Handler,
    )

    store = ArtifactStore(tmp_path)
    server = ArtifactStoreServer(("127.0.0.1", 0), Handler, store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    client = Path(__file__).resolve().parents[1] / "bin" / "artifact-store-client"

    def run(*args):
        done = subprocess.run([str(client), "--url", url, *args],
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done

    try:
        yield run, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _upsert(run, **overrides):
    args = {"--run-id": "r1", "--profile": "p", "--target": "@2026Q3",
            "--ts-utc": "2026-07-25T00:00:00Z", "--bundle-id": "b1",
            "--origin": "ftp/curl", "--result": "failure"}
    args.update(overrides)
    return run("bundle-upsert", *[x for kv in args.items() for x in kv])


def test_the_client_puts_the_ordinal_on_the_wire(live_store):
    run, store = live_store
    _upsert(run, **{"--build-run-id": "57"})
    assert store.conn.execute(
        "SELECT build_run_id FROM runs WHERE run_id='r1'"
    ).fetchone()[0] == 57


def test_an_omitted_ordinal_reaches_the_store_as_null(live_store):
    """Tracking off: the hook passes an empty string and the occurrence is
    still recorded, just without a link."""
    run, store = live_store
    _upsert(run, **{"--build-run-id": ""})
    assert store.conn.execute(
        "SELECT build_run_id FROM runs WHERE run_id='r1'"
    ).fetchone()[0] is None
    assert store.conn.execute(
        "SELECT 1 FROM bundles WHERE bundle_id='b1'").fetchone()


def test_the_flag_is_optional_on_the_client(live_store):
    """An older hook that doesn't pass it at all must still upsert."""
    run, store = live_store
    _upsert(run)
    assert store.conn.execute(
        "SELECT 1 FROM bundles WHERE bundle_id='b1'").fetchone()
