"""poly-gzuv: one flavor must never overwrite another's build evidence."""
import json
import shlex
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from dportsv3.db import schema
from dportsv3.tracker import db, progress_adapter
from dportsv3.tracker.server import create_app


def result(flavor, outcome):
    return dict(origin="devel/llvm21", flavor=flavor, version="21", result=outcome)


def test_api_keeps_flavors_separate_through_queue_build_and_result(tmp_path):
    path = tmp_path / "state.db"
    with TestClient(create_app(path)) as client:
        run = client.post("/api/builds", json={"target": "@main", "build_type": "test"}).json()["id"]
        url = f"/api/builds/{run}"
        queued = client.post(url + "/queue", json={"ports": [
            dict(origin="devel/llvm21", flavor=f, version="21") for f in ("lite", "default")
        ]})
        assert queued.status_code == 200, queued.text
        assert queued.json()["queued"] == 2
        for flavor in ("lite", "default"):
            response = client.patch(url + "/ports/devel/llvm21/status", json={"status": "building", "flavor": flavor})
            assert response.status_code == 200, response.text
        response = client.post(url + "/results", json={"results": [result("lite", "success")]})
        assert response.status_code == 200, response.text
        run_data = client.get(url).json()
        assert run_data["build_run"]["building_count"] == 1
        assert run_data["build_run"]["success_count"] == 1
        conn = db.open_db(path)
        assert progress_adapter.run_summary(conn, run)["builders"][0]["origin"] == "devel/llvm21@default"
        conn.close()
        assert client.post(url + "/results", json={"results": [result("default", "failure")]}).status_code == 200
        # Duplicate delivery updates the same flavor, without inflating counts.
        client.post(url + "/results", json={"results": [result("lite", "success")]})
        rows = client.get(url + "/results").json()["results"]
        assert {r["flavor"]: r["result"] for r in rows} == {"lite": "success", "default": "failure"}
        statuses = client.get("/api/status?target=@main").json()
        assert {r["flavor"]: r["last_attempt_result"] for r in statuses} == {"lite": "success", "default": "failure"}
        page = client.get(f"/?run={run}").text
        assert "devel/llvm21@lite" in page and "devel/llvm21@default" in page
        page = client.get("/target/@main/devel/llvm21")
        assert page.status_code == 200
        assert "lite" in page.text and "default" in page.text
        assert client.get("/target/@main/devel/llvm21@lite").status_code == 200


def test_comparisons_match_origin_and_flavor():
    conn = db.init_db(":memory:")
    a = db.create_build_run(conn, "@main", "test", None)
    b = db.create_build_run(conn, "@2026Q3", "test", None)
    db.record_results(conn, a, "@main", [result("lite", "failure"), result("default", "success")])
    db.record_results(conn, b, "@2026Q3", [result("lite", "success"), result("default", "failure")])
    comparison = db.compare_builds(conn, a, b)
    assert [r["flavor"] for r in comparison["new_successes"]] == ["lite"]
    assert [r["flavor"] for r in comparison["new_failures"]] == ["default"]
    assert len(db.get_diff(conn, "@main", "@2026Q3")["differ"]) == 2
    assert [r["flavor"] for r in db.get_failures(conn, "@main")] == ["lite"]
    assert len(db.get_build_results_page(conn, a, search="@lite")["results"]) == 1
    conn.close()


@pytest.mark.parametrize("raw,flavor", [("", ""), ("devel/llvm21", ""), ("@lite", "lite"), ("lite", "lite")])
def test_dsynth_flavor_normalization(raw, flavor):
    conn = db.init_db(":memory:")
    run = db.create_build_run(conn, "@main", "test", None)
    db.record_results(conn, run, "@main", [result(raw, "success")])
    assert db.get_build_results(conn, run)[0]["flavor"] == flavor
    conn.close()


def test_legacy_migration_preserves_history_and_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    legacy = schema.SCHEMA.replace("    flavor TEXT NOT NULL DEFAULT '',\n", "")
    legacy = legacy.replace("PRIMARY KEY (build_run_id, origin, flavor)", "PRIMARY KEY (build_run_id, origin)")
    legacy = legacy.replace("PRIMARY KEY (target, origin, flavor)", "PRIMARY KEY (target, origin)")
    conn.executescript(legacy)
    conn.execute("INSERT INTO build_types VALUES ('test')")
    conn.execute("INSERT INTO build_runs(id,target,build_type,started_at) VALUES (1,'@main','test','2026-01-01')")
    conn.execute("INSERT INTO build_results VALUES (1,'devel/llvm21','21','failure','log','2026-01-01','failure')")
    conn.execute("INSERT INTO port_status(target,origin,last_attempt_result,last_attempt_run_id) VALUES ('@main','devel/llvm21','failure',1)")
    conn.commit()
    schema.init_db(conn)
    schema.init_db(conn)
    assert db.get_build_results(conn, 1)[0]["flavor"] == ""
    assert db.get_build_results(conn, 1)[0]["log_url"] == "log"
    assert db.get_failures(conn, "@main")[0]["last_attempt_run_id"] == 1
    db.record_results(conn, 1, "@main", [result("lite", "success")])
    assert len(db.get_build_results(conn, 1)) == 2
    assert len(db.get_port_status(conn, "@main")) == 2
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_shell_hooks_send_flavor_for_enqueue_start_and_finish(tmp_path):
    from dports_dev_env.hooks import repo_hook_source
    script = f'''
. {shlex.quote(str(repo_hook_source() / 'hook_common.sh'))}
tracker_should_skip() {{ return 1; }}
tracker_load_config() {{ :; }}
tracker_load_state() {{ :; }}
tracker_log() {{ :; }}
dportsv3_cli() {{
    printf '%s\\n' "$*" >> "$CAPTURE"
    if [ "$2" = enqueue-ports ]; then
        while [ "$1" != --file ]; do shift; done
        cat "$2" >> "$CAPTURE"
    fi
}}
ORIGIN=devel/llvm21
FLAVOR=lite
PKGNAME=llvm21-21.pkg
PROFILE=test
RUN_ID=1
DPORTSV3_TRACKER_URL=http://unused
DPORTSV3_TRACKER_STATE_DIR={shlex.quote(str(tmp_path))}
CAPTURE={shlex.quote(str(tmp_path / 'calls'))}
tracker_mark_building
tracker_record_result success
'''
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    queued = json.loads(next(line for line in calls if line.startswith("[{")))
    assert queued[0]["flavor"] == "lite"
    for command in ("mark-building", "record-result"):
        assert "--flavor lite" in next(line for line in calls if command in line)


def test_each_failed_flavor_links_its_own_evidence():
    conn = db.init_db(":memory:")
    run = db.create_build_run(conn, "@main", "test", None)
    flavors = ("jdk", "jre", "headless", "jre_headless")
    db.record_results(conn, run, "@main", [result(f, "failure") for f in flavors])
    conn.execute("INSERT INTO runs(run_id,build_run_id) VALUES ('farm',?)", (run,))
    for flavor in flavors:
        conn.execute(
            "INSERT INTO bundles(bundle_id,run_id,origin,flavor,target,ts_utc) VALUES (?, 'farm', 'devel/llvm21', ?, '@main', '2026-01-01')",
            ("b-" + flavor, flavor),
        )
    conn.commit()
    page = db.get_build_results_page(conn, run)
    assert {r["flavor"]: r["bundle_id"] for r in page["results"]} == {f: "b-" + f for f in flavors}
    assert db.get_build_run(conn, run)["failure_count"] == 4
    assert len(progress_adapter.run_history_chunk(conn, run, 1)) == 4
    conn.close()


def test_migration_rolls_back_both_tables_on_error():
    conn = sqlite3.connect(":memory:")
    # A malformed second table simulates a copy failure after the first table
    # has been replaced. Neither replacement may survive that failure.
    conn.executescript("""
        CREATE TABLE build_results (
            build_run_id INTEGER, origin TEXT, version TEXT, result TEXT,
            log_url TEXT, recorded_at TEXT, status TEXT,
            PRIMARY KEY(build_run_id, origin));
        INSERT INTO build_results VALUES (1,'a/b','1','failure',NULL,'now','failure');
        CREATE TABLE port_status(target TEXT, origin TEXT, unexpected TEXT);
    """)
    with pytest.raises(sqlite3.OperationalError):
        schema._add_build_flavor_dimension(conn)
    assert "flavor" not in {r[1] for r in conn.execute("PRAGMA table_info(build_results)")}
    assert conn.execute("SELECT result FROM build_results").fetchone()[0] == "failure"
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_flavored'").fetchall()
    conn.close()


def test_cli_flavor_reaches_http_payload(monkeypatch):
    from dportsv3.cli import main
    from dportsv3.tracker import client
    calls = []

    def capture(server, path, **kwargs):
        calls.append((path, kwargs["payload"]))
        return {"recorded": 1}

    monkeypatch.setattr(client, "_request_json", capture)
    common = ["--run", "1", "--origin", "devel/llvm21", "--flavor", "lite", "--server", "http://unused"]
    assert main(["tracker", "mark-building", *common]) == 0
    assert main(["tracker", "record-result", *common, "--version", "21", "--result", "success"]) == 0
    assert calls[0][1] == {"status": "building", "flavor": "lite"}
    assert calls[1][1]["results"][0]["flavor"] == "lite"
