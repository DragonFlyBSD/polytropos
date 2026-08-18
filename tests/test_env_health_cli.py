"""Smoke test for the ``dportsv3 env-health NAME`` subcommand.

The command used to be ``dportsv3 dev-env health NAME``, handled inside the
dev-env package. That forced `dports_dev_env` to import the generator back
(`dportsv3.agent.health` → `dportsv3.agent.worker`), a cycle bridged by a
sys.path hack. The probe's code has always lived on the generator side, so
its CLI now does too, and the dependency runs one way only.

The handler imports ``dportsv3.agent.health`` lazily, so monkeypatching
``health.check`` is how we drive it.

What we cover:
- Status "ready" → exit 0, JSON contains the expected shape.
- Status "broken" → exit 1, operator_action surfaces.
- Status "degraded" → exit 2.
- The ``--no-indent`` path emits one-line JSON.
- ``--only`` is propagated to health.check.
- The parser is registered and parses into the namespace the handler wants.
"""

from __future__ import annotations

import argparse
import json

import pytest

from dportsv3 import cli


def _ns(**fields) -> argparse.Namespace:
    defaults = {"name": "env-x", "only": None, "no_indent": False}
    defaults.update(fields)
    return argparse.Namespace(**defaults)


def _stub_check(status, *, checks=None, operator_action=None):
    """Build a callable that mimics health.check(env, only=...)."""
    from dportsv3.agent import health as h
    eh = h.EnvHealth(
        env="env-x",
        status=status,
        checks=checks or [],
        operator_action=operator_action,
        probed_at="2026-05-21T00:00:00Z",
    )

    def _call(env, *, only=None):
        # Record the env + only-filter for the propagation test.
        _call.last_env = env
        _call.last_only = only
        return eh
    _call.last_env = None
    _call.last_only = None
    return _call


# --- Tests --------------------------------------------------------------------


def test_ready_returns_exit_0(monkeypatch, capsys):
    from dportsv3.agent import health
    monkeypatch.setattr(health, "check", _stub_check("ready"))

    rc = cli.cmd_env_health(_ns())
    assert rc == 0

    data = json.loads(capsys.readouterr().out)
    assert data["env"] == "env-x"
    assert data["status"] == "ready"
    assert data["operator_action"] is None


def test_broken_returns_exit_1_surfaces_action(monkeypatch, capsys):
    from dportsv3.agent import health
    monkeypatch.setattr(
        health, "check",
        _stub_check(
            "broken",
            checks=[health.HealthCheck(
                name="python_runtime", status="broken",
                detail="missing: py311-sqlite3",
                operator_action="recreate the env",
            )],
            operator_action="recreate the env",
        ),
    )

    rc = cli.cmd_env_health(_ns())
    assert rc == 1

    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "broken"
    assert data["operator_action"] == "recreate the env"
    assert data["checks"][0]["name"] == "python_runtime"


def test_degraded_returns_exit_2(monkeypatch, capsys):
    from dportsv3.agent import health
    monkeypatch.setattr(health, "check", _stub_check("degraded"))

    assert cli.cmd_env_health(_ns()) == 2


def test_no_indent_emits_one_line(monkeypatch, capsys):
    from dportsv3.agent import health
    monkeypatch.setattr(health, "check", _stub_check("ready"))

    cli.cmd_env_health(_ns(no_indent=True))
    out = capsys.readouterr().out.strip()
    assert "\n" not in out
    json.loads(out)          # still valid JSON


def test_only_filter_propagates(monkeypatch, capsys):
    from dportsv3.agent import health
    stub = _stub_check("ready")
    monkeypatch.setattr(health, "check", stub)

    cli.cmd_env_health(_ns(only=["python_runtime"]))
    assert stub.last_only == ["python_runtime"]
    assert stub.last_env == "env-x"


# --- wiring -------------------------------------------------------------------


def test_parser_produces_the_namespace_the_handler_reads():
    """The handler is only reachable if the parser feeds it the right
    attribute names — the two drifted apart silently when the command
    moved packages."""
    args = cli.create_parser().parse_args(
        ["env-health", "env-x", "--only", "python_runtime", "--no-indent"]
    )
    assert args.command == "env-health"
    assert (args.name, args.only, args.no_indent) == (
        "env-x", ["python_runtime"], True)


def test_main_dispatches_env_health(monkeypatch, capsys):
    from dportsv3.agent import health
    monkeypatch.setattr(health, "check", _stub_check("ready"))

    assert cli.main(["env-health", "env-x"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_dev_env_package_no_longer_hosts_health():
    """The cycle edge stays deleted: `dports_dev_env` must not grow a
    handler that imports the generator back."""
    from dports_dev_env import cli as dev_env_cli

    assert not hasattr(dev_env_cli, "cmd_health")
    with pytest.raises(SystemExit):
        dev_env_cli.build_parser().parse_args(["health", "env-x"])
