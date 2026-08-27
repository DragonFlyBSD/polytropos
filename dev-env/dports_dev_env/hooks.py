"""dsynth hook install/uninstall/status for a dev-env.

Shares the same path resolution (``env_dsynth_etc_dir`` in
``dsynth.py``) as ``write_dsynth_config`` so there's one source of
truth for "where dsynth configuration lives in an env." That path is
the mounted view (``state.root_dir/etc/dsynth``), which the bind-mount
on ``writable/etc_dsynth`` makes writable while the env is mounted.

Hooks-install therefore requires the env to be mounted, matching the
existing dsynth-config convention. The CLI handler errors with a
helpful message when the env isn't mounted.
"""

from __future__ import annotations

import shutil
import stat
from argparse import Namespace
from pathlib import Path

from .dsynth import env_dsynth_etc_dir
from .mounts import mounts_under
from .runtime import TOOL_VENV_TARGET
from .state import EnvironmentState

# Files we ship as executable hook scripts (chmod 0755 on install).
HOOK_SCRIPTS: tuple[str, ...] = (
    "hook_common.sh",
    "hook_pkg_failure",
    "hook_pkg_ignored",
    "hook_pkg_skipped",
    "hook_pkg_start",
    "hook_pkg_started",
    "hook_pkg_success",
    "hook_run_end",
    "hook_run_start",
)

# Example config — copied as ``dportsv3-hooks.conf`` only if no
# operator-edited config exists yet.
CONF_EXAMPLE = "dportsv3-hooks.conf.example"
CONF_TARGET = "dportsv3-hooks.conf"

#: In-chroot paths of the two tools the hooks call. Both live in the venv
#: that ``prepare_root_runtime`` bind-mounts at ``TOOL_VENV_TARGET``.
CHROOT_VENV = Path("/") / TOOL_VENV_TARGET
CHROOT_PYTHON = CHROOT_VENV / "bin" / "python"
CHROOT_DPORTSV3 = CHROOT_VENV / "bin" / "dportsv3"
CHROOT_STORE_CLIENT = CHROOT_VENV / "bin" / "artifact-store-client"


def env_hook_settings(state: EnvironmentState) -> dict[str, str]:
    """The conf values that only this env can know.

    Two of them are not defaults anyone could have guessed. The tool paths
    are inside the chroot, not on the host, and they only resolve because
    the env mounts the venv. And ``DPORTSV3_TRACKER_TARGET`` has to be
    written out rather than left to the documented ``@${PROFILE}`` default:
    an env's dsynth profile is named after the *env* (``2026Q3-editors_vim``
    for a ``@2026Q3`` env), so the default derives a target no farm build
    will ever produce, and every failure raised in here lands on an issue
    that can never match the same failure from the farm.
    """
    return {
        "DPORTSV3_TRACKER_TARGET": f"@{state.target.lstrip('@')}",
        "POLYTROPOS_PYTHON": str(CHROOT_PYTHON),
        "DPORTSV3_BIN": str(CHROOT_DPORTSV3),
        "ARTIFACT_STORE_CLIENT": str(CHROOT_STORE_CLIENT),
    }


def render_conf(example: str, settings: dict[str, str]) -> str:
    """Apply ``settings`` to the example conf, in place where possible.

    Rewrites an existing assignment (commented or not) so the surrounding
    explanation stays attached to the value it explains; appends the rest
    in one labelled block. Returns the whole file.
    """
    lines = example.splitlines()
    remaining = dict(settings)
    for i, line in enumerate(lines):
        stripped = line.lstrip("#").strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines += ["", "# ---- written by `dev-env hooks-install` ----"]
        lines += [f"{k}={v}" for k, v in remaining.items()]
    return "\n".join(lines) + "\n"


def repo_hook_source() -> Path:
    """Path to the dsynth hooks this package installs into a chroot.

    Package data, co-located: this package is what copies the hooks into an
    env, so it carries them. It used to walk three parents up to a sibling
    `scripts/dsynth-hooks/`, which only resolved while this lived inside the
    DeltaPorts checkout at one exact depth.
    """
    return Path(__file__).resolve().parent / "dsynth-hooks"


def install_hooks(
    target_dir: Path,
    source_dir: Path | None = None,
    *,
    force: bool = False,
    settings: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Copy hook scripts + (optionally) a default conf into ``target_dir``.

    Returns (written_files, skipped_notes). ``dportsv3-hooks.conf`` is
    written from the example only if it doesn't exist or ``force`` is
    set. Hook scripts are always replaced (they're code, not config).

    ``settings`` are folded into the conf as it is written. They are the
    values an operator cannot be expected to supply — see
    :func:`env_hook_settings` — so leaving them to a hand-edit is what
    made every in-env failure vanish.
    """
    src = source_dir or repo_hook_source()
    if not src.is_dir():
        raise FileNotFoundError(f"source dir not found: {src}")
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped: list[str] = []

    for name in HOOK_SCRIPTS:
        sfile = src / name
        if not sfile.is_file():
            raise FileNotFoundError(f"missing hook in source: {sfile}")
        dfile = target_dir / name
        shutil.copy2(sfile, dfile)
        dfile.chmod(
            dfile.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        written.append(name)

    src_conf = src / CONF_EXAMPLE
    dst_conf = target_dir / CONF_TARGET
    if src_conf.is_file():
        if dst_conf.exists() and not force:
            skipped.append(f"{CONF_TARGET} (exists; --force to overwrite)")
        else:
            conf = src_conf.read_text()
            if settings:
                conf = render_conf(conf, settings)
            dst_conf.write_text(conf)
            written.append(CONF_TARGET)

    return written, skipped


def uninstall_hooks(
    target_dir: Path, *, purge: bool = False
) -> list[str]:
    """Remove hook scripts (and config if ``purge``). Returns removed names."""
    if not target_dir.is_dir():
        return []
    removed: list[str] = []
    for name in HOOK_SCRIPTS:
        path = target_dir / name
        if path.exists():
            path.unlink()
            removed.append(name)
    conf = target_dir / CONF_TARGET
    if conf.exists() and purge:
        conf.unlink()
        removed.append(CONF_TARGET)
    return removed


def status_hooks(
    target_dir: Path, source_dir: Path | None = None
) -> dict[str, object]:
    """Return a dict describing what's installed vs. the source.

    Keys: present, missing, stale, conf_present.
    """
    if not target_dir.is_dir():
        return {
            "present": [],
            "missing": list(HOOK_SCRIPTS),
            "stale": [],
            "conf_present": False,
            "exists": False,
        }
    src = source_dir or repo_hook_source()
    src_mtimes: dict[str, float] = {}
    if src.is_dir():
        for name in HOOK_SCRIPTS:
            sfile = src / name
            if sfile.is_file():
                src_mtimes[name] = sfile.stat().st_mtime

    present: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    for name in HOOK_SCRIPTS:
        path = target_dir / name
        if not path.exists():
            missing.append(name)
            continue
        present.append(name)
        src_mtime = src_mtimes.get(name)
        if src_mtime is not None and path.stat().st_mtime < src_mtime:
            stale.append(name)

    return {
        "present": present,
        "missing": missing,
        "stale": stale,
        "conf_present": (target_dir / CONF_TARGET).exists(),
        "exists": True,
    }


# ---- CLI argparse handlers ----


def _require_mounted(state: EnvironmentState) -> Path:
    """Resolve the env's /etc/dsynth dir, refusing if env isn't mounted."""
    if not mounts_under(state.root_dir):
        raise RuntimeError(
            f"env '{state.name}' is not mounted. Run "
            f"`dportsv3 dev-env shell {state.name}` first to mount it, "
            f"then re-run."
        )
    return env_dsynth_etc_dir(state)


def cmd_hooks_install(args: Namespace, state: EnvironmentState) -> int:
    try:
        target = _require_mounted(state)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    source = Path(args.source) if getattr(args, "source", None) else None
    settings = env_hook_settings(state)
    try:
        written, skipped = install_hooks(
            target, source_dir=source, force=bool(args.force),
            settings=settings,
        )
    except FileNotFoundError as exc:
        print(str(exc))
        return 1

    print(f"Installed {len(written)} files into {target}:")
    for name in written:
        print(f"  - {name}")
    for note in skipped:
        print(f"  skipped: {note}")
    print()
    if CONF_TARGET in written:
        print(f"Wrote into {target}/{CONF_TARGET}:")
        for key, value in settings.items():
            print(f"  {key}={value}")
        print()
        print("Next steps:")
        print("  1. Check DPORTSV3_TRACKER_URL and ARTIFACT_STORE_URL point at")
        print("     the services on this host (the chroot shares its loopback).")
    else:
        print("Next steps:")
        print(f"  1. {target}/{CONF_TARGET} was left alone. Re-run with --force")
        print("     to rewrite it, or set these by hand:")
        for key, value in settings.items():
            print(f"       {key}={value}")
    print("  2. Hooks are live at /etc/dsynth inside the chroot.")
    print(f"  3. The tools they call come from {CHROOT_VENV}, which is a")
    print("     bind mount of the venv this command ran from. It exists only")
    print("     while the env is mounted.")
    return 0


def cmd_hooks_uninstall(args: Namespace, state: EnvironmentState) -> int:
    try:
        target = _require_mounted(state)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    if not target.is_dir():
        print(f"Nothing to remove: {target} does not exist")
        return 0
    removed = uninstall_hooks(target, purge=bool(args.purge))
    if not removed:
        print(f"No dportsv3-installed hooks found in {target}")
        return 0
    print(f"Removed {len(removed)} files from {target}:")
    for name in removed:
        print(f"  - {name}")
    if not args.purge and (target / CONF_TARGET).exists():
        print(f"Preserved {target}/{CONF_TARGET} (pass --purge to remove it too)")
    return 0


def cmd_hooks_status(args: Namespace, state: EnvironmentState) -> int:
    try:
        target = _require_mounted(state)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    source = Path(args.source) if getattr(args, "source", None) else None
    info = status_hooks(target, source_dir=source)

    if not info["exists"]:
        print(f"{target}: missing (hooks not installed)")
        return 1

    for name in info["present"]:
        marker = " (stale: source is newer)" if name in info["stale"] else ""
        print(f"  x  {name}{marker}")
    for name in info["missing"]:
        print(f"  missing: {name}")
    print(f"  {'✓' if info['conf_present'] else 'missing:'} {CONF_TARGET}")
    print()
    present = len(info["present"])
    missing = len(info["missing"])
    stale = len(info["stale"])
    print(
        f"{target}: {present} hook(s) installed, {missing} missing, {stale} stale"
    )
    return 0 if missing == 0 else 1
