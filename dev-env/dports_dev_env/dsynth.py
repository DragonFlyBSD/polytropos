from __future__ import annotations

from pathlib import Path

from .config import DevEnvConfig
from .names import sanitize_name
from .state import EnvironmentState

#: Where dsynth keeps the packages it builds, inside the chroot: the
#: ``Directory_repository`` written below.
DSYNTH_REPO_DIR = "/work/dsynth/packages/All"

#: Removes a port's packages from the repository. ``$1`` is the repository,
#: the rest are origins; each file removed is printed as
#: ``DROPPED=<origin> <file>``.
#:
#: A build that proves a candidate fix, the agent's or verify's, runs in the
#: env the operator builds in and leaves its package there. Once the tree is
#: reset that package no longer matches it, but its version is the one dsynth
#: compares, so the next ``dsynth build`` of the port builds nothing and the
#: failure the fix was for goes unseen (poly-jao2).
#:
#: Run it only for origins such a build built: anything else in the
#: repository was built from the baseline, and dropping it costs a rebuild
#: for nothing. And run it before the baseline compose. The name comes from
#: ``make -V PKGNAME`` in ``$DPORTS_COMPOSE_ROOT/<origin>``, which until then
#: still holds the tree that was built, so a fix that bumped PORTREVISION is
#: found under its own name. Every flavor is tried, and the default. So is
#: every suffix pkg has used: on a builder the files end in ``.pkg`` although
#: ``Package_suffix`` below says ``.txz``. It exits 0 unless the shell itself
#: fails.
#:
#: The repository catalogue lists a dropped package until dsynth's next run,
#: which regenerates it at the end.
DROP_PACKAGES_SH = """\
repo=$1
shift
for o in "$@"; do
    cd "$DPORTS_COMPOSE_ROOT/$o" 2>/dev/null || continue
    for f in "" $(make PORTSDIR="$DPORTS_COMPOSE_ROOT" -V FLAVORS); do
        name=$(make PORTSDIR="$DPORTS_COMPOSE_ROOT" ${f:+FLAVOR=$f} -V PKGNAME) \\
            || continue
        [ -n "$name" ] || continue
        for p in "$name.pkg" "$name.txz" "$name.tzst" "$name.tbz" "$name.tgz"; do
            [ -e "$repo/$p" ] || continue
            rm -f "$repo/$p" && echo "DROPPED=$o $p"
        done
    done
done
exit 0
"""


def dropped_packages(stdout: str | None) -> list[str]:
    """The files ``DROP_PACKAGES_SH`` reported removing."""
    return [line.split(" ", 1)[-1]
            for line in (stdout or "").splitlines()
            if line.startswith("DROPPED=")]


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
Directory_repository= {DSYNTH_REPO_DIR}
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
