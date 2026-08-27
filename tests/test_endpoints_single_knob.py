"""One knob decides where a builder talks (poly-fij.2).

The runner used to carry two accessors against two services. There is
one service now, so there is one variable, and every call the runner
makes has to follow it — otherwise pointing a builder at a remote
tracker moves some traffic and silently leaves the rest on loopback.
"""

from __future__ import annotations

import pytest

from dportsv3.common.endpoints import DEFAULT_TRACKER_URL, tracker_url

REMOTE = "http://tracker.example.net:9443"


def test_the_default_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DPORTSV3_TRACKER_URL", raising=False)
    assert tracker_url() == DEFAULT_TRACKER_URL


def test_the_env_var_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DPORTSV3_TRACKER_URL", REMOTE)
    assert tracker_url() == REMOTE


def test_a_trailing_slash_is_stripped_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers join paths onto this. A trailing slash yields '//v1' and a
    404 that reads like a missing endpoint rather than a config typo."""
    monkeypatch.setenv("DPORTSV3_TRACKER_URL", REMOTE + "/")
    assert tracker_url() == REMOTE


def _urls_the_runner_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every URL the runner opens for one round of bundle traffic."""
    import urllib.request

    from dportsv3.agent import runner

    seen: list[str] = []

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, *a, **kw):
        seen.append(req if isinstance(req, str) else req.full_url)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    runner.artifact_get("b1", "logs/errors.txt")
    runner.artifact_store_put("b1", "logs/x", b"x")
    runner.bundle_artifact_list("b1")
    runner.port_bundle_history("editors/vim")
    return seen


def test_every_runner_call_follows_the_one_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DPORTSV3_TRACKER_URL", REMOTE)
    urls = _urls_the_runner_builds(monkeypatch)
    assert urls, "no calls captured — the probe stopped exercising the runner"
    stragglers = [u for u in urls if not u.startswith(REMOTE)]
    assert stragglers == [], f"still on loopback: {stragglers}"


def test_both_surfaces_ride_the_same_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/v1/ ingest and /api/ reads are one service now. If these ever
    split again it must be a decision, not two constants drifting."""
    monkeypatch.setenv("DPORTSV3_TRACKER_URL", REMOTE)
    urls = _urls_the_runner_builds(monkeypatch)
    assert any(u.startswith(f"{REMOTE}/v1/") for u in urls)
    assert any(u.startswith(f"{REMOTE}/api/") for u in urls)


def test_verify_fix_uses_the_same_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DPORTSV3_TRACKER_URL", REMOTE + "/")
    from dportsv3 import verify_fix

    assert verify_fix._tracker_url() == REMOTE


def test_no_module_keeps_a_private_copy_of_the_default() -> None:
    """Three modules used to spell the same literal. The store client is
    the one allowed exception — it must stay importable under a bare
    system python3 — and its copy is pinned equal elsewhere."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "dportsv3"
    allowed = {root / "common" / "endpoints.py", root / "artifact_store_client.py"}
    offenders = []
    for path in root.rglob("*.py"):
        if path in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("http://127.0.0.1:8080"):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"private copies of the tracker URL: {offenders}"
