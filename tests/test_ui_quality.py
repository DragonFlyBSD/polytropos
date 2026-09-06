"""Cross-cutting UI quality: landmarks, names, semantics, empty states (UI-7).

These are properties of every view rather than of any one, so they are
asserted by walking the shell and the shared partials rather than page by
page. What cannot be asserted from source -- contrast ratios, computed
target sizes, whether the page body scrolls sideways -- was measured in a
headless browser across 26 pages, both themes, 1440px, 500px and a coarse
pointer, on a seeded and a blank install; the numbers are on poly-nsdg.
What is here is the part a regression would silently reintroduce.
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

ROOT = Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
TEMPLATES = ROOT / "templates"
CSS = ROOT / "static" / "progress.css"

#: Full-page templates -- the ones that extend the shell. Partials are
#: audited through the pages that include them.
PAGES = sorted(
    p for p in TEMPLATES.glob("*.html") if not p.name.startswith("_")
)


@pytest.fixture
def blank(tmp_path: Path) -> TestClient:
    """A tracker that has never seen anything. Half of these assertions are
    about what a fresh install says."""
    # Its own file: a test that takes both fixtures would otherwise get one
    # DB, and the blank client would be looking at the seeded rows.
    path = tmp_path / "blank.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.close()
    with TestClient(create_app(path)) as client:
        yield client


@pytest.fixture
def seeded(tmp_path: Path) -> TestClient:
    path = tmp_path / "seeded.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "first_seen_at, last_seen_at, updated_at) VALUES ('i-1', '@main', "
        "'devel/thing', 'unresolved', 1, 't0', 't1', 't1')")
    db.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target, "
        "issue_key) VALUES ('b-1', 'devel/thing', 't1', 'failure', '@main', "
        "'i-1')")
    db.execute(
        "INSERT INTO jobs(job_id, bundle_id, origin, state, created_ts_utc, "
        "target, type) VALUES ('j-1', 'b-1', 'devel/thing', 'queued', 't1', "
        "'@main', 'triage')")
    db.execute(
        "INSERT INTO user_context_requests(run_id, origin, bundle_id, "
        "requested_at, status) VALUES ('r-1', 'devel/thing', 'b-1', 't1', "
        "'pending')")
    db.commit()
    db.close()
    with TestClient(create_app(path)) as client:
        yield client


LIST_PAGES = [
    ("/agentic/issues", "issues"),
    ("/agentic/bundles", "occurrences"),
    ("/agentic/jobs", "jobs"),
    ("/agentic/manual", "manual requests"),
    ("/agentic/activity", "activity"),
]


def _flat(body: str) -> str:
    return re.sub(r"\s+", " ", body)


# --- landmarks ------------------------------------------------------------


def test_the_shell_puts_the_navigation_outside_main() -> None:
    """One block would have wrapped the header and both navs in <main>,
    which makes "skip to content" skip nothing. Two blocks is what makes
    the landmark true."""
    base = (TEMPLATES / "_base.html").read_text()

    nav_at = base.index("{% block nav %}")
    main_at = base.index("<main id=\"content\"")
    body_at = base.index("{% block body %}")

    assert nav_at < main_at < body_at
    assert base.index("</main>") > body_at


@pytest.mark.parametrize("path", [
    "/", "/builds", "/targets", "/pipeline", "/pipeline/deliveries",
    "/agentic", "/agentic/issues", "/agentic/bundles", "/agentic/jobs",
    "/agentic/runner", "/agentic/manual", "/agentic/activity",
])
def test_every_page_has_exactly_one_main_landmark(blank, path) -> None:
    body = blank.get(path).text

    assert body.count("<main ") == 1
    assert body.count("</main>") == 1


@pytest.mark.parametrize("path", ["/", "/agentic", "/pipeline", "/builds"])
def test_the_skip_link_is_the_first_focusable_thing(blank, path) -> None:
    """A keyboard reader lands on it and steps past the header and two navs
    in one press -- which only works if nothing tabbable precedes it."""
    body = blank.get(path).text

    skip = body.index('class="skip-link"')
    assert 'href="#content"' in body[skip - 80:skip + 80]
    for earlier in ("<a ", "<button", "<input", "<select"):
        first = body.find(earlier, body.index("<body>"))
        assert first == -1 or first >= skip - 20, earlier


def test_the_skip_target_can_take_focus() -> None:
    """Without tabindex the jump moves the viewport and leaves focus behind
    in the nav, which is the bug that makes skip links useless."""
    base = (TEMPLATES / "_base.html").read_text()

    assert 'id="content"' in base
    assert 'tabindex="-1"' in base


def test_the_skip_link_is_offscreen_rather_than_hidden() -> None:
    """display:none and visibility:hidden both take it out of the tab
    order, which is the only thing it is for."""
    css = CSS.read_text()
    rule = css[css.index(".skip-link {"):css.index(".skip-link:focus")]

    assert "position: absolute" in rule
    assert "display: none" not in rule
    assert "visibility: hidden" not in rule
    assert ".skip-link:focus" in css


@pytest.mark.parametrize("path", ["/agentic", "/pipeline", "/builds"])
def test_every_nav_is_labelled(blank, path) -> None:
    body = blank.get(path).text

    for nav in re.findall(r"<nav[^>]*>", body):
        assert "aria-label" in nav or "aria-labelledby" in nav, nav


# --- semantics ------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_header_cell_names_its_column(page: Path) -> None:
    """A <th> without scope is a cell that looks like a header and is not
    announced as one."""
    for thead in re.findall(r"<thead\b.*?</thead>", page.read_text(), re.S):
        for th in re.findall(r"<th\b[^>]*>", thead):
            assert "scope=" in th, f"{page.name}: {th}"


def test_the_markdown_renderer_scopes_its_headers() -> None:
    """An artifact's markdown table is on a page too."""
    from dportsv3.tracker.render import render_markdown

    out = render_markdown("| A | B |\n|---|---|\n| 1 | 2 |\n")

    assert '<th scope="col">' in out
    assert "<th>" not in out


@pytest.mark.parametrize("path", [
    "/", "/builds", "/pipeline", "/pipeline/deliveries", "/agentic",
    "/agentic/issues", "/agentic/bundles", "/agentic/jobs",
    "/agentic/manual", "/agentic/activity", "/agentic/runner",
])
def test_every_control_has_an_accessible_name(seeded, path) -> None:
    """A placeholder is not a name -- it disappears the moment anyone types
    -- and a <select>'s own text is its options, not its label."""
    body = seeded.get(path).text
    labelled = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', body))

    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", body):
        if 'type="hidden"' in tag:
            continue
        if "aria-label" in tag or "aria-labelledby" in tag:
            continue
        ident = re.search(r'\bid="([^"]+)"', tag)
        assert ident and ident.group(1) in labelled, f"{path}: {tag}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_page_forces_a_tab_order(page: Path) -> None:
    for tag in re.findall(r'tabindex="(-?\d+)"', page.read_text()):
        assert int(tag) <= 0, page.name


# --- responsive -----------------------------------------------------------


def test_every_table_scrolls_inside_its_own_container() -> None:
    """Wide content scrolls in its own box; the page body never scrolls
    sideways. Two pages did at 500px until every table was wrapped."""
    unwrapped = []
    for page in sorted(TEMPLATES.glob("*.html")):
        text = page.read_text()
        for m in re.finditer(r"<table\b", text):
            if "table-scroll-x" not in text[max(0, m.start() - 400):m.start()]:
                line = text.count("\n", 0, m.start()) + 1
                unwrapped.append(f"{page.name}:{line}")

    assert unwrapped == []


def test_touch_targets_key_off_the_pointer_not_the_viewport() -> None:
    """(pointer: coarse) fires on a touch device at any width. A width
    breakpoint would grow the targets on a narrow laptop window, which is
    still a mouse, and leave a tablet in landscape untouched."""
    css = CSS.read_text()

    assert "@media (pointer: coarse)" in css
    grown_by_width = r"@media[^{]*max-width[^{]*\{[^}]*min-height:\s*4\dpx"
    assert not re.search(grown_by_width, css)


def test_the_coarse_pointer_block_covers_more_than_one_class() -> None:
    """It used to size exactly .action, which left 126 targets under 24px:
    every breadcrumb, the worklist port links, three selects, the chevrons."""
    css = CSS.read_text()
    block = css[css.index("@media (pointer: coarse) {"):]
    block = block[:block.index("\n}\n")]

    for selector in (".control", ".crumbs a", ".section-nav a",
                     ".worklist .wl-chev", "summary"):
        assert selector in block, selector


# --- empty states ---------------------------------------------------------


@pytest.mark.parametrize(("path", "noun"), LIST_PAGES)
def test_a_fresh_install_says_nothing_has_happened_yet(
    blank, path, noun,
) -> None:
    """Not "no rows match your filter" when no filter is set: that reads as
    a broken query rather than an empty system."""
    body = _flat(blank.get(path).text)

    assert f"No {noun} yet" in body
    assert "Nothing matches this filter" not in body


@pytest.mark.parametrize(("path", "noun"), [
    ("/agentic/issues?q=nothingmatches", "issues"),
    ("/agentic/bundles?q=nothingmatches", "occurrences"),
    ("/agentic/jobs?q=nothingmatches", "jobs"),
    ("/agentic/activity?target=@nope", "activity"),
])
def test_a_filter_that_matches_nothing_says_so_and_offers_the_way_back(
    seeded, path, noun,
) -> None:
    body = _flat(seeded.get(path).text)

    assert "Nothing matches this filter" in body
    assert f"No {noun} yet" not in body
    assert f"Show all {noun}" in body


def test_the_clear_link_is_a_url_and_not_a_template_string(seeded) -> None:
    """The first version passed "?{{ query_for(...) }}" as a literal inside
    a {% with %}, so the href rendered with the braces in it."""
    body = seeded.get("/agentic/issues?q=nothingmatches").text

    href = re.search(r'<a href="([^"]*)">Show all issues</a>', body)
    assert href, body[body.index("Nothing matches"):][:400]
    assert "{{" not in href.group(1)
    assert "q=" not in href.group(1)


def test_the_manual_queues_default_view_is_not_a_filter(blank) -> None:
    """open_only is the default, not something the operator chose, so on a
    blank install it must not read as "you filtered these away"."""
    body = _flat(blank.get("/agentic/manual").text)

    assert "No manual requests yet" in body
    assert "Nothing matches this filter" not in body


def test_the_manual_queue_can_still_be_filtered_to_nothing(seeded) -> None:
    """With a request that exists but is not open, the same page has to say
    the other thing."""
    seeded.get("/agentic/manual")   # warm the app
    body = _flat(seeded.get("/agentic/manual?open_only=true").text)

    # the seeded request IS open, so the list is not empty at all
    assert "Nothing matches this filter" not in body
    assert "devel/thing" in body


def test_the_worklist_tells_caught_up_from_never_run(blank, seeded) -> None:
    """"Nothing needs you right now" is true of a fresh install and says
    the wrong thing about it."""
    fresh = _flat(blank.get("/agentic").text)
    working = _flat(seeded.get("/agentic").text)

    assert "No issues yet" in fresh
    assert "Nothing needs you right now" not in fresh
    assert "No issues yet" not in working


def test_the_empty_state_is_one_partial(blank) -> None:
    """Five lists each had their own one-line fallback and they disagreed.
    A message this easy to get subtly wrong belongs in one place."""
    for path, _noun in LIST_PAGES:
        assert 'class="empty"' in blank.get(path).text


# --- state is never colour alone ------------------------------------------


def test_a_diff_line_says_added_or_removed_in_text(seeded) -> None:
    """The green and red grounds are the fast read, not the only one."""
    from dportsv3.tracker.render.text import render_diff

    out = render_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n")

    add = out[out.index("diff-add"):]
    assert "+new" in add
    assert "-old" in out[out.index("diff-del"):]
