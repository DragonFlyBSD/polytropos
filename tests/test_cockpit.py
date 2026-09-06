"""The Repairs cockpit: one occurrence, and everything recorded about it.

UI-6 turned a 185-line vertical stack into a header, an occurrence selector
and four surfaces. What it must not do is lose anything: the session viewer,
chat, tool trace, token accounting, verification and delivery state all
still render, and every control still comes from fix_state.
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
ORIGIN = "devel/alpha"


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO runs(run_id, target, build_run_id) "
                 "VALUES ('r-1', ?, 7)", (TARGET,))
    conn.execute("INSERT INTO build_runs(id, target, build_type, started_at) "
                 "VALUES (7, ?, 'release', 't')", (TARGET,))
    conn.execute(
        "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
        "times_seen, first_seen_at, last_seen_at, updated_at) "
        "VALUES ('i-1', ?, ?, 'deadbeef12345678', 'unresolved', 3, "
        "'2026-09-01T00:00:00Z', '2026-09-05T00:00:00Z', 't')",
        (TARGET, ORIGIN),
    )
    for n, resolution in enumerate(
        ("agent_gave_up", "agent_gave_up", "agent_fixed")
    ):
        conn.execute(
            "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
            "issue_key, result, resolution, error_signature) "
            "VALUES (?, 'r-1', ?, ?, ?, 'i-1', 'failure', ?, 'deadbeef12345678')",
            (f"b-{n}", ORIGIN, TARGET, f"2026-09-0{n + 1}T00:00:00Z",
             resolution),
        )
    # The newest occurrence got furthest; the middle one died early.
    conn.execute(
        "INSERT INTO jobs(job_id, state, type, origin, target, bundle_id, "
        "created_ts_utc) VALUES ('j-2', 'done', 'patch', ?, ?, 'b-2', 't')",
        (ORIGIN, TARGET),
    )
    for n, state in enumerate(
        ("queued", "claimed", "triaging", "triaged", "patching", "done")
    ):
        conn.execute(
            "INSERT INTO job_events(ts, job_id, to_state, event_name) "
            "VALUES (?, 'j-2', ?, 'x')",
            (f"2026-09-03T00:0{n}:00Z", state),
        )
    conn.execute(
        "INSERT INTO jobs(job_id, state, type, origin, target, bundle_id, "
        "created_ts_utc) VALUES ('j-1', 'dead', 'patch', ?, ?, 'b-1', 't')",
        (ORIGIN, TARGET),
    )
    for n, state in enumerate(("queued", "claimed", "dead")):
        conn.execute(
            "INSERT INTO job_events(ts, job_id, to_state, event_name) "
            "VALUES (?, 'j-1', ?, 'x')",
            (f"2026-09-02T00:0{n}:00Z", state),
        )
    conn.commit()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db)
    db.close()
    app = create_app(path)
    with TestClient(app) as test_client:
        yield test_client


def _flat(client, bundle_id="b-2") -> str:
    return " ".join(client.get(f"/agentic/bundles/{bundle_id}").text.split())


# --- the header ----------------------------------------------------------


def test_the_header_names_the_issue_this_occurrence_belongs_to(client) -> None:
    body = _flat(client)

    assert ORIGIN in body
    assert "seen ×3" in body
    assert "Open issue" in body


def test_the_header_carries_the_fingerprint(client) -> None:
    """The fingerprint IS the issue's identity -- two failures with the
    same one are the same problem."""
    assert "fp deadbeef12345678" in _flat(client)


def test_the_header_links_the_build_the_failure_came_out_of(client) -> None:
    """build_run_id lives on `runs`, not on the bundle, so the read has to
    join for it -- and without it there is no way back to the build."""
    body = _flat(client)

    assert "build #7" in body
    assert "?run=7" in body


def test_the_header_links_the_ports_own_history(client) -> None:
    assert f"/target/{TARGET}/devel/alpha" in _flat(client)


# --- the occurrence selector ---------------------------------------------


def test_the_selector_lists_every_occurrence_including_this_one(
    client,
) -> None:
    """A table of the OTHERS cannot answer "which of these am I looking
    at", which is the question a selector exists for."""
    body = _flat(client)
    selector = body.split("Occurrences of this issue", 1)[1].split(
        "</details>", 1)[0]

    for bundle_id in ("b-0", "b-1", "b-2"):
        assert bundle_id in selector
    assert 'aria-current="page"' in selector


def test_the_selector_says_how_far_each_occurrence_got(client) -> None:
    selector = _flat(client).split("Occurrences of this issue", 1)[1]

    assert "reached patching" in selector
    assert "reached claimed" in selector
    assert "no jobs" in selector      # the one nothing ran on


def test_the_furthest_attempt_is_marked(client) -> None:
    """It is usually the one worth reading, and it is not always the
    newest."""
    selector = _flat(client).split("Occurrences of this issue", 1)[1].split(
        "</details>", 1)[0]

    assert selector.count(">furthest</span>") == 1
    # b-2 got to patching; b-1 only to claimed.
    before_mark = selector.split(">furthest</span>", 1)[0]
    assert "b-2" in before_mark
    assert "reached patching" in before_mark


def test_a_lone_occurrence_is_not_marked_furthest(tmp_path: Path) -> None:
    """"Furthest of one" says nothing."""
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
        "result) VALUES ('only', 'r-1', 'devel/lone', ?, 't', 'failure')",
        (TARGET,),
    )
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = client.get("/agentic/bundles/only").text

    assert ">furthest</span>" not in body


# --- the four surfaces ---------------------------------------------------


def test_the_page_offers_four_surfaces(client) -> None:
    body = client.get("/agentic/bundles/b-2").text
    tabs = re.findall(r'class="cockpit-tab[^"]*" data-panel="(\w+)"', body)

    assert tabs == ["fix", "evidence", "chat", "history"]


def test_the_session_viewer_is_not_a_tab(client) -> None:
    """A bundle whose run had session dumping off would offer a dead one.
    It is reached from the Evidence reader instead."""
    body = client.get("/agentic/bundles/b-2").text

    assert 'data-panel="session"' not in body


def test_every_panel_renders_so_the_page_works_without_javascript(
    client,
) -> None:
    """The tabs are progressive enhancement: the markup is what this page
    always was, a stack of sections, and JS only hides the inactive ones."""
    body = client.get("/agentic/bundles/b-2").text
    panels = re.findall(r'class="cockpit-panel" data-panel="(\w+)"', body)

    assert set(panels) == {"fix", "evidence", "chat", "history"}


def test_an_anonymous_reader_gets_no_chat_tab(tmp_path: Path, monkeypatch):
    """The chat reasons over the agent's own session dump -- operator
    material, not build status -- so the tab goes with the panel."""
    from dportsv3 import settings

    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db)
    db.close()

    real = settings.get
    monkeypatch.setattr(
        settings, "get",
        lambda key, *a, **k: True if key == "tracker.public_readonly"
        else real(key, *a, **k),
    )
    with TestClient(create_app(path)) as client:
        body = client.get("/agentic/bundles/b-2").text

    assert 'data-panel="chat"' not in body
    assert 'data-panel="evidence"' in body


def test_an_occurrence_with_nothing_to_decide_says_so(client) -> None:
    """b-0 is agent_gave_up with no metadata problem, so it HAS actions.
    b-2's terminal siblings are the interesting case -- but either way the
    Fix panel must never be silently empty."""
    body = client.get("/agentic/bundles/b-0").text

    assert 'data-panel="fix"' in body
    assert ("Operator actions" in body) or ("Nothing to decide here" in body)


# --- the reader ----------------------------------------------------------


def test_the_artifact_reader_has_fixed_geometry() -> None:
    """Switching between a 2 KB diff and a 4 MB log must not reflow the
    frame. The reader is a fixed-height grid that scrolls internally."""
    css = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
           / "static" / "progress.css").read_text()
    block = css[css.index(".reader {"):css.index("}", css.index(".reader {"))]

    assert "height:" in block
    assert "overflow: hidden" in block
