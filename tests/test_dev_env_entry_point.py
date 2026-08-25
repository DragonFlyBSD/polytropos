"""The runtime reaches a chroot without needing the checkout's wrapper.

poly-abr.8. Every chroot call goes through one argv prefix, so where that
prefix comes from decides whether a packaged install can run the
services at all.
"""
from __future__ import annotations

import subprocess

import pytest

from dportsv3.agent import health as health_mod
from dportsv3.agent import worker as worker_mod


@pytest.fixture(autouse=True)
def clean_resolution(monkeypatch):
    """The prefix is cached for the process; each test resolves afresh."""
    monkeypatch.setattr(worker_mod, "_DEV_ENV_CMD", None)
    monkeypatch.delenv("DPORTS_DEV_ENV_CMD", raising=False)
    monkeypatch.delenv("DPORTSV3_CMD", raising=False)


def _which(mapping):
    """shutil.which stand-in over a name -> path (or None) mapping."""
    return lambda name: mapping.get(name)


# --- the packaged world -------------------------------------------------

def test_packaged_install_uses_the_dev_env_console_script(monkeypatch) -> None:
    """A port installs ${PREFIX}/bin/dports-dev-env and no wrapper."""
    monkeypatch.setattr(worker_mod.shutil, "which",
                        _which({"dports-dev-env": "/usr/local/bin/dports-dev-env"}))
    assert worker_mod._resolve_dev_env_cmd() == ["/usr/local/bin/dports-dev-env"]


def test_the_dportsv3_console_script_is_never_picked_up(monkeypatch) -> None:
    """The trap this bead exists for.

    In a packaged world `dportsv3` IS on PATH — as the console script,
    which has no dev-env subcommand. Selecting it produces an argparse
    "invalid choice" at the first chroot call instead of anything an
    operator can act on.
    """
    monkeypatch.setattr(worker_mod.shutil, "which",
                        _which({"dportsv3": "/usr/local/bin/dportsv3"}))
    with pytest.raises(RuntimeError) as exc:
        worker_mod._resolve_dev_env_cmd()
    assert "not a substitute" in str(exc.value)
    assert "dports-dev-env" in str(exc.value)


def test_error_names_all_three_ways_out(monkeypatch) -> None:
    monkeypatch.setattr(worker_mod.shutil, "which", _which({}))
    with pytest.raises(RuntimeError) as exc:
        worker_mod._resolve_dev_env_cmd()
    msg = str(exc.value)
    for hint in ("dports-dev-env", "DPORTS_DEV_ENV_CMD", "DPORTSV3_CMD"):
        assert hint in msg, hint


# --- the checkout world -------------------------------------------------

def test_checkout_wrapper_routes_dev_env(monkeypatch) -> None:
    monkeypatch.setenv("DPORTSV3_CMD", "/home/x/polytropos/bin/dportsv3")
    monkeypatch.setattr(worker_mod.shutil, "which", _which({}))
    assert worker_mod._resolve_dev_env_cmd() == [
        "/home/x/polytropos/bin/dportsv3", "dev-env"]


def test_checkout_wrapper_beats_path(monkeypatch) -> None:
    """A checkout must use ITS OWN dev-env venv, not some other copy that
    happens to be installed on the host."""
    monkeypatch.setenv("DPORTSV3_CMD", "/home/x/polytropos/bin/dportsv3")
    monkeypatch.setattr(worker_mod.shutil, "which",
                        _which({"dports-dev-env": "/usr/local/bin/dports-dev-env"}))
    assert worker_mod._resolve_dev_env_cmd()[0].endswith("bin/dportsv3")


def test_explicit_override_wins(monkeypatch) -> None:
    monkeypatch.setenv("DPORTS_DEV_ENV_CMD", "/opt/bin/dports-dev-env")
    monkeypatch.setenv("DPORTSV3_CMD", "/home/x/polytropos/bin/dportsv3")
    assert worker_mod._resolve_dev_env_cmd() == ["/opt/bin/dports-dev-env"]


def test_multi_word_commands_split(monkeypatch) -> None:
    """Both variables are whitespace-split, so a wrapper can be invoked
    through an interpreter."""
    monkeypatch.setenv("DPORTS_DEV_ENV_CMD", "/usr/bin/env dports-dev-env")
    assert worker_mod._resolve_dev_env_cmd() == [
        "/usr/bin/env", "dports-dev-env"]


# --- what the argv actually looks like ----------------------------------

def test_exec_argv_packaged(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(worker_mod, "_DEV_ENV_CMD", ["/usr/local/bin/dports-dev-env"])
    monkeypatch.setattr(worker_mod.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    worker_mod._exec("env1", "/bin/true")
    assert seen["argv"][:3] == ["/usr/local/bin/dports-dev-env", "exec", "--quiet"]
    assert "dev-env" not in seen["argv"], "kept the wrapper's subcommand word"


def test_exec_argv_via_wrapper(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(worker_mod, "_DEV_ENV_CMD", ["/co/bin/dportsv3", "dev-env"])
    monkeypatch.setattr(worker_mod.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    worker_mod._exec("env1", "/bin/true")
    assert seen["argv"][:3] == ["/co/bin/dportsv3", "dev-env", "exec"]


def test_run_dev_env_does_not_double_the_word(monkeypatch) -> None:
    """Callers pass the subcommand only; the prefix supplies the rest."""
    seen = {}
    monkeypatch.setattr(worker_mod, "_DEV_ENV_CMD", ["/usr/local/bin/dports-dev-env"])
    monkeypatch.setattr(worker_mod.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    worker_mod._run_dev_env("status", "env1")
    assert seen["argv"] == ["/usr/local/bin/dports-dev-env", "status", "env1"]


def test_health_probe_uses_the_same_prefix(monkeypatch) -> None:
    """health.py resolves separately; it must not drift from worker.py."""
    seen = {}
    monkeypatch.setattr(worker_mod, "_DEV_ENV_CMD", ["/usr/local/bin/dports-dev-env"])
    monkeypatch.setattr(health_mod.subprocess, "run",
                        lambda argv, **kw: seen.update(argv=argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    health_mod._run_in_env("env1", "pkg", "info")
    assert seen["argv"][:2] == ["/usr/local/bin/dports-dev-env", "exec"]
    assert "dev-env" not in seen["argv"]


def test_prefix_is_resolved_once(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(worker_mod, "_resolve_dev_env_cmd",
                        lambda: calls.append(1) or ["/x/dports-dev-env"])
    worker_mod._dev_env_cmd()
    worker_mod._dev_env_cmd()
    assert len(calls) == 1
