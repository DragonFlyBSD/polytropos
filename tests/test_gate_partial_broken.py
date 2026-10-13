"""One broken env holds its line's jobs; the runner keeps working (poly-p2ve).

The gate probes every env a build line routes to. Before poly-p2ve it
watched one global env, so one broken env paused everything. Now it pauses
only when every watched env is broken; with some broken it logs that their
lines wait and goes on claiming. Drives the real ``_gate_blocked`` closure
through ``main(--once)``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from dportsv3.agent import runner as runner_mod


@dataclass
class _Health:
    env: str
    status: str
    operator_action: str = ""


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """main(--once) with two watched envs and everything else stubbed."""
    for sub in ("pending", "inflight", "done", "failed"):
        (tmp_path / sub).mkdir()
    for name in ("register_runner", "start_heartbeat", "stop_heartbeat",
                 "deregister_runner"):
        monkeypatch.setattr(runner_mod, name, lambda *a, **k: None,
                            raising=False)
    monkeypatch.setattr(runner_mod, "resolve_env_for_gate", lambda: None)
    monkeypatch.setattr(runner_mod, "_watched_envs",
                        lambda pref: ["env-main", "env-q3"])
    monkeypatch.setattr(runner_mod, "dsynth_active",
                        lambda env, qr: (False, ""))
    seen = {"logs": [], "activity": [], "status": [], "claimed": 0,
            "broken": set()}
    monkeypatch.setattr(runner_mod, "probe_health_cached",
                        lambda env, ttl: _Health(
                            env, "broken" if env in seen["broken"] else "ready",
                            f"fix {env}"))
    monkeypatch.setattr(runner_mod, "log",
                        lambda qr, level, msg: seen["logs"].append(msg))
    monkeypatch.setattr(runner_mod, "activity_log",
                        lambda qr, kind, msg, **k: seen["activity"].append(
                            (kind, msg)))
    monkeypatch.setattr(runner_mod, "update_runner_status",
                        lambda status, **k: seen["status"].append(
                            (status, k.get("stage"))))

    def claim(queue_root):
        seen["claimed"] += 1
        return None

    monkeypatch.setattr(runner_mod, "claim_next_job_batch", claim)

    def run(*broken):
        seen["broken"] = set(broken)
        rc = runner_mod.main(["--queue-root", str(tmp_path), "--once"])
        runner_mod.release_runner_lock()
        assert rc == 0
        return seen

    return run


def test_one_broken_env_holds_its_line_and_the_runner_claims(gate):
    seen = gate("env-q3")
    assert seen["claimed"] == 1
    assert any("env env-q3 broken; its line's jobs wait" in m
               for m in seen["logs"]), seen["logs"]
    assert ("health_broken", "env env-q3 broken; its line's jobs wait") in (
        seen["activity"])
    assert not [s for s in seen["status"] if s[0] == "paused"], seen["status"]


def test_every_watched_env_broken_pauses_the_runner(gate):
    seen = gate("env-main", "env-q3")
    assert seen["claimed"] == 0
    assert any("runner paused: health broken" in m for m in seen["logs"])
    assert any(s == "paused" and (stage or "").startswith("health_broken")
               for s, stage in seen["status"]), seen["status"]


def test_all_healthy_claims_quietly(gate):
    """Nothing changed, so nothing is logged: the gate reports transitions."""
    seen = gate()
    assert seen["claimed"] == 1
    assert not [k for k, _ in seen["activity"] if k.startswith("health_")]
    assert not [s for s in seen["status"] if s[0] == "paused"]
