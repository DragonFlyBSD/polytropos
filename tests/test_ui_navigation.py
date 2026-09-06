"""Routes and templates for the migrated UI (poly-691d).

Three properties that no single view owns and that a per-view test would
not catch.

Navigation has to know where you are: the shell's primary nav highlights
one of Builds / Pipeline / Repairs on every page including the drill-downs,
and each view's section nav highlights its own item. Both are derived from
the request path, so a route added under the wrong prefix silently lands in
the wrong view.

A link has to keep what it carries. Paging and filters compose through one
`query_for` helper -- a Next link that dropped the search, or a Clear that
kept the page number, sends the operator somewhere that does not exist.

And nothing an operator did not write may become markup. Every identifier
here comes from somewhere else: origins and versions from build output,
error text from a compiler, delivery errors from a forge. The sweep that
proved this is in poly-691d's notes; what is asserted below is the shape a
regression would take.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.server import create_app

TARGET = "@main"
#: Slash-free on purpose. A path separator in a path parameter is a
#: different failure -- url_for refuses it outright -- and it has its own
#: bead (poly-13ku).
HOSTILE = '"><script>alert(1)<\\u002fscript>'
HANDLER = "' onmouseover='alert(2)"


def _seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, "
        "finished_at, total_expected) VALUES (1, ?, 'release', 't0', 't1', 3)",
        (TARGET,))
    db.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at, "
        "total_expected) VALUES (2, ?, 'test', 't2', 3)", (TARGET,))
    db.execute(
        "INSERT INTO runs(run_id, target, build_run_id) VALUES ('r-1', ?, 1)",
        (TARGET,))
    for n, origin in enumerate(["devel/alpha", "devel/beta", "www/gamma"]):
        db.execute(
            "INSERT INTO build_results(build_run_id, origin, version, result, "
            "recorded_at, status) VALUES (1, ?, ?, ?, ?, 'recorded')",
            (origin, f"1.{n}.0", "failure" if n else "success", f"t{n}"))
        db.execute(
            "INSERT INTO port_status(target, origin, last_attempt_result, "
            "last_attempt_version, last_attempt_at, last_attempt_run_id) "
            "VALUES (?, ?, 'failure', ?, 't1', 1)",
            (TARGET, origin, f"1.{n}.0"))
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
        "times_seen, first_seen_at, last_seen_at, updated_at) VALUES "
        "('i-1', ?, 'devel/alpha', 'fp', 'unresolved', 1, 't0', 't1', 't1')",
        (TARGET,))
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
        "target, issue_key, resolution) VALUES ('b-1', 'r-1', 'devel/alpha', "
        "'t1', 'failure', ?, 'i-1', 'agent_fixed')", (TARGET,))
    db.execute(
        "INSERT INTO jobs(job_id, bundle_id, origin, state, created_ts_utc, "
        "target, type) VALUES ('j-1', 'b-1', 'devel/alpha', 'queued', 't1', "
        "?, 'triage')", (TARGET,))
    db.execute(
        "INSERT INTO user_context_requests(run_id, origin, bundle_id, "
        "requested_at, status) VALUES ('r-1', 'devel/alpha', 'b-1', 't1', "
        "'pending')")
    db.execute(
        "INSERT INTO bundle_review_requests(bundle_id, provider, "
        "provider_pr_id, url, branch, status, created_at) VALUES "
        "('b-1', 'github', '7', 'https://example.invalid/pull/7', 'fix/a', "
        "'created', 't1')")
    db.execute(
        "INSERT INTO activity_log(ts, job_id, bundle_id, stage, message) "
        "VALUES ('t1', 'j-1', 'b-1', 'triage', 'started')")
    db.execute(
        "INSERT INTO runner_status(id, status, updated_at) "
        "VALUES (1, 'idle', 't1')")
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


def _get(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    return resp.text


def _active(body: str, aria_label: str) -> list[str]:
    """The items marked current inside one <nav>."""
    m = re.search(
        r'<nav[^>]*aria-label="%s"[^>]*>(.*?)</nav>' % re.escape(aria_label),
        body, re.S)
    assert m, f"no nav labelled {aria_label!r}"
    return re.findall(r'<a[^>]*aria-current="page"[^>]*>([^<]*)</a>',
                      m.group(1))


def _primary(body: str) -> list[str]:
    return _active(body, "Primary")


# --- the shell knows which view you are in --------------------------------


@pytest.mark.parametrize(("path", "view"), [
    ("/", "Builds"),
    ("/builds", "Builds"),
    ("/builds/1", "Builds"),
    ("/builds/compare", "Builds"),
    ("/targets", "Builds"),
    ("/target/@main", "Builds"),
    ("/target/@main/devel/alpha", "Builds"),
    ("/pipeline", "Pipeline"),
    ("/pipeline/deliveries", "Pipeline"),
    ("/agentic/jobs", "Pipeline"),
    ("/agentic/jobs/j-1", "Pipeline"),
    ("/agentic/runner", "Pipeline"),
    ("/agentic/manual", "Pipeline"),
    ("/agentic", "Repairs"),
    ("/agentic/issues", "Repairs"),
    ("/agentic/issues/i-1", "Repairs"),
    ("/agentic/bundles", "Repairs"),
    ("/agentic/bundles/b-1", "Repairs"),
])
def test_the_primary_nav_marks_exactly_one_view(client, path, view) -> None:
    """Derived from the request path, so a route added under the wrong
    prefix lands in the wrong view and nothing else notices."""
    assert _primary(_get(client, path)) == [view]


@pytest.mark.parametrize(("path", "section"), [
    ("/agentic", "Worklist"),
    ("/agentic/issues", "Issues"),
    ("/agentic/issues/i-1", "Issues"),
    ("/agentic/bundles", "Occurrences"),
    ("/agentic/bundles/b-1", "Occurrences"),
    ("/pipeline", "Overview"),
    ("/pipeline/deliveries", "Deliveries"),
    ("/agentic/jobs", "Jobs"),
    ("/agentic/jobs/j-1", "Jobs"),
    ("/agentic/runner", "Runner"),
    ("/agentic/manual", "Manual"),
])
def test_the_section_nav_marks_the_page_you_are_on(
    client, path, section,
) -> None:
    body = _get(client, path)
    label = "Pipeline sections" if "Pipeline" in _primary(body)[0] \
        else "Repairs sections"

    assert _active(body, label) == [section]


def test_a_drill_down_keeps_its_section_marked(client) -> None:
    """A detail page is still inside its list's section; losing the mark
    there is how a drill-down feels like it left the app."""
    assert _active(_get(client, "/agentic/issues/i-1"),
                   "Repairs sections") == ["Issues"]
    assert _active(_get(client, "/agentic/jobs/j-1"),
                   "Pipeline sections") == ["Jobs"]


def test_the_three_views_are_the_only_primary_destinations(client) -> None:
    body = _get(client, "/pipeline")
    nav = re.search(r'<nav[^>]*aria-label="Primary"[^>]*>(.*?)</nav>',
                    body, re.S).group(1)

    assert re.findall(r">([^<>]+)</a>", nav) == ["Builds", "Pipeline",
                                                 "Repairs"]


# --- cross-view links keep their identifiers ------------------------------


def test_an_occurrence_links_to_its_issue_and_back(client) -> None:
    occurrence = _get(client, "/agentic/bundles/b-1")
    issue = _get(client, "/agentic/issues/i-1")

    assert "/agentic/issues/i-1" in occurrence
    assert "/agentic/bundles/b-1" in issue


def test_a_job_links_to_the_occurrence_it_worked(client) -> None:
    assert "/agentic/bundles/b-1" in _get(client, "/agentic/jobs/j-1")


def test_a_delivery_links_to_both_ends(client) -> None:
    body = _get(client, "/pipeline/deliveries")

    assert "/agentic/issues/i-1" in body
    assert "/agentic/bundles/b-1" in body
    assert "https://example.invalid/pull/7" in body


def test_a_build_result_links_to_the_port_it_is_about(client) -> None:
    """The dashboard's result rows are the server-rendered path from a run
    to a port. /builds/{id} and /target/{t} build their rows from JSON
    (UI-3), so there is nothing to assert on those.

    port_link returns None for an origin that is not exactly cat/port, so
    the link is conditional and worth pinning."""
    body = _get(client, "/?run=1")

    assert "devel/alpha" in body
    assert ("/target/%40main/devel/alpha" in body
            or "/target/@main/devel/alpha" in body)


def test_an_origin_that_is_not_cat_slash_port_gets_no_link(client) -> None:
    from dportsv3.tracker.routes.pages import _port_link

    class _Req:
        def url_for(self, name, **kw):
            return f"/target/{kw['target']}/{kw['cat']}/{kw['port']}"

    port_link = _port_link(_Req())

    assert port_link("@main", "devel/alpha") == "/target/@main/devel/alpha"
    assert port_link("@main", "flavourless") is None
    assert port_link("@main", "a/b/c") is None
    assert port_link("@main", "a/") is None


def test_a_port_links_to_the_issue_that_covers_it(client) -> None:
    assert "/agentic/issues/i-1" in _get(
        client, "/target/@main/devel/alpha")


def test_the_pipeline_counts_link_to_the_rows_behind_them(client) -> None:
    body = _get(client, "/pipeline")

    for href in ("/agentic/bundles", "/agentic/issues?state=unresolved",
                 "/agentic/jobs?state=queued",
                 "/pipeline/deliveries?status=open"):
        assert href in body


# --- links keep their filters ---------------------------------------------


def _hrefs(body: str, text: str) -> list[str]:
    return re.findall(r'<a[^>]*href="([^"]*)"[^>]*>\s*%s\s*</a>'
                      % re.escape(text), body)


@pytest.mark.parametrize("base", [
    "/agentic/issues", "/agentic/bundles", "/agentic/jobs",
    "/pipeline/deliveries",
])
def test_paging_keeps_the_search(client, base) -> None:
    """A Next that dropped the search would page through a different set
    than the one being counted above it."""
    body = _get(client, f"{base}?q=alpha&page=1")
    controls = _hrefs(body, "Next") + _hrefs(body, "Previous") \
        + _hrefs(body, "Clear")

    assert controls, base
    for href in controls:
        query = urllib.parse.parse_qs(href.lstrip("?"))
        # Clear is the one link that deliberately drops it.
        if href in _hrefs(body, "Clear"):
            assert "q" not in query
        else:
            assert query.get("q") == ["alpha"]


def test_clearing_a_search_also_clears_the_page(client) -> None:
    """Otherwise Clear lands on page 4 of a list that now has one page."""
    body = _get(client, "/agentic/issues?q=alpha&page=1")
    clear = _hrefs(body, "Clear")

    assert clear
    assert "page=" not in clear[0]


def test_a_state_filter_survives_a_search(client) -> None:
    body = _get(client, "/agentic/issues?state=unresolved&q=alpha")

    for href in _hrefs(body, "Clear"):
        assert "state=unresolved" in href


def test_the_delivery_status_filter_carries_across_its_tabs(client) -> None:
    body = _get(client, "/pipeline/deliveries?q=fix")
    tabs = re.findall(
        r'<a class="result-filter[^"]*"\s+href="([^"]*)"', body)

    assert tabs
    for href in tabs:
        assert "q=fix" in href


def test_query_for_drops_empty_values_rather_than_sending_them() -> None:
    """An empty filter in the URL is not the same as an absent one: it is
    what a Clear link has to produce."""
    from dportsv3.tracker.routes.pages import _query_for

    query_for = _query_for({"target": "@main", "q": "x", "page": 3})

    assert query_for(q=None) == "target=%40main&page=3"
    assert query_for(q="") == "target=%40main&page=3"
    assert "page=4" in query_for(page=4)


def test_query_for_encodes_what_it_carries() -> None:
    from dportsv3.tracker.routes.pages import _query_for

    out = _query_for({"q": '"><script>'})()

    assert "<" not in out and '"' not in out


# --- nothing an operator did not write becomes markup ---------------------


@pytest.fixture
def hostile(tmp_path: Path) -> TestClient:
    """Every free-text column carries markup. All of it arrives from
    somewhere else: build output, a compiler, a forge, an LLM."""
    path = tmp_path / "hostile.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db)
    db.execute(
        "UPDATE build_results SET version = ? WHERE origin = 'devel/alpha'",
        (HOSTILE,))
    db.execute("UPDATE bundles SET error_signature = ?, flavor = ?",
               (HOSTILE, HANDLER))
    db.execute("UPDATE jobs SET retire_reason = ?", (HOSTILE,))
    db.execute("UPDATE issues SET fingerprint = ?", (HOSTILE,))
    db.execute("UPDATE bundle_review_requests SET error = ?, branch = ?, "
               "status = 'create_failed'", (HOSTILE, HANDLER))
    db.execute("UPDATE activity_log SET message = ?", (HOSTILE,))
    db.execute("UPDATE runner_status SET current_stage = ?", (HOSTILE,))
    db.commit()
    db.close()
    with TestClient(create_app(path)) as test_client:
        yield test_client


@pytest.mark.parametrize("path", [
    "/", "/builds", "/builds/1", "/targets", "/target/@main",
    "/target/@main/devel/alpha", "/pipeline", "/pipeline/deliveries",
    "/agentic", "/agentic/issues", "/agentic/issues/i-1", "/agentic/bundles",
    "/agentic/bundles/b-1", "/agentic/jobs", "/agentic/jobs/j-1",
    "/agentic/runner", "/agentic/manual", "/agentic/activity",
    "/agentic/runs/r-1",
])
def test_no_page_renders_a_tag_it_was_handed(hostile, path) -> None:
    body = _get(hostile, path)

    assert "<script>alert(1)" not in body
    assert not re.search(r"<[^>]*\bon[a-z]+\s*=\s*'alert", body, re.I)


@pytest.mark.parametrize("path", [
    "/agentic/issues?q=", "/agentic/bundles?q=", "/agentic/jobs?q=",
    "/pipeline/deliveries?q=", "/agentic/bundles?origin=",
    "/agentic/issues?state=", "/pipeline/deliveries?status=",
])
def test_no_query_parameter_comes_back_as_markup(client, path) -> None:
    """The search box echoes what was typed, into an attribute."""
    body = _get(client, path + urllib.parse.quote(HOSTILE, safe=""))

    assert "<script>alert(1)" not in body
    assert not re.search(r'value="[^"]*"[^>]*>alert', body)


@pytest.mark.parametrize("renderer", ["markdown", "diff", "json", "log"])
def test_the_artifact_renderers_emit_only_their_own_tags(renderer) -> None:
    """These four are rendered with `| safe`, so their own escaping is the
    only thing between a build log and the operator's browser."""
    from dportsv3.tracker.render import text as render_text

    payloads = ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
                '<svg/onload=alert(1)>', '" onmouseover="alert(1)']
    allowed = {"p", "pre", "code", "span", "div", "table", "thead", "tbody",
               "tr", "th", "td", "ul", "ol", "li", "strong", "em", "a", "br",
               "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    wrap = {
        "markdown": lambda p: f"## {p}\n\n- {p}\n\n| {p} |\n|---|\n| {p} |\n",
        "diff": lambda p: f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-{p}\n+{p}\n",
        "json": lambda p: '{"k": "%s"}' % p.replace('"', '\\"'),
        "log": lambda p: p,
    }[renderer]
    render = {
        "markdown": render_text.render_markdown,
        "diff": render_text.render_diff,
        "json": render_text.highlight_json,
        "log": render_text.highlight_log,
    }[renderer]

    for payload in payloads:
        out = render(wrap(payload))
        tags = {t.lower() for t in
                re.findall(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)", out)}
        assert tags <= allowed, (renderer, payload, sorted(tags - allowed))
        assert not re.findall(r"<[^>]*?\bon[a-z]+\s*=", out, re.I)
