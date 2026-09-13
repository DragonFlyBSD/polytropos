"""Two occurrences of one issue, side by side (poly-0e02.14).

The cockpit shows an issue's three attempts and says how far each one got.
It never said what was DIFFERENT between them, which is close to the
question it exists to answer.

The comparison is hashes first: artifact_refs.sha256 is the blob backend's
own key, so "the agent produced the same patch again" is a string
comparison that costs no read. Only what differs is opened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker import render
from dportsv3.tracker.server import create_app

TARGET = "@main"
ORIGIN = "devel/thing"


def _seed(db: sqlite3.Connection, art: Path) -> None:
    art.mkdir(parents=True, exist_ok=True)
    db.execute("INSERT INTO runs(run_id, target) VALUES ('r-1', ?)", (TARGET,))
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "first_seen_at, last_seen_at, updated_at) VALUES ('i-1', ?, ?, "
        "'unresolved', 2, 't0', 't2', 't2')", (TARGET, ORIGIN))
    for bundle_id, ts in (("b-old", "t1"), ("b-new", "t2")):
        db.execute(
            "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
            "target, issue_key, resolution, verification_status) VALUES "
            "(?, 'r-1', ?, ?, 'failure', ?, 'i-1', 'agent_fixed', NULL)",
            (bundle_id, ORIGIN, ts, TARGET))

    def put(bundle_id: str, relpath: str, text: str, sha: str) -> None:
        path = art / f"{bundle_id}-{Path(relpath).name}"
        path.write_text(text, encoding="utf-8")
        db.execute(
            "INSERT INTO artifact_refs(bundle_id, relpath, backend, sha256, "
            "fs_path, kind, size, created_at) VALUES (?, ?, 'fs', ?, ?, "
            "'text', ?, 't1')",
            (bundle_id, relpath, sha, str(path), path.stat().st_size))

    # The patch changed between attempts...
    put("b-old", "analysis/changes.diff", "--- a\n+++ b\n-old line\n", "sha-old")
    put("b-new", "analysis/changes.diff", "--- a\n+++ b\n-new line\n", "sha-new")
    # ...the triage did not...
    put("b-old", "analysis/triage.md", "# same\n", "sha-same")
    put("b-new", "analysis/triage.md", "# same\n", "sha-same")
    # ...and only the newer attempt got far enough to write a report.
    put("b-new", "analysis/patch.md", "# report\n", "sha-only")
    db.commit()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db, tmp_path / "artifacts")
    db.close()
    with TestClient(create_app(path)) as test_client:
        yield test_client


# --- the cheap half: hashes ----------------------------------------------


def test_identical_artifacts_are_named_identical_without_being_read() -> None:
    """The blob backend keys on sha256, so the agent producing the same
    patch again is a string comparison."""
    rows = render.compare_artifacts(
        [{"relpath": "a.diff", "sha256": "x"}],
        [{"relpath": "a.diff", "sha256": "x"}],
    )

    assert rows[0]["status"] == "same"
    assert rows[0]["diffable"] is False


def test_an_artifact_only_one_attempt_produced_is_a_finding() -> None:
    """An attempt that never got far enough to write a patch has no
    changes.diff, and that is the answer rather than a gap."""
    rows = render.compare_artifacts(
        [], [{"relpath": "analysis/changes.diff", "sha256": "x"}],
    )

    assert rows[0]["status"] == "only_b"


def test_an_unhashed_artifact_says_so_rather_than_guessing() -> None:
    """The fs backend records no sha. Calling that 'changed' would be a
    claim the row cannot support."""
    rows = render.compare_artifacts(
        [{"relpath": "a.txt", "sha256": None}],
        [{"relpath": "a.txt", "sha256": None}],
    )

    assert rows[0]["status"] == "unknown"


def test_a_log_is_listed_but_not_offered_as_a_diff() -> None:
    """A 40 MB build log is not a comparison, it is a download."""
    rows = render.compare_artifacts(
        [{"relpath": "logs/full.log.gz", "sha256": "x"}],
        [{"relpath": "logs/full.log.gz", "sha256": "y"}],
    )

    assert rows[0]["status"] == "changed"
    assert rows[0]["diffable"] is False


def test_the_page_opens_on_the_fix_rather_than_the_first_file() -> None:
    """"Did the proposed fix change?" is the first thing an operator
    wants, so changes.diff wins over an alphabetically earlier artifact."""
    rows = render.compare_artifacts(
        [{"relpath": "analysis/a-first.md", "sha256": "1"},
         {"relpath": "analysis/changes.diff", "sha256": "3"}],
        [{"relpath": "analysis/a-first.md", "sha256": "2"},
         {"relpath": "analysis/changes.diff", "sha256": "4"}],
    )

    assert render.default_relpath(rows) == "analysis/changes.diff"


# --- the page -------------------------------------------------------------


def test_the_comparison_is_a_url(client) -> None:
    resp = client.get("/agentic/compare?a=b-old&b=b-new")

    assert resp.status_code == 200
    assert "b-old" in resp.text and "b-new" in resp.text


def test_it_says_which_artifacts_changed_and_which_did_not(client) -> None:
    body = client.get("/agentic/compare?a=b-old&b=b-new").text

    assert "changed" in body
    assert "identical" in body
    assert "only b-new" in body


def test_it_diffs_the_two_bodies_and_not_their_rendered_markup(
    client,
) -> None:
    """artifact_view_data returns HTML for a diff, so comparing those
    would compare the markup."""
    body = client.get("/agentic/compare?a=b-old&b=b-new").text

    assert "old line" in body
    assert "new line" in body


def test_two_identical_attempts_say_so_instead_of_showing_nothing(
    client,
) -> None:
    """The agent doing the same thing twice is a real answer, and usually
    the answer."""
    body = client.get("/agentic/compare?a=b-old&b=b-old").text

    assert "Nothing differs" in body


def test_an_unknown_occurrence_404s(client) -> None:
    assert client.get("/agentic/compare?a=b-old&b=nope").status_code == 404


def test_a_file_neither_side_has_falls_back_rather_than_erroring(
    client,
) -> None:
    """The table above is still the answer; a stale ?file= should not take
    the page down with it."""
    resp = client.get("/agentic/compare?a=b-old&b=b-new&file=nope/gone.diff")

    assert resp.status_code == 200
    assert "analysis/changes.diff" in resp.text


def test_the_selector_offers_the_comparison(client) -> None:
    """It lists occurrences and says how far each got; this is the link to
    what the agent actually did differently."""
    body = client.get("/agentic/bundles/b-new").text

    assert "/agentic/compare" in body
    assert "Compare with the previous attempt" in body


def test_a_lone_occurrence_offers_no_comparison(client, tmp_path: Path) -> None:
    """Nothing to compare it with."""
    path = tmp_path / "lone.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute("INSERT INTO runs(run_id, target) VALUES ('r-1', ?)", (TARGET,))
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
        "target) VALUES ('b-only', 'r-1', ?, 't1', 'failure', ?)",
        (ORIGIN, TARGET))
    db.commit()
    db.close()

    with TestClient(create_app(path)) as lone:
        body = lone.get("/agentic/bundles/b-only").text

    assert "Compare with the previous attempt" not in body
