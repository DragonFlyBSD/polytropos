from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from .base import ensure_base_archive, fetch_latest_world_asset
from .chroot import ChrootRunner
from .config import DevEnvConfig, ensure_cache_dirs
from .dsynth import write_dsynth_config
from .errors import CommandError, DevEnvError, UsageError
from .helpers import write_shell_rc
from .layout import (
    FREEBSD_DIR,
    FREEBSD_RELATIVE,
    LOCK_DIR,
    LOCK_RELATIVE,
    PORTS_DIR,
    PORTS_RELATIVE,
    TOOL_BIN,
    TOOL_RELATIVE,
    TOOL_VENV_RELATIVE,
)
from .locks import CacheLock
from .log import info, phase, step_timer, warn
from .names import default_env_name, target_to_branch
from .provision import BaseProvisioner
from .repos import RepoCache
from .runtime import mount_env_root, prepare_root_runtime
from .state import EnvironmentState, FailureState, InitialComposeState, RepoState, RuntimeState, SourceState
from .store import EnvironmentStore
from .venv import GeneratorVenvCache


@dataclass(frozen=True)
class CreateOptions:
    name: str | None
    target: str
    origin: str | None
    delta_root: Path
    tool_root: Path
    backend: str
    freebsd_branch: str | None
    dports_branch: str
    allow_dirty: bool
    no_initial_compose: bool
    oracle_profile: str


@dataclass(frozen=True)
class CreateResult:
    env_name: str
    exit_code: int


class EnvironmentBuilder:
    def __init__(self, config: DevEnvConfig, store: EnvironmentStore, options: CreateOptions) -> None:
        self.config = config
        self.store = store
        self.options = options
        self.env_name = options.name or default_env_name(options.target, options.origin)
        self.env_dir = store.env_dir(self.env_name)
        self.root_dir = store.root_dir(self.env_name)
        self.writable_dir = store.writable_dir(self.env_name)
        self.exit_code = 0

    def create(self) -> CreateResult:
        self.validate()
        ensure_cache_dirs(self.config)
        with CacheLock(self.config.locks_dir, f"env-{self.env_name}"):
            if self.env_dir.exists():
                raise UsageError(f"environment already exists: {self.env_name}")
            self.env_dir.mkdir(parents=True)
            state = self.initial_state(provisioned_base_id="")
            self.store.save(state)
            try:
                with step_timer(f"create environment {self.env_name}"):
                    state = self.build(state)
                self.store.save(replace(state, status="ready", updated_at=now_utc(), failure=None))
                info(f"environment ready: {self.env_name}")
                return CreateResult(self.env_name, self.exit_code)
            except DevEnvError as exc:
                warn(f"create failed; environment retained for manual investigation: {exc}")
                failed = replace(state, status="failed", updated_at=now_utc(), failure=FailureState(str(exc)))
                self.store.save(failed)
                return CreateResult(self.env_name, 1)
            except (Exception, KeyboardInterrupt) as exc:
                # Unexpected failure (incl. ^C) -- record it before bubbling up
                # so a re-run of `list` shows the env as failed instead of stuck
                # in `creating`. We re-raise so cli.main reports the real cause.
                warn(f"create interrupted; environment retained for manual investigation: {exc!r}")
                failed = replace(state, status="failed", updated_at=now_utc(), failure=FailureState(repr(exc)))
                self.store.save(failed)
                raise

    def build(self, state: EnvironmentState) -> EnvironmentState:
        phase("[1/7] Resolving latest DragonFly world asset")
        with step_timer("resolve latest DragonFly world asset"):
            asset = fetch_latest_world_asset(self.config)

        phase("[2/7] Preparing provisioned DragonFly base")
        with step_timer("prepare provisioned DragonFly base"):
            archive = ensure_base_archive(self.config, asset)
            provisioned_base = BaseProvisioner(self.config).prepare(archive)
            state = replace(state, provisioned_base_id=provisioned_base.id, updated_at=now_utc())
            self.store.save(state)

        phase("[3/7] Refreshing cached repo mirrors")
        with step_timer("refresh cached repo mirrors"):
            mirrors = RepoCache(self.config).refresh_all(self.options.delta_root, self.options.tool_root)

        phase("[4/7] Mounting throwaway chroot root from provisioned base")
        with step_timer("create throwaway chroot root"):
            mount_env_root(provisioned_base.root, self.env_dir, self.root_dir)
            prepare_root_runtime(self.config, self.root_dir)

        phase("[5/7] Seeding env-local source trees and writing runtime config")
        with step_timer("seed env-local source trees and runtime config"):
            repos = RepoCache(self.config)
            repos.clone_branch("DeltaPorts", mirrors.deltaports, state.repos.deltaports_branch, self.root_dir / PORTS_RELATIVE)
            repos.clone_branch("polytropos", mirrors.tool, state.repos.tool_branch, self.root_dir / TOOL_RELATIVE)
            # A mirror clone can carry a .venv if one was committed by accident;
            # the venv cache below owns that directory, so start from nothing.
            generator_venv = self.root_dir / TOOL_VENV_RELATIVE
            if generator_venv.exists():
                shutil.rmtree(generator_venv)
            repos.clone_branch("freebsd-ports", mirrors.freebsd_ports, state.repos.freebsd_branch, self.root_dir / FREEBSD_RELATIVE)
            repos.export_branch("DPorts", mirrors.dports, state.repos.dports_branch, self.root_dir / LOCK_RELATIVE)
            (self.root_dir / "work/artifacts/compose").mkdir(parents=True, exist_ok=True)
            write_dsynth_config(self.config, state)
            write_shell_rc(state)

        phase("[6/7] Preparing generator venv")
        with step_timer("prepare generator venv"):
            GeneratorVenvCache(self.config).prepare(self.root_dir, provisioned_base.id)

        state = replace(state, status="ready", updated_at=now_utc(), failure=None)
        self.store.save(state)

        if self.options.no_initial_compose:
            phase("[7/7] Skipping initial compose (--no-initial-compose); run 'regen' inside the shell when ready")
            state = replace(state, initial_compose=InitialComposeState("skipped", now_utc(), "--no-initial-compose"), updated_at=now_utc())
            self.store.save(state)
            return state

        phase("[7/7] Running initial compose")
        state = replace(state, initial_compose=InitialComposeState("running", now_utc()), updated_at=now_utc())
        self.store.save(state)
        with step_timer("initial compose"):
            try:
                self.compose_inside_env(state)
            except CommandError as exc:
                self.exit_code = 1
                state = replace(state, initial_compose=InitialComposeState("failed", now_utc(), str(exc)), updated_at=now_utc())
                self.store.save(state)
                warn(f"initial compose failed; environment remains ready for inspection: {exc}")
                return state
        state = replace(state, initial_compose=InitialComposeState("ok", now_utc()), updated_at=now_utc())
        self.store.save(state)
        return state

    def validate(self) -> None:
        if self.options.backend != "chroot":
            raise UsageError(f"unsupported backend: {self.options.backend}")
        if self.options.oracle_profile not in {"off", "local", "ci"}:
            raise UsageError("--oracle-profile must be one of: off, local, ci")
        self.validate_source_repo(
            "DeltaPorts ports tree",
            self.options.delta_root,
            "--delta-root",
            # Same marker `dportsv3 compose` itself checks for, so a root that
            # passes here cannot fail later for looking like the wrong tree.
            # Either directory counts: composing only special/ is supported.
            lambda root: (root / "ports").is_dir() or (root / "special").is_dir(),
            "it has neither a ports/ nor a special/ directory",
        )
        self.validate_source_repo(
            "polytropos tool checkout",
            self.options.tool_root,
            "--tool-root",
            # bin/dportsv3, not ./dportsv3 — at this repo's root that name is
            # the Python package directory, so a plain is_file() on it fails
            # and an exists() on it would pass for the wrong reason.
            lambda root: (root / "bin" / "dportsv3").is_file() and (root / "pyproject.toml").is_file(),
            "it has no bin/dportsv3 wrapper and pyproject.toml at its root",
        )
        for command in ["tar", "git", "chroot", "mount_null", "mount_procfs"]:
            if shutil.which(command) is None:
                raise UsageError(f"required command not found: {command}")

    def validate_source_repo(
        self,
        label: str,
        root: Path,
        flag: str,
        looks_right: Callable[[Path], bool],
        complaint: str,
    ) -> None:
        """Check one host checkout is the tree it is supposed to be, and clean.

        Both source trees get the same treatment because since the split both
        of them decide what an env contains: the ports tree supplies the
        overlay, the tool checkout supplies the code that composes it. The
        marker test used to be "does --delta-root contain a dportsv3 wrapper",
        which passed only while the tool lived inside the ports checkout and
        rejected every real ports tree once it moved out.
        """
        if not root.is_dir() or not looks_right(root):
            raise UsageError(f"{flag}={root} does not look like the {label}: {complaint}")
        self.run_git(["git", "-C", str(root), "rev-parse", "--git-dir"])
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], text=True, capture_output=True)
        if dirty.stdout.strip():
            warn(f"host {label} has uncommitted changes; only committed state will appear in the env")
            if not self.options.allow_dirty:
                raise UsageError(
                    f"refusing to create env from a dirty {label} at {root} "
                    f"(pass --allow-dirty to proceed)"
                )

    def initial_state(self, provisioned_base_id: str) -> EnvironmentState:
        created_at = now_utc()
        freebsd_branch = self.options.freebsd_branch or target_to_branch(self.options.target)
        return EnvironmentState(
            schema=1,
            name=self.env_name,
            backend="chroot",
            target=self.options.target,
            origin=self.options.origin or "",
            status="creating",
            created_at=created_at,
            updated_at=created_at,
            root_dir=self.root_dir,
            writable_dir=self.writable_dir,
            provisioned_base_id=provisioned_base_id,
            repos=RepoState(
                deltaports_branch=self.config.deltaports_branch,
                freebsd_branch=freebsd_branch,
                dports_branch=self.options.dports_branch,
                tool_branch=self.current_branch(self.options.tool_root),
            ),
            source=SourceState(
                delta_root=str(self.options.delta_root),
                tool_root=str(self.options.tool_root),
            ),
            runtime=RuntimeState(host_distdir=str(self.config.host_distdir), oracle_profile=self.options.oracle_profile),
            initial_compose=InitialComposeState("not-run", created_at),
        )

    def compose_inside_env(self, state: EnvironmentState) -> None:
        result = ChrootRunner(self.root_dir).run(
            [
                TOOL_BIN,
                "compose",
                "--target",
                state.target,
                "--delta-root",
                PORTS_DIR,
                "--freebsd-root",
                FREEBSD_DIR,
                "--lock-root",
                LOCK_DIR,
                "--output",
                f"/work/artifacts/compose/{state.target}",
                "--replace-output",
                "--oracle-profile",
                state.oracle_profile,
            ]
        )
        if result.returncode != 0:
            raise CommandError("initial compose failed")

    def current_branch(self, root: Path) -> str:
        """The branch an env should track for a host checkout.

        The ports and FreeBSD branches are configured, because those trees are
        shared and their branch is part of what a target means. The tool branch
        is not: you build an env to exercise the tool work in front of you, so
        it follows whatever the host checkout has checked out.
        """
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            capture_output=True,
        )
        branch = result.stdout.strip()
        if result.returncode != 0 or not branch or branch == "HEAD":
            raise UsageError(
                f"cannot determine the checked-out branch of {root}"
                + (" (detached HEAD)" if branch == "HEAD" else "")
            )
        return branch

    def run_git(self, command: list[str]) -> None:
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode != 0:
            raise UsageError(f"command failed: {' '.join(command)}")


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_delta_root() -> Path:
    """The DeltaPorts ports checkout, when no ``--delta-root`` was given.

    This is an *external* input: the ports tree is a separate repository from
    this tool, so there is nothing sensible to infer and it has to be named.
    It comes from ``$DPORTS_DEV_DELTA_ROOT``, or this raises.

    It used to be ``parents[4]`` — the tool's own repository root, which was
    the ports checkout only because the tool lived inside it. After the split
    that resolves to the tool's repo (or, from an installed copy, somewhere
    under site-packages), so an env would have been built against the wrong
    tree, or an empty one, without a word of complaint.
    """
    raw = os.environ.get("DPORTS_DEV_DELTA_ROOT", "").strip()
    if not raw:
        raise UsageError(
            "no DeltaPorts checkout specified: pass --delta-root, or set "
            "$DPORTS_DEV_DELTA_ROOT. The ports tree is a separate repository "
            "from this tool, so it cannot be inferred."
        )
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise UsageError(
            f"$DPORTS_DEV_DELTA_ROOT points at {root}, which is not a directory"
        )
    return root


def default_tool_root() -> Path:
    """This tool's own checkout, when no ``--tool-root`` was given.

    Comes from ``$DPORTS_DEV_TOOL_ROOT``, which ``bin/dportsv3`` exports for
    the checkout it lives in. The wrapper is the one component entitled to
    know the repository layout, so routing it through the environment is what
    keeps a ``parents[N]`` walk out of this package while still letting a
    plain ``dportsv3 dev-env create`` work with no arguments.

    An installed copy has no repository above it to find, so an operator
    running the console script directly has to name the checkout — hence a
    raise rather than a guess.
    """
    raw = os.environ.get("DPORTS_DEV_TOOL_ROOT", "").strip()
    if not raw:
        raise UsageError(
            "no polytropos checkout specified: pass --tool-root, or set "
            "$DPORTS_DEV_TOOL_ROOT. Invoking via bin/dportsv3 sets it for you."
        )
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise UsageError(
            f"$DPORTS_DEV_TOOL_ROOT points at {root}, which is not a directory"
        )
    return root
