"""``dev-env rebase``: move an env onto another DragonFly world (poly-xrok).

An env's world is fixed when it is created, and destroy + create, the only
other way to a newer one, throws away its packages, ccache and working
trees. Rebase swaps the read-only base under an env and leaves the writable
half alone: the checkouts, dsynth's packages, logs and ccache, /root, /tmp
and the hooks. Only the generator venv is replaced, because it links the
base's interpreter.

The env has to be unmounted in between, and an unmount that fails halfway
leaves a process inside it without /dev or /tmp. So everything that can
refuse runs before the unmount, and the env is marked ``rebasing`` before
the last check, which keeps every session out from then on
(session.refuse_in_flight).
"""

from __future__ import annotations

import http.client
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .base import (
    BaseArchive,
    ensure_base_archive,
    fetch_latest_world_asset,
    file_sha256,
    provisioned_base_id,
    published_world_sha256,
    validate_world_asset,
)
from .builder import now_utc
from .config import DevEnvConfig
from .errors import DevEnvError, MountError, ProvisionError, StateError, UsageError
from .layout import PORTS_LINK_IDLE_TARGET, PORTS_RELATIVE
from .locks import CacheLock
from .log import error, info, phase, to_user, warn
from .mounts import Mount, holders_under, is_relative_to, list_mounts, unmount_under
from .provision import BaseProvisioner
from .runtime import env_mount_targets
from .session import EnvironmentSession, refuse_in_flight
from .state import EnvironmentState, state_path
from .store import EnvironmentStore
from .venv import GeneratorVenvCache


#: dsynth's Directory_buildbase (dsynth.py). Its workers mount below it.
DSYNTH_BUILD_RELATIVE = "work/dsynth/build"

#: Holders listed in a refusal before the rest are counted.
MAX_LISTED_HOLDERS = 10

#: How long rebase waits after saving "rebasing" before its last busy check.
#: A session that passed the gate just before the save is still mounting,
#: with nothing fstat can see; by now it has either re-checked the status
#: and refused (EnvironmentSession.prepare) or entered the env, where fstat
#: finds it. Its mounting is a few dozen mount(8) runs, well under this.
SESSION_GRACE_SECONDS = 3


@dataclass(frozen=True)
class Target:
    asset: str
    sha256: str
    base_id: str
    archive_cached: bool


def rebase_env(
    config: DevEnvConfig,
    store: EnvironmentStore,
    name: str,
    *,
    asset: str | None = None,
    dry_run: bool = False,
) -> int:
    # Timeout 0: a second rebase of the same env refuses instead of queueing.
    with CacheLock(config.locks_dir, f"env-{name}", timeout=0):
        return _rebase(config, store, name, asset=asset, dry_run=dry_run)


def _rebase(
    config: DevEnvConfig,
    store: EnvironmentStore,
    name: str,
    *,
    asset: str | None,
    dry_run: bool,
) -> int:
    entry = store.load(name)
    if entry.backend != "chroot":
        raise UsageError(f"unsupported backend in environment: {entry.backend}")
    # Creating and destroying are refused; rebasing is a resume.
    refuse_in_flight(entry, rebasing=True)
    if entry.status == "failed":
        raise UsageError(
            f"environment {name} is marked failed: its create never finished, "
            "so a rebase cannot make it ready. Destroy it and create it again."
        )
    env_dir = store.env_dir(name)
    old_id = entry.provisioned_base_id
    current_asset = current_world(config, old_id)

    target_asset = validate_world_asset(asset) if asset else latest_world(config)
    if current_asset != "unknown" and target_asset < current_asset:
        warn(f"{target_asset} is older than the env's current world {current_asset}")
    target = resolve_target(config, target_asset)

    if target.base_id == old_id and entry.status == "ready":
        to_user(f"{name} is already on {target.asset} (base {old_id})")
        return 0
    if target.asset == current_asset and target.base_id != old_id:
        to_user(
            f"{name}: the world is unchanged, but the base inputs changed (packages, "
            f"runtime profile or helper scripts), so {target.asset} is provisioned again"
        )
    if entry.status == "rebasing":
        to_user(f"{name} is in status 'rebasing': resuming an interrupted rebase")

    findings = busy_findings(entry, env_dir)
    if dry_run:
        report_dry_run(config, entry, current_asset, target, findings)
        return 1 if findings else 0
    refuse_if_busy(name, findings)

    phase(f"[1/4] Preparing base for {target.asset}")
    try:
        archive = ensure_base_archive(config, target.asset)
    except (OSError, http.client.HTTPException) as exc:
        raise ProvisionError(f"cannot download {target.asset}: {exc}") from exc
    if archive.sha256 != target.sha256:
        # Only a download can differ: a cached archive gave target.sha256
        # itself. Left in place, a re-run would take its hash from it and pass.
        bad = archive.path.with_name(archive.path.name + ".bad")
        archive.path.replace(bad)
        raise StateError(
            f"downloaded {target.asset} has sha256 {archive.sha256}, but Avalon publishes "
            f"{target.sha256} for it; moved it to {bad}"
        )
    provisioned = BaseProvisioner(config).prepare(archive)
    if provisioned.id != target.base_id:
        raise StateError(f"provisioned base {provisioned.id}, expected {target.base_id}")

    phase(f"[2/4] Unmounting {name}")
    # Provisioning can take tens of minutes. Only a destroy can have touched
    # the env meanwhile, and saving now would bring its env.json back.
    if store.load(name) != entry:
        raise UsageError(f"environment {name} changed while its new base was prepared; re-run")
    rebasing = replace(entry, status="rebasing", updated_at=now_utc())
    save(store, rebasing)
    # Checked again after the save, not before it, and after a grace: no
    # session can pass the gate any more, and one that passed it just before
    # the save has refused or is inside the env by now.
    try:
        time.sleep(SESSION_GRACE_SECONDS)
        findings = busy_findings(entry, env_dir)
    except BaseException:
        save(store, entry)
        raise
    if findings:
        save(store, entry)
        refuse_if_busy(name, findings)
    unmount_under(env_dir)
    survivors = mounts_at_or_below(list_mounts(), env_dir)
    if survivors:
        save(store, entry)
        for mount in survivors:
            error(f"still mounted: {mount.target}")
        raise UsageError(f"could not unmount {name}; unmount the listed paths and re-run")
    save(store, replace(rebasing, provisioned_base_id=target.base_id, updated_at=now_utc()))

    try:
        phase(f"[3/4] Mounting {name} on base {target.base_id}")
        state = EnvironmentSession(config, store).prepare(name, rebasing=True)
        check_root_source(config, state, target.base_id)

        phase("[4/4] Preparing generator venv")
        GeneratorVenvCache(config).prepare(state.root_dir, target.base_id)
    except DevEnvError:
        error(f"{name} stays in status 'rebasing'; re-run `rebase {name}` to resume, or destroy it")
        raise
    save(store, replace(state, status="ready", updated_at=now_utc(), failure=None))

    if old_id == target.base_id:
        # A resumed run that had already recorded the new base: the world it
        # started from is not recorded anywhere.
        to_user(f"==> {name}: on {target.asset} (base {target.base_id})")
        return 0
    to_user(f"==> {name}: {current_asset} -> {target.asset}")
    to_user(f"    base {old_id} -> {target.base_id}")
    to_user("    packages built on the old world stay in writable/work/dsynth/packages; clear them after a big jump")
    if current_asset != "unknown":
        to_user(
            f"    to go back: rebase {name} --asset {current_asset} (reuses base {old_id} "
            "while the base inputs are unchanged and the archive is cached or on Avalon)"
        )
    return 0


def save(store: EnvironmentStore, state: EnvironmentState) -> None:
    """store.save, unless the env was destroyed meanwhile.

    destroy takes no lock, and store.save creates the env dir it writes
    into, so a plain save after a destroy would bring back an env.json with
    nothing under it.
    """
    if not state_path(store.env_dir(state.name)).is_file():
        raise StateError(f"environment {state.name} was destroyed during the rebase")
    store.save(state)


def current_world(config: DevEnvConfig, base_id: str) -> str:
    """The world asset base ``base_id`` was provisioned from, or "unknown"."""
    path = config.provisioned_bases_dir / base_id / "metadata.json"
    try:
        asset = json.loads(path.read_text())["archive"]["asset"]
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"
    return asset if isinstance(asset, str) and asset else "unknown"


def latest_world(config: DevEnvConfig) -> str:
    try:
        return fetch_latest_world_asset(config)
    except OSError as exc:
        raise ProvisionError(f"cannot list {config.avalon_releases_url}: {exc}") from exc


def resolve_target(config: DevEnvConfig, asset: str) -> Target:
    """The base id ``asset`` provisions to, without downloading it.

    The id hashes the archive's sha256, so it comes from the cached archive
    when there is one and from the checksum Avalon publishes otherwise.
    """
    path = config.archives_dir / asset
    if path.is_file():
        sha256, cached = file_sha256(path), True
    else:
        try:
            sha256 = published_world_sha256(config, asset)
        except FileNotFoundError:
            raise UsageError(
                f"{asset} is neither cached in {config.archives_dir} nor published at "
                f"{config.avalon_releases_url}"
            ) from None
        cached = False
    base_id = provisioned_base_id(config, BaseArchive(asset=asset, path=path, sha256=sha256))
    return Target(asset=asset, sha256=sha256, base_id=base_id, archive_cached=cached)


def mounts_at_or_below(mounts: list[Mount], root: Path) -> list[Mount]:
    root = root.resolve(strict=False)
    return [mount for mount in mounts if mount.target == root or is_relative_to(mount.target, root)]


def busy_findings(state: EnvironmentState, env_dir: Path) -> list[str]:
    """Why the env cannot be unmounted now; empty when nothing holds it."""
    findings: list[str] = []
    root = state.root_dir.resolve(strict=False)

    # A job points the ports tree at its worktree for its whole life, and
    # between commands no process of it need be inside the env.
    link = state.writable_dir / PORTS_RELATIVE
    if link.is_symlink():
        link_target = os.readlink(link)
        if link_target != PORTS_LINK_IDLE_TARGET:
            findings.append(
                f"/{PORTS_RELATIVE} points at {link_target}, not {PORTS_LINK_IDLE_TARGET}: "
                "a job holds the env, or a dead one left its link"
            )
    elif not link.exists():
        findings.append(f"/{PORTS_RELATIVE} is missing: a job may be swapping it")

    build = root / DSYNTH_BUILD_RELATIVE
    own = env_mount_targets(root)
    workers = 0
    for mount in mounts_at_or_below(list_mounts(), env_dir):
        if is_relative_to(mount.target, build) and mount.target != build:
            workers += 1
        elif mount.target not in own:
            findings.append(
                f"{mount.target} (from {mount.source}) is not a dev-env mount: rebase would "
                "unmount it and not restore it. Unmount it, rebase, and mount it again."
            )
    if workers:
        findings.append(
            f"{workers} dsynth worker mount(s) under /{DSYNTH_BUILD_RELATIVE}: "
            "dsynth is running, or a crashed run left them"
        )

    try:
        holders = holders_under(root)
    except MountError as exc:
        findings.append(f"{exc}; refusing rather than assuming the env is free")
    else:
        by_pid: dict[str, str] = {}
        for holder in holders:
            by_pid.setdefault(holder.pid, f"{holder.cmd} (pid {holder.pid}) {holder.fd} {holder.path}")
        for line in list(by_pid.values())[:MAX_LISTED_HOLDERS]:
            findings.append(f"process in the env: {line}")
        if len(by_pid) > MAX_LISTED_HOLDERS:
            findings.append(f"... and {len(by_pid) - MAX_LISTED_HOLDERS} more process(es) in the env")
    return findings


def refuse_if_busy(name: str, findings: list[str]) -> None:
    if not findings:
        return
    for finding in findings:
        error(finding)
    raise UsageError(f"{name} is busy; nothing was changed")


def check_root_source(config: DevEnvConfig, state: EnvironmentState, base_id: str) -> None:
    """The env's root must now be the new base, not one a session put back.

    Compared by device and inode, not by mount(8)'s source column: nullfs
    keeps 79 bytes of that (f_mntfromname), and a long cache_root cuts it
    before the base id. Through a DragonFly nullfs mount, stat returns the
    lower directory's st_dev and st_ino (measured on acemagic: an env root
    and its base root report the same pair).
    """
    root = state.root_dir.resolve(strict=False)
    expected = config.provisioned_bases_dir / base_id / "root"
    if not any(mount.target == root for mount in list_mounts()):
        raise StateError(f"nothing is mounted at {root}")
    if not same_directory(root, expected):
        raise StateError(f"{root} is not mounted from {expected}")
    info(f"{root} is mounted from {expected}")


def same_directory(a: Path, b: Path) -> bool:
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def report_dry_run(
    config: DevEnvConfig,
    entry: EnvironmentState,
    current_asset: str,
    target: Target,
    findings: list[str],
) -> None:
    base_ready = (config.provisioned_bases_dir / target.base_id / "ready").exists()
    to_user(f"{entry.name} ({entry.status}), dry run: nothing is changed")
    to_user(f"  current  {current_asset} (base {entry.provisioned_base_id or 'none'})")
    to_user(f"  target   {target.asset} (base {target.base_id})")
    to_user(f"  archive  {'cached' if target.archive_cached else 'to download'}")
    to_user(f"  base     {'ready' if base_ready else 'to provision'}")
    if not findings:
        to_user("  busy     no")
        return
    to_user("  busy     yes; rebase would refuse:")
    for finding in findings:
        to_user(f"    - {finding}")
