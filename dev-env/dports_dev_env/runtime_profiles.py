from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError


# Package data, deliberately: the generator reads this same manifest (the
# agent's `python_runtime` health check compares it against what is actually
# installed in a chroot), and it can only reach it through the installed
# package. Keeping it inside `dports_dev_env/` is what makes that legal —
# a wheel carries it, and no caller has to guess a path.
MANIFEST_PATH = Path(__file__).resolve().parent / "runtime-profiles.toml"


@dataclass(frozen=True)
class RuntimeProfile:
    schema: int
    name: str
    python: str
    packages: list[str]


def load_runtime_profile(name: str | None = None) -> RuntimeProfile:
    try:
        data = tomllib.loads(MANIFEST_PATH.read_text())
    except OSError as exc:
        raise ConfigError(f"could not read runtime profile manifest: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid runtime profile manifest: {exc}") from exc

    schema = data.get("schema")
    if schema != 1:
        raise ConfigError(f"unsupported runtime profile manifest schema: {schema!r}")

    selected = name or os.environ.get("DPORTS_DEV_RUNTIME_PROFILE") or data.get("default")
    if not isinstance(selected, str) or not selected:
        raise ConfigError("runtime profile manifest lacks a default profile")

    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or selected not in profiles:
        raise ConfigError(f"unknown runtime profile: {selected}")

    profile = profiles[selected]
    if not isinstance(profile, dict):
        raise ConfigError(f"invalid runtime profile: {selected}")
    python = profile.get("python")
    packages = profile.get("packages")
    if not isinstance(python, str) or not python:
        raise ConfigError(f"runtime profile {selected} lacks a python version")
    if not isinstance(packages, list) or not all(isinstance(p, str) and p for p in packages):
        raise ConfigError(f"runtime profile {selected} has invalid packages")
    return RuntimeProfile(schema=schema, name=selected, python=python, packages=packages)
