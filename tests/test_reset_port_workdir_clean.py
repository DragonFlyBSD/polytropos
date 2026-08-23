"""``reset_port`` wipes the per-origin WRKDIR and re-materializes the
compose tree from baseline.

It no longer resets the source checkout: since B1 each job works in its
own worktree that is destroyed with the job, so one job's writes cannot
reach the next one through a shared tree. What remains is the state that
lives *outside* the worktree and is shared across jobs — the WRKDIR under
``/work/obj`` and the composed tree under ``/work/artifacts/compose``.

Cross-run pollution before this evolved: early Step 25g only did
the substrate reset, leaving WRKDIR populated; that became the
first regression (next job's ``extract`` no-op'd, agent's
``get_file`` read polluted source). The follow-up adds a baseline
``reapply`` so the compose tree at
``/work/artifacts/compose/<target>/<origin>/`` reflects HEAD
rather than the agent's last patched output — otherwise an
operator verify (or the next attempt's first read) starts against
stale compose output.

Stage order: ``make clean`` (best-effort) → ``reapply`` (best-effort).
``make clean`` runs first against the still-patched substrate because its
in-tree Makefile is what the existing WRKDIR was authored against. A
``reapply`` failure is treated as "baseline already broken" — surfaced but
not flipped to ok=False.

Tests cover:
- Both stages succeed → ok=True, workdir_clean_ok=True, reapply_ok=True,
  calls fired in the documented order.
- ``make clean`` failure → ok=True, workdir_clean_ok=False,
  workdir_clean_error present; reapply still runs.
- ``reapply`` failure → ok=True, reapply_ok=False, reapply_error present.
- No git runs at all — the substrate reset is gone.
- WRKSRC + materialize caches are cleared on every reset.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dportsv3.agent import worker


@pytest.fixture(autouse=True)
def _clear_caches():
    """Each test starts with empty in-process caches so the
    test's pre-seeded entries are the only state under
    observation."""
    worker._WRKSRC_CACHE.clear()
    worker._MATERIALIZE_STATE.clear()
    yield
    worker._WRKSRC_CACHE.clear()
    worker._MATERIALIZE_STATE.clear()


def _make_exec_recorder(clean_rc=0, reapply_rc=0,
                        clean_out="", clean_err="",
                        reapply_out="", reapply_err=""):
    """Return (recorded_calls, fake_exec). The fake routes by argv shape:
    ``reapply`` as argv[0] → reapply; a ``/bin/sh -c <cmd>`` with ``make ``
    in the cmd → workdir clean. Any git, or anything else, raises — git in
    particular because reset_port must not touch the source checkout."""
    calls: list[tuple[str, ...]] = []

    def _fake(env, *argv, **kwargs):
        calls.append(argv)
        if argv and argv[0] == "reapply":
            return SimpleNamespace(returncode=reapply_rc,
                                   stdout=reapply_out, stderr=reapply_err)
        cmd = argv[-1] if argv else ""
        if "git " in cmd:
            raise AssertionError(
                f"reset_port must not run git any more: {cmd!r}"
            )
        if "make " in cmd:
            return SimpleNamespace(returncode=clean_rc,
                                   stdout=clean_out, stderr=clean_err)
        raise AssertionError(f"unexpected _exec invocation: {argv!r}")

    return calls, _fake


def test_reset_port_runs_clean_then_reapply(monkeypatch):
    calls, fake = _make_exec_recorder()
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo")

    assert result["ok"] is True
    assert result["workdir_clean_ok"] is True
    assert result["reapply_ok"] is True
    # Two invocations, in order: clean against the still-patched substrate,
    # then reapply. The substrate reset that sat between them is gone — the
    # job's worktree is thrown away instead. The recorder asserts on any git.
    assert len(calls) == 2
    assert "make " in calls[0][-1]
    assert "WRKDIRPREFIX=" in calls[0][-1]
    assert calls[1][0] == "reapply"
    assert calls[1][1] == "devel/foo"


def test_reset_port_clears_wrksrc_and_materialize_caches(monkeypatch):
    """Once we've asked the WRKDIR to go away, any cached WRKSRC
    path or content hash for it is stale by definition."""
    _, fake = _make_exec_recorder()
    monkeypatch.setattr(worker, "_exec", fake)
    worker._WRKSRC_CACHE[("test-env", "devel/foo")] = "/work/obj/.../wrksrc"
    worker._MATERIALIZE_STATE[("test-env", "devel/foo")] = "a" * 64

    worker.reset_port("test-env", "devel/foo")

    assert ("test-env", "devel/foo") not in worker._WRKSRC_CACHE
    assert ("test-env", "devel/foo") not in worker._MATERIALIZE_STATE


def test_reset_port_tolerates_make_clean_failure(monkeypatch):
    """make clean is best-effort. Failure surfaces as workdir_clean_* keys
    but does not flip the result to ok=False — reapply still runs."""
    calls, fake = _make_exec_recorder(
        clean_rc=2, clean_err="make: no such target 'clean'",
    )
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo")

    assert result["ok"] is True
    assert result["workdir_clean_ok"] is False
    assert "make: no such target" in result["workdir_clean_error"]
    # Both stages still ran — a make clean failure must not short-circuit
    # reapply.
    assert len(calls) == 2


def test_reset_port_tolerates_reapply_failure(monkeypatch):
    """``reapply`` failure means baseline HEAD itself doesn't
    compose — that was the state before reset_port ran, so it
    isn't a regression we caused. Surface it but don't flip ok."""
    _, fake = _make_exec_recorder(
        reapply_rc=2,
        reapply_err="compose: E_COMPOSE_APPLY_FAILED on ports/devel/foo",
    )
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo")

    assert result["ok"] is True
    assert result["reapply_ok"] is False
    assert "E_COMPOSE_APPLY_FAILED" in result["reapply_error"]


def test_reset_port_clears_caches_even_on_make_clean_failure(monkeypatch):
    """Cache invalidation is unconditional once we've decided to
    clean — a half-cleaned WRKDIR is still stale."""
    _, fake = _make_exec_recorder(clean_rc=1)
    monkeypatch.setattr(worker, "_exec", fake)
    worker._WRKSRC_CACHE[("test-env", "devel/foo")] = "/work/obj/.../wrksrc"
    worker._MATERIALIZE_STATE[("test-env", "devel/foo")] = "a" * 64

    worker.reset_port("test-env", "devel/foo")

    assert ("test-env", "devel/foo") not in worker._WRKSRC_CACHE
    assert ("test-env", "devel/foo") not in worker._MATERIALIZE_STATE


def test_reset_port_does_not_leak_state_when_caches_were_empty(monkeypatch):
    """Empty-cache case: no entries to pop, behavior is the same."""
    _, fake = _make_exec_recorder()
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo")

    assert result["ok"] is True
    assert ("test-env", "devel/foo") not in worker._WRKSRC_CACHE
    assert ("test-env", "devel/foo") not in worker._MATERIALIZE_STATE
