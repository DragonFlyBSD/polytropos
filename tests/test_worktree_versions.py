"""The working tree, versioned by attempt (poly-5tgc).

The runner publishes one snapshot per attempt and re-publishes it on every
edit, so the job page can show what the agent changed WHILE it works --
before analysis/changes.diff exists, which is written at the end and keyed
to the bundle rather than the job.

The tracker only reads. It runs unprivileged, has no chroot, and under
poly-fij may not be on the builder at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dportsv3.common import artifacts
from dportsv3.db.schema import init_db
from dportsv3.tracker.render import attribute_across_snapshots

A1 = """diff --git a/ports/devel/foo/Makefile b/ports/devel/foo/Makefile
index 111..222 100644
--- a/ports/devel/foo/Makefile
+++ b/ports/devel/foo/Makefile
@@ -1,2 +1,2 @@
-OLD=1
+NEW=2
"""

A2 = A1 + """diff --git a/ports/devel/foo/files/patch-cmake b/ports/devel/foo/files/patch-cmake
new file mode 100644
index 000..333
--- /dev/null
+++ b/ports/devel/foo/files/patch-cmake
@@ -0,0 +1,1 @@
+--- CMakeLists.txt.orig
"""

A3 = A2.replace("+NEW=2", "+NEWER=3")


# --- the convention, shared by the writer and the reader --------------------

def test_the_relpath_is_keyed_by_job_not_bundle():
    """analysis/changes.diff is the bundle's, so on a retried bundle one
    job's diff overwrote its sibling's. That is what this fixes."""
    rp = artifacts.worktree_snapshot_relpath("j1", 3)
    assert rp == "analysis/worktree/j1.attempt3.diff"
    assert artifacts.worktree_snapshot_attempt(rp, "j1") == 3


def test_a_sibling_job_s_snapshot_is_not_read_as_ours():
    """A bundle holds every job that ran on it."""
    rp = artifacts.worktree_snapshot_relpath("j1", 3)
    assert artifacts.worktree_snapshot_attempt(rp, "j2") is None


# --- attribution, which is the point of keeping one per attempt ------------

def test_attribution_is_exact_across_snapshots():
    """The overlay accumulates, so a path's first appearance IS the attempt
    that created it -- no guessing from tool arguments."""
    got = attribute_across_snapshots([(1, A1), (2, A2), (3, A3)])
    assert got["ports/devel/foo/Makefile"] == {"first": 1, "last_changed": 3}
    assert got["ports/devel/foo/files/patch-cmake"] == {
        "first": 2, "last_changed": 2}


def test_an_untouched_file_does_not_count_as_changed():
    """patch-cmake is identical in A2 and A3; only the Makefile moved."""
    got = attribute_across_snapshots([(2, A2), (3, A3)])
    assert got["ports/devel/foo/files/patch-cmake"]["last_changed"] == 2


def test_a_gap_attributes_to_the_next_attempt_that_published():
    """Publishing is best-effort; a hole must not break attribution."""
    got = attribute_across_snapshots([(1, A1), (3, A2)])
    assert got["ports/devel/foo/files/patch-cmake"]["first"] == 3


# --- the page ---------------------------------------------------------------

@pytest.fixture
def seeded(tmp_path: Path):
    """A patch job with three published snapshots, one of them empty."""
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    now = "2026-09-24T06:00:00+00:00"
    conn.execute(
        "INSERT INTO bundles (bundle_id, run_id, origin, flavor, ts_utc, "
        "result, path, last_seen_at, target) VALUES "
        "('b1','r1','devel/foo','',?, 'failed','/p',?, '@2026Q3')", (now, now))
    conn.execute(
        "INSERT INTO jobs (job_id, state, type, origin, flavor, bundle_dir, "
        "created_ts_utc, path, last_seen_at, target, bundle_id) VALUES "
        "('j1','patching','patch','devel/foo','','',?,'',?,'@2026Q3','b1')",
        (now, now))
    # attempt 2 is deliberately absent: a publish that could not look.
    for attempt, body in ((1, A1), (3, A3), (4, "")):
        blob = blobs / f"a{attempt}.diff"
        blob.write_text(body)
        conn.execute(
            "INSERT INTO artifact_refs (bundle_id, relpath, backend, sha256, "
            "fs_path, kind, size, created_at) VALUES "
            "('b1', ?, 'fs', 'x', ?, 'text', ?, ?)",
            (artifacts.worktree_snapshot_relpath("j1", attempt),
             str(blob), len(body), now))
    conn.commit()
    conn.close()
    return db


def _client(db: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from dportsv3.tracker.server import create_app  # noqa: PLC0415

    return TestClient(create_app(db))


def test_the_newest_version_is_shown_by_default(seeded):
    with _client(seeded) as client:
        body = client.get("/agentic/jobs/j1").text
    assert 'id="worktree"' in body
    # attempt 4 published an empty diff: "changed nothing" is the answer.
    assert "attempt 4 changed nothing" in body


def test_a_version_is_selectable_and_deep_linkable(seeded):
    with _client(seeded) as client:
        body = client.get("/agentic/jobs/j1?attempt=3").text
    assert "attempt=3" in body
    assert 'class="wt-versions"' in body
    assert "2 files changed" in body
    # Not the newest, and it says so rather than looking current.
    assert "historical" in body


def test_an_absent_attempt_is_a_gap_not_a_renumbering(seeded):
    """attempt 2 never published. The selector shows the hole."""
    with _client(seeded) as client:
        body = client.get("/agentic/jobs/j1?attempt=1").text
    assert 'class="wt-v gap"' in body
    assert "published no snapshot" in body


def test_attribution_reaches_the_page(seeded):
    with _client(seeded) as client:
        body = client.get("/agentic/jobs/j1?attempt=3").text
    assert "first written in attempt 1" in body      # the Makefile
    assert "first written in attempt 3" in body      # patch-cmake
    assert "attempt not recorded" not in body


def test_an_unknown_attempt_falls_back_to_the_newest(seeded):
    with _client(seeded) as client:
        body = client.get("/agentic/jobs/j1?attempt=99").text
    assert "attempt 4 changed nothing" in body


# --- the publisher, runner-side --------------------------------------------

def test_an_empty_diff_publishes_zero_bytes(monkeypatch):
    """"This attempt changed nothing" is a finding, especially on an attempt
    that then failed. An absent version would read as "it does not exist"."""
    from dportsv3.agent import runner  # noqa: PLC0415

    put: list[tuple] = []
    monkeypatch.setattr(runner, "_capture_branch_diff", lambda env: b"")
    monkeypatch.setattr(
        runner, "artifact_store_put",
        lambda b, rp, data, kind: put.append((b, rp, data)) or True)

    assert runner.publish_worktree_snapshot("b1", "e", "j1", 2) is True
    assert put == [("b1", "analysis/worktree/j1.attempt2.diff", b"")]


def test_a_capture_that_could_not_look_publishes_nothing(monkeypatch):
    """The distinction _write_changes_diff calls "the one answer we cannot
    tell apart from a real loss": a gap, not a clean tree."""
    from dportsv3.agent import runner  # noqa: PLC0415

    put: list = []

    def boom(env):
        raise RuntimeError("git diff exited 128")

    monkeypatch.setattr(runner, "_capture_branch_diff", boom)
    monkeypatch.setattr(
        runner, "artifact_store_put",
        lambda *a, **k: put.append(a) or True)

    assert runner.publish_worktree_snapshot("b1", "e", "j1", 2) is False
    assert put == [], "a tombstone would render in the band as a diff"


def test_it_never_raises_into_the_patch_loop(monkeypatch):
    """It is called from an event hook on the hot path. A cosmetic feature
    must not be able to end an attempt."""
    from dportsv3.agent import runner  # noqa: PLC0415

    monkeypatch.setattr(runner, "_capture_branch_diff", lambda env: b"x")

    def explode(*a, **k):
        raise OSError("tracker unreachable")

    monkeypatch.setattr(runner, "artifact_store_put", explode)
    with pytest.raises(OSError):
        runner.artifact_store_put()          # the stub really does raise
    # ...and the publisher still does not.
    assert runner.publish_worktree_snapshot("b1", "e", "j1", 1) is False


def _dispatcher(published: list[int]):
    from dportsv3.agent.steps import PatchEventDispatcher  # noqa: PLC0415

    return PatchEventDispatcher(
        queue_root=None,
        job_id="j1",
        origin="devel/foo",
        activity_log=lambda *a, **k: None,
        looks_env_suspicious=lambda res: False,
        invalidate_health_cache=lambda *a, **k: None,
        summarize_tool_call=lambda *a, **k: "",
        publish_worktree=published.append,
    )


def test_the_three_triggers_publish():
    """Attempt boundaries and every successful write tool -- all of them
    events the dispatcher already saw, so attempt_loop needs no change."""
    published: list[int] = []
    d = _dispatcher(published)
    d({"type": "attempt_start", "attempt": 1, "iterations": 8})
    d({"type": "tool_call", "attempt": 1, "turn": 3, "tool": "edit_file",
       "result": {"ok": True}, "args": {}})
    d({"type": "attempt_end", "attempt": 1, "rebuild_ok": False})
    assert published == [1, 1, 1]


def test_a_read_only_tool_does_not_publish():
    """The publish shells git in-chroot, and greps outnumbered edits five to
    one on the window that was measured."""
    published: list[int] = []
    d = _dispatcher(published)
    d({"type": "tool_call", "attempt": 1, "turn": 1, "tool": "grep",
       "result": {"ok": True}, "args": {}})
    d({"type": "tool_call", "attempt": 1, "turn": 2, "tool": "get_file",
       "result": {"ok": True}, "args": {}})
    assert published == []


def test_a_failed_write_does_not_publish():
    published: list[int] = []
    d = _dispatcher(published)
    d({"type": "tool_call", "attempt": 2, "turn": 1, "tool": "edit_file",
       "result": {"ok": False, "error": "no such file"}, "args": {}})
    assert published == []


def test_a_publisher_that_raises_does_not_break_the_dispatcher():
    from dportsv3.agent.steps import PatchEventDispatcher  # noqa: PLC0415

    d = PatchEventDispatcher(
        queue_root=None, job_id="j1", origin="devel/foo",
        activity_log=lambda *a, **k: None,
        looks_env_suspicious=lambda res: False,
        invalidate_health_cache=lambda *a, **k: None,
        summarize_tool_call=lambda *a, **k: "",
        publish_worktree=lambda attempt: (_ for _ in ()).throw(OSError("x")),
    )
    d({"type": "attempt_start", "attempt": 1})   # must not raise
    assert len(d.trace_events) == 1


def test_publishing_is_off_unless_wired():
    """Every existing construction of this class passes no publisher."""
    published: list[int] = []
    from dportsv3.agent.steps import PatchEventDispatcher  # noqa: PLC0415

    d = PatchEventDispatcher(
        queue_root=None, job_id="j1", origin="devel/foo",
        activity_log=lambda *a, **k: None,
        looks_env_suspicious=lambda res: False,
        invalidate_health_cache=lambda *a, **k: None,
        summarize_tool_call=lambda *a, **k: "",
    )
    d({"type": "attempt_start", "attempt": 1})
    assert published == []


# --- the live path ----------------------------------------------------------

def _with_activity(db: Path) -> Path:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO activity_log (ts, job_id, stage, message, extra_json) "
        "VALUES ('2026-09-24T06:05:00+00:00', 'j1', 'tool:edit_file', 'edited',"
        " '{\"attempt\": 3, \"turn\": 2, \"ok\": true}')")
    conn.commit()
    conn.close()
    return db


def test_the_live_fragment_carries_the_band(seeded):
    """So it advances while the agent edits rather than only on reload. The
    band changes when a write tool RETURNS, and that return is itself a row,
    so gating it on new rows is right here -- unlike the attempt strip."""
    with _client(_with_activity(seeded)) as client:
        body = client.get(
            "/api/jobs/j1/activity-fragment?since_id=0").json()
    assert "worktree_html" in body
    assert 'class="wt-versions"' in body["worktree_html"]


def test_a_quiet_poll_sends_no_band(seeded):
    with _client(_with_activity(seeded)) as client:
        latest = client.get(
            "/api/jobs/j1/activity-fragment?since_id=0").json()["since_id"]
        body = client.get(
            f"/api/jobs/j1/activity-fragment?since_id={latest}").json()
    assert "worktree_html" not in body


def test_the_fragment_honours_a_pinned_version(seeded):
    """Otherwise the 3s swap drags the reader back to the newest version."""
    with _client(_with_activity(seeded)) as client:
        body = client.get(
            "/api/jobs/j1/activity-fragment?since_id=0&attempt=1").json()
    html = body["worktree_html"]
    assert "1 file changed" in html, "attempt 1 touched only the Makefile"
    assert "historical" in html
