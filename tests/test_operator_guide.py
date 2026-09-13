"""The operator guide explains the model the UI only implied (poly-9u7).

Issue, occurrence and job are three different things with three different
state machines; `regressed` is derived on read and never stored; the
worklist hides in-progress work on purpose. All of it was written down only
in the docstrings of issue_state.py, fix_state.py and fingerprint.py.

The part worth guarding is chapter 6. It is generated from the same
projection the pages use, and the whole point of generating it is that a
hand-written copy is the one that goes stale.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker import fix_state
from dportsv3.tracker.server import create_app

ROOT = Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.close()
    with TestClient(create_app(path)) as test_client:
        yield test_client


# --- chapter 6 is generated ------------------------------------------------


def test_every_status_the_projection_can_produce_is_documented() -> None:
    """One row per distinct FixStatus. A status the guide omits is one an
    operator meets with no explanation."""
    rows = fix_state.status_matrix()
    keys = {r["key"] for r in rows}

    # Every fixed-mapping resolution...
    for status in fix_state._RESOLUTION_STATUS.values():
        assert status.key in keys, status.key
    # ...plus the ones that depend on verification, and the two NULL cases.
    for key in ("verified", "verify_failed", "needs_review",
                "owned_verified", "operator_owned",
                "in_progress", "unknown"):
        assert key in keys, key


def test_every_documented_status_carries_the_band_the_worklist_gives_it(
) -> None:
    """The band is worklist_bucket's answer, not a second opinion."""
    for row in fix_state.status_matrix():
        bucket = fix_state._WORKLIST_BUCKET.get(row["key"])
        assert row["bucket"] == bucket, row["key"]


def test_the_one_status_with_no_band_is_the_one_the_queue_hides() -> None:
    """A job holds it and the operator has nothing to do, which is a
    deliberate omission and reads as a hole unless the guide says so."""
    hidden = [r for r in fix_state.status_matrix() if r["band"] is None]

    assert [r["key"] for r in hidden] == ["in_progress"]
    assert hidden[0]["actions"] == []


def test_accept_is_documented_as_drawn_but_dead_before_verification() -> None:
    """The button renders on the agent-fixed lane so the path is visible,
    and nothing on the page has ever said why it is disabled -- which is
    the confusion this chapter exists for."""
    rows = {r["key"]: r for r in fix_state.status_matrix()}

    assert rows["needs_review"]["shown_disabled"] == ["Accept"]
    assert "Accept" not in rows["needs_review"]["actions"]
    # ...and once verified it is a real action.
    assert "Accept" in rows["verified"]["actions"]
    assert rows["verified"]["shown_disabled"] == []


def test_the_actions_are_the_gates_and_not_a_second_list() -> None:
    """Generated from bundle_actions, so the guide cannot promise a button
    the state machine refuses."""
    for row in fix_state.status_matrix():
        if "Reopen" in row["actions"]:
            assert row["bucket"] == "done", row["key"]
        if row["key"] == "operator_owned":
            assert "Release" in row["actions"]


# --- it is reachable -------------------------------------------------------


def test_the_guide_ships_with_every_page(client) -> None:
    """In the shell rather than on one page, so the first-visit open
    happens wherever an operator lands."""
    body = client.get("/agentic").text

    assert 'id="tour"' in body
    assert "tour.js" in body


def test_there_is_a_way_back_into_it(client) -> None:
    """It opens once on a first visit; without a trigger the model it
    explains would be unreachable afterwards."""
    body = client.get("/agentic").text

    assert 'id="open-help"' in body


def test_the_generated_chapter_reaches_the_page(client) -> None:
    body = client.get("/agentic").text

    for row in fix_state.status_matrix():
        assert row["label"] in body, row["label"]


def test_the_first_visit_key_is_versioned() -> None:
    """Bumping it is how every operator gets the guide again when the
    model changes."""
    js = (ROOT / "static" / "tour.js").read_text()

    assert "dportsv3.tour.v1" in js


def test_the_guide_needs_no_font_cdn() -> None:
    """It runs on a build host. The design demo asked for two webfonts;
    they are dropped for their own fallbacks."""
    css = (ROOT / "static" / "progress.css").read_text()
    guide = css[css.index("/* --- The operator guide"):]

    assert "fonts.googleapis" not in guide
    assert "Georgia" in guide


def test_the_guide_is_painted_in_tokens_not_literals() -> None:
    """The demo predates the palette migration and hardcoded eight
    colours, which would have pinned the guide to one theme."""
    import re

    css = (ROOT / "static" / "progress.css").read_text()
    guide = css[css.index("/* --- The operator guide"):]
    literals = set(re.findall(r"#[0-9a-fA-F]{3,6}\b", guide))

    assert literals == set()
