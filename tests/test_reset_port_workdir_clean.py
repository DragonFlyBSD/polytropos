"""``reset_port`` wipes the per-origin WRKDIR and recomposes the origin
from the main checkout.

It no longer resets the source checkout: since B1 each job works in its
own worktree that is destroyed with the job, so one job's writes cannot
reach the next one through a shared tree. What remains is the state that
lives *outside* the worktree and is shared across jobs — the WRKDIR under
``/work/obj`` and the composed tree under ``/work/artifacts/compose``.

Cross-run pollution before this evolved: early Step 25g only did
the substrate reset, leaving WRKDIR populated; that became the
first regression (next job's ``extract`` no-op'd, agent's
``get_file`` read polluted source). The follow-up added a ``reapply``
so the compose tree at ``/work/artifacts/compose/<target>/<origin>/``
would reflect HEAD rather than the agent's last patched output.

It did not: ``reapply`` composes ``/work/DeltaPorts``, which is the job's
worktree until runner.py destroys it after this returns, so the "reset"
composed the agent's last state. A dead libx264 job's re-cut patch then
built green under an operator ``dbuild`` while DPorts failed on ironman
(poly-at76). The compose now reads ``/work/ports-main``.

Stage order: ``make clean`` (best-effort) → baseline compose per origin
(best-effort). ``make clean`` runs first against the still-patched
substrate because its in-tree Makefile is what the existing WRKDIR was
authored against. A compose failure is treated as "baseline already
broken" — surfaced but not flipped to ok=False.
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


def _make_exec_recorder(clean_rc=0, compose_rc=0,
                        clean_out="", clean_err="",
                        compose_out="", compose_err=""):
    """Return (recorded_calls, fake_exec). The fake routes by the
    ``/bin/sh -c <cmd>`` shape: `` compose `` in the cmd → baseline
    compose; ``make `` → workdir clean. ``compose_rc`` may be a dict of
    origin → rc. Any git, any ``reapply``, or anything else raises — git
    because reset_port must not touch the source checkout, reapply
    because it composes the job's worktree (poly-at76)."""
    calls: list[tuple[str, ...]] = []

    def _fake(env, *argv, **kwargs):
        calls.append(argv)
        cmd = argv[-1] if argv else ""
        if (argv and argv[0] == "reapply") or "reapply" in cmd:
            raise AssertionError(
                f"reset_port must not compose through reapply: {argv!r}"
            )
        if "git " in cmd:
            raise AssertionError(
                f"reset_port must not run git any more: {cmd!r}"
            )
        if " compose" in cmd:
            rc = compose_rc
            if isinstance(rc, dict):
                rc = next((v for k, v in rc.items() if f"--origin {k} " in cmd), 0)
            return SimpleNamespace(returncode=rc,
                                   stdout=compose_out, stderr=compose_err)
        if "make " in cmd:
            return SimpleNamespace(returncode=clean_rc,
                                   stdout=clean_out, stderr=clean_err)
        raise AssertionError(f"unexpected _exec invocation: {argv!r}")

    return calls, _fake


def test_reset_port_runs_clean_then_the_baseline_compose(monkeypatch):
    calls, fake = _make_exec_recorder()
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo")

    assert result["ok"] is True
    assert result["workdir_clean_ok"] is True
    assert result["baseline_compose_ok"] is True
    # Two invocations, in order: clean against the still-patched substrate,
    # then the compose. The substrate reset that sat between them is gone —
    # the job's worktree is thrown away instead. The recorder asserts on any
    # git and any reapply.
    assert len(calls) == 2
    assert "make " in calls[0][-1]
    assert "WRKDIRPREFIX=" in calls[0][-1]
    assert " compose" in calls[1][-1]
    assert "--origin devel/foo " in calls[1][-1]


def test_the_compose_reads_the_main_checkout_not_the_job_worktree(monkeypatch):
    """/work/DeltaPorts is the job's worktree until runner.py destroys it,
    which is after this returns. Composing it put the agent's discarded
    state in the shared tree (poly-at76)."""
    calls, fake = _make_exec_recorder()
    monkeypatch.setattr(worker, "_exec", fake)

    worker.reset_port("test-env", "devel/foo")

    cmd = calls[1][-1]
    assert f"--delta-root {worker.PORTS_MAIN_DIR}" in cmd
    assert worker.PORTS_DIR not in cmd
    assert '--output "$DPORTS_COMPOSE_ROOT"' in cmd
    assert cmd.startswith(f"{worker.TOOL_BIN} compose ")


def test_every_origin_is_composed_from_the_main_checkout(monkeypatch):
    """A slave's fix lands in its master (poly-lt5q); the master's tree
    carries the job's state just the same."""
    calls, fake = _make_exec_recorder()
    monkeypatch.setattr(worker, "_exec", fake)

    worker.reset_port("test-env", "multimedia/libx264",
                      also=["multimedia/x264"])

    composes = [c[-1] for c in calls if " compose" in c[-1]]
    assert len(composes) == 2
    assert "--origin multimedia/libx264 " in composes[0]
    assert "--origin multimedia/x264 " in composes[1]
    assert all(f"--delta-root {worker.PORTS_MAIN_DIR}" in c for c in composes)


def test_one_origin_failing_does_not_skip_the_rest(monkeypatch):
    """The first failure is reported, and the master after it is still
    composed — leaving it would leave the job's state in exactly the
    port the operator builds next."""
    calls, fake = _make_exec_recorder(
        compose_rc={"devel/foo": 2}, compose_err="E_COMPOSE_APPLY_FAILED",
    )
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo", also=["devel/bar"])

    composes = [c[-1] for c in calls if " compose" in c[-1]]
    assert len(composes) == 2
    assert result["baseline_compose_ok"] is False
    assert "E_COMPOSE_APPLY_FAILED" in result["baseline_compose_error"]


def test_the_relation_is_forgotten_after_composing(monkeypatch):
    """Compose rewrites the Makefile the master/slave relation is read
    from, and the agent may have moved it."""
    _, fake = _make_exec_recorder()
    monkeypatch.setattr(worker, "_exec", fake)
    worker._RELATION_CACHE[("test-env", "devel/foo")] = "stale"
    worker._RELATION_CACHE[("test-env", "devel/bar")] = "stale"
    try:
        worker.reset_port("test-env", "devel/foo", also=["devel/bar"])
        assert ("test-env", "devel/foo") not in worker._RELATION_CACHE
        assert ("test-env", "devel/bar") not in worker._RELATION_CACHE
    finally:
        worker._RELATION_CACHE.pop(("test-env", "devel/foo"), None)
        worker._RELATION_CACHE.pop(("test-env", "devel/bar"), None)


def test_an_origin_is_quoted_into_the_compose():
    cmd = worker._baseline_compose_cmd("devel/a b")
    assert "--origin 'devel/a b' " in cmd


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
    but does not flip the result to ok=False — the compose still runs."""
    calls, fake = _make_exec_recorder(
        clean_rc=2, clean_err="make: no such target 'clean'",
    )
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo")

    assert result["ok"] is True
    assert result["workdir_clean_ok"] is False
    assert "make: no such target" in result["workdir_clean_error"]
    # Both stages still ran — a make clean failure must not short-circuit
    # the compose.
    assert len(calls) == 2


def test_reset_port_tolerates_a_baseline_compose_failure(monkeypatch):
    """A compose failure here means the origin does not compose at
    baseline — that was the state before reset_port ran, so it isn't a
    regression we caused. Surface it but don't flip ok."""
    _, fake = _make_exec_recorder(
        compose_rc=2,
        compose_err="compose: E_COMPOSE_APPLY_FAILED on ports/devel/foo",
    )
    monkeypatch.setattr(worker, "_exec", fake)

    result = worker.reset_port("test-env", "devel/foo")

    assert result["ok"] is True
    assert result["baseline_compose_ok"] is False
    assert "E_COMPOSE_APPLY_FAILED" in result["baseline_compose_error"]


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
