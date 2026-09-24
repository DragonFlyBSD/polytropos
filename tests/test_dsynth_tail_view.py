"""The published tail reaches the page, and keeps reaching it during a
build that writes no activity rows (poly-pvs2).

The second half is the one the bead missed. The live fragment gates almost
everything on ``rows`` -- new activity_log rows since a cursor -- and a
dsynth build produces none for forty minutes. A tail gated that way would
sit frozen for exactly the wait it exists to explain, which is
poly-qqx9.16's defect one surface over.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db import presence
from dportsv3.db.schema import init_db
from dportsv3.tracker.server import create_app

JOB = "job-1"
RUNNER = "builder-A"
JOB_JS = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
          / "static" / "agentic-job.js")


def _seed(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO jobs (job_id, state, type, origin, target, dev_env, "
        "bundle_id) VALUES (?, 'patching', 'patch', 'devel/glib20', "
        "'@2026Q3', '2026Q3', 'b-1')", (JOB,),
    )
    # A turn with dsynth_build still running: the row the tail hangs on.
    db.execute(
        "INSERT INTO activity_log (ts, stage, message, job_id, extra_json) "
        "VALUES ('2026-09-24T10:00:00+00:00', 'attempt_start', 'a', ?, "
        "'{\"attempt\": 1, \"turn\": 1}')", (JOB,),
    )
    db.execute(
        "INSERT INTO activity_log (ts, stage, message, job_id, extra_json) "
        "VALUES ('2026-09-24T10:00:01+00:00', 'tool_start', "
        "'dsynth_build started', ?, "
        "'{\"attempt\": 1, \"turn\": 1, \"tool\": \"dsynth_build\", "
        "\"call_id\": \"c1\"}')", (JOB,),
    )
    db.commit()
    return db


def _publish(db: sqlite3.Connection, text: str, **kw) -> None:
    tail = {
        "job_id": JOB, "tool": "dsynth_build", "text": text,
        "lines": text.count("\n"), "total_bytes": 900_000, "skipped": 0,
        "max_bytes": 32768, "log_mtime": 1_700_000_000.0,
    }
    tail.update(kw)
    presence.apply(db, {"event": "heartbeat", "runner_id": RUNNER,
                        "tail": tail})


@pytest.fixture
def live(tmp_path: Path):
    path = tmp_path / "state.db"
    db = _seed(path)
    app = create_app(path)
    with TestClient(app) as client:
        yield client, db
    db.close()


# --- the page -------------------------------------------------------------


def test_the_page_shows_the_published_tail(live) -> None:
    client, db = live
    _publish(db, "checking for gcc... yes\nconfigure: done\n")

    body = client.get(f"/agentic/jobs/{JOB}").text

    assert "configure: done" in body
    assert 'class="tool-tail"' in body


def test_the_page_has_no_tail_before_anything_is_published(live) -> None:
    """The ordinary state for every job not running dsynth right now. An
    empty slot, not an error and not a stale build."""
    client, _ = live

    body = client.get(f"/agentic/jobs/{JOB}").text

    assert 'class="tool-tail"' not in body


def test_the_tail_states_its_cap_rather_than_applying_it_silently(
    live,
) -> None:
    """A page open through a long build asks for a gap of megabytes and
    gets the newest window. Saying so is the difference between a tail and
    a truncated log."""
    client, db = live
    _publish(db, "x\n", skipped=131_072)

    body = re.sub(r"\s+", " ", client.get(f"/agentic/jobs/{JOB}").text)

    assert "newest 32 KiB of 900,000 bytes" in body
    assert "131,072 skipped" in body


def test_the_tail_carries_the_logs_clock_not_the_polls(live) -> None:
    """"last line 35s ago" is about the BUILD. The live badge already
    reports the poll, and during a 40-minute link step those two numbers
    say very different things."""
    client, db = live
    _publish(db, "linking\n")

    body = client.get(f"/agentic/jobs/{JOB}").text

    assert 'data-mtime="1700000000.0"' in body
    assert 'class="tail-ago"' in body


def test_no_empty_log_link_is_rendered(live) -> None:
    """The old template linked to a route poly-paee deleted, and
    full.log.gz does not exist until the build ends. An href="" reads as a
    broken control."""
    client, db = live
    _publish(db, "linking\n")

    body = client.get(f"/agentic/jobs/{JOB}").text

    assert 'href=""' not in body
    assert "open the whole log" not in body


# --- the live poll, which is the part that was missing -------------------


def _fragment(client, since_id: int) -> dict:
    r = client.get(f"/api/jobs/{JOB}/activity-fragment?since_id={since_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_tail_refreshes_with_no_new_activity_rows(live) -> None:
    """THE test for this feature being live rather than merely rendered.
    A 40-minute build writes nothing to activity_log, so `changed` is
    false and cards_html is empty -- and the tail must still arrive."""
    client, db = live
    cursor = db.execute("SELECT MAX(id) FROM activity_log").fetchone()[0]
    _publish(db, "still linking\n")

    payload = _fragment(client, cursor)

    assert payload["changed"] is False, "no new rows, by construction"
    assert payload["cards_html"] == ""
    assert "still linking" in payload["tail_html"]


def test_each_poll_carries_the_newest_lines(live) -> None:
    client, db = live
    cursor = db.execute("SELECT MAX(id) FROM activity_log").fetchone()[0]

    _publish(db, "step one\n")
    first = _fragment(client, cursor)["tail_html"]
    _publish(db, "step two\n")
    second = _fragment(client, cursor)["tail_html"]

    assert "step one" in first
    assert "step two" in second and "step one" not in second


def test_the_slot_empties_when_the_build_ends(live) -> None:
    """Sent on every poll and rendered empty, which CLEARS it. Left to a
    truthiness check it would keep showing the last lines of a finished
    build under a tool row that has already collapsed."""
    client, db = live
    cursor = db.execute("SELECT MAX(id) FROM activity_log").fetchone()[0]
    _publish(db, "done\n")
    assert "done" in _fragment(client, cursor)["tail_html"]

    presence.apply(db, {"event": "heartbeat", "runner_id": RUNNER})

    payload = _fragment(client, cursor)
    assert "tail_html" in payload
    # The <pre> specifically: the slot wrapper's own id contains the same
    # substring, so a looser check passes on an unrendered slot for the
    # wrong reason.
    assert 'class="tool-tail"' not in payload["tail_html"]
    assert "done" not in payload["tail_html"]


def test_the_slot_wrapper_is_always_present(live) -> None:
    """The client swaps by id, so the anchor has to exist even with
    nothing to show."""
    client, db = live
    cursor = db.execute("SELECT MAX(id) FROM activity_log").fetchone()[0]

    assert 'id="tool-tail-slot"' in _fragment(client, cursor)["tail_html"]


# --- the client half ------------------------------------------------------


def test_the_swap_runs_after_the_cards_swap() -> None:
    """The slot lives INSIDE the card stream, so a cards_html swap
    replaces it; writing the tail first would be thrown away."""
    js = JOB_JS.read_text()

    assert js.index("data.cards_html") < js.index("data.tail_html")


def test_the_swap_follows_the_newest_line_unless_scrolled_away() -> None:
    """A live tail pinned to the top would show the oldest of the last
    forty lines beside a label reading "last line 4s ago"."""
    js = JOB_JS.read_text()
    block = js[js.index("data.tail_html"):]

    assert "scrollHeight" in block
    assert "scrollTop" in block
    assert "startTailClock()" in block
