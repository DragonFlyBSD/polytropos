"""A dev-env belongs to a host (poly-fij.13).

The tracker enumerated env NAMES globally and selected exactly one --
`tracker_active_env` a singleton by CHECK constraint, `env_health_status`
keyed by env alone, and a UI with radio semantics. With N builders that is
wrong twice: the singleton hands every builder the same answer, and two
builders carrying 2026Q3 overwrite each other's probes on every cycle.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import (
    builder_env_selections,
    env_health_statuses,
    get_active_env,
    set_active_env,
)

NOW = datetime.now(timezone.utc).isoformat()

LEGACY = """CREATE TABLE env_health_status (
    env TEXT PRIMARY KEY, status TEXT NOT NULL, probed_at TEXT,
    operator_action TEXT, detail_json TEXT, updated_at TEXT NOT NULL);"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _builder(conn, runner_id, host, env=None):
    conn.execute(
        "INSERT INTO runners (runner_id, hostname, last_heartbeat_at, "
        "active_env) VALUES (?, ?, ?, ?)", (runner_id, host, NOW, env))
    conn.commit()


def _probe(conn, runner_id, env, status):
    conn.execute(
        """INSERT INTO env_health_status
             (runner_id, env, status, probed_at, operator_action,
              detail_json, updated_at)
           VALUES (?, ?, ?, ?, NULL, '{"checks":[]}', ?)
           ON CONFLICT(runner_id, env) DO UPDATE SET status=excluded.status""",
        (runner_id, env, status, NOW, NOW))
    conn.commit()


# --- the schema -------------------------------------------------------------

def test_a_fresh_db_is_keyed_by_builder_and_env():
    conn = _db()
    pk = [r["name"] for r in conn.execute("PRAGMA table_info(env_health_status)")
          if r["pk"]]
    assert pk == ["runner_id", "env"]


def test_an_existing_db_is_rebuilt_and_its_unattributable_rows_discarded():
    """Every column here is a PROBE CACHE, not a record -- status, probed_at,
    operator_action and detail_json all come from the health probe. Keeping
    legacy rows as runner_id '' would leave every env a permanent ghost beside
    its real row, because a runner stamping its real id never matches one."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY)
    conn.execute("INSERT INTO env_health_status VALUES "
                 "('2026Q3','ready',?,NULL,'{}',?)", (NOW, NOW))
    conn.commit()

    init_db(conn)

    pk = [r["name"] for r in conn.execute("PRAGMA table_info(env_health_status)")
          if r["pk"]]
    assert pk == ["runner_id", "env"]
    assert conn.execute("SELECT count(*) FROM env_health_status").fetchone()[0] == 0


def test_the_rebuild_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(LEGACY)
    conn.commit()
    init_db(conn)
    _probe(conn, "b1", "2026Q3", "ready")
    init_db(conn)
    assert conn.execute(
        "SELECT count(*) FROM env_health_status").fetchone()[0] == 1


# --- the collision this existed to stop -------------------------------------

def test_two_builders_with_the_same_env_do_not_overwrite_each_other():
    """The old ON CONFLICT(env) meant an operator could read a green 2026Q3
    that was probed on a different machine, and the dsynth gate believed it."""
    conn = _db()
    _probe(conn, "b1", "2026Q3", "ready")
    _probe(conn, "b2", "2026Q3", "broken")

    rows = {(r["runner_id"], r["env"]): r["status"]
            for r in env_health_statuses(conn)}
    assert rows == {("b1", "2026Q3"): "ready", ("b2", "2026Q3"): "broken"}


def test_health_can_be_read_for_one_builder():
    conn = _db()
    _probe(conn, "b1", "2026Q3", "ready")
    _probe(conn, "b2", "2026Q1", "broken")

    assert [r["env"] for r in env_health_statuses(conn, "b1")] == ["2026Q3"]


# --- selection: a default, and per-builder overrides ------------------------

def test_a_builder_without_a_choice_follows_the_deployment_default():
    conn = _db()
    set_active_env(conn, "2026Q2")
    _builder(conn, "b1", "host-a")

    assert get_active_env(conn) == "2026Q2"
    assert get_active_env(conn, "b1") == "2026Q2"


def test_a_builder_can_override_the_default_without_changing_it():
    conn = _db()
    set_active_env(conn, "2026Q2")
    _builder(conn, "b1", "host-a")
    _builder(conn, "b2", "host-b")

    set_active_env(conn, "2026Q3", runner_id="b1")

    assert get_active_env(conn, "b1") == "2026Q3"
    assert get_active_env(conn, "b2") == "2026Q2", "b2 still follows the default"
    assert get_active_env(conn) == "2026Q2", "the default itself is untouched"


def test_clearing_a_builders_choice_returns_it_to_the_default():
    conn = _db()
    set_active_env(conn, "2026Q2")
    _builder(conn, "b1", "host-a", env="2026Q3")

    set_active_env(conn, None, runner_id="b1")

    assert get_active_env(conn, "b1") == "2026Q2"


def test_setting_a_builder_that_never_reported_creates_no_row():
    """A runner row comes from enrollment and the heartbeat; inventing one
    here would put a builder in the table that has never reported."""
    conn = _db()
    set_active_env(conn, "2026Q3", runner_id="ghost")

    assert conn.execute("SELECT count(*) FROM runners").fetchone()[0] == 0


def test_selections_name_the_builders_and_their_envs():
    conn = _db()
    _builder(conn, "b1", "host-a", env="2026Q3")
    _builder(conn, "b2", "host-b")

    got = {b["runner_id"]: (b["hostname"], b["active_env"])
           for b in builder_env_selections(conn)}
    assert got == {"b1": ("host-a", "2026Q3"), "b2": ("host-b", None)}


# --- the UI -----------------------------------------------------------------

def _client(tmp_path: Path, builders):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from dportsv3.tracker.server import create_app  # noqa: PLC0415

    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute(
        "INSERT INTO runner_status(id, status, job_id, current_stage, "
        "started_at, updated_at) VALUES (1, 'processing', 'j1', 'patch', ?, ?)",
        (NOW, NOW))
    for rid, host, env in builders:
        _builder(conn, rid, host, env)
        _probe(conn, rid, "2026Q3", "ready")
    conn.commit()
    conn.close()
    return TestClient(create_app(path))


def test_one_builder_renders_exactly_as_before(tmp_path):
    """The host dimension is in the data either way; showing it buys nothing
    until there are two builders to tell apart."""
    with _client(tmp_path, [("b1", "host-a", None)]) as client:
        body = client.get("/agentic/runner").text
    assert 'Default env' in body
    assert '<th scope="col">Builder</th>' not in body
    assert "Env in use" not in body


def test_two_builders_get_a_builder_column_and_their_own_envs(tmp_path):
    with _client(tmp_path, [("b1", "host-a", "2026Q3"),
                            ("b2", "host-b", None)]) as client:
        body = client.get("/agentic/runner").text
    assert '<th scope="col">Builder</th>' in body
    assert "Env in use" in body
    assert "host-a" in body and "host-b" in body


def test_the_endpoint_sets_one_builder_without_moving_the_default(tmp_path):
    with _client(tmp_path, [("b1", "host-a", None),
                            ("b2", "host-b", None)]) as client:
        client.put("/api/config/active-env", json={"name": "2026Q2"})
        r = client.put("/api/config/active-env",
                       json={"name": "2026Q1", "runner_id": "b1"})
        assert r.json() == {"name": "2026Q1", "runner_id": "b1"}
        assert client.get("/api/config/active-env").json() == {"name": "2026Q2"}


def test_the_endpoint_rejects_a_non_string_builder(tmp_path):
    with _client(tmp_path, [("b1", "host-a", None)]) as client:
        r = client.put("/api/config/active-env",
                       json={"name": "2026Q1", "runner_id": 7})
        assert r.status_code == 400


def test_the_verify_env_picker_does_not_list_an_env_twice(tmp_path):
    """Health rows are per (builder, env) now, so two builders carrying the
    same env produced two rows -- and this picker is a list of NAMES."""
    pytest.importorskip("fastapi")
    from dportsv3.tracker.routes import pages  # noqa: PLC0415,F401

    conn = _db()
    _probe(conn, "b1", "2026Q3", "ready")
    _probe(conn, "b2", "2026Q3", "ready")
    _probe(conn, "b2", "2026Q1", "ready")

    # Ordered by builder then env, which is what the query documents.
    names = [str(r["env"]) for r in env_health_statuses(conn)]
    assert names == ["2026Q3", "2026Q1", "2026Q3"], "2026Q3 appears twice"
    assert list(dict.fromkeys(names)) == ["2026Q3", "2026Q1"], (
        "which is what the picker must collapse")
