"""The shell is an application frame, not a page header (M1).

The mock this UI was drawn from is a fixed-viewport console: a command
bar that stays put, one scrolling region of work, and a status rail.
What was built instead was an ordinary document with a header on top, so
the chrome scrolled away with the work and the three facts that are true
of the whole system -- what is queued, what needs a person, whether the
runner is alive -- appeared nowhere at all.

The facts are the load-bearing part and the reason this has a module of
its own. They cost 25.8 ms per render on 20,000 issues, almost all of it
the worklist band projection, and the shell is on every page. The cheap
substitute is dishonest: counting unresolved issues takes 0.16 ms and
answers 7% high, because a runner-owned issue is open and does not need
you. So they are memoised, and the memo is keyed by database.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker import shell_facts
from dportsv3.tracker.server import create_app

CSS = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker" / "static"
       / "progress.css")
TEMPLATES = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
             / "templates")


@pytest.fixture(autouse=True)
def _fresh():
    shell_facts.reset()
    yield
    shell_facts.reset()


def _seed(db: sqlite3.Connection, *, issues: int = 3, queued: int = 2) -> None:
    db.execute(
        "INSERT INTO build_runs(id, target, build_type, started_at) "
        "VALUES (1, '@main', 'release', 't0')")
    for n in range(issues):
        db.execute(
            "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
            "first_seen_at, last_seen_at, updated_at) VALUES (?, '@main', ?, "
            "'unresolved', 1, 't0', 't1', 't1')",
            (f"i-{n}", f"devel/p{n}"))
        db.execute(
            "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target, "
            "issue_key, resolution) VALUES (?, ?, 't1', 'failure', '@main', "
            "?, 'triage_failed')", (f"b-{n}", f"devel/p{n}", f"i-{n}"))
    for n in range(queued):
        db.execute(
            "INSERT INTO jobs(job_id, origin, state, created_ts_utc, target, "
            "type) VALUES (?, 'devel/x', 'queued', 't1', '@main', 'triage')",
            (f"q-{n}",))
    db.commit()


def _db(tmp_path: Path, name: str = "state.db", **kw) -> Path:
    path = tmp_path / name
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db, **kw)
    db.close()
    return path


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    with TestClient(create_app(_db(tmp_path))) as test_client:
        yield test_client


def _flat(body: str) -> str:
    return re.sub(r"\s+", " ", body)


# --- the frame ------------------------------------------------------------


def test_the_page_is_a_frame_with_the_work_in_the_middle(client) -> None:
    """Command bar, one scrolling region, status rail -- in that order,
    all three inside the frame."""
    body = client.get("/pipeline").text

    app = body.index('<div class="app">')
    header = body.index("<header", app)
    main = body.index("<main", app)
    rail = body.index('class="status-rail"', app)

    assert app < header < main < rail
    assert body.index("</div>", rail) > rail


def test_only_the_middle_row_scrolls() -> None:
    """The chrome staying put is the whole difference between a console
    and a web page."""
    css = CSS.read_text()
    frame = css[css.index(".app {"):css.index(".app-header {")]

    assert "grid-template-rows" in frame
    assert "100dvh" in frame
    assert "overflow: auto" in frame


def test_the_frame_becomes_a_page_again_on_a_phone() -> None:
    """A phone has no room for chrome that never scrolls away, and UI-7's
    narrow layout depends on the document flowing."""
    css = CSS.read_text()
    narrow = css[css.index("/* --- The frame, narrow (M1)"):]
    narrow = narrow[:narrow.index("\n}\n") + 3]

    assert "max-width: 760px" in narrow
    assert "overflow: auto" in narrow
    assert "height: auto" in narrow


def test_the_skip_link_still_comes_before_the_frame(client) -> None:
    """UI-7's rule survives: nothing focusable precedes it."""
    body = client.get("/pipeline").text

    assert body.index('class="skip-link"') < body.index('<div class="app">')


def test_there_is_still_exactly_one_main(client) -> None:
    body = client.get("/pipeline").text

    assert body.count("<main ") == 1


# --- the facts ------------------------------------------------------------


def test_the_command_bar_carries_the_three_system_facts(client) -> None:
    body = _flat(client.get("/builds").text)

    assert "<span>Queue</span>" in body
    assert "<span>Needs you</span>" in body
    assert "<span>Runner</span>" in body


def test_needs_you_is_the_operator_bands_and_not_every_open_issue(
    tmp_path: Path,
) -> None:
    """An issue the runner is working is open and does not need you.
    Counting `unresolved` is 0.16 ms and 7% high; the bands are 24 ms and
    right, which is what the memo exists to afford."""
    path = _db(tmp_path)
    db = sqlite3.connect(str(path))
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "first_seen_at, last_seen_at, updated_at) VALUES ('i-live', '@main', "
        "'devel/busy', 'unresolved', 1, 't0', 't1', 't1')")
    db.execute(
        "INSERT INTO bundles(bundle_id, origin, ts_utc, result, target, "
        "issue_key) VALUES ('b-live', 'devel/busy', 't1', 'failure', '@main', "
        "'i-live')")
    db.execute(
        "INSERT INTO jobs(job_id, bundle_id, origin, state, created_ts_utc, "
        "target, type) VALUES ('j-live', 'b-live', 'devel/busy', 'patching', "
        "'t1', '@main', 'patch')")
    db.commit()
    db.close()

    facts = shell_facts.current(str(path))

    assert facts.issues == 4          # four fingerprinted
    assert facts.needs_you == 3       # the runner owns the fourth


def test_the_queue_is_what_the_runner_is_holding(client, tmp_path) -> None:
    facts = shell_facts.current(str(tmp_path / "state.db"))

    assert facts.queue == 2


def test_a_dead_runner_is_not_reported_as_its_last_status(
    tmp_path: Path,
) -> None:
    """The status column survives the process. The bar must not repeat it
    as though it were current."""
    path = _db(tmp_path)
    db = sqlite3.connect(str(path))
    db.execute(
        "INSERT INTO runner_status(id, status, updated_at) "
        "VALUES (1, 'processing', '2020-01-01T00:00:00+00:00')")
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = _flat(client.get("/builds").text)

    assert "not running" in body
    assert "<strong>processing</strong>" not in body


# --- the memo -------------------------------------------------------------


def test_a_second_render_reuses_the_reading(tmp_path, set_setting) -> None:
    set_setting("tracker.shell_facts_seconds", 60)
    path = str(_db(tmp_path))
    calls: list[int] = []
    real = shell_facts._read

    def counted(p):
        calls.append(1)
        return real(p)

    shell_facts._read = counted
    try:
        shell_facts.current(path)
        shell_facts.current(path)
        shell_facts.current(path)
    finally:
        shell_facts._read = real

    assert len(calls) == 1


def test_zero_recomputes_every_render(tmp_path, set_setting) -> None:
    set_setting("tracker.shell_facts_seconds", 0)
    path = str(_db(tmp_path))
    calls: list[int] = []
    real = shell_facts._read
    shell_facts._read = lambda p: (calls.append(1), real(p))[1]
    try:
        shell_facts.current(path)
        shell_facts.current(path)
    finally:
        shell_facts._read = real

    assert len(calls) == 2


def test_the_memo_is_keyed_by_database(tmp_path, set_setting) -> None:
    """One process serves one tracker in production, but create_app takes
    a path and nothing stops two -- the test suite builds dozens. An
    unkeyed memo hands the second one the first one's runner."""
    set_setting("tracker.shell_facts_seconds", 60)
    quiet = str(_db(tmp_path, "quiet.db", issues=1, queued=0))
    busy = str(_db(tmp_path, "busy.db", issues=6, queued=5))

    assert shell_facts.current(quiet).issues == 1
    assert shell_facts.current(busy).issues == 6
    assert shell_facts.current(quiet).issues == 1


def test_concurrent_renders_do_not_each_recompute(tmp_path, set_setting) -> None:
    set_setting("tracker.shell_facts_seconds", 60)
    path = str(_db(tmp_path))
    calls: list[int] = []
    real = shell_facts._read
    shell_facts._read = lambda p: (calls.append(1), real(p))[1]
    start = threading.Barrier(8)

    def render() -> None:
        start.wait()
        shell_facts.current(path)

    try:
        threads = [threading.Thread(target=render) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        shell_facts._read = real

    assert len(calls) == 1


def test_an_unreadable_database_renders_a_header_rather_than_a_500(
    tmp_path: Path,
) -> None:
    """A header that takes the page down with it is worse than one that
    says nothing."""
    facts = shell_facts.current(str(tmp_path / "does-not-exist" / "x.db"))

    assert facts.loaded
    assert facts.queue == 0


# --- the status rail and the audience pill --------------------------------


def test_the_rail_carries_inventory_and_not_a_polling_claim(client) -> None:
    """The mock's rail says "Poll 10s builds / 3s jobs". Ours differs per
    page -- 30s on Builds while a run is active, 4s on the run view -- so
    a single number would describe pages it is not about."""
    body = _flat(client.get("/builds").text)
    rail = body[body.index('class="status-rail"'):]
    rail = rail[:rail.index("</footer>")]

    assert "target" in rail and "issue" in rail and "occurrence" in rail
    assert "Poll" not in rail


def test_the_rail_agrees_with_the_facts(client, tmp_path) -> None:
    facts = shell_facts.current(str(tmp_path / "state.db"))
    body = _flat(client.get("/builds").text)

    assert f"<strong>{facts.issues}</strong> issues" in body


def test_an_anonymous_reader_is_told_why_the_controls_are_gone(
    tmp_path: Path, set_setting,
) -> None:
    """Not a page with holes in it. The action surface is one separable
    region (poly-0e02.12); saying so is the other half."""
    path = _db(tmp_path)
    with TestClient(create_app(path)) as operator:
        assert "aud-pill" not in operator.get("/agentic").text

    set_setting("tracker.public_readonly", True)
    with TestClient(create_app(path)) as reader:
        body = reader.get("/agentic").text

    assert "aud-pill" in body
    assert "anonymous" in body


# --- the toast ------------------------------------------------------------


def test_the_toast_region_is_announced(client) -> None:
    body = client.get("/builds").text

    assert 'id="toast"' in body
    assert 'role="status"' in body
    assert 'aria-live="polite"' in body


def test_the_toast_is_outside_the_frame(client) -> None:
    """It is fixed to the viewport, so nesting it in a scrolling region
    would scroll it away from the thing it is announcing."""
    body = client.get("/builds").text

    assert body.index('id="toast"') > body.index('class="status-rail"')
