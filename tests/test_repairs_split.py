"""Repairs is one view, not two pages (M4).

The mock is a two-pane cockpit: a work queue on the left, the selected
occurrence on the right, both on screen the whole time. What was built was
a full-width worklist and a separate cockpit page, so picking an issue
meant leaving the queue behind -- and going back to it meant losing your
place.

The contents matched closely all along. This is about the interaction.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.server import create_app

TARGET = "@main"
TEMPLATES = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
             / "templates")


def _seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO runs(run_id, target) VALUES ('r-1', ?)", (TARGET,))
    rows = [
        ("i-ready", "agent_fixed", "verified"),
        ("i-decide", "triage_failed", None),
        ("i-owned", "operator_owned", None),
    ]
    for key, resolution, verification in rows:
        db.execute(
            "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
            "first_seen_at, last_seen_at, updated_at) VALUES (?, ?, ?, "
            "'unresolved', 1, 't0', 't1', 't1')",
            (key, TARGET, f"devel/{key}"))
        db.execute(
            "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
            "target, issue_key, resolution, verification_status) VALUES "
            "(?, 'r-1', ?, 't1', 'failure', ?, ?, ?, ?)",
            (f"b-{key}", f"devel/{key}", TARGET, key, resolution,
             verification))
    db.commit()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db)
    db.close()
    with TestClient(create_app(path)) as test_client:
        yield test_client


@pytest.fixture
def empty(tmp_path: Path) -> TestClient:
    path = tmp_path / "blank.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.close()
    with TestClient(create_app(path)) as test_client:
        yield test_client


def _get(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    return resp.text


def _flat(body: str) -> str:
    return re.sub(r"\s+", " ", body)


# --- both halves on screen ------------------------------------------------


def test_the_queue_and_the_workspace_are_one_view(client) -> None:
    body = _get(client, "/agentic")

    assert 'class="repairs-split' in body
    assert 'class="repair-rail"' in body
    assert 'class="repair-detail"' in body
    # ...and the rail comes first, because it is what you navigate from.
    assert body.index('class="repair-rail"') < body.index('class="repair-detail"')


def test_the_workspace_is_the_cockpit_and_not_a_second_copy(client) -> None:
    """One partial, rendered in two places. Two assemblies would drift,
    and the one that drifted would be the one nobody was looking at."""
    split = _get(client, "/agentic")
    alone = _get(client, "/agentic/bundles/b-i-ready")

    for marker in ("cockpit-tabs", "occ-facts", 'data-panel="evidence"'):
        assert marker in split, marker
        assert marker in alone, marker


def test_the_cockpit_is_one_template(client) -> None:
    for name in ("agentic_index.html", "agentic_bundle.html"):
        assert '{% include "_cockpit.html" %}' in (TEMPLATES / name).read_text()


# --- selection is a URL ---------------------------------------------------


def test_selecting_an_occurrence_is_a_url(client) -> None:
    """Linkable, survives a reload, and every queue row is a plain link
    that works with nothing enabled."""
    body = _get(client, "/agentic")

    assert 'href="?occ=b-i-decide"' in body


def test_the_url_picks_the_pane(client) -> None:
    body = _flat(_get(client, "/agentic?occ=b-i-owned"))

    assert "devel/i-owned" in body
    assert "b-i-owned" in body


def test_an_issue_can_be_named_instead_of_an_occurrence(client) -> None:
    """A link that knows the problem but not which attempt still lands on
    the newest one."""
    body = _flat(_get(client, "/agentic?issue=i-ready"))

    assert "b-i-ready" in body


def test_a_stale_link_lands_on_the_queue_rather_than_an_error(client) -> None:
    """The queue is the view; the right pane is a selection within it. A
    dead occurrence id should not 404 the page that would let you find a
    live one."""
    resp = client.get("/agentic?occ=does-not-exist")

    assert resp.status_code == 200
    assert 'class="repair-rail"' in resp.text


def test_the_selected_row_is_marked_in_the_queue(client) -> None:
    """Both halves on screen is only useful if the left one says which
    row the right one is showing."""
    body = _get(client, "/agentic?occ=b-i-owned")
    rail = body[body.index('class="repair-rail"'):body.index('class="repair-detail"')]

    assert "current" in rail


def test_arriving_with_no_selection_still_fills_the_pane(client) -> None:
    """An empty right pane beside a full queue is a worse first screen
    than either half alone, so the queue's own first row is chosen."""
    body = _get(client, "/agentic")

    assert 'class="repair-detail"' in body
    assert "occ-facts" in body


def test_an_empty_tracker_shows_the_queue_alone(empty) -> None:
    """Nothing to select, so no pane -- and the split collapses rather
    than leaving a hole where the workspace would be."""
    body = _get(empty, "/agentic")

    assert "repairs-split solo" in body
    assert 'class="repair-detail"' not in body


# --- the standalone page is kept ------------------------------------------


def test_the_occurrence_page_still_stands_alone(client) -> None:
    """Builds links here, the pipeline drawer links here, and so does
    anything else that knows a bundle and not an issue."""
    resp = client.get("/agentic/bundles/b-i-ready")

    assert resp.status_code == 200
    assert 'class="repairs-split' not in resp.text


def test_an_unknown_occurrence_still_404s_on_its_own_page(client) -> None:
    """Different from the split: there, the id is a selection; here it is
    the resource."""
    assert client.get("/agentic/bundles/nope").status_code == 404


# --- one h1, and the audience ---------------------------------------------


def test_the_split_has_one_heading(client) -> None:
    """The rail's "Repairs" is the page's heading; the occurrence is a
    section within it (UI-7)."""
    body = _get(client, "/agentic")

    assert body.count("<h1") == 1


def test_the_standalone_page_still_leads_with_the_occurrence(client) -> None:
    body = _get(client, "/agentic/bundles/b-i-ready")

    assert body.count("<h1") == 1
    assert 'class="occ-h1"' in body


def test_a_reader_is_told_why_the_actions_are_missing(
    client, tmp_path: Path, set_setting,
) -> None:
    """Not a page with a hole in it. The action surface is one separable
    region, which is what makes it omittable; saying so is the other
    half."""
    operator = _get(client, "/agentic/bundles/b-i-ready")
    assert "Operator actions</strong>" in operator

    set_setting("tracker.public_readonly", True)
    path = tmp_path / "state.db"
    with TestClient(create_app(path)) as reader:
        anon = _flat(_get(reader, "/agentic/bundles/b-i-ready"))

    assert "Operator actions hidden" in anon
    assert "Accept, reject, retry, take over and discard" in anon


# --- responsive -----------------------------------------------------------


def test_the_split_becomes_one_column_before_it_stops_fitting() -> None:
    """372px of rail plus a workspace does not fit on a laptop in a
    half-screen window, let alone a phone."""
    css = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
           / "static" / "progress.css").read_text()
    block = css[css.index("/* --- The Repairs split (M4)"):]
    block = block[:block.index("/* --- Repairs, narrow")]

    assert "@media (max-width: 1100px)" in block
    assert "grid-template-columns: minmax(0, 1fr)" in block


def test_each_half_scrolls_on_its_own() -> None:
    """The frame gives one scrolling region; a split inside it has to
    subdivide that or the queue scrolls away from the work."""
    css = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
           / "static" / "progress.css").read_text()
    block = css[css.index("/* --- The Repairs split (M4)"):]
    block = block[:block.index("/* --- Repairs, narrow")]

    assert block.count("overflow-y: auto") >= 2
