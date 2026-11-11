from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "dev-env" / "dports_dev_env" / "runtime-profiles.toml"


def test_default_runtime_profile_loads():
    from dports_dev_env.runtime_profiles import load_runtime_profile

    profile = load_runtime_profile()
    assert profile.name == "dportsv3-py312"
    assert profile.python == "3.12"
    assert "py312-fastapi" in profile.packages


def test_the_py311_profile_is_kept_for_what_already_runs_it():
    from dports_dev_env.runtime_profiles import load_runtime_profile

    profile = load_runtime_profile("dportsv3-py311")
    assert profile.python == "3.11"
    assert "py311-fastapi" in profile.packages


@pytest.mark.parametrize("name", ["dportsv3-py311", "dportsv3-py312"])
def test_a_profiles_packages_are_for_its_python(name):
    """A py311- name in the 3.12 profile installs a module the venv's
    interpreter cannot import."""
    from dports_dev_env.runtime_profiles import load_runtime_profile

    profile = load_runtime_profile(name)
    prefix = "py" + profile.python.replace(".", "") + "-"
    assert profile.packages
    assert all(p.startswith(prefix) for p in profile.packages)


def test_unknown_runtime_profile_fails(monkeypatch):
    from dports_dev_env.errors import ConfigError
    from dports_dev_env.runtime_profiles import load_runtime_profile

    monkeypatch.setenv("DPORTS_DEV_RUNTIME_PROFILE", "missing")
    with pytest.raises(ConfigError, match="unknown runtime profile"):
        load_runtime_profile()


def test_runtime_profile_participates_in_base_id(set_setting, monkeypatch, tmp_path):
    from dataclasses import replace

    from dports_dev_env.base import BaseArchive, provisioned_base_id
    from dports_dev_env.config import load_config
    from dports_dev_env.runtime_profiles import RuntimeProfile

    set_setting("dev_env.cache_root", str(tmp_path / "cache"))
    config = load_config()
    archive = BaseArchive("world.tar.gz", tmp_path / "world.tar.gz", "abc123")

    assert len(provisioned_base_id(config, archive)) == 12

    changed = replace(
        config,
        runtime_profile=RuntimeProfile(
            schema=1,
            name="dportsv3-py311",
            python="3.11",
            packages=["py311-fastapi"],
        ),
    )

    assert provisioned_base_id(config, archive) != provisioned_base_id(changed, archive)


# -- bin/dportsv3's pick ---------------------------------------------------------


RUNNING = "%d.%d" % sys.version_info[:2]


def wrapper_profile_packages(manifest: Path, env: dict[str, str] | None = None) -> str:
    """Run bin/dportsv3's own runtime_profile_packages program."""
    text = (REPO / "bin" / "dportsv3").read_text()
    body = text[text.index("runtime_profile_packages() {"):]
    program = body.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    return subprocess.run(
        [sys.executable, "-", str(manifest)],
        input=program, text=True, capture_output=True, check=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
    ).stdout.strip()


def write_manifest(tmp_path: Path, default: str, profiles: dict[str, str]) -> Path:
    lines = ["schema = 1", f'default = "{default}"']
    for name, python in profiles.items():
        lines += [f"[profiles.{name}]", f'python = "{python}"', f'packages = ["{name}-pkg"]']
    path = tmp_path / "runtime-profiles.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_wrapper_checks_the_default_when_it_is_for_the_running_python(tmp_path):
    manifest = write_manifest(tmp_path, "new", {"old": "2.7", "new": RUNNING})
    assert wrapper_profile_packages(manifest) == "new-pkg"


def test_the_wrapper_checks_the_profile_for_the_running_python(tmp_path):
    """A 3.11 host, or an env provisioned before the default moved, has the
    old profile's modules and none of the new one's."""
    manifest = write_manifest(tmp_path, "new", {"old": RUNNING, "new": "9.99"})
    assert wrapper_profile_packages(manifest) == "old-pkg"


def test_the_wrapper_falls_back_to_the_default(tmp_path):
    manifest = write_manifest(tmp_path, "new", {"old": "2.7", "new": "9.99"})
    assert wrapper_profile_packages(manifest) == "new-pkg"


def test_the_wrapper_honours_an_explicit_profile(tmp_path):
    manifest = write_manifest(tmp_path, "new", {"old": RUNNING, "new": "9.99"})
    env = {"DPORTS_DEV_RUNTIME_PROFILE": "new"}
    assert wrapper_profile_packages(manifest, env) == "new-pkg"


def test_the_wrapper_reads_the_real_manifest():
    """On the test's own Python: its profile when there is one, else the default."""
    from dports_dev_env.runtime_profiles import load_runtime_profile

    profiles = [load_runtime_profile(n) for n in ("dportsv3-py311", "dportsv3-py312")]
    expected = next((p for p in profiles if p.python == RUNNING), load_runtime_profile())
    assert wrapper_profile_packages(MANIFEST).split() == expected.packages


# -- what a base records -----------------------------------------------------------


def test_a_base_records_the_profile_it_was_provisioned_with(set_setting, tmp_path):
    from dports_dev_env.base import BaseArchive, provisioned_runtime_profile
    from dports_dev_env.config import load_config
    from dports_dev_env.provision import BaseProvisioner

    set_setting("dev_env.cache_root", str(tmp_path / "cache"))
    config = load_config()
    base_dir = config.provisioned_bases_dir / "abc"
    base_dir.mkdir(parents=True)
    archive = BaseArchive("world.tar.gz", tmp_path / "world.tar.gz", "0" * 64)
    BaseProvisioner(config).write_metadata(base_dir, archive, "abc")

    assert provisioned_runtime_profile(config, "abc") == config.runtime_profile


@pytest.mark.parametrize("metadata", [
    None,
    "not json",
    {"archive": {"asset": "world.tar.gz"}},
    {"runtime_profile": {"schema": 1, "name": "p", "python": "3.11"}},
    {"runtime_profile": {"schema": 1, "name": "p", "python": "3.11", "packages": [""]}},
    {"runtime_profile": {"schema": 1, "name": "", "python": "3.11", "packages": ["x"]}},
])
def test_a_base_that_does_not_say_has_no_recorded_profile(set_setting, tmp_path, metadata):
    from dports_dev_env.base import provisioned_runtime_profile
    from dports_dev_env.config import load_config

    set_setting("dev_env.cache_root", str(tmp_path / "cache"))
    config = load_config()
    base_dir = config.provisioned_bases_dir / "abc"
    base_dir.mkdir(parents=True)
    if metadata is not None:
        text = metadata if isinstance(metadata, str) else json.dumps(metadata)
        (base_dir / "metadata.json").write_text(text)

    assert provisioned_runtime_profile(config, "abc") is None
    assert provisioned_runtime_profile(config, "") is None


# -- provisioning refuses a mismatched python3 -------------------------------------


@pytest.mark.parametrize("stdout, returncode, ok", [
    ("3.12\n", 0, True),
    ("3.11\n", 0, False),
    ("", 127, False),
])
def test_provisioning_stops_when_python3_is_not_the_profiles(
        set_setting, tmp_path, monkeypatch, stdout, returncode, ok):
    from dports_dev_env import provision
    from dports_dev_env.config import load_config
    from dports_dev_env.errors import ProvisionError

    set_setting("dev_env.cache_root", str(tmp_path / "cache"))
    config = load_config()
    assert config.runtime_profile.python == "3.12"
    seen = []

    class Runner:
        def __init__(self, root):
            pass

        def run(self, argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(provision, "ChrootRunner", Runner)
    provisioner = provision.BaseProvisioner(config)
    if ok:
        provisioner.check_profile_python(tmp_path)
    else:
        with pytest.raises(ProvisionError, match=r"dportsv3-py312 is for Python 3\.12"):
            provisioner.check_profile_python(tmp_path)
    assert seen and seen[0][0] == "python3"
