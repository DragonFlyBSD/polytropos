"""`dportsv3 deploy install` — planning is separate from doing.

poly-abr.3. Every decision lives in plan(), which touches nothing, so
the interesting behaviour is testable without root and --dry-run shows
exactly what would happen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3 import paths
from dportsv3.commands import deploy as dep

DEPLOY_SRC = Path(__file__).resolve().parents[1] / "deploy"


def plan(tmp_path, *, exists=False, logs_root=None, prefix=None):
    return dep.plan(
        deploy=DEPLOY_SRC,
        prefix=prefix or tmp_path / "usr" / "local",
        user="polytropos", group="polytropos",
        logs_root=logs_root or tmp_path / "logs",
        user_exists=lambda n: exists, group_exists=lambda n: exists,
    )


def kinds(actions, kind):
    return [a for a in actions if a.kind == kind]


# --- what the plan contains ---------------------------------------------

def test_every_rc_script_is_installed(tmp_path) -> None:
    got = kinds(plan(tmp_path), "rc_script")
    assert sorted(a.target for a in got) == sorted(dep.RC_SCRIPTS)


def test_all_four_queue_subdirs_are_created(tmp_path) -> None:
    """The runner exits 1 naming a missing one."""
    made = [str(a.target) for a in kinds(plan(tmp_path), "mkdir")]
    for sub in dep.QUEUE_SUBDIRS:
        assert any(m.endswith(f"queue/{sub}") for m in made), sub


def test_account_creation_is_skipped_when_present(tmp_path) -> None:
    for action in plan(tmp_path, exists=True):
        if action.kind in ("user", "group"):
            assert action.skipped == "already exists"


def test_account_creation_is_planned_when_absent(tmp_path) -> None:
    for action in plan(tmp_path, exists=False):
        if action.kind in ("user", "group"):
            assert action.skipped is None


# --- tool-owned vs operator-owned, the @sample rule ---------------------

def test_operator_files_are_never_overwritten(tmp_path) -> None:
    """Ports' @sample keyword copies <f>.sample to <f> only when <f> is
    absent. Config an operator has edited is theirs."""
    prefix = tmp_path / "usr" / "local"
    (prefix / "etc" / "polytropos").mkdir(parents=True)
    (prefix / "etc" / "polytropos.conf").write_text("# edited by hand\n")

    for action in kinds(plan(tmp_path, prefix=prefix), "operator_file"):
        if action.target == "polytropos.conf":
            assert action.skipped == "exists; left as it is"
        else:
            assert action.skipped is None


def test_rc_scripts_are_always_replaced(tmp_path) -> None:
    """The opposite rule, and deliberately so: an upgrade that leaves a
    stale rc script in place is worse than one that overwrites it."""
    prefix = tmp_path / "usr" / "local"
    rc = prefix / "etc" / "rc.d"
    rc.mkdir(parents=True)
    for name in dep.RC_SCRIPTS:
        (rc / name).write_text("#!/bin/sh\n# old version\n")

    for action in kinds(plan(tmp_path, prefix=prefix), "rc_script"):
        assert action.skipped is None, "left a stale rc script in place"


def test_chown_runs_even_on_a_fresh_logs_root(tmp_path) -> None:
    """The plan's own mkdir steps create the queue directories under the
    logs root, as root. Skipping the chown because the tree does not exist
    *at plan time* leaves it root-owned, and the services then fail on
    their first write with PermissionError creating evidence/blobstore.
    """
    action, = kinds(plan(tmp_path, logs_root=tmp_path / "nope"), "chown")
    assert action.skipped is None, "left a freshly created tree root-owned"


def test_chown_comes_after_the_directories_it_hands_over(tmp_path) -> None:
    """Order matters: chown -R has to run once the tree is fully built."""
    actions = plan(tmp_path)
    kinds_in_order = [a.kind for a in actions]
    assert kinds_in_order[-1] == "chown", kinds_in_order[-3:]


def test_chown_is_planned_when_the_tree_exists(tmp_path) -> None:
    """Group inheritance covers new files, but what is already there
    predates the account and is owned by root."""
    logs = tmp_path / "logs"
    (logs / "evidence").mkdir(parents=True)
    action, = kinds(plan(tmp_path, logs_root=logs), "chown")
    assert action.skipped is None


# --- a broken checkout is caught before anything is touched -------------

def test_missing_rc_script_raises(tmp_path) -> None:
    fake = tmp_path / "deploy"
    (fake / "rc.d").mkdir(parents=True)
    with pytest.raises(paths.MissingInput, match="missing rc script"):
        dep.plan(deploy=fake, prefix=tmp_path, user="u", group="g",
                 logs_root=tmp_path / "l",
                 user_exists=lambda n: True, group_exists=lambda n: True)


def test_missing_sample_raises(tmp_path) -> None:
    fake = tmp_path / "deploy"
    (fake / "rc.d").mkdir(parents=True)
    for name in dep.RC_SCRIPTS:
        (fake / "rc.d" / name).write_text("#!/bin/sh\n")
    with pytest.raises(paths.MissingInput, match="missing sample"):
        dep.plan(deploy=fake, prefix=tmp_path, user="u", group="g",
                 logs_root=tmp_path / "l",
                 user_exists=lambda n: True, group_exists=lambda n: True)


# --- apply, for the steps that need no privileges -----------------------

def test_apply_installs_the_files_with_the_right_modes(tmp_path) -> None:
    prefix = tmp_path / "usr" / "local"
    actions = [a for a in plan(tmp_path, exists=True, prefix=prefix)
               if a.kind in ("mkdir", "rc_script", "operator_file")
               and "polytropos/chat.env" != str(a.target)]  # chat.env chowns
    dep.apply(actions, deploy=DEPLOY_SRC, prefix=prefix,
              user="polytropos", group="polytropos",
              logs_root=tmp_path / "logs", log=lambda *a: None)

    for name in dep.RC_SCRIPTS:
        dst = prefix / "etc" / "rc.d" / name
        assert dst.is_file()
        assert dst.stat().st_mode & 0o777 == 0o755, name

    conf = prefix / "etc" / "polytropos.conf"
    assert conf.stat().st_mode & 0o777 == 0o644
    secret = prefix / "etc" / "polytropos" / "harness.env"
    assert secret.stat().st_mode & 0o777 == 0o600, "API keys must not be readable"


def test_apply_is_idempotent(tmp_path) -> None:
    prefix = tmp_path / "usr" / "local"
    for _ in range(2):
        actions = [a for a in plan(tmp_path, exists=True, prefix=prefix)
                   if a.kind in ("mkdir", "rc_script", "operator_file")
                   and "polytropos/chat.env" != str(a.target)]
        dep.apply(actions, deploy=DEPLOY_SRC, prefix=prefix,
                  user="polytropos", group="polytropos",
                  logs_root=tmp_path / "logs", log=lambda *a: None)
    assert (prefix / "etc" / "rc.d" / "polytropos_runner").is_file()


def test_apply_does_not_clobber_edited_config(tmp_path) -> None:
    prefix = tmp_path / "usr" / "local"
    (prefix / "etc").mkdir(parents=True)
    (prefix / "etc" / "polytropos.conf").write_text("# mine\n")

    actions = [a for a in plan(tmp_path, exists=True, prefix=prefix)
               if a.kind in ("mkdir", "rc_script", "operator_file")
               and "polytropos/chat.env" != str(a.target)]
    dep.apply(actions, deploy=DEPLOY_SRC, prefix=prefix,
              user="polytropos", group="polytropos",
              logs_root=tmp_path / "logs", log=lambda *a: None)
    assert (prefix / "etc" / "polytropos.conf").read_text() == "# mine\n"


def test_skipped_actions_do_nothing(tmp_path) -> None:
    """A skipped step must not run — that is what makes re-running safe."""
    ran = []
    dep.apply([dep.Action("chown", "would chown", tmp_path, skipped="no")],
              deploy=DEPLOY_SRC, prefix=tmp_path, user="u", group="g",
              logs_root=tmp_path, log=lambda m: ran.append(m))
    assert ran and ran[0].startswith("  skip")


# --- the command wrapper ------------------------------------------------

def test_refuses_on_the_wrong_platform(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(dep.platform, "system", lambda: "Linux")
    from argparse import Namespace
    rc = dep.cmd_deploy(Namespace(deploy_action="install", prefix="/usr/local",
                                  tool_root=str(tmp_path), user="u", group="g",
                                  logs_root=str(tmp_path), dry_run=True))
    assert rc == 1
    assert "DragonFly" in capsys.readouterr().err


def test_dry_run_changes_nothing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(dep.platform, "system", lambda: "DragonFly")
    monkeypatch.setenv("DPORTS_DEV_TOOL_ROOT",
                       str(Path(__file__).resolve().parents[1]))
    prefix = tmp_path / "usr" / "local"
    from argparse import Namespace
    rc = dep.cmd_deploy(Namespace(deploy_action="install", prefix=str(prefix),
                                  tool_root=None, user="polytropos",
                                  group="polytropos",
                                  logs_root=str(tmp_path / "logs"),
                                  dry_run=True))
    assert rc == 0
    assert not prefix.exists(), "dry run created something"
    assert "nothing was changed" in capsys.readouterr().out


def test_non_root_is_refused(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(dep.platform, "system", lambda: "DragonFly")
    monkeypatch.setattr(dep.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("DPORTS_DEV_TOOL_ROOT",
                       str(Path(__file__).resolve().parents[1]))
    from argparse import Namespace
    rc = dep.cmd_deploy(Namespace(deploy_action="install",
                                  prefix=str(tmp_path / "p"), tool_root=None,
                                  user="u", group="g",
                                  logs_root=str(tmp_path / "l"), dry_run=False))
    assert rc == 1
    assert "needs root" in capsys.readouterr().err


def test_a_checkout_without_deploy_is_reported(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(dep.platform, "system", lambda: "DragonFly")
    from argparse import Namespace
    rc = dep.cmd_deploy(Namespace(deploy_action="install", prefix="/usr/local",
                                  tool_root=str(tmp_path), user="u", group="g",
                                  logs_root=str(tmp_path), dry_run=True))
    assert rc == 1
    assert "does not look like a polytropos checkout" in capsys.readouterr().err


# --- the packaged-vs-checkout command gap (poly-abr.9) ------------------

def test_missing_commands_are_detected(tmp_path) -> None:
    assert dep.missing_commands(tmp_path) == list(dep.EXPECTED_COMMANDS)


def test_present_commands_are_not_reported(tmp_path) -> None:
    (tmp_path / "bin").mkdir()
    for name in dep.EXPECTED_COMMANDS:
        (tmp_path / "bin" / name).write_text("#!/bin/sh\n")
    assert dep.missing_commands(tmp_path) == []


def test_partial_install_names_only_what_is_missing(tmp_path) -> None:
    """dports-dev-env comes from a different distribution than dportsv3,
    so one can be installed without the other."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "dportsv3").write_text("#!/bin/sh\n")
    assert dep.missing_commands(tmp_path) == [
        c for c in dep.EXPECTED_COMMANDS if c != "dportsv3"
    ]


def test_the_store_client_is_one_of_the_installed_commands() -> None:
    """The dsynth hooks shell out to it on every failed build. It was in
    no distribution's scripts and no install step copied it, so a packaged
    host had nothing for ARTIFACT_STORE_CLIENT to point at and the hook
    died at require_artifact_store — dropping the evidence, quietly."""
    assert "artifact-store-client" in dep.EXPECTED_COMMANDS


# --- installing the software itself -------------------------------------

def software(actions):
    return [a for a in actions if a.kind in ("venv", "pip", "link")]


def test_software_is_installed_from_a_source_tree(tmp_path) -> None:
    """Without this the only way to get the commands onto a host is to
    point the services at a checkout, which is the coupling the whole
    design removes."""
    acts = dep.plan(deploy=DEPLOY_SRC, prefix=tmp_path / "usr/local",
                    user="u", group="g", logs_root=tmp_path / "l",
                    source=tmp_path / "src",
                    user_exists=lambda n: True, group_exists=lambda n: True)
    kinds = [a.kind for a in software(acts)]
    expected = ["venv", "pip", "pip"] + ["link"] * len(dep.EXPECTED_COMMANDS)
    assert kinds == expected, kinds


def test_dev_env_is_installed_before_the_generator(tmp_path) -> None:
    """dports-dev-env is a sibling source tree, not a PyPI package. Install
    the generator first and pip reaches for an index for a name that does
    not exist there."""
    acts = dep.plan(deploy=DEPLOY_SRC, prefix=tmp_path, user="u", group="g",
                    logs_root=tmp_path / "l", source=tmp_path / "src",
                    user_exists=lambda n: True, group_exists=lambda n: True)
    pips = [a.target for a in acts if a.kind == "pip"]
    assert pips[0].endswith("dev-env"), pips
    assert "[tracker]" in pips[1], pips


def test_the_tracker_extra_is_requested(tmp_path) -> None:
    """Base profile has no uvicorn, so the tracker would not start."""
    acts = dep.plan(deploy=DEPLOY_SRC, prefix=tmp_path, user="u", group="g",
                    logs_root=tmp_path / "l", source=tmp_path / "src",
                    user_exists=lambda n: True, group_exists=lambda n: True)
    assert any("[tracker]" in a.target for a in acts if a.kind == "pip")


def test_commands_are_linked_where_the_rc_defaults_look(tmp_path) -> None:
    prefix = tmp_path / "usr/local"
    acts = dep.plan(deploy=DEPLOY_SRC, prefix=prefix, user="u", group="g",
                    logs_root=tmp_path / "l", source=tmp_path / "src",
                    user_exists=lambda n: True, group_exists=lambda n: True)
    links = [a.detail for a in acts if a.kind == "link"]
    for command in dep.EXPECTED_COMMANDS:
        assert any(str(prefix / "bin" / command) in d for d in links), command


def test_pip_steps_are_never_skipped(tmp_path) -> None:
    """Re-running install IS the upgrade path, so the installs must repeat
    even when the venv is already there."""
    prefix = tmp_path / "usr/local"
    (prefix / dep.VENV_RELATIVE / "bin").mkdir(parents=True)
    acts = dep.plan(deploy=DEPLOY_SRC, prefix=prefix, user="u", group="g",
                    logs_root=tmp_path / "l", source=tmp_path / "src",
                    user_exists=lambda n: True, group_exists=lambda n: True)
    assert all(a.skipped is None for a in acts if a.kind == "pip")
    venv, = [a for a in acts if a.kind == "venv"]
    assert venv.skipped == "already exists"


def test_no_source_means_no_software_steps(tmp_path) -> None:
    """A packaged install has nothing to install itself from, and a port
    owns the software there."""
    acts = dep.plan(deploy=DEPLOY_SRC, prefix=tmp_path, user="u", group="g",
                    logs_root=tmp_path / "l", source=None,
                    user_exists=lambda n: True, group_exists=lambda n: True)
    assert [a.kind for a in acts if a.kind == "pip"] == []
    venv, = [a for a in acts if a.kind == "venv"]
    assert venv.skipped


def test_software_comes_before_the_services_that_need_it(tmp_path) -> None:
    """rc.d scripts probe their command in start_precmd; installing them
    before the command exists would just fail on first start."""
    acts = dep.plan(deploy=DEPLOY_SRC, prefix=tmp_path, user="u", group="g",
                    logs_root=tmp_path / "l", source=tmp_path / "src",
                    user_exists=lambda n: True, group_exists=lambda n: True)
    kinds = [a.kind for a in acts]
    assert kinds.index("link") < kinds.index("rc_script")
