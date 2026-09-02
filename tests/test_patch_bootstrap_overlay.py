"""poly-451: the bootstrap overlay has to exist in the tree the patch
job actually composes.

Triage has no worktree, so it writes the header into the shared checkout
untracked. The patch job then cuts a fresh `git worktree` and repoints
PORTS_DIR at it, and `worktree add` does not carry untracked files — so
the overlay the harness had just created was absent when the patch job
composed, the compose failed, and the job refused. Recorded as
`agent_gave_up`, which reads as "the agent tried and could not fix it"
when the agent was never invoked at all.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import worker
from dportsv3.agent.overlay_state import OverlayFacts


def _facts(origin="cat/port", **kw):
    return OverlayFacts(origin=origin, port_exists=True, **kw)


@pytest.fixture
def spy(monkeypatch):
    """Record what would be written, without touching an env."""
    written: dict = {}

    def fake_put_file(env, path, content, **kw):
        written["path"] = path
        written["content"] = content
        return {"ok": True}

    monkeypatch.setattr(worker, "put_file", fake_put_file)
    return written


def test_writes_the_header_when_the_tree_has_no_overlay(monkeypatch, spy):
    monkeypatch.setattr(worker, "probe_overlay_facts",
                        lambda e, o: _facts(o, overlay_dops=False))
    monkeypatch.setattr(worker, "read_status_type", lambda e, o: "port")

    res = worker.ensure_bootstrap_overlay("2026Q3", "cat/port")

    assert res["written"] is True
    assert spy["path"] == "/work/DeltaPorts/ports/cat/port/overlay.dops"
    assert "port cat/port" in spy["content"]
    # The header must be byte-identical to triage's, which is why the
    # reason string lives in one place now.
    assert worker.BOOTSTRAP_REASON in spy["content"]


def test_is_a_noop_when_the_port_already_has_an_overlay(monkeypatch, spy):
    """Every port that never needed a bootstrap must be untouched."""
    monkeypatch.setattr(worker, "probe_overlay_facts",
                        lambda e, o: _facts(o, overlay_dops=True))
    res = worker.ensure_bootstrap_overlay("2026Q3", "cat/port")
    assert res == {"written": False, "reason": "overlay_present"}
    assert spy == {}, "must not write over an existing overlay"


def test_refuses_to_stub_a_port_carrying_compat_artifacts(monkeypatch, spy):
    """`abort` means a stub overlay would silently drop real compat
    files. Triage routes those to a manual handoff; if one reaches here
    anyway, leave the tree alone rather than destroy the artifacts."""
    monkeypatch.setattr(
        worker, "probe_overlay_facts",
        lambda e, o: _facts(o, overlay_dops=False,
                            dragonfly_files=("dragonfly/patch-aa",)),
    )
    monkeypatch.setattr(worker, "read_status_type", lambda e, o: None)

    res = worker.ensure_bootstrap_overlay("2026Q3", "cat/port")

    assert res["written"] is False
    assert res["reason"] == "abort"
    assert spy == {}


def test_a_failed_write_is_reported_but_does_not_fail_the_job(monkeypatch):
    """The compose right after is the gate; this only has to say so."""
    monkeypatch.setattr(worker, "probe_overlay_facts",
                        lambda e, o: _facts(o, overlay_dops=False))
    monkeypatch.setattr(worker, "read_status_type", lambda e, o: "port")
    monkeypatch.setattr(worker, "put_file",
                        lambda e, p, c, **k: {"ok": False, "error": "EROFS"})

    res = worker.ensure_bootstrap_overlay("2026Q3", "cat/port")

    assert res["written"] is False
    assert res["reason"] == "write_failed"
    assert "EROFS" in res["error"]


def test_a_probe_failure_does_not_raise_into_the_preflight(monkeypatch):
    def boom(env, origin):
        raise RuntimeError("chroot gone")
    monkeypatch.setattr(worker, "probe_overlay_facts", boom)

    res = worker.ensure_bootstrap_overlay("2026Q3", "cat/port")

    assert res["written"] is False
    assert res["reason"] == "probe_failed"
    assert "chroot gone" in res["error"]


def test_triage_writes_the_same_header_from_the_same_source():
    """Triage and the patch preflight must produce a byte-identical
    header, so both read it from worker rather than keeping a spelling
    each — that drift is what this dedupe prevents."""
    import inspect
    from dportsv3.agent import runner
    src = inspect.getsource(runner._ensure_overlay_or_abort)
    assert "worker.BOOTSTRAP_REASON" in src
    assert "worker.read_status_type" in src
    assert not hasattr(runner, "_BOOTSTRAP_REASON"), (
        "a second definition is exactly the drift being prevented"
    )
