"""dsynth's compiler cache is configurable (poly-dei7).

dsynth force-rebuilds every round, so with Directory_ccache disabled the
agent recompiles a whole port for a one-file edit — ~13 h on www/chromium.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dports_dev_env import config as dev_env_config
from dports_dev_env.dsynth import write_dsynth_config


@pytest.fixture
def cfg():
    return dev_env_config.load_config()


@pytest.fixture
def state(tmp_path):
    """Only the fields write_dsynth_config touches."""
    class _S:
        name = "2026Q3"
        target = "@2026Q3"
        root_dir = tmp_path / "root"
    return _S()


def _ini(cfg, state) -> str:
    write_dsynth_config(cfg, state)
    from dports_dev_env.dsynth import env_dsynth_etc_dir
    return (env_dsynth_etc_dir(state) / "dsynth.ini").read_text()


def test_ccache_disabled_by_default(cfg, state):
    """Off unless asked for: it needs ccache installed in the env first."""
    assert cfg.dsynth_ccache is False
    assert "Directory_ccache= disabled" in _ini(cfg, state)
    assert not (state.root_dir / "work/dsynth/ccache").exists()


def test_ccache_enabled_points_at_the_in_chroot_path(cfg, state):
    ini = _ini(replace(cfg, dsynth_ccache=True), state)
    assert "Directory_ccache= /work/dsynth/ccache" in ini
    assert "disabled" not in ini


def test_ccache_enabled_creates_the_directory(cfg, state):
    write_dsynth_config(replace(cfg, dsynth_ccache=True), state)
    assert (state.root_dir / "work/dsynth/ccache").is_dir()


def test_ccache_setting_is_declared_without_an_env_var(cfg):
    """CLAUDE.md: declare it, don't read the environment."""
    s = next(s for s in dev_env_config.SETTINGS
             if s.path == "dev_env.dsynth_ccache")
    assert s.kind == "bool"
    assert s.default is False
    assert not s.env


def test_ccache_binary_is_provisioned(cfg):
    """dsynth wires the directory; the jail still needs the binary."""
    assert "ccache" in cfg.tool_pkgs_optional
