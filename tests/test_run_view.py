"""The run view: one build run, live.

Serves /target/{target} (the newest run there) and /builds/{run_id}. It
replaced the lifted dsynth-progress page, so half of these are about what it
no longer claims: there are no builder slots, no per-port duration and no
load average behind the tracker, and a view that showed those columns empty
was inviting someone to fill them in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.tracker.db import (
    create_build_run,
    enqueue_ports,
    finish_build_run,
    init_db,
    record_results,
    update_port_status,
)
from dportsv3.tracker.server import create_app

TARGET = "@main"
RUN_JS = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
          / "static" / "run.js")


@pytest.fixture
def app_db(tmp_path: Path):
    path = tmp_path / "state.db"
    conn = init_db(str(path))

    old = create_build_run(conn, TARGET, "release", "2026-09-01T08:00:00Z")
    record_results(conn, old, TARGET, [
        {"origin": "devel/alpha", "version": "1.0", "result": "success"},
    ])
    finish_build_run(conn, old, "2026-09-01T09:00:00Z")

    live = create_build_run(conn, TARGET, "release", "2026-09-06T06:00:00Z")
    record_results(conn, live, TARGET, [
        {"origin": "devel/alpha", "version": "1.1", "result": "success"},
        {"origin": "devel/beta", "version": "1.1", "result": "failure"},
    ])
    enqueue_ports(conn, live, [
        {"origin": "www/gamma", "version": "2.0"},
        {"origin": "x11/delta", "version": "3.0"},
    ], total_expected=4)
    update_port_status(conn, live, "x11/delta", "building")
    conn.execute(
        "UPDATE build_runs SET commit_sha = 'abcdef0123456789', "
        "commit_branch = 'main' WHERE id = ?", (live,),
    )
    conn.commit()
    conn.close()

    app = create_app(path)
    with TestClient(app) as client:
        yield client, old, live


# --- the two routes ------------------------------------------------------


def test_a_run_page_names_the_run_it_is_showing(app_db) -> None:
    client, _, live = app_db

    body = client.get(f"/builds/{live}").text

    assert f"Run #{live}" in body
    assert TARGET in body
    assert "release" in body
    assert "abcdef012345" in body          # short sha, not the whole thing
    assert "abcdef0123456789" not in body


def test_a_target_page_resolves_the_newest_run(app_db) -> None:
    """/target/{target} follows whichever run is latest rather than naming
    one, so the header has to resolve it the same way the poll does."""
    client, old, live = app_db

    body = client.get(f"/target/{TARGET}").text

    assert f"Run #{live}" in body
    assert f"Run #{old}" not in body


def test_a_target_with_no_run_says_so_instead_of_an_empty_frame(
    app_db,
) -> None:
    client, _, _ = app_db

    resp = client.get("/target/@2026Q3")

    assert resp.status_code == 200
    assert "Nothing to show" in resp.text
    assert "run.js" not in resp.text      # nothing to poll for


def test_an_unknown_run_id_still_404s(app_db) -> None:
    client, _, _ = app_db

    assert client.get("/builds/99999").status_code == 404


def test_the_summary_carries_the_run_it_describes(app_db) -> None:
    """The target view is showing a header for one run while polling an
    endpoint that follows the newest. run_id is how it notices a swap."""
    client, _, live = app_db

    summary = client.get(f"/api/progress/{TARGET}/summary.json").json()

    assert summary["run_id"] == live


# --- crossing back into Builds -------------------------------------------


def test_the_run_links_back_to_the_same_run_in_builds(app_db) -> None:
    client, _, live = app_db

    body = client.get(f"/builds/{live}").text

    assert f"?run={live}" in body


def test_the_queued_origins_are_a_link_and_not_a_table(app_db) -> None:
    """A run starts with every origin queued, so they are not in this
    payload at all. The dashboard pages them."""
    client, _, live = app_db

    body = client.get(f"/builds/{live}").text

    assert f"?run={live}&amp;state=queued" in body


# --- what it no longer claims --------------------------------------------


@pytest.mark.parametrize("gone", [
    "stats_load", "stats_swapinfo", "stats_pkghour", "stats_impulse",
    "stat-card", "preset-tag", "builders_zone_2", "report_table",
    "Build Phase", "Lines", "Impulse", "Pkg/hr",
])
def test_the_lifted_farm_chrome_is_gone(app_db, gone: str) -> None:
    client, _, live = app_db

    body = client.get(f"/builds/{live}").text

    assert gone not in body


def test_the_page_says_which_numbers_do_not_exist(app_db) -> None:
    """Absent is a statement, not a blank cell. The old page showed Load,
    Swap and Pkg/hr columns filled with "  -" and 0."""
    client, _, live = app_db

    body = " ".join(client.get(f"/builds/{live}").text.split())

    assert "are not recorded by this tracker, so they are not shown" in body


def test_the_client_reads_no_field_the_tracker_does_not_measure() -> None:
    js = RUN_JS.read_text()

    for field in (".load", ".swapinfo", ".pkghour", ".impulse",
                  ".phase", ".lines", ".duration", ".elapsed_row"):
        assert field not in js, field


def test_the_client_builds_no_html_from_data() -> None:
    """Origins, versions and bundle ids are database values. The lifted
    client concatenated them into innerHTML with a hand-rolled escaper."""
    js = re.sub(r"/\*.*?\*/", "", RUN_JS.read_text(), flags=re.S)

    assert "innerHTML" not in js
    assert "textContent" in js


def test_the_client_stops_at_kfiles() -> None:
    """The chunk count is the server's; fetching past it walks off the end
    of the run forever."""
    js = RUN_JS.read_text()
    start = js.index("function loadChunks(")
    body = js[start:js.index("\n  }", start)]

    assert "loaded >= kfiles" in body
    assert "k <= kfiles" in body


def test_a_missing_chunk_stops_the_accumulation_rather_than_skipping_it() -> None:
    """A gap means a chunk is not written yet. Continuing past it would put
    later rows in before earlier ones and never come back for them."""
    js = RUN_JS.read_text()
    start = js.index("function loadChunks(")
    body = js[start:js.index("\n  }", start)]

    assert "break" in body


# --- escaping ------------------------------------------------------------


def test_a_target_name_is_escaped_in_the_empty_state(app_db) -> None:
    """The path segment is echoed into the heading. A slash would not route
    here at all, so the payload that matters is the one without one."""
    client, _, _ = app_db

    body = client.get("/target/@main<script>alert(1)").text

    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;" in body
