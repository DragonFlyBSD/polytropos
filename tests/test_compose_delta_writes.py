"""compose writes its own input tree only from a full compose (poly-7pwa.16).

Only a full compose, without --dry-run, whose freebsd checkout passed the
branch check writes --delta-root: removed_in markers for newly stale
overlays, and the special/<component>/{diffs,replacements}/@<target>/
bootstrap. An --origin compose reads the delta tree and never writes it --
reapply, apply-and-build and the absorb parity gates all run --origin, and
a write there lands in a job's worktree or in a tree the gate promised not
to touch. Every write a compose does make is listed under
stages[].metadata.delta_writes, apart from its output.

Synthetic trees only: "2026Q3" is a fixture branch name, and nothing here
reads DeltaPorts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from dportsv3.cli import main
from dportsv3.compose_reporting import (
    build_compose_report_overview,
    format_compose_overview,
)
from dportsv3.migration.parity import check_parity


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _overlay(port: Path, origin: str, value: str) -> None:
    port.mkdir(parents=True)
    (port / "overlay.dops").write_text(
        f"port {origin}\n"
        "type port\n"
        'reason "fixture"\n'
        "target @any\n"
        f'mk set VAR "{value}"\n'
    )


def _tree(root: Path, branch: str = "main", *, stale=("missing",)) -> Path:
    """freebsd on <branch> with devel/present and Mk/; a stale overlay per name.

    Each name in stale gets a type port overlay.dops in the delta for an
    origin freebsd does not have.
    """
    freebsd = root / "freebsd"
    (freebsd / "devel" / "present").mkdir(parents=True)
    (freebsd / "devel" / "present" / "Makefile").write_text("VAR= upstream\n")
    (freebsd / "Mk").mkdir()
    (freebsd / "Mk" / "bsd.port.mk").write_text("X= 1\n")
    _run(["git", "init"], freebsd)
    _run(["git", "checkout", "-b", branch], freebsd)
    (root / "delta" / "ports").mkdir(parents=True)
    for name in stale:
        _overlay(
            root / "delta" / "ports" / "devel" / name, f"devel/{name}", "from-overlay"
        )
    return root


def _special_diff(root: Path) -> None:
    """An unscoped main special/Mk diff, -p0 relative to Mk/.

    compose runs patch -p0 inside the output's Mk/, so a git-style a/ b/
    header would fail with E_COMPOSE_SPECIAL_PATCH_FAILED.
    """
    diffs = root / "delta" / "special" / "Mk" / "diffs"
    diffs.mkdir(parents=True)
    (diffs / "x.diff").write_text(
        "--- bsd.port.mk\n+++ bsd.port.mk\n@@ -1 +1 @@\n-X= 1\n+X= 2\n"
    )


def _snapshot(delta: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(delta)): path.read_bytes()
        for path in sorted(delta.rglob("*"))
        if path.is_file()
    }


def _compose(root: Path, capsys, target: str, *extra: str) -> tuple[int, str]:
    code = main(
        [
            "compose",
            "--target", target,
            "--output", str(root / "out"),
            "--delta-root", str(root / "delta"),
            "--freebsd-root", str(root / "freebsd"),
            "--oracle-profile", "off",
            *extra,
        ]
    )
    return code, capsys.readouterr().out


def _stage(payload: dict, name: str) -> dict:
    return next(stage for stage in payload["stages"] if stage["name"] == name)


def _marked(payload: dict) -> list[str]:
    return [
        warning
        for warning in _stage(payload, "preflight_validate")["warnings"]
        if warning.startswith("I_COMPOSE_STALE_MARKED_REMOVED")
    ]


def _has_delta_writes(payload: dict) -> bool:
    return any("delta_writes" in stage["metadata"] for stage in payload["stages"])


def test_an_origin_compose_never_writes_the_delta_tree(tmp_path, capsys):
    # A quarterly, so the snapshot also proves no special/ bootstrap.
    root = _tree(tmp_path, "2026Q3")
    _special_diff(root)
    shutil.copytree(root / "freebsd", root / "out")
    before = _snapshot(root / "delta")

    code, out = _compose(
        root, capsys, "@2026Q3", "--origin", "devel/missing", "--json"
    )
    payload = json.loads(out)

    assert code == 2, payload  # still a stale error
    assert _snapshot(root / "delta") == before
    marked = _marked(payload)
    assert len(marked) == 1, marked
    assert "would add removed_in target @2026Q3" in marked[0]
    assert "(an --origin compose does not write the delta tree)" in marked[0]
    assert not _has_delta_writes(payload)


def test_a_full_compose_on_the_wrong_branch_writes_nothing(tmp_path, capsys):
    root = _tree(tmp_path, "2026Q3")
    before = _snapshot(root / "delta")

    code, out = _compose(root, capsys, "@main", "--replace-output", "--json")
    payload = json.loads(out)

    assert code == 2, payload
    assert any(
        error.startswith("E_COMPOSE_TARGET_BRANCH_MISMATCH")
        for error in _stage(payload, "preflight_validate")["errors"]
    ), payload
    assert _snapshot(root / "delta") == before
    marked = _marked(payload)
    assert len(marked) == 1, marked
    assert "(the freebsd checkout is not on branch main)" in marked[0]


def test_delta_writes_are_listed_apart_from_output(tmp_path, capsys):
    root = _tree(tmp_path, "2026Q3")
    _special_diff(root)

    code, out = _compose(root, capsys, "@2026Q3", "--replace-output")

    assert code == 2, out
    assert (
        "delta_writes: count=2 (input tree, not output; review and commit): "
        "ports/devel/missing/overlay.toml (removed_in +@2026Q3), "
        "special/Mk/diffs/@2026Q3/ (bootstrapped from unscoped main)\n"
    ) in out
    # The bootstrapped diff applied: a header patch -p0 cannot read fails
    # here rather than passing silently (the exit is 2 either way).
    assert (root / "out" / "Mk" / "bsd.port.mk").read_text() == "X= 2\n"


def test_json_lists_every_write_and_the_text_line_says_when_it_stops(
    tmp_path, capsys
):
    names = tuple(f"gone{index}" for index in range(7))
    root = _tree(tmp_path, "main", stale=names)

    code, out = _compose(root, capsys, "@main", "--json")
    payload = json.loads(out)

    writes = _stage(payload, "preflight_validate")["metadata"]["delta_writes"]
    assert sorted(writes) == [
        f"ports/devel/{name}/overlay.toml (removed_in +@main)" for name in names
    ]
    lines = format_compose_overview(build_compose_report_overview(payload, top=5))
    line = next(item for item in lines if item.startswith("delta_writes:"))
    assert line.startswith("delta_writes: count=7 "), line
    assert "gone4" in line and "gone5" not in line, line
    assert line.endswith(", ..."), line


def test_a_dry_run_writes_nothing_and_says_why(tmp_path, capsys):
    root = _tree(tmp_path, "main")
    before = _snapshot(root / "delta")

    code, out = _compose(root, capsys, "@main", "--dry-run", "--json")
    payload = json.loads(out)

    assert _snapshot(root / "delta") == before
    marked = _marked(payload)
    assert len(marked) == 1, marked
    assert marked[0].endswith("(dry run)"), marked
    assert not _has_delta_writes(payload)


def test_the_parity_gate_leaves_both_delta_roots_unchanged(tmp_path):
    # parity.py promises to be read-only with respect to both delta roots.
    _tree(tmp_path, "main", stale=())
    roots = (tmp_path / "base", tmp_path / "cand")
    for delta in roots:
        _overlay(delta / "ports" / "devel" / "gone", "devel/gone", "x")

    check_parity(
        "devel/gone",
        "@main",
        baseline_root=roots[0],
        candidate_root=roots[1],
        freebsd_root=tmp_path / "freebsd",
    )

    for delta in roots:
        assert not (delta / "ports" / "devel" / "gone" / "overlay.toml").exists()
