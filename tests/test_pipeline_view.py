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
    RUNNER_BAND,
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


def _stage(client: TestClient, key: str) -> str:
    return _flat(_get(client, f"/pipeline?stage={key}"))


def _get(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    return resp.text


def test_the_six_stages_are_all_there(client) -> None:
    body = _flat(_page(client))

    for name in ("Build failures", "Issues", "Automated work",
                 "Operator work", "Delivery", "Outcome"):
        assert f"/ {name}" in body


def test_the_counts_are_what_the_queries_say(client, conn) -> None:
    """Not a sample and not a cap: the page prints the query's answer.
    Asserted against the queries rather than against literals, so a
    fixture change cannot quietly turn this into a test of nothing."""
    issues = issue_inventory(conn)
    bands = worklist_band_counts(conn)
    deliveries = delivery_counts(conn)
    outcomes = job_outcome_counts(conn)
    body = _flat(_page(client))

    assert f'>{issues["total"]}</span>' in body            # 02 Issues
    assert f'>{deliveries["open"]}</span>' in body         # 05 Delivery
    band_work = sum(v for k, v in bands.items()
                    if k not in (RUNNER_BAND, "confirming"))
    assert f'>{band_work}</span>' in body                  # 04 Operator
    assert f'>{outcomes["by_state"]["done"]}<' in _stage(client, "outcome")


def test_the_issue_drawer_breaks_the_total_down_by_state(client, conn) -> None:
    issues = issue_inventory(conn)
    body = _stage(client, "issues")

    for key in ("unresolved", "resolving", "resolved", "muted"):
        assert f'<td>{key}</td><td class="num">{issues["by_state"][key]}<' \
            in body


def test_regressed_is_counted_separately_from_resolved(client) -> None:
    """It is derived from a resolved row, so it cannot be a stored-state
    count, and printing only `resolved` would call a fix that came back a
    fix that held."""
    body = _stage(client, "issues")

    assert "regressed" in body
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
    withholding them."""
    body = _flat(_page(client))

    for n, name in enumerate([
        "Build failures", "Issues", "Automated work", "Operator work",
        "Delivery", "Outcome",
    ], start=1):
        assert f"{n:02d} / {name}" in body


# --- empty states ---------------------------------------------------------


def test_an_empty_tracker_says_so_in_its_drawer(empty) -> None:
    """One drawer, so one empty state -- not six cards each saying it."""
    body = _flat(_page(empty))

    assert "No issues yet" in body


@pytest.mark.parametrize("key", [
    "failures", "issues", "automation", "operator", "delivery", "outcome",
])
def test_every_stage_renders_on_an_empty_tracker(empty, key) -> None:
    assert empty.get(f"/pipeline?stage={key}").status_code == 200


def test_an_empty_tracker_still_draws_the_whole_flow(empty) -> None:
    body = _page(empty)
    flow = body[body.index('class="pipeline-flow"'):body.index("</ol>")]

    assert flow.count('class="pipe-node') == 6


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
    body = _stage(client, "outcome")

    assert "The work was attempted and failed" in body
    assert "Interrupted" in body and "runner restart, broken env" in body
    assert "Skipped" in body and "origin locked, issue muted" in body


def test_an_unclassified_retire_reason_is_visible(client) -> None:
    """A dead row with no reason is not folded into failed. A number that
    grows without anyone noticing is how the 3.1x happened."""
    body = _stage(client, "outcome")

    assert "Unclassified<em>no retire_reason, or a new one</em>" in body


def test_the_page_says_the_historical_rows_were_not_backfilled(client) -> None:
    """The interrupted count is inflated by finished triage work and stays
    that way until it ages out. A page that showed the split without
    saying so would be precise and still misleading."""
    body = _stage(client, "outcome")

    assert "not backfilled" in body
    assert "until it ages out" in body


def test_the_flows_failure_figure_is_the_failed_bucket(client, conn) -> None:
    outcomes = job_outcome_counts(conn)
    body = _stage(client, "outcome")

    assert outcomes["dead"]["failed"] < outcomes["by_state"]["dead"]
    assert (f'<td>The work was attempted and failed</td>'
            f'<td class="num">{outcomes["dead"]["failed"]}<') in body


# --- delivery is two axes -------------------------------------------------


def test_delivery_and_confirm_build_are_separate_numbers(client) -> None:
    body = _flat(_page(client))

    assert "open upstream" in body
    assert "awaiting a confirm build" in body


def test_a_delivery_that_never_reached_the_provider_is_counted(client) -> None:
    body = _flat(_page(client))

    assert "never sent" in body


# --- navigation -----------------------------------------------------------


def test_the_primary_nav_lands_on_the_overview(client) -> None:
    body = _page(client)

    assert re.search(r'<a href="[^"]*/pipeline"[^>]*>Pipeline</a>', body)


# --- two audiences --------------------------------------------------------


def test_the_manual_count_is_public_and_the_queue_is_not(
    db_path: Path, set_setting,
) -> None:
    """Reading how much is queued is not acting on it. Only the
    destination is operator-gated."""
    with TestClient(create_app(db_path)) as op_client:
        op = _stage(op_client, "operator")
    set_setting("tracker.public_readonly", True)
    with TestClient(create_app(db_path)) as anon_client:
        anon = _stage(anon_client, "operator")

    assert "Waiting for context" in anon and "Waiting for context" in op
    assert "/agentic/manual/" in op
    assert "/agentic/manual/" not in anon


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


# --- the drawer (M3) ------------------------------------------------------


def test_the_default_stage_is_the_issues_node(client) -> None:
    """The unit the whole workspace is built on, and the mock's default."""
    body = _page(client)

    assert 'id="stage-issues"' in body
    head = body[body.index('class="drawer-head"'):]
    assert "<h2>Issues</h2>" in head[:400]


def test_selecting_a_stage_is_a_url(client) -> None:
    """Shareable, survives a reload, and works with no JS at all -- which
    is why the nodes are links and not buttons."""
    body = _page(client)
    flow = body[body.index('class="pipeline-flow"'):body.index("</ol>")]

    for key in ("failures", "issues", "automation", "operator", "delivery",
                "outcome"):
        assert f'href="?stage={key}"' in flow


def test_the_selected_node_says_it_is_selected(client) -> None:
    body = _get(client, "/pipeline?stage=delivery")
    flow = body[body.index('class="pipeline-flow"'):body.index("</ol>")]
    node = flow[flow.index("stage-delivery") - 300:flow.index("stage-outcome")]

    assert "selected" in node
    assert 'aria-current="true"' in node


def test_a_stage_nobody_named_falls_back_rather_than_404ing(client) -> None:
    """The URL is a view preference, not a resource."""
    resp = client.get("/pipeline?stage=nonsense")

    assert resp.status_code == 200
    assert "<h2>Issues</h2>" in resp.text


def test_the_drawer_head_names_the_query(client) -> None:
    """UI-4's requirement was that every number have a named real query.
    The mock is what puts the name on the screen."""
    body = _flat(_stage(client, "automation"))

    assert "source: agentic_status() · worklist_band_counts()" in body


@pytest.mark.parametrize(("key", "marker"), [
    ("failures", "Occurrence"),
    ("issues", "Issue"),
    ("automation", "Job"),
    ("operator", "Worklist bands"),
    ("delivery", "Review"),
    ("outcome", "How the jobs ended"),
])
def test_each_stage_opens_its_own_rows(client, key, marker) -> None:
    assert marker in _stage(client, key)


def test_the_drawer_costs_one_query_and_not_six(client) -> None:
    """Six bodies per render would pay six times to show one. Measured on
    20,000 issues: the most expensive body is 4.2 ms, and only the
    selected one runs."""
    body = _stage(client, "delivery")

    # The delivery body is on the page...
    assert "Review" in body
    # ...and no other stage's body is.
    assert "How the jobs ended" not in body
    assert "Worklist bands" not in body


def test_a_capped_drawer_says_so_and_offers_the_list(tmp_path: Path) -> None:
    """The mock slices at 20 with no total and no way onward. An operator
    who reaches the bottom of a drawer needs the list, not a dead end."""
    path = tmp_path / "many.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    for n in range(40):
        db.execute(
            "INSERT INTO issues(issue_key, target, origin, state, "
            "times_seen, first_seen_at, last_seen_at, updated_at) VALUES "
            "(?, '@main', ?, 'unresolved', 1, 't0', 't1', 't1')",
            (f"i-{n:03d}", f"devel/p{n}"))
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = _flat(_page(client))

    assert "Showing 25 of 40" in body
    assert "See all" in body


def test_an_uncapped_drawer_does_not_pretend_to_be(client) -> None:
    body = _flat(_page(client))

    assert "Showing" not in body
    # ...and still offers the list, because a drawer is a page of rows
    # whether or not it happens to fit today.
    assert "See all" in body


def test_the_drawer_keeps_the_two_kinds_of_nothing(empty) -> None:
    """UI-7's rule: an empty tracker and a filter matching nothing are
    different messages, and the drawer is where the rows are now."""
    body = _flat(_page(empty))

    assert "No issues yet" in body
    assert "Nothing matches this filter" not in body


def test_the_outcome_drawer_keeps_what_the_cards_carried(client) -> None:
    """The six cards had facts no row table has -- the dead-job split and
    the poly-h6c note. Replacing them with a bare table would have lost
    them."""
    body = _stage(client, "outcome")

    assert "The work was attempted and failed" in body
    assert "not backfilled" in body


def test_the_issue_drawer_keeps_the_per_state_links(client) -> None:
    """UI-4 linked each count to its filtered list. The drawer shows the
    rows AND keeps the way onward."""
    body = _stage(client, "issues")

    assert "state=unresolved" in body
    assert "state=regressed" in body
