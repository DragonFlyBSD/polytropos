"""The Pipeline overview (poly-435j).

Its job is "is the system healthy and moving", which is a different
question from "what needs me" -- that is the Repairs worklist. Two rules
follow from it and most of these tests are about one or the other.

Every number is a real count of a named population, uncapped, and no
number is a rate: there is no time-window aggregation query, and a
made-up throughput figure on a page whose whole job is to be trusted is
worse than no figure at all.

And it is a set of inventories, not a queue. Work does not advance
through these in order -- triage and patch are separate job rows, a
failed verify or fresh operator context enqueues new work on a problem
that had already been through once, and an issue can reopen after
resolving. A device that implied a pipeline would be the one wrong thing
this page could say.
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
from dportsv3.tracker import preflight_status
from dportsv3.tracker.agentic_queries import (
    delivery_counts,
    issue_inventory,
    job_outcome_counts,
    worklist_band_counts,
)
from dportsv3.tracker.server import create_app

TARGET = "@main"


def _flat(body: str) -> str:
    return re.sub(r"\s+", " ", body)


def _seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, "
        "finished_at) VALUES (1, ?, 'release', 't0', 't1')", (TARGET,))
    db.execute(
        "INSERT INTO runs(run_id, target, build_run_id) VALUES ('r-1', ?, 1)",
        (TARGET,))
    rows = [
        # key, state, resolution, verification, job state
        ("i-ready", "unresolved", "agent_fixed", "verified", None),
        ("i-verify", "unresolved", "agent_fixed", None, None),
        ("i-decide", "unresolved", "triage_failed", None, None),
        ("i-owned", "unresolved", "operator_owned", None, None),
        ("i-live", "unresolved", None, None, "patching"),
        ("i-confirm", "resolving", "accepted", None, None),
        ("i-done", "resolved", "merged", None, None),
        ("i-muted", "muted", None, None, None),
    ]
    for n, (key, state, resolution, verification, job_state) in \
            enumerate(rows):
        db.execute(
            "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
            "first_seen_at, last_seen_at, green_head_run_id, resolved_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, 't0', 't1', ?, ?, 't1')",
            (key, TARGET, f"devel/{key}", state, 4 if n < 2 else 1,
             1 if state == "resolved" else None,
             "t1" if state in ("resolved", "resolving") else None),
        )
        db.execute(
            "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
            "target, issue_key, resolution, verification_status) "
            "VALUES (?, 'r-1', ?, 't1', 'failure', ?, ?, ?, ?)",
            (f"b-{key}", f"devel/{key}", TARGET, key, resolution,
             verification),
        )
        if job_state:
            db.execute(
                "INSERT INTO jobs(job_id, bundle_id, origin, state, "
                "created_ts_utc, target, type) VALUES (?, ?, ?, ?, 't1', ?, "
                "'patch')",
                (f"j-{key}", f"b-{key}", f"devel/{key}", job_state, TARGET),
            )
    for n in range(3):
        db.execute(
            "INSERT INTO jobs(job_id, origin, state, created_ts_utc, target, "
            "type) VALUES (?, ?, 'queued', 't1', ?, 'triage')",
            (f"q-{n}", f"devel/q{n}", TARGET))
    for n, reason in enumerate(
        ["runner_restart", "runner_restart", "patch_gave_up",
         "abandoned", None],
    ):
        db.execute(
            "INSERT INTO jobs(job_id, origin, state, created_ts_utc, target, "
            "type, retire_reason) VALUES (?, ?, 'dead', 't1', ?, 'triage', ?)",
            (f"d-{n}", f"devel/d{n}", TARGET, reason))
    db.execute(
        "INSERT INTO jobs(job_id, origin, state, created_ts_utc, target, type)"
        " VALUES ('done-1', 'devel/x', 'done', 't1', ?, 'patch')", (TARGET,))
    for n, status in enumerate(["created", "merged", "create_failed"]):
        # A create_failed row never reached the provider, so it carries no
        # PR id and no url -- that is the whole point of poly-8e2.
        failed = status == "create_failed"
        db.execute(
            "INSERT INTO bundle_review_requests(bundle_id, provider, "
            "provider_pr_id, url, branch, status, created_at, error) "
            "VALUES (?, 'github', ?, ?, ?, ?, 't1', ?)",
            (f"b-{rows[n][0]}", None if failed else str(700 + n),
             None if failed else f"https://example.invalid/pull/{700 + n}",
             f"fix/{n}", status, "GitApplyConflict" if failed else None))
    db.execute(
        "INSERT INTO user_context_requests(run_id, origin, bundle_id, status, "
        "requested_at) VALUES ('r-1', 'devel/ask', 'b-i-decide', 'pending', "
        "'t1')")
    db.execute(
        "INSERT INTO runner_status(id, status, current_stage, updated_at) "
        "VALUES (1, 'processing', 'patch_start', ?)",
        (datetime.now(timezone.utc).isoformat(),))
    db.commit()


def _make(tmp_path: Path, *, seed=_seed) -> Path:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    if seed:
        seed(db)
    db.close()
    return path


@pytest.fixture(autouse=True)
def _fresh_preflight():
    preflight_status.reset()
    yield
    preflight_status.reset()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return _make(tmp_path)


@pytest.fixture
def client(db_path: Path) -> TestClient:
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


@pytest.fixture
def empty(tmp_path: Path) -> TestClient:
    with TestClient(create_app(_make(tmp_path, seed=None))) as test_client:
        yield test_client


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    return db


def _page(client: TestClient) -> str:
    resp = client.get("/pipeline")
    assert resp.status_code == 200, resp.text
    return resp.text


# --- every number comes from a real query ---------------------------------


def test_the_six_stages_are_all_there(client) -> None:
    body = _page(client)

    for stage in ("Ingested", "Fingerprinted", "Automated work",
                  "Operator work", "Delivery", "Outcome"):
        assert f">{stage}</h2>" in body


def test_the_counts_are_what_the_queries_say(client, conn) -> None:
    """Not a sample and not a cap: the page prints the query's answer.
    Asserted against the queries rather than against literals, so a fixture
    change cannot quietly turn this into a test of nothing."""
    body = _flat(_page(client))
    issues = issue_inventory(conn)
    bands = worklist_band_counts(conn)
    deliveries = delivery_counts(conn)
    outcomes = job_outcome_counts(conn)

    def row(label: str, n: int) -> str:
        return f'<span>{label}</span><span class="n">{n}<'

    total = issues["total"]
    assert f'>Fingerprinted</h2> <span class="stage-n">{total}' in body
    assert row("Unresolved", issues["by_state"]["unresolved"]) in body
    assert row("Ready to accept", bands["ready"]) in body
    assert row("Open upstream", deliveries["open"]) in body
    assert row("Jobs done", outcomes["by_state"]["done"]) in body


def test_the_operator_headline_equals_the_rows_under_it(client, conn) -> None:
    bands = worklist_band_counts(conn)
    expected = (bands["ready"] + bands["verify"] + bands["decide"]
                + bands["owned"] + 1)  # + the one pending context request

    body = _flat(_page(client))

    assert f'>Operator work</h2> <span class="stage-n">{expected}<' in body


def test_regressed_is_counted_separately_from_resolved(client) -> None:
    """It is derived from a resolved row, so it cannot be a stored-state
    count, and printing only `resolved` would call a fix that came back a
    fix that held."""
    body = _flat(_page(client))

    assert "Regressed" in body
    assert "fixed, then seen again" in body


def test_the_page_shows_no_rates(client) -> None:
    """No time-window aggregation query exists, so any per-hour or per-day
    figure here would be invented."""
    body = _page(client).lower()

    for rate in ("per hour", "per day", "/hr", "/day", "throughput",
                 "velocity"):
        assert rate not in body
    # word-boundary: "separate jobs" is the note saying the flow branches
    assert not re.search(r"\brates?\b", body)


# --- it is not a queue ----------------------------------------------------


def test_the_page_says_the_flow_branches(client) -> None:
    body = _flat(_page(client))

    assert "not stages of a queue" in body
    assert "separate jobs" in body           # triage and patch
    assert "enqueues new work" in body       # failed verify / new context
    assert "reopen after resolving" in body  # an issue can come back


def test_the_totals_are_not_presented_as_adding_up(client) -> None:
    body = _flat(_page(client))

    assert "do not add up" in body


def test_no_stage_is_numbered(client) -> None:
    """Numbering encodes a sequence. There isn't one, so step markers would
    be the single most misleading thing this page could draw."""
    body = _page(client)
    heads = re.findall(r'<h2>([^<]*)</h2>', body)

    assert heads
    for head in heads:
        assert not re.match(r"\s*\d", head), head


# --- empty states ---------------------------------------------------------


def test_an_empty_tracker_says_so_in_every_stage(empty) -> None:
    body = _page(empty)

    assert body.count('class="stage-empty"') == 6
    assert "Nothing has been ingested yet" in _flat(body)
    assert "No issue has been fingerprinted yet" in body
    assert "Nothing needs a person right now" in body


def test_an_empty_tracker_still_renders_every_stage(empty) -> None:
    body = _page(empty)

    for stage in ("Ingested", "Fingerprinted", "Automated work",
                  "Operator work", "Delivery", "Outcome"):
        assert f">{stage}</h2>" in body


# --- health strip ---------------------------------------------------------


def test_the_health_strip_reads_the_runners_heartbeat(client) -> None:
    body = _flat(_page(client))

    assert "RUNNER</b> processing" in body or "Runner</b> processing" in body


def test_a_dead_runner_shows_as_not_running(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    db.execute(
        "INSERT INTO runner_status(id, status, updated_at) "
        "VALUES (1, 'processing', ?)", (stale.isoformat(),))
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = _flat(_page(client))

    assert "not running" in body


def test_the_preflight_reading_is_stamped(client) -> None:
    """Stale by design -- it re-checks on a throttle and skips while a
    delivery holds the clone -- so the strip says when it was taken rather
    than implying now."""
    body = _flat(_page(client))

    assert "as of" in body


# --- the dead-job split ---------------------------------------------------


def test_dead_is_split_by_why(client) -> None:
    """`dead` on its own counted finished triage work as failure: 246 of
    364 rows on the builder were runner_restart, a 3.1x overstatement
    (poly-h6c). Only one of these three buckets says anything about a
    port."""
    body = _flat(_page(client))

    assert ('The work was attempted and failed</span>'
            '<span class="n">1<') in body
    assert "Interrupted" in body and "runner restart, broken env" in body
    assert "Skipped" in body and "origin locked, issue muted" in body


def test_an_unclassified_retire_reason_is_visible(client) -> None:
    """A dead row with no reason is not folded into failed. A number that
    grows without anyone noticing is how the 3.1x happened."""
    body = _flat(_page(client))

    assert "Unclassified <em>no retire_reason, or a new one</em>" in body


def test_the_page_says_the_historical_rows_were_not_backfilled(client) -> None:
    """The interrupted count is inflated by finished triage work and stays
    that way until it ages out. A page that showed the split without saying
    so would be precise and still misleading."""
    body = _flat(_page(client))

    assert "not backfilled" in body
    assert "until it ages out" in body


def test_the_headline_failure_count_is_the_failed_bucket(client, conn) -> None:
    outcomes = job_outcome_counts(conn)
    body = _flat(_page(client))

    assert outcomes["dead"]["failed"] < outcomes["by_state"]["dead"]
    assert ("Jobs that failed the work</span>"
            f"<span class=\"n\">{outcomes['dead']['failed']}<") in body


# --- delivery is two axes -------------------------------------------------


def test_delivery_and_confirm_build_are_separate_numbers(client) -> None:
    body = _flat(_page(client))

    assert "Open upstream" in body
    assert "Awaiting a confirm build" in body
    assert "a different axis" in body


def test_a_delivery_that_never_reached_the_provider_is_counted(client) -> None:
    body = _flat(_page(client))

    assert ('Never reached the provider</span>'
            '<span class="n">1<') in body


# --- navigation -----------------------------------------------------------


def test_the_primary_nav_lands_on_the_overview(client) -> None:
    body = _page(client)

    assert re.search(r'<a href="[^"]*/pipeline"[^>]*>Pipeline</a>', body)


def test_every_count_opens_the_rows_behind_it(client) -> None:
    """A count with no way through to its rows is an assertion the operator
    cannot check."""
    body = _page(client)
    hrefs = re.findall(r'<a class="stage-row" href="([^"]*)"', body)

    assert len(hrefs) >= 12
    for href in hrefs:
        assert href.startswith("http://") or href.startswith("/")


@pytest.mark.parametrize("href", [
    "/agentic/bundles", "/agentic/issues?state=unresolved",
    "/agentic/issues?state=regressed", "/agentic/jobs?state=queued",
    "/pipeline/deliveries?status=open",
    "/pipeline/deliveries?status=create_failed",
    "/agentic/issues?state=resolving", "/agentic/jobs?state=dead",
])
def test_the_drawer_links_resolve(client, href) -> None:
    """Every link the overview offers has to be a page that exists and a
    filter the route accepts."""
    assert href in _page(client)

    resp = client.get(href)
    assert resp.status_code == 200, href


# --- two audiences --------------------------------------------------------


def test_the_manual_count_is_public_and_the_queue_is_not(
    db_path: Path, set_setting,
) -> None:
    """Reading how much is queued is not acting on it, and the headline
    already includes it -- hiding the row would leave the two disagreeing.
    Only the destination is operator-gated."""
    with TestClient(create_app(db_path)) as op_client:
        op = _flat(_page(op_client))
    set_setting("tracker.public_readonly", True)
    with TestClient(create_app(db_path)) as anon_client:
        anon = _flat(_page(anon_client))

    assert ("Waiting for an operator's context</span>"
            '<span class="n">1<') in anon
    assert 'Waiting for your context</span><span class="n">1<' in op
    assert ">Runner</a>" in op and ">Runner</a>" not in anon


# --- the delivery drawer --------------------------------------------------


def _deliveries(client: TestClient, query: str = "") -> str:
    resp = client.get("/pipeline/deliveries" + query)
    assert resp.status_code == 200, resp.text
    return resp.text


def test_a_delivery_row_carries_its_issue_and_origin(client) -> None:
    """One read, not one query per row: the origin, the issue and the
    review all come off the same row."""
    body = _flat(_deliveries(client))

    assert "devel/i-ready" in body
    assert "i-ready</a>" in body
    assert "#700" in body


def test_the_status_filters_carry_their_own_counts(client, conn) -> None:
    counts = delivery_counts(conn)
    body = _flat(_deliveries(client))

    assert f'>All <span class="n">{counts["total"]}<' in body
    assert f'>Open upstream <span class="n">{counts["open"]}<' in body


def test_open_is_a_filter_even_though_it_is_not_a_status(client) -> None:
    body = _deliveries(client, "?status=open")

    assert "created" in body
    assert "GitApplyConflict" not in body


def test_an_unknown_status_falls_back_to_everything(client, conn) -> None:
    """A filter that quietly returns an empty page reads exactly like a
    stage with no work in it, so a nonsense one shows all of it instead."""
    body = _deliveries(client, "?status=nonsense")

    assert str(delivery_counts(conn)["total"]) in _flat(body)


def test_a_failed_delivery_shows_why_and_offers_no_link(client) -> None:
    """poly-8e2's shape: the fix exists only in the local DeltaPorts and no
    PR was ever opened."""
    body = _flat(_deliveries(client, "?status=create_failed"))

    assert "GitApplyConflict" in body
    assert "never opened" in body


def test_the_row_says_what_its_issue_thinks(client) -> None:
    """An issue resolves when a build proves the fix, never when a PR
    merges, so the two can disagree and the row has to show both."""
    body = _flat(_deliveries(client))

    assert "issue unresolved" in body


def test_an_empty_delivery_list_explains_itself(empty) -> None:
    body = _flat(_deliveries(empty))

    assert "Nothing has been delivered" in body
    assert "no_config" in body


def test_a_filter_that_matches_nothing_says_how_many_exist(client) -> None:
    body = _flat(_deliveries(client, "?q=nothingmatchesthis"))

    assert "Nothing matches this filter" in body
    assert "Nothing has been delivered" not in body
