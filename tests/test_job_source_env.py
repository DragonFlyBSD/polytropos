"""A job raised in a dev-env runs in that env (poly-p2ve, step 4).

Routing by build line (d0fce69) runs every job in an env of its own line.
With two envs of one line, a job the hooks raised has to say which one it
came from, or it waits for an operator to pick: the hooks conf names the
env (DPORTSV3_TRACKER_DEV_ENV), the job file and the jobs row carry it,
and the runner pins the job to it. A conf written before that stamp
existed -- hooks-install never rewrites one -- falls back to the job's
dsynth profile, which a dev-env names after itself.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from dportsv3.agent import env_resolver as er
from dportsv3.agent import runner as runner_mod
from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import envs_by_line, set_active_env


# --- the hook stamps it -----------------------------------------------------

def _hooks() -> Path:
    from dports_dev_env.hooks import repo_hook_source
    return repo_hook_source()


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "artifact-store-client"
    path.write_text(f"#!/bin/sh\nexec {sys.executable} -m "
                    f"dportsv3.artifact_store_client \"$@\"\n")
    path.chmod(0o755)
    return path


def _fire(tmp_path, url, client, **extra):
    logs = tmp_path / "logs"
    port = tmp_path / "ports" / "editors" / "vim"
    port.mkdir(parents=True)
    (port / "Makefile").write_text("PORTNAME=\tvim\n")
    logs.mkdir()
    (logs / "editors___vim.log").write_text(
        "buffer.c:120:5: error: use of undeclared identifier 'x'\n"
        "*** Error code 1\n")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin",
        "RESULT": "failure", "ORIGIN": "editors/vim", "FLAVOR": "",
        "PKGNAME": "vim-9.1", "PROFILE": "2026Q3-editors_vim",
        "DIR_LOGS": str(logs), "DIR_PORTS": str(tmp_path / "ports"),
        "ARTIFACT_STORE_URL": url, "ARTIFACT_STORE_CLIENT": str(client),
        "DPORTSV3_HOOKS_CONFIG": str(tmp_path / "absent.conf"),
        "DPORTSV3_TRACKER_TARGET": "@2026Q3",
        **extra,
    }
    done = subprocess.run([str(_hooks() / "hook_pkg_failure")],
                          capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    pending = sorted((logs / "evidence" / "queue" / "pending").iterdir())
    assert len(pending) == 1, pending
    return pending[0]


def test_a_stamped_hook_names_its_env_in_the_file_and_the_row(
        tmp_path, ingest_server, client):
    url, store = ingest_server
    job = _fire(tmp_path, url, client,
                DPORTSV3_TRACKER_DEV_ENV="2026Q3-editors_vim")
    assert "dev_env=2026Q3-editors_vim\n" in job.read_text()
    row = store.conn.execute("SELECT dev_env FROM jobs WHERE job_id = ?",
                             (job.name,)).fetchone()
    assert row["dev_env"] == "2026Q3-editors_vim"


def test_an_unstamped_hook_names_none(tmp_path, ingest_server, client):
    """A farm, or a conf from before the stamp: nothing is invented."""
    url, store = ingest_server
    job = _fire(tmp_path, url, client)
    assert "dev_env=" not in job.read_text()
    row = store.conn.execute("SELECT dev_env FROM jobs WHERE job_id = ?",
                             (job.name,)).fetchone()
    assert row["dev_env"] is None


# --- the resolver honours it ------------------------------------------------

LINES = {"q3-a": "@2026Q3", "q3-b": "@2026Q3", "main-a": "@main"}


def _resolve(job, active=None):
    conn = None
    if active:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        set_active_env(conn, active)
    return er.resolve_env_for_job(job, conn, env_lines=LINES)


def test_two_envs_of_one_line_and_no_stamp_is_ambiguous():
    assert _resolve({"target": "@2026Q3"}).source == "ambiguous"


def test_the_stamp_routes_to_its_env():
    r = _resolve({"target": "@2026Q3", "dev_env": "q3-b"})
    assert (r.env, r.source) == ("q3-b", "job")


def test_without_a_stamp_the_profile_names_the_env():
    r = _resolve({"target": "@2026Q3", "profile": "q3-b"})
    assert (r.env, r.source) == ("q3-b", "profile")


def test_the_profile_outranks_the_preferred_env():
    """It is the job's own env, the same rank as the stamp."""
    r = _resolve({"target": "@2026Q3", "profile": "q3-b"}, active="q3-a")
    assert r.env == "q3-b"


def test_a_profile_of_another_lines_env_names_nothing():
    """Only an env of the job's line: a mismatch is the job-start check's."""
    r = _resolve({"target": "@2026Q3", "profile": "main-a"})
    assert r.source == "ambiguous"


def test_a_farm_profile_names_nothing():
    assert _resolve({"target": "@2026Q3", "profile": "2026Q3"}).source == (
        "ambiguous")


def test_profile_env_matches_the_sanitized_env_name():
    """dports_dev_env names the profile sanitize_name(env)."""
    assert er.profile_env({"profile": "q3_b"}, ["q3@b", "q3-a"]) == "q3@b"
    assert er.profile_env({"profile": "x"}, ["x", "x"]) is None
    assert er.profile_env({}, ["x"]) is None


# --- the runner claims it ---------------------------------------------------

def test_the_db_claim_carries_the_stamp():
    meta = runner_mod._job_meta_from_row(
        {"target": "@2026Q3", "dev_env": "q3-b", "profile": "p"})
    assert meta["dev_env"] == "q3-b"
    assert "dev_env" not in runner_mod._job_meta_from_row(
        {"target": "@2026Q3", "profile": "p"})


@pytest.mark.parametrize("meta", [
    {"target": "@2026Q3", "dev_env": "q3-b"},
    {"target": "@2026Q3", "profile": "q3-b"},
], ids=["stamped", "by-profile"])
def test_a_job_of_an_ambiguous_line_with_its_env_does_not_wait(
        monkeypatch, meta):
    monkeypatch.setattr(er, "envs_by_build_line",
                        lambda envs=None: {"@2026Q3": ("q3-a", "q3-b")})
    runner_mod._HELD_FOR_LINE.clear()
    held = {"@2026Q3": "2 envs compose it (q3-a, q3-b); select one"}
    assert runner_mod._waits_for_its_line(meta, held) is False
    assert runner_mod._waits_for_its_line({"target": "@2026Q3"}, held)


def test_env_lines_are_read_once_per_tick(monkeypatch):
    reads = []
    monkeypatch.setattr(er, "envs_by_build_line",
                        lambda envs=None: reads.append(1) or {"@main": ("m",)})
    assert runner_mod._env_lines() == {"@main": ("m",)}
    assert runner_mod._env_lines() == {"@main": ("m",)}
    assert len(reads) == 1
    monkeypatch.setattr(runner_mod, "_ENV_LINES_CACHE", None)
    runner_mod._env_lines()
    assert len(reads) == 2


# --- the tracker says which env each line uses ------------------------------

def test_envs_by_line_groups_the_health_rows():
    rows = [
        {"env": "q3-a", "detail": {"target": "@2026Q3"}},
        {"env": "main-a", "detail": {"target": "main"}},
        {"env": "q3-a", "detail": {"target": "@2026Q3"}},  # another builder
        {"env": "q3-b", "detail": {"target": "@2026Q3"}},
        {"env": "old", "detail": None},
    ]
    assert envs_by_line(rows) == [
        ("@2026Q3", ["q3-a", "q3-b"]), ("@main", ["main-a"])]
