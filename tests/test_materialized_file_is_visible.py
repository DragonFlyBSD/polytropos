"""A file that ``file materialize`` wrote must be the file later ops see.

poly-bz7n.1: materialize stages its file as bytes, and FileTransaction's
reads only consulted staged *text*, so every later op in the same apply saw
the disk underneath instead. A text edit then edited the upstream file and
its write replaced the materialized one -- the overlay's file silently gone,
``ok=True``. Copy and remove failed on a file that was, in fact, there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3.engine.api import apply_dsl
from dportsv3.engine.fsops import FileTransaction

HEAD = "target @main\nport category/name\n"


def _apply(tmp_path: Path, ops: str, *, upstream: str | None = None):
    overlay_dir = tmp_path / "delta" / "ports" / "category" / "name"
    (overlay_dir / "dragonfly").mkdir(parents=True)
    (overlay_dir / "dragonfly" / "a").write_text("AAA\n")

    port_root = tmp_path / "port"
    (port_root / "files").mkdir(parents=True)
    if upstream is not None:
        (port_root / "files" / "x").write_text(upstream)

    result = apply_dsl(
        HEAD + ops,
        source_path=overlay_dir / "overlay.dops",
        port_root=port_root,
        target="@main",
        dry_run=False,
        strict=False,
        oracle_profile="off",
    )
    return result, port_root


MATERIALIZE = "file materialize dragonfly/a -> files/x\n"


def test_a_text_edit_edits_the_materialized_file(tmp_path: Path) -> None:
    result, port = _apply(
        tmp_path,
        MATERIALIZE + 'text replace-once file files/x from "AAA" to "EDITED"\n',
        upstream="UPSTREAM\n",
    )

    assert result.ok
    assert (port / "files" / "x").read_text() == "EDITED\n"


def test_a_text_edit_does_not_reach_through_to_the_upstream_file(
    tmp_path: Path,
) -> None:
    # The silent case: this edit matched the upstream file on disk, applied,
    # and its write replaced the materialized one. The materialized file has
    # no "UPSTREAM" in it, so the edit must not find its pattern.
    result, port = _apply(
        tmp_path,
        MATERIALIZE + 'text replace-once file files/x from "UPSTREAM" to "EDITED"\n',
        upstream="UPSTREAM\n",
    )

    assert not result.ok
    assert "EDITED" not in (port / "files" / "x").read_text()


def test_a_copy_reads_the_materialized_file(tmp_path: Path) -> None:
    result, port = _apply(tmp_path, MATERIALIZE + "file copy files/x -> files/y\n")

    assert result.ok
    assert (port / "files" / "y").read_text() == "AAA\n"


@pytest.mark.parametrize("upstream", [None, "UPSTREAM\n"], ids=["new", "over-upstream"])
def test_a_remove_removes_the_materialized_file(
    tmp_path: Path, upstream: str | None
) -> None:
    # poly-7pwa.3: a materialize and a remove of one file in the same block
    # would make the materialize a dead op (E_SEM_DEAD_OP). The @any
    # materialize still runs first on @main, so this still pins poly-bz7n.1.
    result, port = _apply(
        tmp_path,
        "target @any\n" + MATERIALIZE + "target @main\nfile remove files/x\n",
        upstream=upstream,
    )

    assert result.ok
    assert not (port / "files" / "x").exists()


@pytest.mark.parametrize(
    "data", [b"one\r\ntwo\n", b"latin1 \xa0 nbsp\n"], ids=["crlf", "non-utf8"]
)
def test_a_staged_read_decodes_exactly_like_a_disk_read(
    tmp_path: Path, data: bytes
) -> None:
    on_disk = tmp_path / "on_disk"
    on_disk.write_bytes(data)
    staged = tmp_path / "staged"
    txn = FileTransaction(dry_run=False)
    txn.stage_write_bytes(staged, data)

    def outcome(read):
        try:
            return ("text", read())
        except UnicodeDecodeError as exc:
            return ("error", type(exc))

    assert outcome(lambda: txn.read_text(staged)) == outcome(on_disk.read_text)
