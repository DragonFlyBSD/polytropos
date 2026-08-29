"""An unreadable env store is not an empty machine.

Both halves of poly-i7y: the enumeration says *why* it came back empty,
and a runner that cannot answer "is dsynth busy?" holds instead of
running jobs with the gate off.
"""
from __future__ import annotations

import os

import pytest

import dports_dev_env.store as store_mod
from dportsv3.agent import env_resolver as er
from dportsv3.agent import runner as runner_mod


@pytest.fixture
def envs_dir(set_setting, tmp_path, monkeypatch):
    d = tmp_path / "envs"
    d.mkdir()
    set_setting("dev_env.envs_dir", str(d))
    return d


# --- telling the three empties apart ------------------------------------

def test_readable_and_empty_is_not_an_error(envs_dir) -> None:
    """The ordinary fresh machine must not be reported as broken."""
    names, err = er.list_available_envs_detailed()
    assert names == () and err is None


def test_permission_error_names_the_fix(envs_dir, monkeypatch) -> None:
    def _boom(self):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(store_mod.EnvironmentStore, "list_infos", _boom)

    names, err = er.list_available_envs_detailed()

    assert names == ()
    assert err and "root" in err and str(envs_dir) in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses mode-000 dirs")
def test_an_unreachable_store_is_a_permission_error(set_setting, tmp_path, monkeypatch) -> None:
    """Denied one level up, not on the store itself.

    Path.is_dir() ignores ENOENT/ENOTDIR/EBADF/ELOOP but not EACCES, so
    this surfaces as a raise from the guard rather than an empty list —
    worth pinning, since a fallback for the empty-list reading would be
    dead code resting on the opposite belief.
    """
    priv = tmp_path / "priv"
    (priv / "envs").mkdir(parents=True)
    set_setting("dev_env.envs_dir", str(priv / "envs"))
    os.chmod(priv, 0o000)
    try:
        names, err = er.list_available_envs_detailed()
    finally:
        os.chmod(priv, 0o755)
    assert names == ()
    assert err and "root" in err


def test_missing_dir_is_the_ordinary_empty(set_setting, tmp_path, monkeypatch) -> None:
    """Absent != unreadable: 'create an env' is the right advice here."""
    set_setting("dev_env.envs_dir", str(tmp_path / "nope"))
    assert er.list_available_envs_detailed() == ((), None)


def test_legacy_wrapper_still_returns_just_names(envs_dir) -> None:
    assert er.list_available_envs() == ()


# --- the refusal the operator reads -------------------------------------

def test_refusal_reports_the_enumeration_failure(monkeypatch) -> None:
    monkeypatch.setattr(er, "list_available_envs_detailed",
                        lambda: ((), "store unreadable (Permission denied)"))
    r = er.resolve_env_for_job(None, None)

    assert r.env is None
    assert r.enumeration_error == "store unreadable (Permission denied)"
    assert "store unreadable" in r.refusal_reason
    assert "create one" not in r.refusal_reason, \
        "sent the operator at the wrong problem"


def test_genuine_empty_still_says_create_one(monkeypatch) -> None:
    monkeypatch.setattr(er, "list_available_envs_detailed", lambda: ((), None))
    r = er.resolve_env_for_job(None, None)
    assert r.enumeration_error is None
    assert "create one" in r.refusal_reason


def test_explicit_env_list_does_not_touch_the_store(monkeypatch) -> None:
    """Tests and callers that pass a list must not hit the filesystem."""
    monkeypatch.setattr(er, "list_available_envs_detailed",
                        lambda: (_ for _ in ()).throw(AssertionError("called")))
    r = er.resolve_env_for_job(None, None, available_envs=["a"])
    assert r.env == "a"


# --- the runner refuses to start ----------------------------------------

def _queue(tmp_path):
    for sub in ("pending", "inflight", "done", "failed"):
        (tmp_path / sub).mkdir()
    return tmp_path


def test_main_refuses_when_the_store_is_unreadable(tmp_path, monkeypatch) -> None:
    queue_root = _queue(tmp_path)
    monkeypatch.setattr(er, "list_available_envs_detailed",
                        lambda: ((), "not readable; run as root"))

    reached = []
    for name in ("acquire_runner_lock", "register_runner", "start_heartbeat"):
        monkeypatch.setattr(runner_mod, name,
                            lambda *a, _n=name, **k: reached.append(_n),
                            raising=False)

    rc = runner_mod.main(["--queue-root", str(queue_root), "--once"])

    assert rc == runner_mod.EXIT_ENV_UNREADABLE
    assert reached == [], f"a refused runner still started: {reached}"


def test_refusal_codes_are_all_distinct() -> None:
    """A supervisor tells 'run as root' from 'already running' from 'broke'."""
    assert runner_mod.EXIT_ENV_UNREADABLE not in (
        0, 1, runner_mod.EXIT_RUNNER_LOCKED)


# --- the runner holds rather than running ungated -----------------------

def test_no_env_holds_instead_of_claiming_a_job(tmp_path, monkeypatch) -> None:
    """The hazard poly-i7y describes: jobs processed with no dsynth gate."""
    queue_root = _queue(tmp_path)
    monkeypatch.setattr(er, "list_available_envs_detailed", lambda: ((), None))
    monkeypatch.setattr(runner_mod, "resolve_env", lambda job: None)
    monkeypatch.setattr(runner_mod, "resolve_env_for_gate", lambda: None)
    monkeypatch.setattr(runner_mod, "register_runner", lambda: None,
                        raising=False)
    monkeypatch.setattr(runner_mod, "start_heartbeat", lambda: None,
                        raising=False)
    monkeypatch.setattr(runner_mod, "stop_heartbeat", lambda: None,
                        raising=False)

    claimed = []
    monkeypatch.setattr(runner_mod, "claim_next_job_batch",
                        lambda *a, **k: claimed.append("claim"))

    rc = runner_mod.main(["--queue-root", str(queue_root), "--once"])
    runner_mod.release_runner_lock()

    assert rc == 0
    assert claimed == [], "claimed a job with the dsynth gate unanswerable"


def test_dsynth_is_never_probed_without_an_env(tmp_path, monkeypatch) -> None:
    """dsynth_active takes an env name; calling it with '' would ask about
    the wrong thing rather than about nothing."""
    queue_root = _queue(tmp_path)
    monkeypatch.setattr(er, "list_available_envs_detailed", lambda: ((), None))
    monkeypatch.setattr(runner_mod, "resolve_env", lambda job: None)
    monkeypatch.setattr(runner_mod, "resolve_env_for_gate", lambda: None)
    monkeypatch.setattr(runner_mod, "register_runner", lambda: None,
                        raising=False)
    monkeypatch.setattr(runner_mod, "start_heartbeat", lambda: None,
                        raising=False)
    monkeypatch.setattr(runner_mod, "stop_heartbeat", lambda: None,
                        raising=False)

    probed = []
    monkeypatch.setattr(runner_mod, "dsynth_active",
                        lambda env, qr: probed.append(env) or (False, ""))

    runner_mod.main(["--queue-root", str(queue_root), "--once"])
    runner_mod.release_runner_lock()

    assert probed == []
