"""The dragonfly/ folder is the install (poly-u9n4.2).

For a DSL port, dragonfly/<x> is installed on every build line and
dragonfly/@<line>/<x> on that line only, as dragonfly/<x>; a file an
explicit op reads, on any line, is left to that op. Each folder is staged
at the head of its scope: the flat files before the @any ops, the line's
own after them and before the line's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3.compose_discovery import validate_target_scoped_payloads
from dportsv3.compose_models import ComposePortContext
from dportsv3.engine.api import apply_dsl
from dportsv3.engine.models import FolderInstall, PlanOp
from dportsv3.engine.payload import (
    folder_destination,
    folder_installs,
    folder_scope,
    with_folder_installs,
)

HEADER = 'port cat/port\ntype port\nreason "r"\n'


def _overlay(tmp_path: Path, ops: str, files: dict[str, str]) -> Path:
    overlay = tmp_path / "delta" / "ports" / "cat" / "port"
    overlay.mkdir(parents=True)
    (overlay / "overlay.dops").write_text(HEADER + ops)
    for rel, content in files.items():
        path = overlay / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return overlay


def _apply(tmp_path: Path, overlay: Path, target: str):
    port = tmp_path / "out" / target.lstrip("@") / "cat" / "port"
    port.mkdir(parents=True)
    (port / "Makefile").write_text("PORTNAME= port\n")
    dops = overlay / "overlay.dops"
    result = apply_dsl(
        dops.read_text(), source_path=dops, port_root=port, target=target,
        oracle_profile="off",
    )
    assert result.ok, [(d.code, d.message) for d in result.diagnostics] + [
        (d.code, d.message) for row in result.op_results for d in row.diagnostics
    ]
    return port, result


def _payload(port: Path) -> dict[str, str]:
    lane = port / "dragonfly"
    if not lane.is_dir():
        return {}
    return {p.relative_to(port).as_posix(): p.read_text()
            for p in sorted(lane.rglob("*")) if p.is_file()}


def test_a_flat_file_is_installed_on_every_line_with_no_op(tmp_path):
    overlay = _overlay(tmp_path, "", {"dragonfly/patch-a": "flat\n"})
    for target in ("@main", "@2026Q3"):
        port, result = _apply(tmp_path, overlay, target)
        assert _payload(port) == {"dragonfly/patch-a": "flat\n"}
        assert result.installed == [
            FolderInstall("dragonfly/patch-a", "dragonfly/patch-a", "@any")]
        assert result.report["installed_files"] == 1


def test_a_line_folder_is_installed_flat_on_that_line_only(tmp_path):
    overlay = _overlay(tmp_path, "", {"dragonfly/@main/patch-b": "main\n"})
    port, result = _apply(tmp_path, overlay, "@main")
    assert _payload(port) == {"dragonfly/patch-b": "main\n"}
    assert result.installed == [
        FolderInstall("dragonfly/@main/patch-b", "dragonfly/patch-b", "@main")]
    port, result = _apply(tmp_path, overlay, "@2026Q3")
    assert _payload(port) == {}
    assert result.installed == []


def test_the_line_copy_wins_a_name_clash_with_the_flat_file(tmp_path):
    overlay = _overlay(tmp_path, "", {
        "dragonfly/patch-a": "flat\n",
        "dragonfly/@main/patch-a": "main\n",
    })
    port, _ = _apply(tmp_path, overlay, "@main")
    assert _payload(port) == {"dragonfly/patch-a": "main\n"}
    port, _ = _apply(tmp_path, overlay, "@2026Q3")
    assert _payload(port) == {"dragonfly/patch-a": "flat\n"}


def test_the_line_copy_wins_over_an_explicit_any_op(tmp_path):
    # Q2: the line's folder goes after the @any ops, so an own copy needs
    # no op of its own while the explicit @any op still exists.
    overlay = _overlay(
        tmp_path,
        "file materialize dragonfly/patch-a -> dragonfly/patch-a\n",
        {"dragonfly/patch-a": "flat\n", "dragonfly/@main/patch-a": "main\n"},
    )
    port, result = _apply(tmp_path, overlay, "@main")
    assert _payload(port) == {"dragonfly/patch-a": "main\n"}
    assert [i.src for i in result.installed] == ["dragonfly/@main/patch-a"]
    port, result = _apply(tmp_path, overlay, "@2026Q3")
    assert _payload(port) == {"dragonfly/patch-a": "flat\n"}
    assert result.installed == []


def test_a_file_one_lines_op_reads_reaches_no_other_line(tmp_path):
    overlay = _overlay(
        tmp_path,
        "target @main\nfile materialize dragonfly/patch-c -> dragonfly/patch-c\n",
        {"dragonfly/patch-c": "c\n"},
    )
    port, result = _apply(tmp_path, overlay, "@main")
    assert _payload(port) == {"dragonfly/patch-c": "c\n"}
    assert result.installed == []
    port, result = _apply(tmp_path, overlay, "@2026Q3")
    assert _payload(port) == {}
    assert result.installed == []


def test_a_file_a_patch_apply_reads_is_not_installed(tmp_path):
    overlay = _overlay(
        tmp_path,
        "patch apply dragonfly/fix.diff\n",
        {"dragonfly/fix.diff": (
            "--- Makefile\n+++ Makefile\n@@ -1 +1 @@\n"
            "-PORTNAME= port\n+PORTNAME= fixed\n")},
    )
    port, result = _apply(tmp_path, overlay, "@main")
    assert (port / "Makefile").read_text() == "PORTNAME= fixed\n"
    assert _payload(port) == {}
    assert result.installed == []


def test_a_line_op_removes_an_installed_flat_file_on_that_line(tmp_path):
    overlay = _overlay(
        tmp_path,
        "target @main\nfile remove dragonfly/patch-a\n",
        {"dragonfly/patch-a": "flat\n"},
    )
    port, _ = _apply(tmp_path, overlay, "@main")
    assert _payload(port) == {}
    port, _ = _apply(tmp_path, overlay, "@2026Q3")
    assert _payload(port) == {"dragonfly/patch-a": "flat\n"}


def test_an_any_op_edits_an_installed_flat_file(tmp_path):
    overlay = _overlay(
        tmp_path,
        'text replace-once file dragonfly/pkg-message from "old" to "new"\n',
        {"dragonfly/pkg-message": "old\n"},
    )
    port, _ = _apply(tmp_path, overlay, "@main")
    assert _payload(port) == {"dragonfly/pkg-message": "new\n"}


def test_an_apply_without_an_overlay_dir_installs_nothing(tmp_path):
    port = tmp_path / "port"
    (port / "dragonfly").mkdir(parents=True)
    (port / "Makefile").write_text("PORTNAME= port\n")
    (port / "dragonfly" / "patch-a").write_text("flat\n")
    result = apply_dsl(HEADER, port_root=port, target="@main",
                       oracle_profile="off")
    assert result.ok
    assert result.installed == []


def test_a_mask_installs_nothing(tmp_path):
    overlay = tmp_path / "delta" / "ports" / "cat" / "port"
    (overlay / "dragonfly").mkdir(parents=True)
    (overlay / "dragonfly" / "patch-a").write_text("flat\n")
    dops = overlay / "overlay.dops"
    dops.write_text("port cat/port\ntype mask\n")
    port = tmp_path / "out" / "cat" / "port"
    port.mkdir(parents=True)
    result = apply_dsl(dops.read_text(), source_path=dops, port_root=port,
                       target="@main", oracle_profile="off")
    assert result.installed == []
    assert not (port / "dragonfly").exists()


def test_installed_bytes_are_copied_verbatim(tmp_path):
    overlay = _overlay(tmp_path, "", {})
    (overlay / "dragonfly").mkdir()
    (overlay / "dragonfly" / "patch-latin1").write_bytes(b"caf\xe9\n")
    port, _ = _apply(tmp_path, overlay, "@main")
    assert (port / "dragonfly" / "patch-latin1").read_bytes() == b"caf\xe9\n"


@pytest.mark.parametrize("rel,scope,dst", [
    ("dragonfly/patch-a", "@any", "dragonfly/patch-a"),
    ("dragonfly/sub/patch-a", "@any", "dragonfly/sub/patch-a"),
    ("./dragonfly/patch-a", "@any", "dragonfly/patch-a"),
    ("dragonfly/@main/patch-a", "@main", "dragonfly/patch-a"),
    ("dragonfly/@2026Q3/sub/x", "@2026Q3", "dragonfly/sub/x"),
    ("dragonfly/@any/patch-a", None, None),
    ("dragonfly/@bogus/patch-a", None, None),
    ("diffs/patch-a", None, None),
    ("dragonfly", None, None),
])
def test_the_scope_the_folder_gives_a_path(rel, scope, dst):
    assert folder_scope(rel) == scope
    if dst is not None:
        assert folder_destination(rel) == dst


def test_folder_installs_skips_every_file_any_line_reads(tmp_path):
    ops = [PlanOp("op-1", "@2026Q3", "file.materialize",
                  {"src": "./dragonfly/patch-a", "dst": "dragonfly/patch-a"}),
           PlanOp("op-2", "@any", "patch.apply", {"path": "dragonfly/fix.diff"})]
    files = ["dragonfly/patch-a", "dragonfly/fix.diff", "dragonfly/patch-b",
             "dragonfly/@main/patch-c", "dragonfly/@2026Q3/patch-d"]
    assert folder_installs(tmp_path, ops, "@main", files=files) == [
        FolderInstall("dragonfly/patch-b", "dragonfly/patch-b", "@any"),
        FolderInstall("dragonfly/@main/patch-c", "dragonfly/patch-c", "@main"),
    ]


def test_each_folder_goes_at_the_head_of_its_scope():
    any_op = PlanOp("op-1", "@any", "mk.var.set", {})
    main_op = PlanOp("op-2", "@main", "mk.var.set", {})
    flat = FolderInstall("dragonfly/a", "dragonfly/a", "@any")
    own = FolderInstall("dragonfly/@main/b", "dragonfly/b", "@main")
    assert with_folder_installs([any_op, main_op], [flat, own]) == [
        flat, any_op, own, main_op]
    assert with_folder_installs([main_op], [flat, own]) == [flat, own, main_op]
    assert with_folder_installs([any_op], [flat, own]) == [flat, any_op, own]


def _ctx(path: Path, mode: str) -> ComposePortContext:
    return ComposePortContext(origin="cat/port", path=path, dops_path=None,
                              mode=mode, mode_reason="test")


def test_an_any_folder_is_an_error_in_a_dops_port(tmp_path):
    (tmp_path / "dragonfly" / "@any").mkdir(parents=True)
    (tmp_path / "dragonfly" / "@any" / "patch-a").write_text("x\n")
    errors = validate_target_scoped_payloads(_ctx(tmp_path, "dops"))
    assert len(errors) == 1 and "dragonfly/@any" in errors[0]
    # A compat port keeps its script-parity copy.
    assert validate_target_scoped_payloads(_ctx(tmp_path, "compat")) == []
