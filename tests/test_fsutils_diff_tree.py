"""Tests for ``dportsv3.fsutils.diff_tree`` — the content-aware tree
comparison that backs the Step 47 compose-parity gate. Classification
is content-based (mtime/size never count), matching ``reconcile``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dportsv3.fsutils import diff_tree


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_identical_trees_are_equal(tmp_path: Path) -> None:
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left / "Makefile", "PORTNAME=foo\n")
    _write(left / "files" / "patch-a", "x\n")
    _write(right / "Makefile", "PORTNAME=foo\n")
    _write(right / "files" / "patch-a", "x\n")

    assert diff_tree(left, right) == []


def test_same_content_different_mtime_still_equal(tmp_path: Path) -> None:
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left / "f", "same\n")
    _write(right / "f", "same\n")
    old = time.time() - 86400
    os.utime(right / "f", (old, old))

    assert diff_tree(left, right) == []


def test_only_left_and_only_right(tmp_path: Path) -> None:
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left / "gone", "x\n")
    _write(right / "added", "y\n")

    assert sorted(diff_tree(left, right)) == [
        ("only_left", "gone"),
        ("only_right", "added"),
    ]


def test_content_difference_is_reported(tmp_path: Path) -> None:
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left / "files" / "patch-a", "one\n")
    _write(right / "files" / "patch-a", "two\n")

    assert diff_tree(left, right) == [("content", str(Path("files") / "patch-a"))]


def test_different_content_with_identical_stat_is_reported(tmp_path: Path) -> None:
    """Regression for poly-mdx.

    ``filecmp``'s shallow compare reduces a file to (type, size,
    mtime) and never reads the bytes, so a same-length edit written
    inside one filesystem tick looks unchanged. Equalising both stat
    fields on purpose makes that reproducible everywhere: on APFS
    consecutive writes get distinct nanosecond mtimes and the bug
    hides, on HAMMER2 they share a tick and it bites. The nested path
    also pins that ``phase4`` recursion inherits the deep comparison.
    """
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left / "files" / "patch-a", "DISTVERSION=1.2.3\n")
    _write(right / "files" / "patch-a", "DISTVERSION=1.2.4\n")
    src_stat = (left / "files" / "patch-a").stat()
    os.utime(
        right / "files" / "patch-a",
        ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns),
    )

    # Guard the premise: without these the test would pass for the
    # wrong reason (a shallow compare would catch a size/mtime skew).
    assert (left / "files" / "patch-a").stat().st_size == (
        right / "files" / "patch-a"
    ).stat().st_size
    assert (left / "files" / "patch-a").stat().st_mtime_ns == (
        right / "files" / "patch-a"
    ).stat().st_mtime_ns

    assert diff_tree(left, right) == [("content", str(Path("files") / "patch-a"))]


def test_nested_paths_are_relative(tmp_path: Path) -> None:
    left, right = tmp_path / "l", tmp_path / "r"
    _write(left / "a" / "b" / "c", "1\n")
    # right lacks the nested file entirely
    (right / "a" / "b").mkdir(parents=True)

    assert diff_tree(left, right) == [("only_left", str(Path("a") / "b" / "c"))]
