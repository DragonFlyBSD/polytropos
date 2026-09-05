"""Two audiences share every view: an operator who drives the loop, and an
anonymous reader who wants to know how the build is going.

One capability answers it — ``_common.can_operate``, resolved from
``tracker.public_readonly`` until operator auth (poly-fij.5) gives it a
session to read. Three properties matter and are pinned here:

- the read path is untouched for everyone, including the machine ingest
  the farm depends on;
- every mutating control disappears from the page AND is refused at the
  endpoint, because a hidden button in front of a live handler is a hidden
  button, not read-only; and
- agent working material (session dumps, fix-chat) is operator-only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.tracker.db import init_db
from dportsv3.tracker.server import create_app
from dportsv3.tracker import fix_state, issue_state


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = init_db(str(path))
    now = _now()
    conn.execute(
        """INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, target,
             result, path, last_seen_at, issue_key, resolution,
             verification_status)
           VALUES ('b-1', 'r-1', 'lang/gcc14', ?, '@main', 'failure', '', ?,
                   'i-1', 'agent_fixed', 'verified')""", (now, now))
    conn.execute(
        """INSERT INTO issues(issue_key, target, origin, fingerprint, state,
             times_seen, first_seen_at, last_seen_at, latest_bundle_id,
             updated_at)
           VALUES ('i-1', '@main', 'lang/gcc14', 'fp1', 'unresolved', 1,
                   ?, ?, 'b-1', ?)""", (now, now, now))
    conn.commit()
    conn.close()
    return path


def _client(db: Path, tmp_path: Path, *, readonly: bool, monkeypatch):
    cfg = tmp_path / ("cfg-anon" if readonly else "cfg-op")
    cfg.mkdir(exist_ok=True)
    (cfg / "polytropos.toml").write_text(
        f"[tracker]\npublic_readonly = {str(readonly).lower()}\n"
    )
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(cfg))
    from dportsv3 import settings
    settings.reload() if hasattr(settings, "reload") else None
    app = create_app(str(db))
    app.state.artifact_root = str(tmp_path / "evidence")
    return TestClient(app)


# --- the policy layer, pure ------------------------------------------------


def test_an_anonymous_reader_is_offered_no_bundle_action():
    """Not a panel of disabled buttons: an anonymous reader is not a blocked
    operator, so the panel is absent entirely."""
    bundle = {"resolution": "agent_fixed", "verification_status": "verified",
              "target": "@main", "origin": "lang/gcc14"}

    assert fix_state.bundle_actions(bundle)["show"] is True
    anon = fix_state.bundle_actions(bundle, can_operate=False)
    assert anon["show"] is False
    assert not any(v for k, v in anon.items() if k.startswith("can_"))


def test_an_anonymous_reader_is_offered_no_issue_action():
    issue = {"state": "unresolved", "times_seen": 1}

    assert issue_state.issue_actions(issue)["can_mute"] is True
    assert not any(issue_state.issue_actions(issue, can_operate=False).values())


@pytest.mark.parametrize("action", ["accept", "reject", "retry", "discard",
                                    "take-over", "release", "reopen", "verify"])
def test_the_audience_is_checked_before_the_state(action):
    """State can never re-permit what the audience forbids, whatever the
    bundle looks like."""
    assert fix_state.action_allowed(
        action, "agent_fixed", "verified", can_operate=False) is False


@pytest.mark.parametrize("action", ["mute", "resolve", "reopen", "build"])
def test_the_audience_is_checked_before_the_issue_state(action):
    assert issue_state.issue_action_allowed(
        action, "resolving", can_operate=False) is False


# --- the endpoint gate -----------------------------------------------------


MUTATING = [
    "/api/bundles/b-1/accept", "/api/bundles/b-1/reject",
    "/api/bundles/b-1/retry", "/api/bundles/b-1/take-over",
    "/api/bundles/b-1/discard", "/api/bundles/b-1/release",
    "/api/bundles/b-1/reopen", "/api/bundles/b-1/deliver",
    "/api/bundles/b-1/delivery/status", "/api/bundles/b-1/verify",
    "/api/issues/i-1/mute", "/api/issues/i-1/resolve",
    "/api/issues/i-1/build", "/api/jobs/j-1/abandon",
    "/api/config/active-env",
]


@pytest.mark.parametrize("url", MUTATING)
def test_every_mutating_endpoint_refuses_an_anonymous_caller(
    url, db, tmp_path, monkeypatch,
):
    """Refused at the endpoint, not merely unrendered, and refused with the
    real reason: an empty body must not produce a 400 that reveals the
    schema instead of a 403."""
    with _client(db, tmp_path, readonly=True, monkeypatch=monkeypatch) as cl:
        send = cl.put if url.endswith("active-env") else cl.post
        assert send(url, json={}).status_code == 403


def test_the_read_path_is_untouched(db, tmp_path, monkeypatch):
    with _client(db, tmp_path, readonly=True, monkeypatch=monkeypatch) as cl:
        for url in ("/", "/builds", "/agentic", "/agentic/issues",
                    "/agentic/issues/i-1", "/agentic/bundles",
                    "/agentic/bundles/b-1", "/agentic/jobs"):
            assert cl.get(url).status_code == 200, url


def test_machine_ingest_still_works_read_only(db, tmp_path, monkeypatch):
    """public_readonly is about the operator UI. The farm's hooks must keep
    posting evidence, or turning it on would stop the build recording."""
    with _client(db, tmp_path, readonly=True, monkeypatch=monkeypatch) as cl:
        resp = cl.post("/v1/bundles/upsert",
                       json={"bundle_id": "b-2", "origin": "www/nginx"})
        assert resp.status_code == 200


def test_agent_internals_are_operator_only(db, tmp_path, monkeypatch):
    """Tier 3: not mutating, but the agent's own working material rather
    than build status."""
    with _client(db, tmp_path, readonly=True, monkeypatch=monkeypatch) as cl:
        assert cl.post("/api/bundles/b-1/chat", json={
            "messages": [{"role": "user", "content": "why"}]},
        ).status_code == 403
        assert cl.get(
            "/agentic/bundles/b-1/sessions/x.jsonl.gz").status_code == 403


# --- what the page composes ------------------------------------------------


def test_the_page_omits_what_it_will_not_accept(db, tmp_path, monkeypatch):
    with _client(db, tmp_path, readonly=False, monkeypatch=monkeypatch) as cl:
        op = cl.get("/agentic/bundles/b-1").text
        op_nav = cl.get("/agentic").text
    with _client(db, tmp_path, readonly=True, monkeypatch=monkeypatch) as cl:
        anon = cl.get("/agentic/bundles/b-1").text
        anon_nav = cl.get("/agentic").text

    assert "Operator actions" in op and "Operator actions" not in anon
    # Runner and Manual are places only an operator can act.
    assert ">Runner<" in op_nav and ">Runner<" not in anon_nav
    assert ">Manual<" in op_nav and ">Manual<" not in anon_nav
