from __future__ import annotations

from pathlib import Path

from dportsv3.agent.reconcile import reconcile_orphaned_artifacts

_HEADER = 'port shells/bash\ntype port\nreason "r"\ntarget @any\n\n'


def _port(tmp_path: Path, overlay_ops: str, files: dict[str, str]) -> Path:
    port = tmp_path / "ports" / "shells" / "bash"
    port.mkdir(parents=True)
    if overlay_ops is not None:
        (port / "overlay.dops").write_text(_HEADER + overlay_ops)
    for rel, content in files.items():
        p = port / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return port


def test_keeps_a_dragonfly_file_no_op_reads(tmp_path: Path) -> None:
    """poly-u9n4.4: the folder installs it, so it is not an orphan -- and
    deleting it would drop every patch the agent wrote with no op."""
    port = _port(
        tmp_path,
        "mk add USES ncurses\n",  # no file materialize: the folder installs it
        {"dragonfly/patch-lib_readline_terminal.c": "patch body\n",
         "dragonfly/@main/patch-x": "own copy\n"},
    )
    assert reconcile_orphaned_artifacts(port) == []
    assert (port / "dragonfly/patch-lib_readline_terminal.c").is_file()
    assert (port / "dragonfly/@main/patch-x").is_file()


def test_removes_an_unreferenced_diff_and_its_empty_dir(tmp_path: Path) -> None:
    port = _port(tmp_path, "mk add USES ncurses\n",
                 {"diffs/orphan.diff": "gone\n"})
    assert reconcile_orphaned_artifacts(port) == ["diffs/orphan.diff"]
    assert not (port / "diffs").exists()  # empty dir dropped too


def test_keeps_referenced_materialize(tmp_path: Path) -> None:
    rel = "dragonfly/patch-lib_readline_terminal.c"
    port = _port(
        tmp_path,
        f"file materialize {rel} -> {rel}\n",
        {rel: "patch body\n"},
    )
    assert reconcile_orphaned_artifacts(port) == []
    assert (port / rel).is_file()


def test_keeps_referenced_patch_apply_drops_other_diff(tmp_path: Path) -> None:
    port = _port(
        tmp_path,
        "patch apply diffs/keep.diff\n",
        {"diffs/keep.diff": "kept\n", "diffs/orphan.diff": "gone\n"},
    )
    removed = reconcile_orphaned_artifacts(port)
    assert removed == ["diffs/orphan.diff"]
    assert (port / "diffs" / "keep.diff").is_file()


def test_noop_when_overlay_missing(tmp_path: Path) -> None:
    port = tmp_path / "ports" / "shells" / "bash"
    port.mkdir(parents=True)
    (port / "dragonfly").mkdir()
    (port / "dragonfly" / "patch-x").write_text("x\n")
    assert reconcile_orphaned_artifacts(port) == []
    assert (port / "dragonfly" / "patch-x").is_file()  # never delete blind


def test_noop_when_overlay_unparseable(tmp_path: Path) -> None:
    port = _port(
        tmp_path,
        "mk totally not valid !!!\n",
        {"dragonfly/patch-x": "x\n"},
    )
    assert reconcile_orphaned_artifacts(port) == []
    assert (port / "dragonfly" / "patch-x").is_file()
