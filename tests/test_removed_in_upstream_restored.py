"""removed_in skips a port only while its origin is missing upstream.

poly-7pwa.6, the epic's decision D3 (option a, 2026-10-04). removed_in
records that the origin was missing upstream for a build line. Once the
port is back, compose applies its overlay again, and a full compose that
may write the delta tree (poly-7pwa.16's rule, branch check included)
removes the target from removed_in. Skipping a present port would build it
as pure upstream with its overlay silently gone.

Synthetic trees only: "2026Q3" is a fixture branch name, and nothing here
reads DeltaPorts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from dportsv3.cli import main


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _tree(
    root: Path, kind: str = "dops", ops: str = 'mk set VAR "from-overlay"\n'
) -> Path:
    """devel/back is upstream on main, and its overlay says removed_in @main."""
    freebsd = root / "freebsd"
    for name in ("back", "present"):
        (freebsd / "devel" / name).mkdir(parents=True)
        (freebsd / "devel" / name / "Makefile").write_text("VAR= upstream\n")
    _run(["git", "init"], freebsd)
    _run(["git", "checkout", "-b", "main"], freebsd)

    port = root / "delta" / "ports" / "devel" / "back"
    port.mkdir(parents=True)
    if kind == "dops":
        (port / "overlay.dops").write_text(
            "port devel/back\n"
            "type port\n"
            'reason "fixture"\n'
            "target @any\n" + ops
        )
    else:
        (port / "Makefile.DragonFly").write_text("DFLY= yes\n")
    (port / "overlay.toml").write_text('removed_in = ["@main"]\n')
    return port


def _compose(root: Path, capsys, *extra: str) -> tuple[int, str]:
    code = main(
        [
            "compose",
            "--target", "@main",
            "--output", str(root / "out"),
            "--delta-root", str(root / "delta"),
            "--freebsd-root", str(root / "freebsd"),
            "--oracle-profile", "off",
            *extra,
        ]
    )
    return code, capsys.readouterr().out


def _full(root: Path, capsys) -> tuple[int, dict]:
    code, out = _compose(root, capsys, "--replace-output", "--json")
    return code, json.loads(out)


def _back(payload: dict) -> dict:
    return next(port for port in payload["ports"] if port["origin"] == "devel/back")


def _stage(payload: dict, name: str) -> dict:
    return next(stage for stage in payload["stages"] if stage["name"] == name)


def _warnings(payload: dict, prefix: str) -> list[str]:
    return [
        warning
        for stage in payload["stages"]
        for warning in stage["warnings"]
        if warning.startswith(prefix)
    ]


@pytest.mark.parametrize("kind", ["dops", "compat"])
def test_a_port_restored_upstream_gets_its_overlay_back(tmp_path, capsys, kind):
    # 842 of the 1093 markers in the tree are on compat ports.
    _tree(tmp_path, kind)

    code, payload = _full(tmp_path, capsys)

    assert code == 0, payload
    report = _back(payload)
    assert "removed-for-target" not in report["notes"]
    composed = tmp_path / "out" / "devel" / "back"
    if kind == "dops":
        assert report["applied_ops"] == 1
        assert "from-overlay" in (composed / "Makefile").read_text()
    else:
        assert (composed / "Makefile.DragonFly").is_file()


def test_a_full_compose_clears_the_marker_of_a_port_that_is_back(tmp_path, capsys):
    port = _tree(tmp_path)

    code, payload = _full(tmp_path, capsys)

    assert code == 0, payload
    assert not (port / "overlay.toml").exists()
    assert "removed-for-target-cleared" in _back(payload)["notes"]
    cleared = _warnings(payload, "I_COMPOSE_STALE_MARK_CLEARED")
    assert len(cleared) == 1, cleared
    assert cleared[0].startswith("I_COMPOSE_STALE_MARK_CLEARED: devel/back:")
    assert _stage(payload, "preflight_validate")["metadata"]["delta_writes"] == [
        "ports/devel/back/overlay.toml (removed_in -@main)"
    ]


def test_clearing_keeps_the_rest_of_the_manifest(tmp_path, capsys):
    port = _tree(tmp_path)
    (port / "overlay.toml").write_text(
        'type = "port"\nremoved_in = ["@2026Q3", "@main"]\n'
    )

    code, payload = _full(tmp_path, capsys)

    assert code == 0, payload
    assert tomllib.loads((port / "overlay.toml").read_text()) == {
        "type": "port",
        "removed_in": ["@2026Q3"],
    }


def test_reapply_applies_the_overlay_and_writes_and_warns_nothing(tmp_path, capsys):
    port = _tree(tmp_path)
    shutil.copytree(tmp_path / "freebsd", tmp_path / "out")
    before = (port / "overlay.toml").read_bytes()

    code, out = _compose(tmp_path, capsys, "--origin", "devel/back", "--json")
    payload = json.loads(out)

    assert code == 0, payload
    report = _back(payload)
    assert report["applied_ops"] == 1
    assert "removed-for-target-ignored" in report["notes"]
    assert _warnings(payload, "I_COMPOSE_STALE_MARK") == []
    assert (port / "overlay.toml").read_bytes() == before


def test_a_dry_run_names_the_stale_marker_and_leaves_it(tmp_path, capsys):
    port = _tree(tmp_path)
    before = (port / "overlay.toml").read_bytes()

    code, out = _compose(tmp_path, capsys, "--dry-run")

    assert code == 0, out
    assert "I_COMPOSE_STALE_MARK_IGNORED=1" in out
    assert (
        "hint: removed_in named a build line on which the port exists upstream again"
        in out
    )
    assert (port / "overlay.toml").read_bytes() == before


def test_a_full_compose_on_the_wrong_branch_clears_nothing(tmp_path, capsys):
    # D3's gate, pinned where the clear lives: removing poly-7pwa.16's
    # branch condition fails this.
    port = _tree(tmp_path)
    _run(["git", "checkout", "-b", "2026Q3"], tmp_path / "freebsd")
    before = (port / "overlay.toml").read_bytes()

    code, payload = _full(tmp_path, capsys)

    assert code == 2, payload
    assert (port / "overlay.toml").read_bytes() == before
    assert _warnings(payload, "I_COMPOSE_STALE_MARK_CLEARED") == []
    ignored = _warnings(payload, "I_COMPOSE_STALE_MARK_IGNORED")
    assert len(ignored) == 1, ignored
    assert ignored[0].endswith("(the freebsd checkout is not on branch main)")


@pytest.mark.parametrize(
    ("kind", "code"),
    [("dops", "E_COMPOSE_APPLY_FAILED"), ("compat", "E_COMPOSE_COMPAT_FAILED")],
)
def test_a_restored_port_whose_overlay_no_longer_applies_fails_loudly(
    tmp_path, capsys, kind, code
):
    # Before this change the port composed silently as pure upstream.
    port = _tree(tmp_path, kind, ops="mk unset OLDVAR\n")
    if kind == "compat":
        (port / "diffs").mkdir()
        (port / "diffs" / "Makefile.diff").write_text(
            "--- Makefile.orig\n"
            "+++ Makefile\n"
            "@@ -1 +1 @@\n"
            "-OLDVAR= gone\n"
            "+OLDVAR= new\n"
        )

    exit_code, payload = _full(tmp_path, capsys)

    assert exit_code == 2, payload
    errors = [error for stage in payload["stages"] for error in stage["errors"]]
    assert any(
        error.startswith(code) and "devel/back" in error for error in errors
    ), errors


def test_the_marker_still_skips_a_port_missing_upstream_and_stays_quiet(
    tmp_path, capsys
):
    # Passes before and after: it guards against adding noise.
    port = _tree(tmp_path)
    shutil.rmtree(tmp_path / "freebsd" / "devel" / "back")

    code, payload = _full(tmp_path, capsys)

    assert code == 0, payload
    report = _back(payload)
    assert "removed-for-target" in report["notes"]
    assert report["applied_ops"] == 0
    assert _stage(payload, "preflight_validate")["warnings"] == []
    assert (port / "overlay.toml").exists()
    assert not (tmp_path / "out" / "devel" / "back").exists()
