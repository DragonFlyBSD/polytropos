"""The Runner page shows one runner, and no control it does not have
(poly-0e02.8).

Two facts the redesigned page was going to show had nothing behind them.

The mock puts a Pause button on this page. There is no endpoint and no
writable field: the runner pauses *itself* when its dev-env is broken,
when no env resolves, or while dsynth holds the build lock, and operator
intent has nowhere to live -- runner_status.status is rewritten by the
runner on every tick, so a tracker write there would last one loop. The
decision for this epic is to have no such button, which makes the page's
existing [pause] control -- which stops the page refreshing -- worse than
useless: it sat beside a Status cell that reads "paused" when the runner
pauses itself.

The second is identity. `runners` (runner_id, hostname, pid,
last_heartbeat_at) is read by nothing here, so "heartbeat 3s ago" per
runner is unbacked; poly-fij.3 and poly-fij.6 own that. What IS backed is
the singleton's own heartbeat, and it is what separates a runner working
from a runner that died with `processing` still on the row.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.server import create_app

RUNNER_JS = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
             / "static" / "agentic-runner.js")


def _flat(body: str) -> str:
    """Templates wrap prose, so a sentence to assert on spans lines."""
    return re.sub(r"\s+", " ", body)


def _client(tmp_path: Path, *, status: str, heartbeat_age: int) -> TestClient:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    seen = datetime.now(timezone.utc) - timedelta(seconds=heartbeat_age)
    db.execute(
        "INSERT INTO runner_status(id, status, job_id, current_stage, "
        "started_at, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
        (status, "job-1", "patch_start", "2026-09-06T10:00:00+00:00",
         seen.isoformat()),
    )
    db.commit()
    db.close()
    app = create_app(path)
    return TestClient(app)


@pytest.fixture
def live(tmp_path: Path) -> TestClient:
    with _client(tmp_path, status="processing", heartbeat_age=2) as client:
        yield client


@pytest.fixture
def dead(tmp_path: Path) -> TestClient:
    """A runner killed mid-job: the row still says processing."""
    with _client(tmp_path, status="processing", heartbeat_age=3600) as client:
        yield client


# --- no control the page does not have ------------------------------------


def test_the_page_offers_no_runner_pause(live: TestClient) -> None:
    body = live.get("/agentic/runner").text

    assert "[pause]" not in body
    assert not re.search(r"<button[^>]*>\s*Pause\s*</button>", body, re.I)


def test_the_refresh_control_names_what_it_controls(live: TestClient) -> None:
    """It stops the page refreshing, not the runner, and the label has to
    say which -- the Status cell one row up reads "paused" when the runner
    pauses itself."""
    body = live.get("/agentic/runner").text

    assert "stop refreshing" in _flat(body)
    assert 'id="live-toggle"' in body


def test_the_js_toggle_labels_do_not_read_as_runner_controls() -> None:
    js = RUNNER_JS.read_text()
    labels = re.findall(r'toggle\.textContent = "([^"]*)"', js)

    assert labels
    for label in labels:
        assert "refresh" in label


def test_the_page_says_who_can_pause_the_runner(live: TestClient) -> None:
    """Rather than leaving the absence unexplained: the runner pauses
    itself for three reasons and the stage cell names which."""
    body = live.get("/agentic/runner").text

    assert "no operator pause" in _flat(body).lower()
    assert "dsynth" in body


# --- one runner, not a fleet ----------------------------------------------


def test_the_page_shows_one_runner(live: TestClient) -> None:
    """`runners` and jobs.owner_id exist and nothing reads them, so the
    page must not imply there is more than one."""
    body = live.get("/agentic/runner").text

    assert "single agent queue runner" in _flat(body)
    assert "runner_id" not in body
    assert "hostname" not in body


# --- the heartbeat qualifies the status -----------------------------------


def test_a_live_runners_status_stands_on_its_own(live: TestClient) -> None:
    body = live.get("/agentic/runner").text

    assert "processing" in body
    assert "not running" not in body


def test_a_dead_runner_is_not_still_processing(dead: TestClient) -> None:
    """The status column survives the process. Without the heartbeat read
    a runner killed mid-job reads as working forever."""
    body = dead.get("/agentic/runner").text

    assert "not running" in body
    assert "survives the process dying" in _flat(body)


def test_the_api_carries_the_heartbeat_read(dead: TestClient) -> None:
    """The page refreshes its cells from this payload, so the liveness has
    to travel with the status or it goes stale the moment the poller
    overwrites the server-rendered value."""
    body = dead.get("/api/runner-status").json()

    assert body["status"] == "processing"
    assert body["live"] is False


def test_the_api_says_live_for_a_fresh_heartbeat(live: TestClient) -> None:
    assert live.get("/api/runner-status").json()["live"] is True


def test_the_poller_updates_the_liveness_cell() -> None:
    """Server-rendered and polled paths must agree: both write the same
    element from the same field."""
    js = RUNNER_JS.read_text()

    assert "setLive(d.live)" in js
    assert '"runner-live"' in js
