"""Repairs: the issue-centric operator workspace.

The stable unit is the fingerprinted issue; bundles are occurrences of it.
The queue's five bands, the recurrence marking and the action policy all
predate UI-5 -- what it changed is that they render in the console's shell
and that `resolving` finally says where in the confirm loop it is.

The invariant these guard: no lifecycle decision is made in a template. Band
membership comes from issue_state.issue_bucket, the status pill from
fix_state.fix_status, and every control from issue_actions /
bundle_actions.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker import issue_state
from dportsv3.tracker.server import create_app

TARGET = "@main"

# (issue_key, origin, issue state, newest occurrence's resolution,
#  its verification, confirm-column overrides)
SHAPES = [
    ("i-ready", "devel/alpha", "unresolved", "agent_fixed", "verified", {}),
    ("i-verify", "graphics/beta", "unresolved", "agent_fixed", None, {}),
    ("i-decide", "net/gamma", "unresolved", "agent_gave_up", None, {}),
    ("i-owned", "lang/delta", "unresolved", "operator_owned", None, {}),
    ("i-queued", "x11/eps", "resolving", "accepted", None,
     {"requested_build_generation": 1}),
    ("i-green", "www/eta", "resolving", "accepted", None,
     {"requested_build_generation": 1, "confirm_green_count": 1}),
    ("i-done", "sysutils/theta", "resolved", "accepted", None,
     {"green_head_run_id": 1}),
    ("i-muted", "devel/iota", "muted", "agent_gave_up", None, {}),
]


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO runs(run_id, target, build_run_id) "
                 "VALUES ('r-1', ?, 1)", (TARGET,))
    conn.execute("INSERT INTO build_runs(id, target, build_type, started_at, "
                 "finished_at) VALUES (1, ?, 'release', 't', 't')", (TARGET,))
    for key, origin, state, resolution, verification, cols in SHAPES:
        c = {"requested_build_generation": 0,
             "last_confirmed_build_generation": 0, "building_generation": None,
             "confirm_green_count": 0, "confirm_failure_count": 0,
             "next_eligible_at": None, "green_head_run_id": None}
        c.update(cols)
        conn.execute(
            "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
            "times_seen, first_seen_at, last_seen_at, updated_at, "
            "requested_build_generation, last_confirmed_build_generation, "
            "building_generation, confirm_green_count, confirm_failure_count, "
            "next_eligible_at, green_head_run_id) "
            "VALUES (?, ?, ?, ?, ?, 3, '2026-09-01T00:00:00Z', "
            "'2026-09-05T00:00:00Z', 't', ?, ?, ?, ?, ?, ?, ?)",
            (key, TARGET, origin, "fp" + key, state,
             c["requested_build_generation"],
             c["last_confirmed_build_generation"], c["building_generation"],
             c["confirm_green_count"], c["confirm_failure_count"],
             c["next_eligible_at"], c["green_head_run_id"]),
        )
        for n in range(3):
            conn.execute(
                "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
                "issue_key, result, resolution, verification_status) "
                "VALUES (?, 'r-1', ?, ?, ?, ?, 'failure', ?, ?)",
                (f"b-{key}-{n}", origin, TARGET,
                 f"2026-09-0{n + 1}T00:00:00Z", key,
                 resolution if n == 2 else "agent_gave_up",
                 verification if n == 2 else None),
            )
    conn.commit()


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


def _bands(body: str) -> list[str]:
    return re.findall(r'<div class="wl-band-head">\s*<h2>([^<]*)</h2>', body)


# --- the five bands ------------------------------------------------------


def test_the_queue_has_the_five_bands_in_urgency_order(client) -> None:
    body = client.get("/agentic").text

    assert _bands(body) == [
        "Ready to accept", "Needs verify", "Needs a decision",
        "You own", "Awaiting build confirmation",
    ]


def test_the_band_order_is_the_projections_and_not_the_templates(
    client,
) -> None:
    """The template iterates ISSUE_WORKLIST_SECTIONS; it does not know the
    order itself."""
    body = client.get("/agentic").text
    expected = [
        label for key, label, _ in issue_state.ISSUE_WORKLIST_SECTIONS
        if key not in ("done", "muted")
    ]

    assert _bands(body) == expected


def test_runner_owned_work_is_not_queue_work(tmp_path: Path) -> None:
    """An issue whose newest occurrence is still being worked belongs to the
    system, not the operator. issue_bucket returns None for it and it must
    land in no band at all."""
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute("INSERT INTO runs(run_id, target) VALUES ('r-1', ?)", (TARGET,))
    db.execute(
        "INSERT INTO issues(issue_key, target, origin, state, times_seen, "
        "updated_at) VALUES ('i-live', ?, 'devel/busy', 'unresolved', 1, 't')",
        (TARGET,),
    )
    db.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc, "
        "issue_key, result) VALUES ('b-live', 'r-1', 'devel/busy', ?, "
        "'2026-09-05T00:00:00Z', 'i-live', 'failure')",
        (TARGET,),
    )
    # The job is what makes it in-flight; the bundle row itself looks the
    # same as one nobody is working.
    db.execute(
        "INSERT INTO jobs(job_id, state, type, origin, target, bundle_id, "
        "created_ts_utc) VALUES ('j-live', 'patching', 'patch', 'devel/busy', "
        "?, 'b-live', 't')",
        (TARGET,),
    )
    db.commit()
    db.close()

    with TestClient(create_app(path)) as client:
        body = client.get("/agentic").text

    assert _bands(body) == []
    assert "devel/busy" not in body
    assert "Nothing needs you right now" in body


# --- what each band says -------------------------------------------------


def test_each_band_offers_its_own_action(client) -> None:
    body = client.get("/agentic").text

    for label in ("Accept", "Verify", "Retry latest", "Open"):
        assert label in body


def test_the_confirming_band_says_where_in_the_loop_each_issue_is(
    client,
) -> None:
    """"Awaiting build confirmation" is one word for a whole loop. The band
    used to say only that, which is why an operator could see an issue was
    stuck but not whether a build was running (poly-chf)."""
    body = client.get("/agentic").text

    assert "confirm build queued" in body
    assert "provisional green 1/2" in body


def test_a_repeatedly_failing_issue_is_marked_systemic(client) -> None:
    body = client.get("/agentic").text

    assert "systemic" in body


def test_the_archives_are_collapsed_and_present(client) -> None:
    body = client.get("/agentic").text

    assert "Recently resolved · 1" in body
    assert "Muted · 1" in body
    assert "<details" in body


# --- the issues list -----------------------------------------------------


def test_the_issues_list_filters_on_the_derived_state(client) -> None:
    """`regressed` is not a stored state: the rows come back as `resolved`
    and the split happens on read."""
    resolved = client.get("/agentic/issues?state=resolved").text
    regressed = client.get("/agentic/issues?state=regressed").text

    assert "sysutils/theta" in resolved
    assert "sysutils/theta" not in regressed


def test_the_issues_list_carries_the_confirm_column(client) -> None:
    body = client.get("/agentic/issues").text

    assert "<th>Confirm build</th>" in body
    assert "confirm build queued" in body
    assert "confirmed by build" in body


def test_an_issue_with_no_confirm_build_in_play_shows_no_pill(client) -> None:
    """An unresolved issue has no accepted fix, so there is nothing to
    confirm and a badge would be noise."""
    body = client.get("/agentic/issues?state=unresolved").text

    assert "confirm build" not in body


# --- the shell -----------------------------------------------------------


def test_repairs_navigates_like_builds(client) -> None:
    body = client.get("/agentic").text

    assert 'aria-label="Repairs sections"' in body
    assert '<h1>Repairs</h1>' in body
    assert "Agentic" not in body


def test_the_operator_only_destinations_are_hidden_from_a_reader(
    tmp_path: Path, monkeypatch,
) -> None:
    """Driving the runner and answering the manual queue are things only an
    operator can do, so they are not offered to someone who could not act."""
    from dportsv3 import settings

    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db)
    db.close()

    real = settings.get
    monkeypatch.setattr(
        settings, "get",
        lambda key, *a, **k: True if key == "tracker.public_readonly"
        else real(key, *a, **k),
    )
    with TestClient(create_app(path)) as client:
        body = client.get("/agentic").text

    assert ">Worklist<" in body
    assert ">Runner<" not in body
    assert ">Manual<" not in body
