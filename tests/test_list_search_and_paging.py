"""Search and paging on the operator lists.

Every list was a single capped fetch -- 300 issues, 200 bundles, 200 jobs --
with the search box filtering client-side over whatever had already arrived.
A port outside the cap could not be found by typing its name, and the box
said nothing about having only looked at the first N. Worse, issues come
back times_seen DESC, so the cap kept the loudest and dropped the long tail
-- which is where a specific port someone is looking for usually lives
(poly-0e02.5).

Search runs in SQL now and the count is of everything that matched.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import (
    count_bundles,
    count_issues,
    count_jobs,
    list_bundles,
    list_issues,
    list_jobs,
)
from dportsv3.tracker.server import create_app

CATS = ["devel", "graphics", "net", "lang", "x11", "math",
        "sysutils", "www", "textproc", "audio"]
N = 450


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO runs(run_id, target) VALUES ('r-1', '@main')")
    for i in range(N):
        origin = f"{CATS[i % 10]}/pkg{i:03d}"
        state = ["unresolved", "resolving", "resolved", "muted"][i % 4]
        conn.execute(
            "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
            "times_seen, first_seen_at, last_seen_at, updated_at) "
            "VALUES (?, '@main', ?, ?, ?, ?, 't', 't', 't')",
            (f"i-{i:04d}", origin, f"fp{i:04d}", state, (i % 7) + 1),
        )
        conn.execute(
            "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
            "issue_key, result, resolution) "
            "VALUES (?, 'r-1', ?, '@main', ?, ?, 'failure', ?)",
            (f"b-{i:04d}", origin, f"2026-09-0{1 + i % 5}", f"i-{i:04d}",
             None if i % 3 else "agent_fixed"),
        )
        conn.execute(
            "INSERT INTO jobs(job_id, state, type, origin, target, bundle_id, "
            "created_ts_utc) VALUES (?, ?, 'triage', ?, '@main', ?, ?)",
            (f"j-{i:04d}", ["queued", "done", "dead", "triaging"][i % 4],
             origin, f"b-{i:04d}", f"2026-09-0{1 + i % 5}"),
        )
    # Two origins carrying LIKE metacharacters, to prove they are escaped.
    for key, origin in (("i-meta1", "www/one_two"), ("i-meta2", "www/100%pure")):
        conn.execute(
            "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
            "updated_at) VALUES (?, '@main', ?, 'unresolved', 1, 't')",
            (key, origin),
        )
    conn.commit()


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    init_db(connection)
    _seed(connection)
    yield connection
    connection.close()


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


def _pager(body: str) -> str:
    m = re.search(r'<div class="pager">\s*<span>\s*(.*?)\s*</span>', body, re.S)
    return " ".join(m.group(1).split()) if m else ""


# --- the query layer -----------------------------------------------------


def test_search_finds_a_port_past_the_old_cap(conn) -> None:
    """pkg449 sorts last by times_seen and would never have been in the
    first 300 rows the page used to fetch."""
    found = list_issues(conn, search="pkg449")

    assert [i["origin"] for i in found] == ["audio/pkg449"]


def test_search_matches_what_an_operator_pastes(conn) -> None:
    """An issue key or a fingerprint goes in the same box as an origin."""
    assert count_issues(conn, search="i-0042") == 1
    assert count_issues(conn, search="fp0042") == 1


def test_search_ignores_case(conn) -> None:
    assert count_issues(conn, search="DEVEL/") == count_issues(
        conn, search="devel/")


@pytest.mark.parametrize(("term", "origin"), [
    ("_", "www/one_two"),
    ("%", "www/100%pure"),
])
def test_like_metacharacters_are_matched_literally(conn, term, origin) -> None:
    """Unescaped, '%' matches every row and '_' matches every row with at
    least one character."""
    found = list_issues(conn, search=term)

    assert [i["origin"] for i in found] == [origin]


def test_the_count_is_of_the_match_not_of_the_page(conn) -> None:
    assert count_issues(conn, search="devel/") == 45
    assert len(list_issues(conn, search="devel/", limit=10)) == 10


def test_offset_walks_the_whole_set_without_gaps_or_repeats(conn) -> None:
    seen: list[str] = []
    for offset in range(0, N + 2, 100):
        seen.extend(
            i["issue_key"] for i in list_issues(conn, limit=100, offset=offset)
        )

    assert len(seen) == count_issues(conn) == N + 2
    assert len(set(seen)) == len(seen)


def test_the_count_and_the_list_agree_on_every_filter(conn) -> None:
    """They are built from one WHERE, so a page and the total it is a page
    of cannot describe different sets."""
    for kwargs in (
        {}, {"search": "lang/"}, {"target": "@main"},
        {"states": ("unresolved",)},
        {"states": ("unresolved",), "search": "devel/"},
        {"origin": "devel/pkg000"},
    ):
        assert count_issues(conn, **kwargs) == len(
            list_issues(conn, limit=1000, **kwargs)), kwargs


def test_bundles_and_jobs_search_their_own_ids_too(conn) -> None:
    assert [b["origin"] for b in list_bundles(conn, search="b-0042")] == [
        "net/pkg042"]
    assert [j["origin"] for j in list_jobs(conn, search="j-0042")] == [
        "net/pkg042"]
    assert count_bundles(conn, search="lang/") == 45
    assert count_jobs(conn, search="lang/") == 45


def test_an_exact_origin_filter_is_still_exact(conn) -> None:
    """`origin` is what the port page passes; widening it to a substring
    would have quietly changed that caller."""
    assert count_bundles(conn, origin="devel/pkg000") == 1
    assert count_bundles(conn, origin="devel/") == 0


def test_bundles_can_be_filtered_by_resolution(conn) -> None:
    """NULL is a real filter -- untriaged occurrences -- and NULL never
    equals anything, so it needs its own branch."""
    assert count_bundles(conn, resolution="agent_fixed") == 150
    assert count_bundles(conn, resolution="") == 300


def test_a_job_state_bucket_still_expands(conn) -> None:
    """The bucket aliases survived the WHERE being factored out."""
    assert count_jobs(conn, state="inflight") == count_jobs(
        conn, state="triaging")


# --- the pages -----------------------------------------------------------


def test_the_issues_page_says_how_many_matched(client) -> None:
    assert _pager(client.get("/agentic/issues").text) == "1–100 of 452"


def test_the_issues_page_pages(client) -> None:
    assert _pager(client.get("/agentic/issues?page=2").text) == "101–200 of 452"


def test_searching_narrows_the_count_too(client) -> None:
    body = client.get("/agentic/issues?q=devel/").text

    assert _pager(body) == "1–45 of 45 for “devel/”"
    # The box keeps what was typed. Normalised because the attribute sits on
    # its own line in the template.
    assert 'name="q" value="devel/"' in " ".join(body.split())


def test_a_search_that_matches_nothing_says_so(client) -> None:
    """It used to render an empty table with no explanation, which looked
    the same as a list that was simply capped short of the answer."""
    body = client.get("/agentic/issues?q=nosuchport").text

    assert "nothing matches for “nosuchport”" in _pager(body)


def test_the_search_survives_paging_and_the_filter_survives_search(
    client,
) -> None:
    body = client.get("/agentic/issues?state=unresolved&q=devel/").text

    flat = " ".join(body.split())
    assert 'name="state" value="unresolved"' in flat   # carried by the form
    assert "q=devel%2F" in body or "q=devel/" in body   # carried by the links


def test_a_derived_state_filter_counts_after_the_split(client) -> None:
    """`regressed` is derived from occurrences, so SQL paging would leave
    holes: the rows it drops are interleaved with the ones it keeps. Those
    two filters page in Python, and the count is of what survived."""
    assert _pager(client.get("/agentic/issues?state=resolved").text) == (
        "1–100 of 112")
    assert "nothing matches" in _pager(
        client.get("/agentic/issues?state=regressed").text)


@pytest.mark.parametrize("path", ["/agentic/bundles", "/agentic/jobs"])
def test_the_other_lists_page_and_search_the_same_way(client, path) -> None:
    assert _pager(client.get(path).text) == "1–100 of 450"
    assert _pager(client.get(f"{path}?q=lang/").text) == "1–45 of 45 for “lang/”"


def test_the_worklist_is_silent_when_its_cap_did_not_bite(client) -> None:
    """It groups by band rather than paging, so its bound is a cap and not a
    page. With fewer issues than the cap it has nothing to disclose."""
    body = client.get("/agentic").text

    assert body.count("most-seen of") == 0


def test_the_worklist_says_when_its_cap_bit(tmp_path: Path) -> None:
    """The cap orders times_seen DESC, so what it drops is the long tail --
    exactly where a specific port someone is looking for usually lives."""
    import dportsv3.tracker.routes.pages as pages_mod

    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db)
    db.close()

    original = pages_mod._WORKLIST_CAP
    pages_mod._WORKLIST_CAP = 10
    try:
        with TestClient(create_app(path)) as client:
            body = client.get("/agentic").text
    finally:
        pages_mod._WORKLIST_CAP = original

    assert "Reading the 10 most-seen of 452 issues" in " ".join(body.split())


def test_an_issue_whose_occurrences_are_gone_does_not_break_the_worklist(
    client,
) -> None:
    """issue_bucket already bands such an issue as needing a look. The row
    used to dereference its newest occurrence and 500 the whole page."""
    body = client.get("/agentic").text

    assert "no occurrences recorded" in body


def test_the_api_lists_take_a_search_too(client) -> None:
    """The CLI reads these; a search that only works in the browser is half
    an answer."""
    rows = client.get("/api/bundles", params={"q": "lang/", "limit": 500}).json()

    assert len(rows) == 45
    assert all("lang/" in r["origin"] for r in rows)


def test_the_api_lists_take_an_offset(client) -> None:
    first = client.get("/api/jobs", params={"limit": 10}).json()
    second = client.get("/api/jobs", params={"limit": 10, "offset": 10}).json()

    assert {r["job_id"] for r in first}.isdisjoint({r["job_id"] for r in second})
