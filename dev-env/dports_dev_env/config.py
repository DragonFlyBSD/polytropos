from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .confschema import Schema, Setting
from .errors import ConfigError
from .runtime_profiles import RuntimeProfile, load_runtime_profile


@dataclass(frozen=True)
class DevEnvConfig:
    cache_root: Path
    bases_dir: Path
    archives_dir: Path
    provisioned_bases_dir: Path
    envs_dir: Path
    repos_dir: Path
    venvs_dir: Path
    generator_venvs_dir: Path
    locks_dir: Path
    avalon_releases_url: str
    freebsd_ports_url: str
    dports_url: str
    deltaports_branch: str
    dports_branch: str
    host_distdir: Path
    bootstrap_pkgs: list[str]
    tool_pkgs_required: list[str]
    tool_cmds_required: list[str]
    python_pkgs: list[str]
    tool_pkgs_optional: list[str]
    python_commands: list[str]
    runtime_profile: RuntimeProfile
    dsynth_builders: int
    dsynth_jobs: int


#: The dev-env's own settings, in the ``[dev_env]`` section of the same
#: file the generator reads. Same engine, separate table: this package
#: does not know what a bundle or a tracker is, and must not learn.
#:
#: None of these carries an ``env=``. Every one is a cache directory, an
#: upstream URL or a package list — all knowable when the file is
#: written, and all previously settable only by exporting a variable no
#: sample or script mentioned.
SETTINGS: list[Setting] = [
    Setting("dev_env.cache_root", "path", Path("/root/.cache/dports-dev"),
            "Everything below defaults to a position inside this."),
    Setting("dev_env.archives_dir", "path", None,
            "Downloaded base archives. Default: <cache_root>/bases/archives."),
    Setting("dev_env.provisioned_bases_dir", "path", None,
            "Provisioned bases. Default: <cache_root>/bases/p."),
    Setting("dev_env.envs_dir", "path", None,
            "The environments themselves. Default: <cache_root>/envs."),
    Setting("dev_env.repos_dir", "path", None,
            "Mirror clones. Default: <cache_root>/repos."),
    Setting("dev_env.generator_venvs_dir", "path", None,
            "Generator venvs. Default: <cache_root>/venvs/generator."),
    Setting("dev_env.locks_dir", "path", None,
            "Lock files. Default: <cache_root>/locks."),
    Setting("dev_env.host_distdir", "path", Path("/usr/distfiles"),
            "The host distfiles directory, bind-mounted into a chroot."),
    Setting("dev_env.avalon_releases_url", "str",
            "https://avalon.dragonflybsd.org/snapshots/x86_64/assets/releases/",
            "Where base archives are fetched from."),
    Setting("dev_env.freebsd_ports_url", "str",
            "https://git.FreeBSD.org/ports.git", "Upstream ports tree."),
    Setting("dev_env.dports_url", "str",
            "https://github.com/DragonFlyBSD/DPorts.git", "The DPorts tree."),
    Setting("dev_env.deltaports_branch", "str", "master",
            "Branch of DeltaPorts an environment tracks."),
    Setting("dev_env.dports_branch", "str", "staged",
            "Branch of DPorts an environment tracks."),
    Setting("dev_env.bootstrap_pkgs", "list", ["indexinfo"],
            "Installed before anything else can run."),
    Setting("dev_env.tool_pkgs_required", "list",
            ["bash", "curl", "git", "patch", "jq"],
            "Must be present; provisioning fails without them."),
    Setting("dev_env.tool_pkgs_optional", "list",
            ["dsynth", "python311", "python312", "python313",
             "py311-pip", "py312-pip", "py313-pip", "genpatch"],
            "Installed when available; absence is not fatal."),
    Setting("dev_env.tool_cmds_required", "list",
            ["pkg", "indexinfo", "bash", "curl", "git", "patch", "jq",
             "python3"],
            "Commands checked for after provisioning."),
    Setting("dev_env.python_pkgs", "list",
            ["python3", "python313", "python312", "python311"],
            "Python packages tried, in order."),
    Setting("dev_env.python_commands", "list",
            ["python3", "python3.13", "python3.12", "python3.11"],
            "Interpreter names tried, in order."),
    Setting("dev_env.dsynth_builders", "int", 2,
            "Parallel dsynth builders. Must be positive."),
    Setting("dev_env.dsynth_jobs", "int", 2,
            "Make jobs per builder. Must be positive."),
]

_schema: Schema | None = None
_schema_path: Path | None = None


def settings_path() -> Path | None:
    """The shared settings file, via ``$DPORTSV3_CONFIG_DIR``.

    The same variable the generator uses, because it is the same file.
    This package still owns only its own table, so nothing here has to
    know what else is in there.
    """
    raw = os.environ.get("DPORTSV3_CONFIG_DIR", "").strip()
    return Path(raw) / "polytropos.toml" if raw else None


def schema() -> Schema:
    global _schema, _schema_path
    current = settings_path()
    if _schema is None or _schema_path != current:
        _schema = Schema(SETTINGS, name="dports-dev-env")
        _schema.load(current)
        _schema_path = current
    return _schema


def reset_schema() -> None:
    """Drop the loaded schema. For tests."""
    global _schema, _schema_path
    _schema = None
    _schema_path = None


def load_config() -> DevEnvConfig:
    """Resolve the dev-env's configuration.

    Every value used to come from a ``DPORTS_DEV_*`` environment
    variable, and not one of the 22 was named in any sample or rc script
    — so on a packaged install there was nowhere to put a value at all.
    They are settings now.
    """
    s = schema()

    def value(name: str):
        return s.get(f"dev_env.{name}")

    cache_root = Path(value("cache_root"))
    bases_dir = cache_root / "bases"
    venvs_dir = cache_root / "venvs"

    def directory(name: str, fallback: Path) -> Path:
        # The derived paths default to positions under cache_root, so an
        # operator who moves the cache moves all of them and one who
        # names a directory explicitly keeps it.
        configured = value(name)
        return Path(configured) if configured is not None else fallback

    bootstrap = list(value("bootstrap_pkgs"))
    return DevEnvConfig(
        cache_root=cache_root,
        bases_dir=bases_dir,
        archives_dir=directory("archives_dir", bases_dir / "archives"),
        provisioned_bases_dir=directory("provisioned_bases_dir", bases_dir / "p"),
        envs_dir=directory("envs_dir", cache_root / "envs"),
        repos_dir=directory("repos_dir", cache_root / "repos"),
        venvs_dir=venvs_dir,
        generator_venvs_dir=directory("generator_venvs_dir",
                                      venvs_dir / "generator"),
        locks_dir=directory("locks_dir", cache_root / "locks"),
        avalon_releases_url=value("avalon_releases_url"),
        freebsd_ports_url=value("freebsd_ports_url"),
        dports_url=value("dports_url"),
        deltaports_branch=value("deltaports_branch"),
        dports_branch=value("dports_branch"),
        host_distdir=Path(value("host_distdir")),
        bootstrap_pkgs=bootstrap,
        tool_pkgs_required=without_words(
            list(value("tool_pkgs_required")), bootstrap,
        ),
        tool_cmds_required=list(value("tool_cmds_required")),
        python_pkgs=list(value("python_pkgs")),
        tool_pkgs_optional=list(value("tool_pkgs_optional")),
        python_commands=list(value("python_commands")),
        runtime_profile=load_runtime_profile(),
        dsynth_builders=_positive("dev_env.dsynth_builders",
                                  value("dsynth_builders")),
        dsynth_jobs=_positive("dev_env.dsynth_jobs", value("dsynth_jobs")),
    )


def _positive(path: str, value: int) -> int:
    if int(value) <= 0:
        raise ConfigError(f"{path} must be a positive integer, got {value!r}")
    return int(value)


def without_words(values: list[str], excluded: list[str]) -> list[str]:
    excluded_set = set(excluded)
    return [value for value in values if value not in excluded_set]


Config = DevEnvConfig


def validate_cache_root(cache_root: Path) -> None:
    if not cache_root.is_absolute():
        raise ConfigError(f"dev_env.cache_root must be absolute: {cache_root}")
    if str(cache_root) in {"/", "/root", "/home", "/usr", "/var", "/tmp"}:
        raise ConfigError(
            f"dev_env.cache_root is too broad for safe cleanup: {cache_root}")


def ensure_cache_dirs(config: DevEnvConfig) -> None:
    validate_cache_root(config.cache_root)
    for path in [
        config.cache_root,
        config.bases_dir,
        config.archives_dir,
        config.provisioned_bases_dir,
        config.envs_dir,
        config.repos_dir,
        config.venvs_dir,
        config.generator_venvs_dir,
        config.locks_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def require_root() -> None:
    if os.geteuid() != 0:
        raise ConfigError("dports dev-env must run as root")
