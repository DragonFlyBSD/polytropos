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


def test_the_row_opens_a_workspace_rather_than_naming_an_action(
    client,
) -> None:
    """Every band used to put its own verb on the row -- Accept, Verify,
    Retry latest -- on a button that was a link to ?occ=. So a green
    "Accept" in the queue moved a selection and accepted nothing. One
    honest label; the acting is done by the operator action bar in the
    pane the row opens (poly-x3pg.7)."""
    body = client.get("/agentic").text
    rail = body.split('class="repair-detail"')[0]

    assert "Review \u2192" in rail
    for gone in ("wl-btn-accept", "wl-btn-verify", "wl-btn-decide",
                 "wl-btn-take", "Retry latest"):
        assert gone not in rail, gone


def test_the_queue_filters(client) -> None:
    """The queue caps at 500 issues and the cap drops the long tail, which
    is exactly where the port someone came looking for lives. Scrolling to
    it is not a search (poly-x3pg.7)."""
    all_rail = client.get("/agentic").text.split('class="repair-detail"')[0]
    assert "devel/alpha" in all_rail
    assert "graphics/beta" in all_rail

    filtered = client.get("/agentic?q=graphics").text
    rail = filtered.split('class="repair-detail"')[0]

    assert "graphics/beta" in rail
    assert "devel/alpha" not in rail


def test_a_filter_that_matches_nothing_says_so(client) -> None:
    """A third nothing. The queue would otherwise have claimed nothing
    needs you, which is a different and much better piece of news."""
    body = client.get("/agentic?q=no-such-port").text

    assert "No issue matches" in body
    assert "Nothing needs you right now" not in body


def test_a_band_chip_narrows_the_queue(client) -> None:
    """They were #wl-<band> anchors: at 500 issues that lands you on a
    heading with every other band still rendered below it, which is the
    scrolling problem again."""
    body = client.get("/agentic?band=verify").text
    rail = body.split('class="repair-detail"')[0]

    assert "graphics/beta" in rail          # the verify band
    assert "devel/alpha" not in rail        # the ready band

    # ...and the chips still count every band, because they are the only
    # place the others are visible while one is showing.
    assert "Ready to accept" in rail


def test_an_unknown_band_shows_the_whole_queue(client) -> None:
    """A hand-typed or stale band is not an error; the queue is the view."""
    rail = client.get("/agentic?band=nonsense").text.split(
        'class="repair-detail"')[0]

    assert "devel/alpha" in rail
    assert "graphics/beta" in rail


def test_the_filter_survives_selecting_an_occurrence(client) -> None:
    """Otherwise picking a row out of a filtered queue throws you back to
    all 500 of them."""
    rail = client.get("/agentic?q=graphics").text.split(
        'class="repair-detail"')[0]

    assert "q=graphics" in rail
    assert "occ=" in rail


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

    # scope="col" since UI-7: every header cell names its column for a
    # screen reader rather than being a bare <th>.
    assert '<th scope="col">Confirm build</th>' in body
    assert "confirm build queued" in body
    assert "confirmed by build" in body


def test_an_issue_with_no_confirm_build_in_play_shows_no_pill(client) -> None:
    """An unresolved issue has no accepted fix, so there is nothing to
    confirm and a badge would be noise."""
    body = client.get("/agentic/issues?state=unresolved").text
    # <main>, not the whole document: the operator guide is shell chrome
    # outside it, and its chapters name every status and control the
    # tracker has (poly-9u7).
    page = body[body.index("<main "):body.index("</main>")]

    assert "confirm build" not in page


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
    # Matched as links. The command bar names the runner as a system fact
    # now, and reading its state is not a destination an anonymous viewer
    # is being offered.
    assert ">Runner</a>" not in body
    assert ">Manual</a>" not in body
    # ...and the shell says why the controls are missing rather than
    # leaving holes (M1).
    assert "anonymous" in body


# --- a verify in flight looks different from one nobody asked for ---------
#
# fix_status's agent_fixed branch returned before it ever read the job
# state, so an occurrence being verified sat in "Needs verify" looking
# untouched for the several minutes the job took. The data was already on
# the row -- _OCCURRENCE_JOB_STATE puts the newest job's state there -- it
# was simply never consulted on this path (poly-x3pg.11).


def test_an_untouched_fix_still_says_it_needs_verifying() -> None:
    from dportsv3.tracker import fix_state

    status = fix_state.fix_status(
        {"resolution": "agent_fixed", "verification_status": None})

    assert status.key == "needs_review"


def test_a_running_verify_says_so() -> None:
    from dportsv3.tracker import fix_state

    bundle = {"resolution": "agent_fixed", "verification_status": None,
              "job_state": "verifying_fix"}

    assert fix_state.fix_status(bundle).key == "verifying"


def test_a_verify_the_runner_has_not_picked_up_says_so_too() -> None:
    """One tick normally, forever if the runner is down -- which is
    exactly when the queue must not look untouched."""
    from dportsv3.tracker import fix_state

    bundle = {"resolution": "agent_fixed", "verification_status": None,
              "verify_request_status": "pending"}

    assert fix_state.fix_status(bundle).key == "verify_queued"


def test_a_verify_in_flight_stays_findable_in_the_queue() -> None:
    """Hiding it would be defensible -- a job holds it, nothing to decide
    -- but build_issue_worklist drops a bucket-None issue entirely, so the
    port you just clicked Verify on would vanish from the queue and from a
    filter search for it."""
    from dportsv3.tracker import fix_state

    for state in ("verifying", "verify_queued"):
        assert fix_state._WORKLIST_BUCKET[state] == "verify", state


def test_a_finished_verify_beats_a_stale_job_row() -> None:
    """The result posts back to the bundle; the job row is not what
    decides, and a job left in an in-flight state must not mask it."""
    from dportsv3.tracker import fix_state

    passed = {"resolution": "agent_fixed", "verification_status": "verified",
              "job_state": "verifying_fix"}
    failed = {"resolution": "agent_fixed",
              "verification_status": "verification_failed",
              "job_state": "verifying_fix"}

    assert fix_state.fix_status(passed).key == "verified"
    assert fix_state.fix_status(failed).key == "verify_failed"


def test_the_queue_carries_the_verify_request_it_needs(client) -> None:
    """The status comes off the occurrence row, so the query has to put it
    there -- one indexed lookup per row, like the job state beside it."""
    from dportsv3.tracker.agentic_queries import issues as q

    assert "verify_request_status" in q._OCCURRENCE_SELECT


def test_the_worklist_reflects_a_verify_end_to_end(client, tmp_path) -> None:
    """The whole point: the row changes when you ask for a verify, changes
    again when it starts, and lands when it finishes."""
    import sqlite3

    def band_and_pill() -> tuple[str, str]:
        body = client.get("/agentic?q=alpha").text
        rail = body.split('class="repair-detail"')[0]
        flat = re.sub(r"\s+", " ", rail)
        m = re.search(
            r'<div class="wl-row ([a-z]+)[^"]*"[^>]*>.*?'
            r'wl-port[^>]*>(devel/alpha)<.*?class="wl-meta">(.*?)</div>', flat)
        assert m, "row not found"
        pills = [p.strip() for p in re.findall(r">([^<>]+)</span>", m.group(3))]
        return m.group(1), " ".join(pills)

    db = sqlite3.connect(str(client.app.state.db_path), isolation_level=None)
    # devel/alpha's newest occurrence is agent_fixed + verified; walk a
    # fresh unverified one instead.
    db.execute("UPDATE bundles SET verification_status = NULL "
               "WHERE bundle_id = 'b-i-ready-2'")
    assert band_and_pill()[1].count("agent fixed") == 1

    db.execute("INSERT INTO verify_requests(bundle_id, env, requested_by, "
               "requested_at, status) VALUES ('b-i-ready-2', 'e', "
               "'operator', 't', 'pending')")
    assert "verify queued" in band_and_pill()[1]

    db.execute("UPDATE verify_requests SET status = 'enqueued'")
    db.execute("INSERT INTO jobs(job_id, bundle_id, state, created_ts_utc) "
               "VALUES ('j-v', 'b-i-ready-2', 'verifying_fix', 't')")
    assert "verifying" in band_and_pill()[1]

    db.execute("UPDATE jobs SET state = 'done' WHERE job_id = 'j-v'")
    db.execute("UPDATE bundles SET verification_status = 'verified' "
               "WHERE bundle_id = 'b-i-ready-2'")
    band, pills = band_and_pill()
    db.close()

    assert band == "ready"
    assert "verified" in pills
