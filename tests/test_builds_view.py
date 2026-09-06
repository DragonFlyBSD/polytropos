"""The Builds view: the landing dashboard and the pages under it.

Builds is the default landing surface and the only view an anonymous reader
is likely to open, so these cover what it says about a run rather than only
that it returns 200: which run is selected, what the state chips count, what
the origin table pages through, and where a failure links to.
"""

from __future__ import annotations

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


@pytest.fixture
def app_db(tmp_path: Path):
    path = tmp_path / "state.db"
    conn = init_db(str(path))

    done = create_build_run(conn, TARGET, "release", "2026-09-01T08:00:00Z")
    record_results(conn, done, TARGET, [
        {"origin": "devel/alpha", "version": "1.0", "result": "success",
         "recorded_at": "2026-09-01T08:10:00Z"},
        {"origin": "devel/beta", "version": "1.0", "result": "failure",
         "recorded_at": "2026-09-01T08:20:00Z"},
        {"origin": "www/gamma", "version": "2.0", "result": "success",
         "recorded_at": "2026-09-01T08:30:00Z"},
    ])
    finish_build_run(conn, done, "2026-09-01T09:00:00Z")

    live = create_build_run(conn, TARGET, "release", "2026-09-06T06:00:00Z")
    record_results(conn, live, TARGET, [
        {"origin": "devel/alpha", "version": "1.1", "result": "success",
         "recorded_at": "2026-09-06T06:10:00Z"},
        {"origin": "devel/beta", "version": "1.1", "result": "failure",
         "recorded_at": "2026-09-06T06:20:00Z"},
        {"origin": "lang/delta", "version": "3.0", "result": "skipped",
         "recorded_at": "2026-09-06T06:30:00Z"},
    ])
    enqueue_ports(conn, live, [
        {"origin": "www/gamma", "version": "2.1"},
        {"origin": "x11/epsilon", "version": "4.0"},
    ], total_expected=5)
    update_port_status(conn, live, "x11/epsilon", "building")

    conn.execute(
        "INSERT INTO runs(run_id, target, build_run_id) VALUES ('r-1', ?, ?)",
        (TARGET, live),
    )
    conn.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, issue_key) "
        "VALUES ('b-1', 'r-1', 'devel/beta', ?, '2026-09-06T06:21:00Z', 'i-1')",
        (TARGET,),
    )
    conn.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "first_seen_at, last_seen_at, updated_at) VALUES "
        "('i-1', ?, 'devel/beta', 'unresolved', 2, '2026-09-01T08:20:00Z', "
        "'2026-09-06T06:20:00Z', '2026-09-06T06:20:00Z')",
        (TARGET,),
    )
    conn.commit()
    conn.close()

    app = create_app(path)
    with TestClient(app) as client:
        yield client, done, live


# --- the landing surface -------------------------------------------------


def test_root_serves_builds_not_targets(app_db) -> None:
    client, _, _ = app_db

    body = client.get("/").text

    assert "<h1>Builds</h1>" in body
    assert "Active builds" in body
    assert "<h1>Targets</h1>" not in body


def test_the_target_rollup_is_still_reachable(app_db) -> None:
    """/ took Builds; the cumulative per-target view moved rather than went."""
    client, _, _ = app_db

    body = client.get("/targets").text

    assert "<h1>Targets</h1>" in body
    assert TARGET in body


def test_the_running_build_is_selected_by_default(app_db) -> None:
    client, done, live = app_db

    body = client.get("/").text

    assert f"Run #{live}" in body
    assert f"Run #{done}" not in body


def test_a_run_can_be_selected_explicitly(app_db) -> None:
    client, done, live = app_db

    body = client.get("/", params={"run": done}).text

    assert f"Run #{done}" in body
    assert "www/gamma" in body


def test_an_unknown_run_is_a_404_not_a_silent_fallback(app_db) -> None:
    client, _, _ = app_db

    assert client.get("/", params={"run": 4242}).status_code == 404


# --- the origin table ----------------------------------------------------


def test_the_state_chips_carry_the_runs_own_counts(app_db) -> None:
    client, _, live = app_db

    body = client.get("/").text

    assert 'All<span class="n">5</span>' in body
    assert 'Failed<span class="n">1</span>' in body
    assert 'Building<span class="n">1</span>' in body
    assert 'Queued<span class="n">1</span>' in body


def test_a_state_the_run_never_produced_gets_no_chip(app_db) -> None:
    """No run here ignored anything, so there is no "Ignored 0" to click."""
    client, _, _ = app_db

    assert "Ignored<span" not in client.get("/").text


def test_the_state_filter_narrows_the_table(app_db) -> None:
    client, _, _ = app_db

    body = client.get("/", params={"state": "failure"}).text

    assert "devel/beta" in body
    assert "devel/alpha" not in body
    assert 'class="result-filter active"' in body


def test_an_unknown_state_is_rejected(app_db) -> None:
    client, _, _ = app_db

    resp = client.get("/", params={"state": "recorded"})

    assert resp.status_code == 400
    assert "Invalid build result state" in resp.json()["detail"]


def test_search_narrows_the_table_and_stays_in_the_box(app_db) -> None:
    client, _, _ = app_db

    body = client.get("/", params={"q": "devel/"}).text

    assert "devel/alpha" in body
    assert "lang/delta" not in body
    assert 'name="q" value="devel/"' in body


def test_a_queued_origin_reports_queued_and_not_a_result(app_db) -> None:
    """enqueue_ports leaves result empty; the table must show the status it
    is actually in rather than a blank pill."""
    client, _, _ = app_db

    body = client.get("/", params={"state": "queued"}).text

    assert '<span class="pill neutral">queued</span>' in body
    assert "www/gamma" in body


def test_a_failure_links_to_the_evidence_it_produced(app_db) -> None:
    client, _, _ = app_db

    body = client.get("/", params={"state": "failure"}).text

    assert "/agentic/bundles/b-1" in body


def test_a_success_offers_no_repair_link(app_db) -> None:
    client, _, _ = app_db

    body = client.get("/", params={"state": "success"}).text

    assert "Open repair" not in body


def test_an_origin_links_to_its_port_page(app_db) -> None:
    client, _, _ = app_db

    body = client.get("/").text

    assert f"/target/{TARGET}/devel/alpha" in body


def test_paging_carries_the_filter_it_was_paging_through(
    app_db, monkeypatch,
) -> None:
    """Next must not quietly drop the state filter -- page 2 of "failures"
    has to still be failures."""
    import dportsv3.tracker.routes.pages as pages_mod

    monkeypatch.setattr(pages_mod, "_ORIGIN_PAGE", 1)
    client, done, _ = app_db

    body = client.get("/", params={"run": done, "state": "success"}).text

    assert "1–1 of 2" in body
    assert "state=success" in body
    assert f"run={done}" in body
    assert "page=2" in body


def test_the_pager_appears_only_when_a_page_is_not_the_whole_run(
    app_db, monkeypatch,
) -> None:
    import dportsv3.tracker.routes.pages as pages_mod

    monkeypatch.setattr(pages_mod, "_ORIGIN_PAGE", 2)
    client, _, live = app_db

    first = client.get("/", params={"run": live}).text
    assert 'class="pager"' in first
    assert "1–2 of 5" in first

    second = client.get("/", params={"run": live, "page": 3}).text
    assert "5–5 of 5" in second


# --- the pages under Builds ----------------------------------------------


def test_history_lists_every_run_with_a_compare_link(app_db) -> None:
    client, done, live = app_db

    body = client.get("/builds").text

    assert f"#{done}" in body and f"#{live}" in body
    assert f"a={done}&amp;b={live}" in body


def test_compare_with_no_runs_chosen_renders_the_picker(app_db) -> None:
    """The section nav reaches this page with nothing selected; requiring a
    and b would make that link a 422."""
    client, _, _ = app_db

    resp = client.get("/builds/compare")

    assert resp.status_code == 200
    assert "Pick two runs" in resp.text


def test_compare_names_the_run_a_regression_broke_in(app_db) -> None:
    client, done, live = app_db

    body = client.get("/builds/compare", params={"a": done, "b": live}).text

    assert f"Broke in #{live}" in body
    assert f"Fixed in #{live}" in body


def test_target_diff_with_no_targets_chosen_renders_the_picker(app_db) -> None:
    client, _, _ = app_db

    resp = client.get("/diff")

    assert resp.status_code == 200
    assert "Pick two targets" in resp.text


def test_port_detail_links_the_repair_issue_for_that_origin(app_db) -> None:
    client, _, _ = app_db

    body = client.get(f"/target/{TARGET}/devel/beta").text

    assert "Open repair issue" in body
    assert "/agentic/issues/i-1" in body


def test_port_detail_says_never_succeeded_rather_than_showing_a_green_pill(
    app_db,
) -> None:
    client, _, _ = app_db

    body = client.get(f"/target/{TARGET}/devel/beta").text

    assert "never succeeded" in body


def test_port_detail_without_an_issue_offers_no_repair_link(app_db) -> None:
    client, _, _ = app_db

    body = client.get(f"/target/{TARGET}/devel/alpha").text

    assert "Open repair issue" not in body


# --- audience and escaping -----------------------------------------------


def test_builds_renders_the_same_for_an_anonymous_reader(
    tmp_path: Path, monkeypatch,
) -> None:
    """Builds is status, not action: nothing on it is operator-only, so
    public_readonly must not change what it says."""
    from dportsv3 import settings

    path = tmp_path / "state.db"
    conn = init_db(str(path))
    run = create_build_run(conn, TARGET, "release", "2026-09-01T08:00:00Z")
    record_results(conn, run, TARGET, [
        {"origin": "devel/alpha", "version": "1.0", "result": "failure"},
    ])
    conn.commit()
    conn.close()

    from dportsv3.tracker.routes._common import can_operate

    real = settings.get

    def render(readonly: bool) -> str:
        monkeypatch.setattr(
            settings, "get",
            lambda key, *a, **k: readonly
            if key == "tracker.public_readonly" else real(key, *a, **k),
        )
        # Without this the two renders are identical for the boring reason
        # that the setting was never actually switched.
        assert can_operate() is (not readonly)
        with TestClient(create_app(path)) as client:
            return client.get("/").text

    assert render(True) == render(False)


def test_an_origin_is_escaped_not_interpolated(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    conn = init_db(str(path))
    run = create_build_run(conn, TARGET, "release", "2026-09-01T08:00:00Z")
    record_results(conn, run, TARGET, [
        {"origin": "<script>x</script>/evil", "version": "1.0",
         "result": "failure"},
    ])
    conn.commit()
    conn.close()

    with TestClient(create_app(path)) as client:
        body = client.get("/").text

    assert "<script>x</script>/evil" not in body
    assert "&lt;script&gt;" in body


def test_the_page_stops_reloading_itself_while_someone_is_filtering(
    app_db,
) -> None:
    """The dashboard polls by reloading. Doing that while an operator is two
    pages into a search throws away what they were reading."""
    client, _, _ = app_db

    plain = client.get("/").text
    filtered = client.get("/", params={"state": "failure"}).text

    assert "dpLive" in plain
    assert "polling 30s" in plain
    assert "dpLive" not in filtered
    assert "paused while filtered" in filtered
