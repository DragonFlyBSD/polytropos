"""``dsynth_test`` exposes the acceptance gate to the patch agent (poly-8ni).

The loop proved fixes with ``dsynth build`` while ``verify-fix`` gated on
``dsynth test`` (dev-env ``apply-and-build`` runs ``dtest``). ``test`` is
strictly stricter: it force-rebuilds, runs the Q/A phases, and builds with
``DEVELOPER`` set — and DragonFly's ``Mk/bsd.sanity.mk`` gates its whole
``DEV_ERROR`` block on ``DEVELOPER``. So a port could reach ``agent_fixed``
on a green build and then be refused by verify for a defect the loop had no
way to observe. devel/libunwind did exactly that.

These tests pin the two properties that matter: the new tool really runs
dsynth's ``test`` subcommand, and it inherits every guard ``dsynth_build``
has rather than reimplementing them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_GEN = Path(__file__).resolve().parents[1]
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))


def _seed_fresh_compose(monkeypatch, worker, env="test-env",
                        origin="devel/foo"):
    """Satisfy the stale-compose guard so tests reach the dsynth call."""
    monkeypatch.setattr(worker, "_port_subtree_hash",
                        lambda e, o: "deadbeef")
    monkeypatch.setitem(worker._MATERIALIZE_STATE, (env, origin), "deadbeef")


def _capture_exec(monkeypatch, worker, returncode=0):
    captured: dict = {}

    def fake_exec(env, *argv, cwd="/work/DeltaPorts",
                  input_text=None, timeout=None):
        captured["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=returncode,
                                           stdout="", stderr="")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    monkeypatch.setattr(worker, "_dsynth_log_path", lambda origin: "/tmp/log")
    monkeypatch.setattr(worker, "_dsynth_log_candidates", lambda e, o: [])
    return captured


def test_dsynth_test_runs_the_test_subcommand(monkeypatch):
    """The whole point: dsynth's ``test``, not ``build``."""
    from dportsv3.agent import worker

    _seed_fresh_compose(monkeypatch, worker)
    captured = _capture_exec(monkeypatch, worker)

    res = worker.dsynth_test("test-env", "devel/foo")

    cmd = " ".join(str(a) for a in captured["argv"])
    assert ' test "$1"' in cmd
    assert " build " not in cmd
    assert res["rebuild_ok"] is True


def test_dsynth_build_still_runs_build(monkeypatch):
    """The refactor must not have swapped the cheap loop build."""
    from dportsv3.agent import worker

    _seed_fresh_compose(monkeypatch, worker)
    captured = _capture_exec(monkeypatch, worker)

    worker.dsynth_build("test-env", "devel/foo")

    cmd = " ".join(str(a) for a in captured["argv"])
    assert ' build "$1"' in cmd
    assert " test " not in cmd


def test_dsynth_test_suppresses_hooks(monkeypatch):
    """A gate run is still agent-driven, so it must not raise a bundle.

    Same reasoning as dsynth_build: one env per target, hooks live in it.
    """
    from dportsv3.agent import worker

    _seed_fresh_compose(monkeypatch, worker)
    captured = _capture_exec(monkeypatch, worker)

    worker.dsynth_test("test-env", "devel/foo")

    cmd = " ".join(str(a) for a in captured["argv"])
    assert "/work/.dports-agent-hooks-disabled" in cmd
    assert "trap" in cmd


def test_dsynth_test_refuses_without_materialize(monkeypatch):
    """The stale-compose guard is inherited, and the refusal names the
    tool the model actually called — not dsynth_build."""
    from dportsv3.agent import worker

    worker._MATERIALIZE_STATE.pop(("test-env", "devel/foo"), None)
    res = worker.dsynth_test("test-env", "devel/foo")

    assert res["ok"] is False
    assert res["rebuild_ok"] is False
    assert res["blocked_by"] == "stale_compose"
    assert res["error"].startswith("dsynth_test refused:")


def test_dsynth_test_refuses_on_changed_subtree(monkeypatch):
    """Edited-since-materialize guard, likewise inherited and named."""
    from dportsv3.agent import worker

    monkeypatch.setitem(worker._MATERIALIZE_STATE,
                        ("test-env", "devel/foo"), "a" * 40)
    monkeypatch.setattr(worker, "_port_subtree_hash", lambda e, o: "b" * 40)

    res = worker.dsynth_test("test-env", "devel/foo")

    assert res["ok"] is False
    assert res["error"].startswith("dsynth_test refused:")
    assert "materialize_dports" in res["error"]


def test_dsynth_test_is_registered_and_dispatchable():
    """Schema present and the handler resolves — the handler map is
    derived from the schema list by getattr(worker, name), so a schema
    without a worker function would blow up at import."""
    from dportsv3.agent import tools

    assert "dsynth_test" in tools.names()
    assert "dsynth_test" in tools.patch_tool_names()

    spec = next(s for s in tools.schemas()
                if s["function"]["name"] == "dsynth_test")
    desc = spec["function"]["description"]
    # The model has to be told this is the gate, not a second build.
    assert "DEVELOPER" in desc
    assert "verify" in desc
    assert spec["function"]["parameters"]["required"] == ["origin"]


def test_prompt_tells_the_agent_to_confirm_at_the_gate():
    """A tool the prompt never mentions is a tool the agent won't call."""
    from dportsv3.agent import prompts

    text = "\n".join(
        v for v in vars(prompts).values() if isinstance(v, str)
    )
    assert "dsynth_test(origin)" in text
    # The oracle is deliberately unchanged: rebuild_ok still tracks the
    # build, and a gate refusal is reported rather than hidden.
    assert "It tracks the build, not the gate" in text
