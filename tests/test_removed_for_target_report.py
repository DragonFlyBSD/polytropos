"""A skipped port's report must say what it is and why it was skipped.

poly-7pwa.7. Two defects, both observed on devel/plasma composed for
@main: a dops port with a valid overlay was reported ``mode: compat,
mode_reason: legacy-overlay`` (the report dataclass defaults, because the
removed_in ``continue`` preceded mode resolution), and both apply stages
wrote the single string "stale-skipped" whatever the cause -- so the
report said "stale" twice, next to the preflight's correct
"removed-for-target".

This report is the only place the skip is visible: compose exits 0 with
zero warnings (poly-7pwa.6). It has to be right.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dportsv3.cli import main


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_freebsd_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], root)
    _run(["git", "checkout", "-b", "main"], root)


def _compose(tmp_path, capsys, target="@main", expect_code=0):
    code = main(
        [
            "compose",
            "--target", target,
            "--output", str(tmp_path / "out"),
            "--delta-root", str(tmp_path / "delta"),
            "--freebsd-root", str(tmp_path / "freebsd"),
            "--oracle-profile", "off",
            "--replace-output",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == expect_code, payload
    return payload


def _report(payload, origin):
    return next(p for p in payload["ports"] if p["origin"] == origin)


@pytest.fixture
def dops_port_removed_in_main(tmp_path):
    """devel/plasma's shape: a live overlay.dops, removed_in @main, and
    the port ABSENT from upstream main.

    Every removed_in+overlay.dops port in the tree has this shape (251;
    249 of them list @main): absent upstream on each line it is removed
    in. It is also the only shape compose still skips once a marker
    stops outliving its reason (poly-7pwa.6). The @2026Q3 test below
    adds the port upstream on that branch, where its overlay applies.
    """
    freebsd = tmp_path / "freebsd"
    (freebsd / "devel" / "present").mkdir(parents=True)
    (freebsd / "devel" / "present" / "Makefile").write_text("VAR= upstream\n")
    _init_freebsd_repo(freebsd)

    port = tmp_path / "delta" / "ports" / "devel" / "plasma"
    port.mkdir(parents=True)
    (port / "overlay.dops").write_text(
        'port devel/plasma\ntype port\nreason "fixture"\n'
        'target @any\nmk set VAR "from-overlay"\n'
    )
    (port / "overlay.toml").write_text('removed_in = ["@main"]\n')
    return tmp_path


def test_a_dops_port_is_not_reported_as_compat_just_because_it_was_skipped(
    dops_port_removed_in_main, capsys
):
    """The defect an operator counts migration progress from."""
    report = _report(_compose(dops_port_removed_in_main, capsys), "devel/plasma")
    assert report["mode"] == "dops"
    assert report["mode_reason"] != "legacy-overlay"


def test_the_skip_note_names_the_cause_not_staleness(
    dops_port_removed_in_main, capsys
):
    report = _report(_compose(dops_port_removed_in_main, capsys), "devel/plasma")
    assert "removed-for-target" in report["notes"]
    assert "removed-for-target-skipped" in report["notes"]
    # "stale-skipped" means THIS run found the overlay stale and raised
    # E_COMPOSE_STALE_OVERLAY; this run skipped on a persisted marker and
    # raised nothing. Reporting both under one note was the bug.
    assert "stale-skipped" not in report["notes"]


def test_the_skip_note_is_recorded_once_not_once_per_stage(
    dops_port_removed_in_main, capsys
):
    """Two stages declining to touch a port is not two facts about it."""
    report = _report(_compose(dops_port_removed_in_main, capsys), "devel/plasma")
    assert report["notes"].count("removed-for-target-skipped") == 1


def test_the_overlay_really_is_skipped_so_the_report_is_the_only_signal(
    dops_port_removed_in_main, capsys
):
    """Guards the premise: if the ops applied, none of the above matters."""
    payload = _compose(dops_port_removed_in_main, capsys)
    assert payload["ok"] is True
    report = _report(payload, "devel/plasma")
    assert report["applied_ops"] == 0
    assert report["errors"] == 0
    # Nothing upstream to seed and the overlay skipped: no port at all.
    # The sibling proves the output tree is where this test looks.
    out = dops_port_removed_in_main / "out"
    assert (out / "devel" / "present" / "Makefile").is_file()
    assert not (out / "devel" / "plasma").exists()


def test_a_genuinely_stale_port_still_reports_stale(tmp_path, capsys):
    """The other branch of the note must keep working.

    A `type port` overlay whose origin is absent upstream is stale -- a
    different condition that shared the same note.

    Note the exit code: discovering staleness raises
    E_COMPOSE_STALE_OVERLAY and compose FAILS. That is the difference
    between the two causes and worth pinning here -- a stale port is loud
    the first time, whereas a removed-for-target port is silent forever
    after (poly-7pwa.6). Same note before this change; opposite volume.
    """
    freebsd = tmp_path / "freebsd"
    (freebsd / "devel" / "present").mkdir(parents=True)
    (freebsd / "devel" / "present" / "Makefile").write_text("VAR= upstream\n")
    _init_freebsd_repo(freebsd)

    stale = tmp_path / "delta" / "ports" / "devel" / "vanished"
    stale.mkdir(parents=True)
    (stale / "overlay.dops").write_text(
        'port devel/vanished\ntype port\nreason "fixture"\n'
        'target @any\nmk set VAR "x"\n'
    )

    report = _report(_compose(tmp_path, capsys, expect_code=2), "devel/vanished")
    assert "stale-skipped" in report["notes"]
    assert "removed-for-target-skipped" not in report["notes"]


def test_the_overlay_is_valid_and_does_apply_on_a_target_it_is_not_removed_in(
    dops_port_removed_in_main, capsys
):
    """Pins the premise the other tests cannot see.

    removed_in's `continue` precedes build_plan, so a removed-for-target
    port's overlay is never parsed -- which means every assertion above
    holds just as well for a file of garbage, and "a dops port WITH A
    VALID OVERLAY is misreported" would go untested. Composing the same
    fixture for a build line it is NOT removed in proves the overlay
    parses, has a real op, and that mode: dops was not a coincidence.
    """
    # compose_target_branch() is the target minus its "@", so composing
    # @2026Q3 needs the checkout on that branch -- build it there rather
    # than reusing the @main fixture's repo.
    tmp_path = dops_port_removed_in_main
    _run(["git", "checkout", "-b", "2026Q3"], tmp_path / "freebsd")
    (tmp_path / "freebsd" / "devel" / "plasma").mkdir(parents=True)
    (tmp_path / "freebsd" / "devel" / "plasma" / "Makefile").write_text(
        "VAR= upstream\n"
    )

    payload = _compose(tmp_path, capsys, target="@2026Q3")
    report = _report(payload, "devel/plasma")
    assert report["mode"] == "dops"
    assert report["applied_ops"] == 1
    assert "removed-for-target" not in report["notes"]
    composed = tmp_path / "out" / "devel" / "plasma" / "Makefile"
    assert "from-overlay" in composed.read_text()


def test_the_transition_every_real_port_goes_through(tmp_path, capsys):
    """Run 1 is loud and says stale; run 2 is quiet and says removed_in.

    This is the sequence all 249 removed_in+overlay.dops ports in the tree
    actually took, and it is what changed: their steady-state note used to
    read "stale-skipped". Pinning both runs records the change instead of
    leaving it incidental. A quiet removed-for-target means "still absent
    upstream": the marker skips a port only while that holds (poly-7pwa.6).
    """
    freebsd = tmp_path / "freebsd"
    (freebsd / "devel" / "present").mkdir(parents=True)
    (freebsd / "devel" / "present" / "Makefile").write_text("VAR= upstream\n")
    _init_freebsd_repo(freebsd)

    port = tmp_path / "delta" / "ports" / "devel" / "vanished"
    port.mkdir(parents=True)
    (port / "overlay.dops").write_text(
        'port devel/vanished\ntype port\nreason "fixture"\n'
        'target @any\nmk set VAR "x"\n'
    )

    first = _report(_compose(tmp_path, capsys, expect_code=2), "devel/vanished")
    assert "stale-skipped" in first["notes"]
    assert "removed-for-target-skipped" not in first["notes"]
    # The stale policy wrote the marker into the delta tree (poly-7pwa.16).
    assert "@main" in (port / "overlay.toml").read_text()

    second = _report(_compose(tmp_path, capsys), "devel/vanished")
    assert "removed-for-target-skipped" in second["notes"]
    assert "stale-skipped" not in second["notes"]
