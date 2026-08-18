from __future__ import annotations

from pathlib import Path

import pytest




def test_mount_env_root_rejects_long_mount_target(monkeypatch, tmp_path):
    from dports_dev_env import runtime
    from dports_dev_env.errors import ProvisionError

    def fail_mount(*_args, **_kwargs):
        raise AssertionError("mount should not be attempted")

    monkeypatch.setattr(runtime, "mount_null", fail_mount)
    long_root = tmp_path / ("x" * 90)

    with pytest.raises(ProvisionError, match="mount target too long"):
        runtime.mount_env_root(tmp_path / "base", tmp_path / "env", long_root)


def test_prepare_mountpoints_creates_short_repo_target_and_compat_symlink(monkeypatch, tmp_path):
    from dports_dev_env.config import load_config
    from dports_dev_env.provision import BaseProvisioner

    monkeypatch.setenv("DPORTS_DEV_CACHE_ROOT", str(tmp_path / "cache"))
    root = tmp_path / "root"

    BaseProvisioner(load_config()).prepare_mountpoints(root)

    assert (root / "work/repos").is_dir()
    repos_link = root / "root/.cache/dports-dev/repos"
    assert repos_link.is_symlink()
    assert repos_link.readlink() == Path("/work/repos")
