"""An env is built from two separate checkouts since the tool was split
out of the ports repository.

None of the modules exercised here had any coverage, so the rest of the
suite staying green says nothing about them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    # gpgsign is set globally on some dev boxes and breaks commits over a
    # non-tty; force it off per-invocation rather than touching a gitconfig.
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "master")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    return path


def make_ports_tree(path: Path) -> Path:
    init_repo(path)
    (path / "ports" / "editors" / "vim").mkdir(parents=True)
    (path / "ports" / "editors" / "vim" / "overlay.dops").write_text("version 0\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "ports")
    return path


def make_tool_checkout(path: Path) -> Path:
    init_repo(path)
    (path / "bin").mkdir()
    (path / "bin" / "dportsv3").write_text("#!/bin/sh\n")
    (path / "pyproject.toml").write_text('[project]\nname = "dports-dragonfly"\n')
    (path / "dev-env").mkdir()
    (path / "dev-env" / "pyproject.toml").write_text('[project]\nname = "dports-dev-env"\n')
    git(path, "add", "-A")
    git(path, "commit", "-qm", "tool")
    return path


def make_builder(tmp_path, monkeypatch, *, delta_root: Path, tool_root: Path, allow_dirty: bool = False):
    from dports_dev_env.builder import CreateOptions, EnvironmentBuilder
    from dports_dev_env.config import load_config
    from dports_dev_env.store import EnvironmentStore

    monkeypatch.setenv("DPORTS_DEV_CACHE_ROOT", str(tmp_path / "cache"))
    config = load_config()
    options = CreateOptions(
        name="env1",
        target="@2026Q3",
        origin=None,
        delta_root=delta_root,
        tool_root=tool_root,
        backend="chroot",
        freebsd_branch=None,
        dports_branch="staged",
        allow_dirty=allow_dirty,
        no_initial_compose=True,
        oracle_profile="off",
    )
    return EnvironmentBuilder(config, EnvironmentStore(config), options)


def stub_host_commands(monkeypatch) -> None:
    """validate() also requires chroot/mount_null, which no dev machine but
    DragonFly has. Not what these tests are about."""
    from dports_dev_env import builder

    monkeypatch.setattr(builder.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_validate_accepts_the_two_trees(tmp_path, monkeypatch):
    ports = make_ports_tree(tmp_path / "DeltaPorts")
    tool = make_tool_checkout(tmp_path / "polytropos")
    builder = make_builder(tmp_path, monkeypatch, delta_root=ports, tool_root=tool)
    stub_host_commands(monkeypatch)

    builder.validate()


def test_validate_rejects_the_two_trees_swapped(tmp_path, monkeypatch):
    """The old check asked whether --delta-root held a `dportsv3` file, which
    was true only while the tool lived inside the ports checkout — so it
    rejected every real ports tree once it moved out."""
    from dports_dev_env.errors import UsageError

    ports = make_ports_tree(tmp_path / "DeltaPorts")
    tool = make_tool_checkout(tmp_path / "polytropos")
    builder = make_builder(tmp_path, monkeypatch, delta_root=tool, tool_root=ports)
    stub_host_commands(monkeypatch)

    with pytest.raises(UsageError, match="--delta-root.*ports/ nor a special/"):
        builder.validate()


def test_validate_rejects_a_tool_root_without_the_wrapper(tmp_path, monkeypatch):
    """bin/dportsv3, not ./dportsv3 — at the tool repo root that name is the
    Python package directory."""
    from dports_dev_env.errors import UsageError

    ports = make_ports_tree(tmp_path / "DeltaPorts")
    tool = make_tool_checkout(tmp_path / "polytropos")
    (tool / "bin" / "dportsv3").unlink()
    (tool / "dportsv3").mkdir()
    git(tool, "add", "-A")
    git(tool, "commit", "-qm", "package dir, no wrapper")
    builder = make_builder(tmp_path, monkeypatch, delta_root=ports, tool_root=tool)
    stub_host_commands(monkeypatch)

    with pytest.raises(UsageError, match="--tool-root.*bin/dportsv3"):
        builder.validate()


def test_validate_rejects_a_dirty_checkout_unless_allowed(tmp_path, monkeypatch):
    from dports_dev_env.errors import UsageError

    ports = make_ports_tree(tmp_path / "DeltaPorts")
    tool = make_tool_checkout(tmp_path / "polytropos")
    (tool / "pyproject.toml").write_text('[project]\nname = "edited"\n')
    stub_host_commands(monkeypatch)

    strict = make_builder(tmp_path, monkeypatch, delta_root=ports, tool_root=tool)
    with pytest.raises(UsageError, match="refusing to create env from a dirty"):
        strict.validate()

    lenient = make_builder(tmp_path, monkeypatch, delta_root=ports, tool_root=tool, allow_dirty=True)
    lenient.validate()


def test_tool_branch_follows_the_host_checkout(tmp_path, monkeypatch):
    ports = make_ports_tree(tmp_path / "DeltaPorts")
    tool = make_tool_checkout(tmp_path / "polytropos")
    git(tool, "switch", "-qc", "x5-work")
    builder = make_builder(tmp_path, monkeypatch, delta_root=ports, tool_root=tool)
    stub_host_commands(monkeypatch)

    builder.validate()

    assert builder.tool_branch == "x5-work"
    assert builder.initial_state(provisioned_base_id="b").repos.tool_branch == "x5-work"


def test_a_created_env_can_be_read_back(tmp_path, monkeypatch):
    """initial_state() hardcoded schema=1, so every env was unreadable the
    moment it was saved. create() never re-reads its own state, so it reported
    success and the next command failed."""
    ports = make_ports_tree(tmp_path / "DeltaPorts")
    tool = make_tool_checkout(tmp_path / "polytropos")
    builder = make_builder(tmp_path, monkeypatch, delta_root=ports, tool_root=tool)
    stub_host_commands(monkeypatch)
    builder.validate()

    builder.store.save(builder.initial_state(provisioned_base_id="base1"))

    assert builder.store.load("env1").source.tool_root == str(tool)


def test_detached_tool_head_fails_before_the_env_dir_exists(tmp_path, monkeypatch):
    """Resolving the branch inside initial_state() would raise after
    env_dir.mkdir() and outside the try that records a failed env, leaving an
    orphan directory holding the name."""
    from dports_dev_env.errors import UsageError

    ports = make_ports_tree(tmp_path / "DeltaPorts")
    tool = make_tool_checkout(tmp_path / "polytropos")
    git(tool, "checkout", "-q", "--detach")
    builder = make_builder(tmp_path, monkeypatch, delta_root=ports, tool_root=tool)
    stub_host_commands(monkeypatch)

    with pytest.raises(UsageError, match="detached HEAD"):
        builder.validate()

    assert not builder.env_dir.exists()


def test_default_tool_root_prefers_env_then_raises(tmp_path, monkeypatch):
    from dports_dev_env.builder import default_tool_root
    from dports_dev_env.errors import UsageError

    monkeypatch.delenv("DPORTS_DEV_TOOL_ROOT", raising=False)
    with pytest.raises(UsageError, match="pass --tool-root"):
        default_tool_root()

    monkeypatch.setenv("DPORTS_DEV_TOOL_ROOT", str(tmp_path))
    assert default_tool_root() == tmp_path

    monkeypatch.setenv("DPORTS_DEV_TOOL_ROOT", str(tmp_path / "absent"))
    with pytest.raises(UsageError, match="not a directory"):
        default_tool_root()


def make_state(**overrides):
    from dports_dev_env import state as state_mod

    fields = dict(
        schema=state_mod.STATE_SCHEMA,
        name="env1",
        backend="chroot",
        target="@2026Q3",
        origin="",
        status="ready",
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-20T00:00:00Z",
        root_dir=Path("/envs/env1/root"),
        writable_dir=Path("/envs/env1/writable"),
        provisioned_base_id="base1",
        repos=state_mod.RepoState("master", "2026Q3", "staged", "x5-work"),
        source=state_mod.SourceState("/host/DeltaPorts", "/host/polytropos"),
        runtime=state_mod.RuntimeState("/usr/distfiles", "off"),
        initial_compose=state_mod.InitialComposeState("not-run", "2026-08-20T00:00:00Z"),
    )
    fields.update(overrides)
    return state_mod.EnvironmentState(**fields)


def test_state_round_trips_both_checkouts():
    from dports_dev_env.state import state_from_json, state_to_json

    original = make_state()
    restored = state_from_json(json.loads(json.dumps(state_to_json(original))))

    assert restored == original
    assert restored.source.tool_root == "/host/polytropos"
    assert restored.repos.tool_branch == "x5-work"


def test_pre_split_state_is_refused_not_migrated(tmp_path):
    """Schema 1 envs were built with the tool inside the ports checkout, a
    layout that can no longer be provisioned."""
    from dports_dev_env.errors import StateError
    from dports_dev_env.state import read_env_state, state_to_json, write_env_state

    write_env_state(tmp_path, make_state())
    data = json.loads((tmp_path / "env.json").read_text())
    data["schema"] = 1
    (tmp_path / "env.json").write_text(json.dumps(data))

    with pytest.raises(StateError, match="destroy it and create it again"):
        read_env_state(tmp_path)


def seed_env_projects(root_dir: Path, *, generator: str, dev_env: str) -> None:
    from dports_dev_env.layout import TOOL_RELATIVE

    tool = root_dir / TOOL_RELATIVE
    (tool / "dev-env").mkdir(parents=True, exist_ok=True)
    (tool / "pyproject.toml").write_text(generator)
    (tool / "dev-env" / "pyproject.toml").write_text(dev_env)


def venv_cache(tmp_path, monkeypatch):
    from dports_dev_env.config import load_config
    from dports_dev_env.venv import GeneratorVenvCache

    monkeypatch.setenv("DPORTS_DEV_CACHE_ROOT", str(tmp_path / "cache"))
    return GeneratorVenvCache(load_config())


def test_venv_key_covers_the_dev_env_project_too(tmp_path, monkeypatch):
    """bin/dportsv3 installs dev-env into the generator venv, so keying on the
    generator pyproject alone hands back a stale venv when dev-env's deps move."""
    cache = venv_cache(tmp_path, monkeypatch)
    root = tmp_path / "root"

    seed_env_projects(root, generator="a", dev_env="b")
    baseline = cache.pyproject_hash(root)

    seed_env_projects(root, generator="a2", dev_env="b")
    assert cache.pyproject_hash(root) != baseline

    seed_env_projects(root, generator="a", dev_env="b2")
    assert cache.pyproject_hash(root) != baseline

    seed_env_projects(root, generator="a", dev_env="b")
    assert cache.pyproject_hash(root) == baseline


def test_venv_key_distinguishes_a_dependency_moving_between_projects(tmp_path, monkeypatch):
    """Same combined bytes, different project — the path is hashed in too."""
    cache = venv_cache(tmp_path, monkeypatch)
    root = tmp_path / "root"

    seed_env_projects(root, generator="dep", dev_env="")
    moved_to_generator = cache.pyproject_hash(root)

    seed_env_projects(root, generator="", dev_env="dep")
    assert cache.pyproject_hash(root) != moved_to_generator


def test_venv_key_reports_a_missing_project(tmp_path, monkeypatch):
    from dports_dev_env.errors import ProvisionError
    from dports_dev_env.layout import TOOL_RELATIVE

    cache = venv_cache(tmp_path, monkeypatch)
    root = tmp_path / "root"
    seed_env_projects(root, generator="a", dev_env="b")
    (root / TOOL_RELATIVE / "dev-env" / "pyproject.toml").unlink()

    with pytest.raises(ProvisionError, match="missing project in env"):
        cache.pyproject_hash(root)


@pytest.mark.parametrize("helper", ["regen", "reapply"])
def test_helpers_call_the_tool_but_compose_the_ports_tree(helper):
    """The two paths must not collapse into one: /work/DeltaPorts is the
    agent's edit surface and the guardrails reject writes anywhere else."""
    from dports_dev_env.helpers import helper_body
    from dports_dev_env.layout import PORTS_DIR, TOOL_BIN

    body = helper_body(helper)

    assert f"{TOOL_BIN} compose" in body
    assert f"--delta-root {PORTS_DIR}" in body
    assert f"{PORTS_DIR}/dportsv3" not in body


def test_shell_env_names_both_trees():
    from dports_dev_env.helpers import build_env_dict
    from dports_dev_env.layout import PORTS_DIR, TOOL_DIR

    env = build_env_dict(make_state())

    assert env["DELTAPORTS_ROOT"] == PORTS_DIR
    assert env["POLYTROPOS_ROOT"] == TOOL_DIR


def test_env_repos_fast_forwards_the_tool_checkout():
    from dports_dev_env.layout import PORTS_RELATIVE, TOOL_RELATIVE
    from dports_dev_env.update import ENV_REPOS

    assert {rel for _, rel in ENV_REPOS} >= {PORTS_RELATIVE, TOOL_RELATIVE}
