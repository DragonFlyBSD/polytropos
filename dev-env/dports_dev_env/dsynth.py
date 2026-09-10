from __future__ import annotations

from pathlib import Path

from .config import DevEnvConfig
from .names import sanitize_name
from .state import EnvironmentState


def dsynth_profile_name(state: EnvironmentState) -> str:
    return sanitize_name(state.name)


def env_dsynth_etc_dir(state: EnvironmentState) -> Path:
    """Per-env /etc/dsynth path (mounted view).

    Single source of truth for "where dsynth configuration lives in an
    env." Used by ``write_dsynth_config`` and by hook install/status.
    Requires the env to be mounted — when unmounted, this path either
    doesn't exist or points at the read-only base layer.
    """
    return state.root_dir / "etc/dsynth"


def write_dsynth_config(config: DevEnvConfig, state: EnvironmentState) -> None:
    config_dir = env_dsynth_etc_dir(state)
    dsynth_root = state.root_dir / "work/dsynth"
    profile_name = dsynth_profile_name(state)
    dirs = [
        config_dir,
        dsynth_root / "packages/All",
        dsynth_root / "options",
        dsynth_root / "build",
        dsynth_root / "logs",
        state.root_dir / f"work/artifacts/compose/{state.target}",
        state.root_dir / "usr/distfiles",
    ]
    if config.dsynth_ccache:
        dirs.append(dsynth_root / "ccache")
    for path in dirs:
        path.mkdir(parents=True, exist_ok=True)

    # dsynth force-rebuilds every round, so without a compiler cache the
    # agent recompiles the whole port for a one-file edit. The path is the
    # in-chroot one, like every other Directory_ entry (poly-dei7).
    ccache_dir = "/work/dsynth/ccache" if config.dsynth_ccache else "disabled"

    (config_dir / "dsynth.ini").write_text(
        f"""[Global Configuration]
profile_selected= {profile_name}

[{profile_name}]
Operating_system= DragonFly
Directory_packages= /work/dsynth/packages
Directory_repository= /work/dsynth/packages/All
Directory_portsdir= /work/artifacts/compose/{state.target}
Directory_options= /work/dsynth/options
Directory_distfiles= /usr/distfiles
Directory_buildbase= /work/dsynth/build
Directory_logs= /work/dsynth/logs
Directory_ccache= {ccache_dir}
Directory_system= /
Package_suffix= .txz
Number_of_builders= {config.dsynth_builders}
Max_jobs_per_builder= {config.dsynth_jobs}
Display_with_ncurses= true
"""
    )
    (config_dir / f"{profile_name}-make.conf").write_text("DISTDIR=/usr/distfiles\nWRKDIRPREFIX=/construction\n")
