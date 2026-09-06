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
CSS = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker" / "static"
       / "progress.css")


def _flat(body: str) -> str:
    return re.sub(r"\s+", " ", body)


def _seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, "
        "finished_at) VALUES (1, ?, 'release', 't0', 't1')", (TARGET,))
    # Unfinished, so the pipeline has a source to name.
    db.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, "
        "total_expected) VALUES (2, ?, 'test', 't2', 3)", (TARGET,))
    db.execute(
        "INSERT INTO build_results(build_run_id, origin, version, result, "
        "recorded_at, status) VALUES (2, 'devel/alpha', '1.0', 'failure', "
        "'t2', 'recorded')")
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


def _make(tmp_path: Path, *, seed=_seed, name: str | None = None) -> Path:
    # Its own file per fixture. A shared name means a test that takes both
    # gets one database, and the "empty" client is looking at the seeded
    # rows -- which is exactly how the operator-work tint test first
    # passed against a tracker that was not empty.
    path = tmp_path / (name or ("seeded.db" if seed else "blank.db"))
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
    # The caption's own "not a rate" is the page saying so, not a figure.
    without_caption = body.replace("count, not a rate", "")
    assert not re.search(r"\brates?\b", without_caption)


# --- it is not a queue ----------------------------------------------------


def test_the_page_says_the_flow_branches(client) -> None:
    body = _flat(_page(client))

    # The caption for the diagram, in the mock's own words (M2).
    assert "not one row advancing through six states" in body
    assert "separate jobs" in body            # triage and patch
    assert "enqueues fresh work" in body      # failed verify / new context
    assert "reopen after it resolved" in body  # an issue can come back
    # ...and the loop the arrows cannot draw on their own.
    assert "retry / re-triage" in body
    assert "feeds back" in body


def test_the_totals_are_not_presented_as_adding_up(client) -> None:
    body = _flat(_page(client))

    assert "do not add up" in body


def test_the_stages_are_numbered_and_still_not_a_queue(client) -> None:
    """UI-4 banned numbering, reasoning that "numbering encodes a sequence
    and there isn't one". The mock numbers them, says it branches, and
    draws a feedback loop, all at once -- the numbers are addresses, and
    the branching is carried by the caption and the loop rather than by
    withholding them.

    What the page must never do is imply the six are a queue. That is
    asserted next door, on the caption and the loop."""
    body = _flat(_page(client))

    for n, name in enumerate([
        "Build failures", "Issues", "Automated work", "Operator work",
        "Delivery", "Outcome",
    ], start=1):
        assert f"{n:02d} / {name}" in body


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


# --- the flow (M2) --------------------------------------------------------


def test_the_source_strip_names_the_runs_failures_come_from(client) -> None:
    """Where is this coming from -- a question six inventory cards cannot
    answer, and the first thing the mock puts on the page."""
    body = _flat(_page(client))

    assert "failures enter from these runs" in body
    assert "#2 @main" in body


def test_a_tracker_with_no_active_run_has_no_source_strip(empty) -> None:
    body = _page(empty)

    assert "source-strip" not in body


def test_the_flow_is_an_ordered_list_of_six(client) -> None:
    body = _page(client)
    flow = body[body.index('class="pipeline-flow"'):]
    flow = flow[:flow.index("</ol>")]

    assert flow.count('class="pipe-node') == 6


def test_the_edges_are_drawn_and_one_of_them_is_different() -> None:
    """Automated work handing off to a person is a different kind of
    transition from the rest, so it is a different colour."""
    css = CSS.read_text()
    block = css[css.index("/* --- The pipeline flow (M2)"):]
    block = block[:block.index("/* --- Pipeline overview (UI-4)")]

    assert ".pipe-node:not(:last-child)::after" in block
    assert ".pipe-node:nth-child(3)::after" in block
    assert "var(--amber)" in block


def test_the_edges_are_decoration_and_not_the_only_signal() -> None:
    """Pseudo-elements are invisible to a screen reader. The caption has
    to carry the branching in text, which is why it says so."""
    css = CSS.read_text()
    block = css[css.index("/* --- The pipeline flow (M2)"):]

    assert "::after" in block and "content: \"\"" in block


def test_the_loop_is_on_the_page(client) -> None:
    """The arrows run one way. This is the edge that makes it a graph and
    not a queue, and it is the thing a row of cards cannot show."""
    body = _flat(_page(client))

    assert "retry / re-triage" in body
    assert "retry-line" in body


def test_every_node_names_the_query_behind_it(client) -> None:
    """UI-4's bead said every number needs a named real query. The mock
    puts the name on the screen; this checks they are OUR names, because
    the mock's own labels guess at several and get them wrong."""
    body = _flat(_page(client))

    for src in ("agentic_status()", "issue_inventory()",
                "regressed_issue_count()", "worklist_band_counts()",
                "delivery_counts()", "job_outcome_counts()"):
        assert src in body


@pytest.mark.parametrize("src", [
    "list_bundles(limit=200)", "open_delivery_bundle_ids()",
    "issues WHERE state='resolved'",
])
def test_the_mocks_wrong_query_names_are_not_used(client, src) -> None:
    """Naming the query is the whole point of the label. Naming one that
    does not produce the figure is worse than naming none."""
    assert src not in _page(client)


# --- the tint is a condition, not a census --------------------------------


def test_a_working_system_is_not_painted_red(client) -> None:
    """The mock tints node 01 whenever any failure has been ingested and
    node 02 whenever any issue exists -- which is every working system,
    permanently. A tint that is always on is not a signal."""
    body = _page(client)
    flow = body[body.index('class="pipeline-flow"'):body.index("</ol>")]
    failures = flow[flow.index("stage-failures") - 200:flow.index("stage-issues")]

    assert "alert" not in failures
    assert "warning" not in failures


def test_queued_work_with_no_runner_is_an_alert(tmp_path: Path) -> None:
    """Not "there are jobs" -- there are always jobs. Jobs nobody is
    doing, which is a fault an operator would act on."""
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    for n in range(3):
        db.execute(
            "INSERT INTO jobs(job_id, origin, state, created_ts_utc, target, "
            "type) VALUES (?, 'devel/x', 'queued', 't1', '@main', 'triage')",
            (f"q-{n}",))
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = _page(client)

    flow = body[body.index('class="pipeline-flow"'):body.index("</ol>")]
    node = flow[flow.index("stage-automation") - 200:flow.index("stage-operator")]
    assert "alert" in node


def test_operator_work_warns_only_when_something_is_waiting(client, empty) -> None:
    busy = _page(client)
    quiet = _page(empty)

    def node(body: str) -> str:
        flow = body[body.index('class="pipeline-flow"'):body.index("</ol>")]
        return flow[flow.index("stage-operator") - 200:flow.index("stage-delivery")]

    assert "warning" in node(busy)
    assert "warning" not in node(quiet)


def test_a_never_sent_delivery_does_not_paint_the_node_forever(
    client,
) -> None:
    """create_failed rows are terminal and historical. Telling today's
    from last quarter's needs a time-window query, and this page has none
    -- so the count is in the sub-line and the tint is not."""
    body = _page(client)
    flow = body[body.index('class="pipeline-flow"'):body.index("</ol>")]
    node = flow[flow.index("stage-delivery") - 200:flow.index("stage-outcome")]

    assert "never sent" in node
    assert "alert" not in node


def test_delivery_still_shows_both_axes_in_the_flow(client) -> None:
    """The mock's node 05 reads "1 open PRs / awaiting confirm build" as
    one figure. poly-8e2 measured 17 live counterexamples."""
    body = _flat(_page(client))

    assert "open upstream" in body
    assert "awaiting a confirm build" in body


def test_the_health_strip_sits_under_the_flow_it_describes(client) -> None:
    body = _page(client)

    assert (body.index('class="pipeline-flow"')
            < body.index('class="pipeline-health"')
            < body.index("</div>", body.index('class="pipeline-health"')))
