"""The generator venv cache keys on what the venv is built from (poly-gfe7).

Not on the provisioned base id: that also moves with the world archive and
the helper scripts, neither of which the venv contains, so every new world
rebuilt an identical venv. And not on the pyprojects alone: bin/dportsv3's
install stamp covers every dev-env source file, so a hit after a dev-env
edit restored a venv the wrapper then reinstalled, uncached.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dports_dev_env import venv
from dports_dev_env.errors import ProvisionError
from dports_dev_env.layout import TOOL_RELATIVE, TOOL_VENV_RELATIVE

REPO = Path(__file__).resolve().parents[1]

INTERPRETER = {
    "version": "3.11.13",
    "executable": "/usr/local/bin/python3",
    "packages": ["pydantic==2.11.7", "pydantic-core==2.33.2"],
}


def seed_tool(root_dir: Path) -> Path:
    tool = root_dir / TOOL_RELATIVE
    package = tool / "dev-env" / "dports_dev_env"
    (package / "sub").mkdir(parents=True, exist_ok=True)
    (tool / "pyproject.toml").write_text("generator")
    (tool / "dev-env" / "pyproject.toml").write_text("dev-env")
    (package / "__init__.py").write_text("")
    (package / "sub" / "mod.py").write_text("x = 1\n")
    return tool


def key(**changes) -> str:
    interpreter = {**INTERPRETER, **changes.pop("interpreter", {})}
    args = {"pyproject_hash": "p", "dev_env_hash": "d", **changes}
    return venv.GeneratorVenvCache(None).venv_id(interpreter, **args)


def wrapper_dev_env_stamp(dev_env_dir: Path) -> str:
    """Run bin/dportsv3's own compute_dev_env_stamp program."""
    text = (REPO / "bin" / "dportsv3").read_text()
    body = text[text.index("compute_dev_env_stamp() {"):]
    program = body.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    return subprocess.run(
        [sys.executable, "-", str(dev_env_dir)],
        input=program, text=True, capture_output=True, check=True,
    ).stdout.strip()


# -- the key -----------------------------------------------------------------


def test_key_does_not_take_a_base_id():
    """Two bases with the same interpreter, packages and sources share one
    entry; the base id is not an input at all."""
    assert key() == key()


@pytest.mark.parametrize("changes", [
    {"interpreter": {"version": "3.11.14"}},
    {"interpreter": {"executable": "/usr/local/bin/python3.11"}},
    {"interpreter": {"packages": ["pydantic==2.11.8", "pydantic-core==2.33.2"]}},
    {"interpreter": {"packages": ["pydantic==2.11.7"]}},
    {"pyproject_hash": "p2"},
    {"dev_env_hash": "d2"},
])
def test_key_moves_with_every_input(changes):
    assert key(**changes) != key()


def test_key_moves_with_the_schema(monkeypatch):
    """An entry written under schema 2 is never matched again."""
    current = key()
    monkeypatch.setattr(venv, "GENERATOR_VENV_SCHEMA", 2)
    assert key() != current


# -- the dev-env digest ------------------------------------------------------


def test_dev_env_digest_matches_the_wrapper_on_the_real_tree():
    """If bin/dportsv3 changes what its stamp covers, this fails: the key
    has to change with it or hits go back to reinstalling."""
    dev_env = REPO / "dev-env"
    assert venv.dev_env_digest(dev_env) == wrapper_dev_env_stamp(dev_env)


def test_dev_env_digest_matches_the_wrapper_on_a_nested_tree(tmp_path):
    dev_env = seed_tool(tmp_path) / "dev-env"
    assert venv.dev_env_digest(dev_env) == wrapper_dev_env_stamp(dev_env)


def test_dev_env_digest_moves_with_a_source_edit(tmp_path):
    dev_env = seed_tool(tmp_path) / "dev-env"
    before = venv.dev_env_digest(dev_env)
    (dev_env / "dports_dev_env" / "sub" / "mod.py").write_text("x = 2\n")
    assert venv.dev_env_digest(dev_env) != before


def test_dev_env_digest_ignores_files_the_wrapper_ignores(tmp_path):
    dev_env = seed_tool(tmp_path) / "dev-env"
    before = venv.dev_env_digest(dev_env)
    (dev_env / "dports_dev_env" / "notes.txt").write_text("not python")
    (dev_env / "README.md").write_text("docs")
    assert venv.dev_env_digest(dev_env) == before


# -- the interpreter probe ---------------------------------------------------


def test_probe_reports_the_interpreter_and_what_it_can_import():
    out = subprocess.run(
        [sys.executable, "-c", venv.INTERPRETER_PROBE],
        text=True, capture_output=True, check=True,
    ).stdout
    probed = json.loads(out)

    assert probed["version"] == "%d.%d.%d" % sys.version_info[:3]
    assert probed["executable"] == sys.executable
    assert probed["packages"] == sorted(probed["packages"])
    assert f"pytest=={pytest.__version__}" in probed["packages"]
    # Names are normalized the way pip compares them.
    assert all(p.split("==")[0] == p.split("==")[0].lower() for p in probed["packages"])
    assert not any("_" in p.split("==")[0] for p in probed["packages"])


@pytest.mark.parametrize("output", ["not json", "[]", '{"version": "3.11.1"}'])
def test_unreadable_probe_output_is_a_provision_error(output):
    runner = SimpleNamespace(output=lambda argv: output)
    with pytest.raises(ProvisionError, match="interpreter probe"):
        venv.GeneratorVenvCache(None).probe_interpreter(runner)


# -- prepare -----------------------------------------------------------------


@pytest.fixture
def cache(set_setting, tmp_path, monkeypatch):
    """A cache with the chroot faked: the probe answers ``probe``, and the
    wrapper "builds" a venv only when none is there, as bin/dportsv3 does."""
    from dports_dev_env.config import load_config

    set_setting("dev_env.cache_root", str(tmp_path / "cache"))
    builds: list[Path] = []
    probe = dict(INTERPRETER)

    class Runner:
        def __init__(self, root_dir):
            self.root_dir = root_dir

        def output(self, argv):
            return json.dumps(probe)

        def run(self, argv):
            dest = self.root_dir / TOOL_VENV_RELATIVE
            if not dest.exists():
                builds.append(self.root_dir)
                (dest / "bin").mkdir(parents=True)
                (dest / "built-in").write_text(self.root_dir.name)
            return SimpleNamespace(returncode=0)

    monkeypatch.setattr(venv, "ChrootRunner", Runner)
    return SimpleNamespace(cache=venv.GeneratorVenvCache(load_config()), builds=builds, probe=probe)


def entries(cache) -> list[Path]:
    root = cache.cache.config.generator_venvs_dir
    return sorted(p for p in root.iterdir() if p.is_dir())


def test_two_bases_with_the_same_interpreter_share_one_entry(cache, tmp_path):
    first, second = tmp_path / "env1", tmp_path / "env2"
    seed_tool(first)
    seed_tool(second)

    cache.cache.prepare(first, "base-old-world")
    cache.cache.prepare(second, "base-new-world")

    assert cache.builds == [first]
    [entry] = entries(cache)
    assert (second / TOOL_VENV_RELATIVE / "built-in").read_text() == "env1"
    metadata = json.loads((entry / "metadata.json").read_text())
    assert metadata["bases"] == ["base-new-world", "base-old-world"]
    assert metadata["schema"] == venv.GENERATOR_VENV_SCHEMA
    assert metadata["system_packages"] == INTERPRETER["packages"]
    assert "provisioned_base_id" not in metadata


def test_a_dev_env_edit_builds_a_new_entry(cache, tmp_path):
    first, second = tmp_path / "env1", tmp_path / "env2"
    seed_tool(first)
    tool = seed_tool(second)
    (tool / "dev-env" / "dports_dev_env" / "sub" / "mod.py").write_text("x = 2\n")

    cache.cache.prepare(first, "base")
    cache.cache.prepare(second, "base")

    assert cache.builds == [first, second]
    assert len(entries(cache)) == 2


def test_a_different_system_package_builds_a_new_entry(cache, tmp_path):
    first, second = tmp_path / "env1", tmp_path / "env2"
    seed_tool(first)
    seed_tool(second)

    cache.cache.prepare(first, "base1")
    cache.probe["packages"] = ["pydantic==2.12.0", "pydantic-core==2.33.2"]
    cache.cache.prepare(second, "base2")

    assert cache.builds == [first, second]


def test_a_miss_does_not_reuse_a_venv_already_in_the_env(cache, tmp_path):
    """bin/dportsv3 would reuse it when its stamp matches; built against
    another base's interpreter, it must not be cached under this key."""
    env = tmp_path / "env"
    seed_tool(env)
    stale = env / TOOL_VENV_RELATIVE
    (stale / "bin").mkdir(parents=True)
    (stale / "built-in").write_text("another base")

    cache.cache.prepare(env, "base")

    assert cache.builds == [env]
    [entry] = entries(cache)
    assert (entry / "venv" / "built-in").read_text() == "env"


def test_a_restore_records_each_base_once(cache, tmp_path):
    for name in ("env1", "env2", "env3"):
        seed_tool(tmp_path / name)
    cache.cache.prepare(tmp_path / "env1", "base")
    cache.cache.prepare(tmp_path / "env2", "base")
    cache.cache.prepare(tmp_path / "env3", "other")

    [entry] = entries(cache)
    assert json.loads((entry / "metadata.json").read_text())["bases"] == ["base", "other"]


def test_unreadable_metadata_does_not_fail_a_restore(cache, tmp_path):
    seed_tool(tmp_path / "env1")
    seed_tool(tmp_path / "env2")
    cache.cache.prepare(tmp_path / "env1", "base")
    [entry] = entries(cache)
    (entry / "metadata.json").write_text("{broken")

    cache.cache.prepare(tmp_path / "env2", "other")

    assert cache.builds == [tmp_path / "env1"]
    assert (tmp_path / "env2" / TOOL_VENV_RELATIVE / "built-in").exists()
