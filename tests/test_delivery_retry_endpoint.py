"""poly-86t: retrying a delivery must not disturb the decision above it.

Accept commits ``resolution='accepted'`` and then delivers. When the
provider leg fails — most often poly-lt1's clone race — the bundle is
left accurate and unactionable: it IS accepted, and accept refuses to run
again because ``accepted`` is terminal. Measured 2026-09-04: 17
create_failed rows, every one against an accepted bundle with no PR.

The only exit was ``/reopen``, which retracts a judgement that was never
wrong and re-runs the whole accept path — re-racing the same clone.

These tests pin what ``/deliver`` may and may not touch.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker import agentic_queries as q
from dportsv3.tracker.server import create_app


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_bundle(conn, bundle_id, resolution="accepted"):
    now = _now()
    conn.execute(
        """INSERT INTO bundles (
              bundle_id, run_id, origin, flavor, ts_utc, result,
              target, path, last_seen_at, resolution, accepted_at
           ) VALUES (?, '', 'devel/foo', '', ?, 'failure',
                     '@2026Q3', '', ?, ?, ?)""",
        (bundle_id, now, now, resolution, now if resolution == "accepted" else None),
    )
    conn.commit()


def _seed_review_request(conn, bundle_id, status, error=None, branch="agentic/x"):
    rid = q.insert_review_request(
        conn, bundle_id=bundle_id, provider="github", status=status,
        provider_pr_id=None, url=None, branch=branch, title="fix",
        operator="alice", error=error, error_signature="sig-1",
    )
    conn.commit()
    return rid


@pytest.fixture
def deployment(tmp_path):
    db_path = tmp_path / "state.db"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.close()
    app = create_app(db_path)
    app.state.artifact_root = artifact_root
    return {"db_path": db_path, "app": app}


@pytest.fixture
def client(deployment):
    with TestClient(deployment["app"]) as c:
        yield c


def _open(deployment):
    conn = sqlite3.connect(str(deployment["db_path"]))
    conn.row_factory = sqlite3.Row
    return conn


CLONE_RACE = (
    "DeliveryError: another delivery is using the clone (waited 60s); "
    "retry once it finishes"
)


# --- the gate ---------------------------------------------------------

def test_the_stranded_case_is_accepted_by_the_gate(client, deployment):
    """The whole point: accepted + newest row create_failed. Accept
    itself refuses this state, which is why the bundle was stuck."""
    from dportsv3.tracker import fix_state
    assert fix_state.action_allowed("deliver", "accepted", "verified")
    assert not fix_state.action_allowed("accept", "accepted", "verified")


def test_an_unaccepted_bundle_cannot_retry_delivery(client, deployment):
    conn = _open(deployment)
    _seed_bundle(conn, "b-1", resolution="agent_fixed")
    _seed_review_request(conn, "b-1", "create_failed", error=CLONE_RACE)
    conn.close()
    resp = client.post("/api/bundles/b-1/deliver")
    assert resp.status_code == 409
    assert "accepted bundle" in resp.json()["detail"]


def test_a_bundle_that_never_delivered_has_nothing_to_retry(client, deployment):
    conn = _open(deployment)
    _seed_bundle(conn, "b-1")
    conn.close()
    resp = client.post("/api/bundles/b-1/deliver")
    assert resp.status_code == 409
    assert "nothing to retry" in resp.json()["detail"]


@pytest.mark.parametrize("status", ["created", "updated", "merged", "closed"])
def test_a_delivery_that_did_not_fail_is_not_retryable(
    client, deployment, status,
):
    """Re-delivering an open PR is the orchestrator's idempotency
    problem, not an operator button's."""
    conn = _open(deployment)
    _seed_bundle(conn, "b-1")
    _seed_review_request(conn, "b-1", status)
    conn.close()
    resp = client.post("/api/bundles/b-1/deliver")
    assert resp.status_code == 409
    assert status in resp.json()["detail"]


def test_unknown_bundle_is_404(client):
    assert client.post("/api/bundles/nope/deliver").status_code == 404


# --- what it must NOT touch -------------------------------------------

def test_the_retry_leaves_the_decision_alone(client, deployment):
    """The operator's accept was correct and stays recorded. Reopen was
    the wrong instrument precisely because it retracts this."""
    conn = _open(deployment)
    _seed_bundle(conn, "b-1")
    _seed_review_request(conn, "b-1", "create_failed", error=CLONE_RACE)
    before = conn.execute(
        "select resolution, accepted_at from bundles where bundle_id='b-1'"
    ).fetchone()
    conn.close()

    resp = client.post("/api/bundles/b-1/deliver")
    assert resp.status_code == 200, resp.text

    conn = _open(deployment)
    after = conn.execute(
        "select resolution, accepted_at from bundles where bundle_id='b-1'"
    ).fetchone()
    events = [
        r[0] for r in conn.execute(
            "select type from events "
            "where json_extract(data_json,'$.bundle_id')='b-1'"
        ).fetchall()
    ]
    conn.close()
    assert after["resolution"] == before["resolution"] == "accepted"
    assert after["accepted_at"] == before["accepted_at"]
    # A retry is not an accept. Emitting bundle_accepted again would put
    # a second acceptance in the lineage for one decision.
    assert "bundle_accepted" not in events
    assert "bundle_delivery_retried" in events


def test_the_retry_records_what_it_was_retrying(client, deployment):
    """Three of the seventeen stranded rows were not the clone race.
    Carrying the prior error forward is how an operator sees that a
    retry failed the same way rather than a new way."""
    conn = _open(deployment)
    _seed_bundle(conn, "b-1")
    _seed_review_request(conn, "b-1", "create_failed", error=CLONE_RACE)
    conn.close()
    body = client.post("/api/bundles/b-1/deliver").json()
    assert body["prior_error"] == CLONE_RACE
    assert body["resolution"] == "accepted"


def test_the_retry_writes_the_same_activity_stage_as_accept(client, deployment):
    """accept writes delivery_complete; so does this, so the ribbon
    reads as one story rather than two vocabularies."""
    conn = _open(deployment)
    _seed_bundle(conn, "b-1")
    _seed_review_request(conn, "b-1", "create_failed", error=CLONE_RACE)
    conn.close()
    client.post("/api/bundles/b-1/deliver")
    conn = _open(deployment)
    rows = conn.execute(
        "select stage, extra_json from activity_log "
        "where bundle_id='b-1' order by id desc"
    ).fetchall()
    conn.close()
    assert rows, "expected a delivery_complete row"
    assert rows[0]["stage"] == "delivery_complete"
    assert '"retry": true' in rows[0]["extra_json"].lower()


def test_delivery_off_reports_skipped_rather_than_success(client, deployment):
    """With no delivery configured the retry lands on skipped/no_config.
    That is neither a success nor a failure and must not be dressed as
    either."""
    conn = _open(deployment)
    _seed_bundle(conn, "b-1")
    _seed_review_request(conn, "b-1", "create_failed", error=CLONE_RACE)
    conn.close()
    body = client.post("/api/bundles/b-1/deliver").json()
    assert body["delivery"]["status"] == "skipped"
    assert body["delivery"]["skip_reason"] == "no_config"


# --- the id prefix is a route binding ---------------------------------

def test_every_op_prefixed_button_maps_to_a_real_endpoint(deployment):
    """agentic-bundle.js auto-wires every ``button[id^="op-"]``:

        const action = btn.id.replace('op-', '');
        dpOpAction(action, btn.dataset.bundle);

    so the id IS the route. A button named ``op-retry-delivery`` posts to
    ``/api/bundles/{id}/retry-delivery`` whether or not anyone wrote that
    endpoint — it 404s into the operator-actions status line, which is
    how this was found. Two pre-existing buttons (mark-merged,
    mark-closed) had the same collision and post via their own handler,
    so they were renamed off the prefix rather than given routes.
    """
    import re
    from pathlib import Path

    tpl = Path(__file__).resolve().parents[1] / "dportsv3/tracker/templates"
    ids: set[str] = set()
    for f in tpl.glob("*.html"):
        ids.update(
            m.group(1) for m in
            re.finditer(r'<button[^>]*\bid="(op-[a-z0-9-]+)"', f.read_text())
        )
    assert ids, "expected to find op-* buttons"

    paths = {
        r.path for r in deployment["app"].routes if hasattr(r, "path")
    }
    missing = sorted(
        f"{bid} -> /api/bundles/{{bundle_id}}/{bid.replace('op-', '', 1)}"
        for bid in ids
        if f"/api/bundles/{{bundle_id}}/{bid.replace('op-', '', 1)}"
        not in paths
    )
    assert not missing, (
        "op-prefixed buttons with no matching endpoint (they will 404 on "
        "click): " + "; ".join(missing)
    )


def test_the_retry_button_is_the_one_the_generic_handler_can_reach(deployment):
    """Guards the specific rename: the endpoint is /deliver, so the
    button must be op-deliver. Renaming either side alone silently
    repoints the button at a route that does not exist."""
    from pathlib import Path
    card = (
        Path(__file__).resolve().parents[1]
        / "dportsv3/tracker/templates/_bundle_delivery.html"
    ).read_text()
    assert 'id="op-deliver"' in card
    assert "/api/bundles/{bundle_id}/deliver" in {
        r.path for r in deployment["app"].routes if hasattr(r, "path")
    }
