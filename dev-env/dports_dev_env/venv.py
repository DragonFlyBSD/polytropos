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
# 3: keyed on what the venv is built from (its interpreter, the system
# packages it sees, the wrapper's install inputs) instead of the provisioned
# base id, which also moves with the world archive and the helper scripts
# (poly-gfe7).
GENERATOR_VENV_SCHEMA = 3

#: Both projects that end up in the generator venv. ``bin/dportsv3`` installs
#: dev-env into it first — the generator declares it as a dependency and it
#: resolves from this sibling source tree, not from PyPI — so a change to
#: either project's dependencies invalidates a cached venv. Keying on the
#: generator alone silently handed back a stale venv when dev-env's deps moved.
VENV_PYPROJECTS: tuple[str, ...] = (
    f"{TOOL_RELATIVE}/pyproject.toml",
    f"{TOOL_RELATIVE}/dev-env/pyproject.toml",
)

#: The dev-env tree inside the tool checkout.
DEV_ENV_RELATIVE = f"{TOOL_RELATIVE}/dev-env"

#: What the venv is made of besides the two checkouts, measured with the
#: chroot's ``python3`` — the interpreter ``bin/dportsv3`` creates the venv
#: with. The venv's bin/python is a symlink to it, and
#: ``--system-site-packages`` puts every distribution it can see on the
#: venv's path, which is also what pip resolved the venv against. Run
#: without -I on purpose: such a venv inherits the user site directory too.
INTERPRETER_PROBE = """\
import importlib.metadata, json, re, sys
packages = set()
for dist in importlib.metadata.distributions():
    name, version = dist.metadata["Name"], dist.version
    if name and version:
        packages.add(re.sub(r"[-_.]+", "-", name).lower() + "==" + version)
print(json.dumps({
    "version": "%d.%d.%d" % sys.version_info[:3],
    "executable": sys.executable,
    "packages": sorted(packages),
}))
"""


def dev_env_digest(dev_env_dir: Path) -> str:
    """The dev-env half of ``bin/dportsv3``'s install stamp.

    The same files hashed the same way as the wrapper's
    ``compute_dev_env_stamp``: on a restore, the wrapper reinstalls unless
    the venv's stamp matches, and the stamp covers every dev-env source
    file, not only its pyproject. A key that left them out handed back a
    venv the wrapper then reinstalled, uncached, after every dev-env edit.
    A test runs the wrapper's program to keep the two in step.
    """
    digest = hashlib.sha256()
    for path in [dev_env_dir / "pyproject.toml", *sorted((dev_env_dir / "dports_dev_env").rglob("*.py"))]:
        digest.update(str(path.relative_to(dev_env_dir)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class GeneratorVenvCache:
    def __init__(self, config: DevEnvConfig) -> None:
        self.config = config

    def prepare(self, root_dir: Path, provisioned_base_id: str) -> None:
        """Put a working generator venv into the env at ``root_dir``.

        ``provisioned_base_id`` is recorded in the entry's metadata, not
        keyed on: one entry serves every base with the same interpreter and
        system packages.
        """
        runner = ChrootRunner(root_dir)
        interpreter = self.probe_interpreter(runner)
        pyproject_hash = self.pyproject_hash(root_dir)
        dev_env_hash = dev_env_digest(root_dir / DEV_ENV_RELATIVE)
        venv_id = self.venv_id(interpreter, pyproject_hash, dev_env_hash)
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
                    self.record_base(cache_root, provisioned_base_id)
                    return
                warn("cached generator venv failed validation; rebuilding it")
                shutil.rmtree(venv_dest)
                shutil.rmtree(cache_root)

            # Start from nothing. bin/dportsv3 reuses a venv whose stamp
            # matches, and the stamp does not cover the interpreter, so a
            # venv left by another base would be cached under this key.
            if venv_dest.exists():
                shutil.rmtree(venv_dest)
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
                "python": interpreter["version"],
                "executable": interpreter["executable"],
                "system_packages": interpreter["packages"],
                "pyproject_sha256": pyproject_hash,
                "dev_env_sha256": dev_env_hash,
                # Every base this entry has been built on or restored into,
                # so a prune can tell which entries a kept base still uses.
                "bases": [provisioned_base_id],
            }
            (tmp_cache / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
            (tmp_cache / "ready").write_text("")
            if cache_root.exists():
                shutil.rmtree(cache_root)
            tmp_cache.replace(cache_root)

    def record_base(self, cache_root: Path, provisioned_base_id: str) -> None:
        """Add a base to the entry's ``bases``. Bookkeeping only: a failure
        to read or write the metadata is reported, and the restore stands."""
        path = cache_root / "metadata.json"
        try:
            metadata = json.loads(path.read_text())
            bases = set(metadata.get("bases", []))
            if provisioned_base_id in bases:
                return
            metadata["bases"] = sorted(bases | {provisioned_base_id})
            tmp_path = path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
            tmp_path.replace(path)
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            warn(f"could not record base {provisioned_base_id} in {path}: {exc}")

    def validate(self, root_dir: Path) -> bool:
        return ChrootRunner(root_dir).run([TOOL_BIN, "compose", "--help"]).returncode == 0

    def probe_interpreter(self, runner: ChrootRunner) -> dict:
        output = self.chroot_output(runner, ["python3", "-c", INTERPRETER_PROBE])
        try:
            probed = json.loads(output)
        except ValueError as exc:
            raise ProvisionError(f"unreadable interpreter probe output: {output[:200]!r}") from exc
        if (
            not isinstance(probed, dict)
            or not isinstance(probed.get("version"), str)
            or not isinstance(probed.get("executable"), str)
            or not isinstance(probed.get("packages"), list)
        ):
            raise ProvisionError(f"unexpected interpreter probe output: {output[:200]!r}")
        return probed

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

    def venv_id(self, interpreter: dict, pyproject_hash: str, dev_env_hash: str) -> str:
        data = {
            "schema": GENERATOR_VENV_SCHEMA,
            "python": interpreter["version"],
            "executable": interpreter["executable"],
            "system_packages": interpreter["packages"],
            "pyproject_sha256": pyproject_hash,
            "dev_env_sha256": dev_env_hash,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]

    def chroot_output(self, runner: ChrootRunner, argv: list[str]) -> str:
        try:
            return runner.output(argv)
        except Exception as exc:
            raise ProvisionError(f"chroot command failed: {' '.join(argv)}") from exc
