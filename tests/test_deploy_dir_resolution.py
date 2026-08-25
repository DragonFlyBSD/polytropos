"""Where the rc.d scripts come from, in both worlds.

poly-abr.10. A wheel carries them inside the package; a checkout has only
the repo-root deploy/. Both have to resolve, and a packaged host must not
depend on an environment variable to find its own data files.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from dportsv3 import paths

REPO = Path(__file__).resolve().parents[1]


def _fake_deploy(root: Path) -> Path:
    d = root / "deploy"
    (d / "rc.d").mkdir(parents=True)
    (d / "rc.d" / "polytropos_runner").write_text("#!/bin/sh\n")
    return d


# --- precedence ---------------------------------------------------------

def test_explicit_tool_root_wins(tmp_path, monkeypatch) -> None:
    d = _fake_deploy(tmp_path)
    bundled = tmp_path / "bundled"
    (bundled / "rc.d").mkdir(parents=True)
    monkeypatch.setattr(paths, "BUNDLED_DEPLOY_DIR", bundled)
    assert paths.deploy_dir(tmp_path) == d


def test_bundled_copy_is_preferred_over_the_environment(tmp_path, monkeypatch) -> None:
    """A packaged install must not need $DPORTS_DEV_TOOL_ROOT to find data
    it already carries."""
    bundled = tmp_path / "pkg" / "data" / "deploy"
    (bundled / "rc.d").mkdir(parents=True)
    monkeypatch.setattr(paths, "BUNDLED_DEPLOY_DIR", bundled)
    monkeypatch.setenv("DPORTS_DEV_TOOL_ROOT", str(tmp_path / "elsewhere"))
    assert paths.deploy_dir() == bundled


def test_checkout_falls_back_to_the_tool_root(tmp_path, monkeypatch) -> None:
    """An editable install has no bundled copy: the package directory IS
    the source tree, and force-include only applies to a built wheel."""
    d = _fake_deploy(tmp_path)
    monkeypatch.setattr(paths, "BUNDLED_DEPLOY_DIR", tmp_path / "absent")
    monkeypatch.setenv("DPORTS_DEV_TOOL_ROOT", str(tmp_path))
    assert paths.deploy_dir() == d


# --- failures name both places -----------------------------------------

def test_neither_source_reports_both(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "BUNDLED_DEPLOY_DIR", tmp_path / "absent")
    monkeypatch.setenv("DPORTS_DEV_TOOL_ROOT", str(tmp_path))
    with pytest.raises(paths.MissingInput) as exc:
        paths.deploy_dir()
    msg = str(exc.value)
    assert "absent" in msg and "deploy" in msg and "--tool-root" in msg


def test_explicit_root_without_deploy_is_rejected(tmp_path) -> None:
    with pytest.raises(paths.MissingInput, match="no rc.d"):
        paths.deploy_dir(tmp_path)


def test_no_bundled_and_no_env_raises_about_the_checkout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "BUNDLED_DEPLOY_DIR", tmp_path / "absent")
    monkeypatch.delenv("DPORTS_DEV_TOOL_ROOT", raising=False)
    with pytest.raises(paths.MissingInput, match="DPORTS_DEV_TOOL_ROOT"):
        paths.deploy_dir()


# --- the packaging declaration itself -----------------------------------

def test_pyproject_force_includes_the_deploy_tree() -> None:
    """Verified by building the wheel: without this stanza the files are
    simply absent from it, and `deploy install` on a packaged host fails
    with nothing to copy.
    """
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    force = (data["tool"]["hatch"]["build"]["targets"]["wheel"]
             ["force-include"])
    assert force["deploy"] == "dportsv3/data/deploy"


def test_the_bundled_path_matches_what_packaging_declares() -> None:
    """The two halves have to agree or the wheel ships files nothing reads."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    dest = (data["tool"]["hatch"]["build"]["targets"]["wheel"]
            ["force-include"]["deploy"])
    assert paths.BUNDLED_DEPLOY_DIR.name == Path(dest).name
    assert paths.BUNDLED_DEPLOY_DIR.parent.name == Path(dest).parent.name
