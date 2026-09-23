"""Step 9b — filter (server) + sort (client, via data attributes).

Filter is a SQL narrowing on the activity_log query. Sort is
client-side because the same data drives the per-job table; we
don't want to lose chronological grouping unless the operator
explicitly asks for it.

The sort affordance is data-attribute-driven; this test confirms
the rows carry the right ``data-sort-*`` keys so the JS has
something to sort on. The JS itself is exercised in a browser,
not in pytest — we pin the *contract* it consumes here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db as init_state_db
from dportsv3.tracker.agentic_queries import activity_for_job
from dportsv3.tracker.server import create_app


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def seeded(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_state_db(conn)

    now = _now()
    conn.execute(
        """INSERT INTO jobs
           (job_id, state, type, origin, flavor, bundle_dir,
            created_ts_utc, path, last_seen_at, target)
           VALUES ('job-mixed', 'patching', 'patch', 'devel/foo', '', '',
                   ?, '', ?, '@2026Q2')""",
        (now, now),
    )
    activities = [
        ("attempt_start", "attempt 1/4", None, None),
        ("llm_turn", "T1",
         {"turn": 1, "prompt_tokens": 1000, "completion_tokens": 50,
          "total_tokens": 1050, "cumulative_total_tokens": 1050,
          "tools_requested": ["env_verify"]}, None),
        ("tool:env_verify", "status=ready ok", None, 10),
        ("llm_turn", "T2",
         {"turn": 2, "prompt_tokens": 5000, "completion_tokens": 200,
          "total_tokens": 5200, "cumulative_total_tokens": 6250,
          "tools_requested": ["get_file"]}, None),
        ("tool:get_file", "/work/foo ok", None, 30),
        ("llm_turn", "T3",
         {"turn": 3, "prompt_tokens": 80000, "completion_tokens": 600,
          "total_tokens": 80600, "cumulative_total_tokens": 86850,
          "tools_requested": ["dupe"]}, None),
    ]
    for stage, msg, extra, dur in activities:
        conn.execute(
            """INSERT INTO activity_log
               (ts, job_id, stage, message, duration_ms, extra_json)
               VALUES (?, 'job-mixed', ?, ?, ?, ?)""",
            (_now(), stage, msg, dur, json.dumps(extra) if extra else None),
        )

    # A terminal job with a bundle, so the prior-attempts band -- and the
    # per-port spend beside it -- has something to list.
    conn.execute(
        """INSERT INTO bundles
           (bundle_id, run_id, origin, flavor, ts_utc, result, path,
            last_seen_at, target)
           VALUES ('bd-bar-1', 'run-1', 'devel/bar', '', ?, 'failed',
                   '/var/log/dsynth/bd-bar-1', ?, '@2026Q2')""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO jobs
           (job_id, state, type, origin, flavor, bundle_dir,
            created_ts_utc, path, last_seen_at, target, bundle_id)
           VALUES ('job-done', 'done', 'patch', 'devel/bar', '', '',
                   ?, '', ?, '@2026Q2', 'bd-bar-1')""",
        (now, now),
    )
    big = [("attempt_start", "attempt 1/3", {"attempt": 1}, None)]
    for t in range(1, 22):
        big.append(("llm_turn", f"T{t}",
                    {"turn": t, "prompt_tokens": 100, "completion_tokens": 10,
                     "total_tokens": 110, "cumulative_total_tokens": 110 * t},
                    None))
        big.append(("tool:grep", "pattern=x ok", None, 5))
    big.append(("attempt_end", "done", {"attempt": 1, "rebuild_ok": True}, None))
    for stage, msg, extra, dur in big:
        conn.execute(
            """INSERT INTO activity_log
               (ts, job_id, stage, message, duration_ms, extra_json)
               VALUES (?, 'job-done', ?, ?, ?, ?)""",
            (_now(), stage, msg, dur, json.dumps(extra) if extra else None),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def client(seeded):
    app = create_app(seeded)
    with TestClient(app) as c:
        yield c


# --- server-side filter ----------------------------------------------------


def test_activity_for_job_filter_llm_turn(seeded):
    conn = sqlite3.connect(str(seeded))
    conn.row_factory = sqlite3.Row
    rows = activity_for_job(conn, "job-mixed", stage_filter="llm_turn")
    assert {r["stage"] for r in rows} == {"llm_turn"}
    assert len(rows) == 3


def test_activity_for_job_filter_tool(seeded):
    conn = sqlite3.connect(str(seeded))
    conn.row_factory = sqlite3.Row
    rows = activity_for_job(conn, "job-mixed", stage_filter="tool")
    assert all(r["stage"].startswith("tool:") for r in rows)
    assert len(rows) == 2


def test_activity_for_job_filter_none_returns_all(seeded):
    """Filter=None matches the default behavior — every row for the
    job, no narrowing."""
    conn = sqlite3.connect(str(seeded))
    conn.row_factory = sqlite3.Row
    rows = activity_for_job(conn, "job-mixed")
    assert len(rows) == 6


def test_activity_for_job_filter_unknown_value_ignored(seeded):
    """A bogus stage_filter should not narrow — defensive."""
    conn = sqlite3.connect(str(seeded))
    conn.row_factory = sqlite3.Row
    rows = activity_for_job(conn, "job-mixed", stage_filter="bogus")
    assert len(rows) == 6   # treated as no filter


def test_activity_for_job_filter_with_since_id(seeded):
    """Filter + since_id compose: only NEW llm_turn rows past cursor."""
    conn = sqlite3.connect(str(seeded))
    conn.row_factory = sqlite3.Row
    all_rows = activity_for_job(conn, "job-mixed")
    mid = sorted(r["id"] for r in all_rows)[2]
    fresh = activity_for_job(
        conn, "job-mixed", since_id=mid, stage_filter="llm_turn",
    )
    assert all(r["stage"] == "llm_turn" for r in fresh)
    assert all(r["id"] > mid for r in fresh)


# --- API surface -----------------------------------------------------------


def test_api_activity_filter_passes_through(client):
    body = client.get(
        "/api/activity?job_id=job-mixed&stage_filter=llm_turn"
    ).json()
    assert {r["stage"] for r in body} == {"llm_turn"}


def test_api_activity_filter_tool(client):
    body = client.get(
        "/api/activity?job_id=job-mixed&stage_filter=tool"
    ).json()
    assert all(r["stage"].startswith("tool:") for r in body)


# --- page rendering: pills + sort affordance --------------------------------


def test_transcript_renders_filter_pills(client):
    """The pills and the sortable table moved to the transcript with the
    raw table they control (poly-qqx9.5)."""
    body = client.get("/agentic/jobs/job-mixed/transcript").text
    assert "filter-pill" in body
    assert "llm_turn only" in body
    assert "tool calls only" in body
    # "all" is the active pill when no filter is set.
    assert 'class="filter-pill active">all<' in body


def test_transcript_active_pill_reflects_filter(client):
    body = client.get(
        "/agentic/jobs/job-mixed/transcript?stage_filter=llm_turn"
    ).text
    assert "filter-pill active" in body
    # The activity_log query NARROWED — only llm_turn rows in body.
    # (The token cells of those rows show up via the data-attributes.)
    assert "tool:get_file" not in body
    assert "tool:env_verify" not in body


def test_transcript_sortable_headers_present(client):
    """Sortable columns have data-sort=KEY so the JS knows what to
    sort. The JS itself runs in a browser; we pin the contract.
    Prompt/Compl collapsed into the Tokens column (poly-up2f); the
    split moved to the cell's title attribute."""
    body = client.get("/agentic/jobs/job-mixed/transcript").text
    for key in ("total", "cumulative"):
        assert f'data-sort="{key}"' in body
    # The corresponding row-level data-sort-* attributes exist too.
    for key in ("total", "cumulative"):
        assert f"data-sort-{key}=" in body
    # The prompt/completion split survives as the hover title.
    assert "prompt 80,000" in body
    assert "completion 600" in body


def test_transcript_row_sort_keys_use_neg_one_for_non_llm(client):
    """Non-llm_turn rows carry data-sort-*=-1 so they sort to the
    bottom on descending. Without this sentinel, sorting by tokens
    would show "0" tool rows interleaved with the real values."""
    body = client.get("/agentic/jobs/job-mixed/transcript").text
    # Tool rows: -1 sentinels.
    assert 'data-sort-total="-1"' in body
    # llm_turn rows: actual turn totals.
    assert 'data-sort-total="80600"' in body
    assert 'data-sort-total="5200"' in body


def test_job_detail_renders_turn_cards_for_a_terminal_job(client):
    """Cards are the reading view for both job states — one stream, one
    vocabulary. The attempt accordion they replaced is gone (poly-qqx9.4).
    """
    body = client.get("/agentic/jobs/job-done").text
    assert 'id="turn-cards"' in body
    assert 'class="turn-card' in body
    assert "attempt-group" not in body
    assert 'class="folded-rows"' not in body


def test_job_detail_boundary_card_carries_the_attempt_outcome(client):
    """What the accordion header used to say, in the stream instead."""
    body = client.get("/agentic/jobs/job-done").text
    assert "Attempt 1" in body
    assert "rebuild passed" in body
    assert "21 turns" in body


def test_the_raw_table_is_on_the_transcript_not_the_job_page(client):
    """The sortable token column is how a 690k-token turn gets found
    (poly-9hjm), so the table survives — one route away. Measured on a
    908-event job it was 78KB of a 160KB job-page render, and the reader
    who wants every row has already said so by going there."""
    for job in ("job-mixed", "job-done"):
        page = client.get(f"/agentic/jobs/{job}").text
        assert 'id="raw-events"' not in page, job
        assert 'id="activity-table"' not in page, job

        full = client.get(f"/agentic/jobs/{job}/transcript").text
        assert 'id="raw-events"' in full, job
        assert 'id="activity-table"' in full, job
        assert "job-activity-limit" in full, job


def test_job_detail_live_view_has_no_fold(client):
    """The live (active-job) view is a flat stream the poller prepends
    to — folding would fight row insertion, so it must not render."""
    # The style block mentions the class names in selectors on every
    # render; assert on the actual markup instead.
    body = client.get("/agentic/jobs/job-mixed").text
    assert 'class="folded-rows"' not in body
    assert 'class="fold-toggle"' not in body


def test_job_detail_live_polling_passes_stage_filter(client):
    """When the page is loaded with a filter, the live-refresh JS
    must include it in its polling URL — otherwise prepended rows
    would bleed past the filter."""
    body = client.get(
        "/agentic/jobs/job-mixed?stage_filter=llm_turn"
    ).text
    assert 'data-stage-filter="llm_turn"' in body
    # The JS reads dataset.stageFilter and appends it to the fragment URL;
    # the filter pills that used to spell it out in the markup went to the
    # transcript with the table they control, so the attribute is the
    # whole contract now.
    assert 'data-limit="' in body
    # And the filter still narrows what the cards are built from.
    assert "tool:get_file" not in body


# --- the window and the transcript route (poly-qqx9.5) ---------------------


def test_job_page_shows_five_turns_and_links_to_the_rest(client):
    """21 turns on job-done; the page shows five and routes to the rest."""
    body = client.get("/agentic/jobs/job-done").text
    assert body.count('class="turn-card state-') <= 12   # 5 turns + structure
    assert "last 5 of 21 turns" in body
    assert "Full transcript — all 21 turns" in body
    assert "/transcript" in body


def test_the_transcript_renders_every_turn(client):
    body = client.get("/agentic/jobs/job-done/transcript").text
    assert "21 turns" in body
    # job-done's llm_turn rows carry no attempt, so the card id is the
    # bare turn -- which is what a triage job's rows look like too.
    for t in (1, 11, 21):
        assert f'class="turn-id">T{t}<' in body


def test_the_transcript_works_on_a_running_job(client):
    """A route, not a job state: the accordion it replaced could only be
    reached by a job being terminal."""
    r = client.get("/agentic/jobs/job-mixed/transcript")
    assert r.status_code == 200
    assert 'class="turn-stream"' in r.text


def test_the_transcript_404s_on_an_unknown_job(client):
    assert client.get("/agentic/jobs/nope/transcript").status_code == 404


def test_the_turn_count_is_the_job_s_not_the_fetched_window_s(client):
    """Counting turns in the row window would offer 'all 67 turns' on a
    job that took 300."""
    body = client.get("/agentic/jobs/job-done?limit=10").text
    assert "all 21 turns" in body


def test_the_job_page_pins_a_now_bar(client):
    body = client.get("/agentic/jobs/job-mixed").text
    assert 'id="now-bar-slot"' in body
    assert 'class="now-bar"' in body


def test_the_live_fragment_carries_the_bar(client):
    body = client.get("/api/jobs/job-mixed/activity-fragment?since_id=0").json()
    assert 'class="now-bar"' in body["nowbar_html"]


def test_the_job_page_renders_an_attempt_strip(client):
    body = client.get("/agentic/jobs/job-done").text
    assert 'id="attempt-strip"' in body
    assert 'class="wf-track"' in body


def test_a_job_with_no_attempts_gets_no_strip(client):
    """job-q2-foo-style jobs write no attempt boundaries; an empty chart
    is worse than none."""
    body = client.get("/agentic/jobs/job-other").text
    assert 'id="attempt-strip"' not in body


def test_the_prior_attempts_band_shows_what_each_bundle_cost(client):
    """"What has this port cost me across four jobs" could not be asked:
    the band listed siblings with no spend beside them (poly-qqx9.9)."""
    body = client.get("/agentic/jobs/job-done").text
    assert "This port, all jobs" in body
    assert "billable tokens across" in body
    assert "Billable is the real" in body


def test_the_strip_carries_each_attempt_s_cost(client):
    body = client.get("/agentic/jobs/job-done").text
    assert 'class="wf-cost"' in body


def test_the_token_card_names_the_largest_turn_without_opening(client):
    """Finding it is what the sortable token column was for (poly-9hjm),
    and that table is a route away now."""
    body = client.get("/agentic/jobs/job-done").text
    summary = body[body.index("<summary>Token usage"):]
    assert "largest" in summary[:summary.index("</summary>")]
