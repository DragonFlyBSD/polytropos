from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .chroot import ChrootRunner
from .config import DevEnvConfig
from .errors import ProvisionError
from .fs import copy_tree
from .layout import TOOL_BIN, TOOL_RELATIVE, TOOL_VENV_RELATIVE
from .locks import CacheLock
from .log import info, step_timer, subphase, warn


# 2: the venv moved out of the ports checkout and its cache key now covers
# the dev-env project as well, so schema 1 entries key on different inputs and
# must not be reused.
GENERATOR_VENV_SCHEMA = 2

#: Both projects that end up in the generator venv. ``bin/dportsv3`` installs
#: dev-env into it first — the generator declares it as a dependency and it
#: resolves from this sibling source tree, not from PyPI — so a change to
#: either project's dependencies invalidates a cached venv. Keying on the
#: generator alone silently handed back a stale venv when dev-env's deps moved.
VENV_PYPROJECTS: tuple[str, ...] = (
    f"{TOOL_RELATIVE}/pyproject.toml",
    f"{TOOL_RELATIVE}/dev-env/pyproject.toml",
)


class GeneratorVenvCache:
    def __init__(self, config: DevEnvConfig) -> None:
        self.config = config

    def prepare(self, root_dir: Path, provisioned_base_id: str) -> None:
        runner = ChrootRunner(root_dir)
        python_version = self.chroot_output(runner, ["python3", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        pyproject_hash = self.pyproject_hash(root_dir)
        venv_id = self.venv_id(provisioned_base_id, python_version, pyproject_hash)
        cache_root = self.config.generator_venvs_dir / venv_id
        cache_venv = cache_root / "venv"
        venv_dest = root_dir / TOOL_VENV_RELATIVE

        with CacheLock(self.config.locks_dir, f"venv-generator-{venv_id}", timeout=1800):
            if (cache_root / "ready").exists():
                subphase("restoring cached generator venv")
                info(f"restoring cached generator venv {venv_id}")
                if venv_dest.exists():
                    shutil.rmtree(venv_dest)
                copy_tree(cache_venv, venv_dest)
                if self.validate(root_dir):
                    return
                warn("cached generator venv failed validation; rebuilding it")
                shutil.rmtree(venv_dest)
                shutil.rmtree(cache_root)

            subphase("building generator venv (first time)")
            with step_timer("bootstrap generator venv"):
                result = runner.run([TOOL_BIN, "compose", "--help"])
                if result.returncode != 0:
                    raise ProvisionError("failed to bootstrap dportsv3 generator venv inside chroot")
            subphase("caching generator venv for next time")
            tmp_cache = cache_root.with_suffix(".tmp")
            if tmp_cache.exists():
                shutil.rmtree(tmp_cache)
            tmp_cache.mkdir(parents=True)
            copy_tree(venv_dest, tmp_cache / "venv")
            metadata = {
                "schema": GENERATOR_VENV_SCHEMA,
                "provisioned_base_id": provisioned_base_id,
                "python": python_version,
                "pyproject_sha256": pyproject_hash,
            }
            (tmp_cache / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
            (tmp_cache / "ready").write_text("")
            if cache_root.exists():
                shutil.rmtree(cache_root)
            tmp_cache.replace(cache_root)

    def validate(self, root_dir: Path) -> bool:
        return ChrootRunner(root_dir).run([TOOL_BIN, "compose", "--help"]).returncode == 0

    def pyproject_hash(self, root_dir: Path) -> str:
        """One digest over every project that goes into the generator venv.

        Hashed in a fixed order with the relative path included, so moving a
        dependency between the two projects changes the key even when the
        combined bytes happen not to.
        """
        digest = hashlib.sha256()
        for relative in VENV_PYPROJECTS:
            pyproject = root_dir / relative
            if not pyproject.is_file():
                raise ProvisionError(f"missing project in env: {pyproject}")
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(pyproject.read_bytes())
        return digest.hexdigest()

    def venv_id(self, provisioned_base_id: str, python_version: str, pyproject_hash: str) -> str:
        data = {
            "schema": GENERATOR_VENV_SCHEMA,
            "provisioned_base_id": provisioned_base_id,
            "python": python_version,
            "pyproject_sha256": pyproject_hash,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]

    def chroot_output(self, runner: ChrootRunner, argv: list[str]) -> str:
        try:
            return runner.output(argv)
        except Exception as exc:
            raise ProvisionError(f"chroot command failed: {' '.join(argv)}") from exc
