"""`dev-env create` needs a git repository, and says so.

poly-abr.11. The boundary is deliberate: create clones the branch the
tool tree is on into the chroot, so the env matches what you are working
on. That means an unpacked tarball or an installed copy cannot be a
source — which is fine, as long as the error explains it rather than
naming a git command that failed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dports_dev_env import builder as builder_mod
from dports_dev_env.errors import UsageError


class _Builder:
    """Just enough of EnvironmentBuilder to exercise the validator."""
    def __init__(self, allow_dirty=False):
        self.options = type("O", (), {"allow_dirty": allow_dirty})()
    run_git = builder_mod.EnvironmentBuilder.run_git
    validate_source_repo = builder_mod.EnvironmentBuilder.validate_source_repo


def _tool_tree(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "dportsv3").write_text("#!/bin/sh\n")
    (root / "pyproject.toml").write_text("[project]\n")
    return root


def _looks_right(root: Path) -> bool:
    return (root / "bin" / "dportsv3").is_file() and (root / "pyproject.toml").is_file()


def _validate(root, allow_dirty=False):
    _Builder(allow_dirty).validate_source_repo(
        "polytropos tool checkout", root, "--tool-root", _looks_right,
        "it has no bin/dportsv3 wrapper and pyproject.toml at its root")


# --- the boundary -------------------------------------------------------

def test_a_tarball_extraction_is_rejected_with_the_reason(tmp_path) -> None:
    """Every file present, no .git — exactly what unpacking a release
    tarball gives you."""
    _tool_tree(tmp_path)
    with pytest.raises(UsageError) as exc:
        _validate(tmp_path)
    msg = str(exc.value)
    assert "not a git repository" in msg
    assert "tarball" in msg, "does not explain why files alone are not enough"
    assert "branch" in msg, "does not say what the history is needed for"


def test_the_message_is_not_just_a_failed_command(tmp_path) -> None:
    """Regression: this used to raise 'command failed: git -C ... rev-parse'
    and swallow git's own explanation."""
    _tool_tree(tmp_path)
    with pytest.raises(UsageError) as exc:
        _validate(tmp_path)
    assert "rev-parse" not in str(exc.value)


def test_a_real_repository_passes(tmp_path) -> None:
    _tool_tree(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    _validate(tmp_path)   # must not raise


def test_a_dirty_repository_is_still_refused(tmp_path) -> None:
    """Unchanged behaviour: only committed state reaches the env."""
    _tool_tree(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(UsageError, match="dirty"):
        _validate(tmp_path)


def test_allow_dirty_overrides_that(tmp_path) -> None:
    _tool_tree(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _validate(tmp_path, allow_dirty=True)   # warns, does not raise


def test_the_shape_check_still_comes_first(tmp_path) -> None:
    """A directory that is not the tool tree at all gets the clearer
    complaint, not a git one."""
    with pytest.raises(UsageError, match="does not look like"):
        _validate(tmp_path)


# --- git's own words are no longer discarded ----------------------------

def test_run_git_surfaces_stderr(tmp_path) -> None:
    with pytest.raises(UsageError) as exc:
        _Builder().run_git(["git", "-C", str(tmp_path), "rev-parse", "--git-dir"])
    assert "not a git repository" in str(exc.value), \
        "swallowed git's explanation again"
