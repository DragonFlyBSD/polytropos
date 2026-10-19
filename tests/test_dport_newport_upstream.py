"""A dport's newport/ is its upstream (poly-7pwa.35).

Compose copies newport/ in as the port root and applies overlay.dops to it,
as it does to the FreeBSD tree for any other port. So a fix goes in the
overlay, where the scoping table decides which lines it reaches; an edit to
newport/ reaches every line and nothing can override it.

Two things got this wrong. devel/libcxx22's fix was written into
newport/pkg-plist, and then escalated anyway: assess_overlay counted every
newport/ as unmigrated, whatever the overlay's type, so no dport could reach
`converted` and every successful dport fix went to the Manual Queue
(poly-8fxz).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dportsv3.agent import steps, worker
from dportsv3.agent.dops import assess
from dportsv3.agent.overlay_state import declared_overlay_type
from dportsv3.agent.playbooks import find_playbooks_dir

DPORT = 'port devel/thing\ntype dport\nreason "fixture"\ntarget @any\n'
PORT = 'port devel/thing\ntype port\nreason "fixture"\ntarget @any\n'


def _port(root: Path, overlay: str | None, *, newport: bool) -> Path:
    port = root / "ports" / "devel" / "thing"
    port.mkdir(parents=True)
    if overlay is not None:
        (port / "overlay.dops").write_text(overlay)
    if newport:
        (port / "newport").mkdir()
        (port / "newport" / "Makefile").write_text("PORTNAME= thing\n")
    return port


def _codes(assessment) -> list[str]:
    return [rule.code for rule in assessment.rules]


# --- the assessment: a dport can be converted ------------------------------


def test_a_dport_with_its_newport_is_converted(tmp_path):
    _port(tmp_path, DPORT, newport=True)
    assessment = assess("devel/thing", tmp_path)
    assert assessment.state == "converted"
    assert "dport_newport_upstream" in _codes(assessment)
    assert assessment.unmigrated_artifacts == ()


def test_newport_beside_any_other_overlay_is_still_unmigrated(tmp_path):
    """A type port composes from FreeBSD; a newport/ beside it is left over."""
    _port(tmp_path, PORT, newport=True)
    assessment = assess("devel/thing", tmp_path)
    assert assessment.state != "converted"
    assert "legacy_newport_present" in _codes(assessment)
    assert "newport/" in assessment.unmigrated_artifacts


def test_a_dport_without_newport_is_not_converted(tmp_path):
    """Compose has nothing to copy in for it."""
    _port(tmp_path, DPORT, newport=False)
    assessment = assess("devel/thing", tmp_path)
    assert assessment.state != "converted"
    assert "dport_without_newport" in assessment.invariant_violations


def test_an_overlay_that_does_not_parse_keeps_newport_unmigrated(tmp_path):
    """No declared type to go by: the assessment stays as it was."""
    _port(tmp_path, DPORT + "mk set X <<EOF\n", newport=True)
    assert assess("devel/thing", tmp_path).state != "converted"


def test_newport_without_an_overlay_still_needs_judgment(tmp_path):
    _port(tmp_path, None, newport=True)
    assert assess("devel/thing", tmp_path).state == "needs_judgment"


@pytest.mark.parametrize("text, expected", [
    (DPORT, "dport"),
    (PORT, "port"),
    ('port devel/thing\nreason "no type"\n', "port"),
    ("# type dport\nport devel/thing\n", "port"),
    (DPORT + "mk set X <<EOF\n", None),
])
def test_the_declared_type_is_read_as_compose_reads_it(text, expected):
    assert declared_overlay_type(text) == expected


# --- the chroot probe feeds the same assessment ----------------------------


def _probe(monkeypatch, *, overlay: str | None, cat_rc: int = 0):
    from dportsv3.agent.overlay_state import assess_overlay

    calls = []

    def fake_exec(env, *argv, cwd="/work/DeltaPorts", input_text=None,
                  timeout=None):
        calls.append(argv)
        if argv[2] == 'cat "$DELTAPORTS_ROOT/ports/$1/overlay.dops"':
            return subprocess.CompletedProcess(argv, cat_rc, overlay or "", "")
        dops = "1" if overlay is not None else "0"
        return subprocess.CompletedProcess(
            argv, 0, f"MISSING=0\nDOPS={dops}\nNEWPORT=1\n", "")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    facts = worker.probe_overlay_facts("env", "devel/thing")
    return facts, assess_overlay(facts), calls


def test_the_probe_reads_the_declared_type(monkeypatch):
    facts, assessment, _ = _probe(monkeypatch, overlay=DPORT)
    assert facts.overlay_type == "dport"
    assert assessment.state == "converted"


def test_the_probe_reads_no_overlay_when_there_is_none(monkeypatch):
    facts, _, calls = _probe(monkeypatch, overlay=None)
    assert facts.overlay_type is None
    assert len(calls) == 1


def test_an_unreadable_overlay_has_no_type(monkeypatch):
    facts, assessment, _ = _probe(monkeypatch, overlay=DPORT, cat_rc=1)
    assert facts.overlay_type is None
    assert assessment.state != "converted"


# --- the guard: put_file and edit_file never write newport/ ----------------


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    writable = tmp_path / "writable"
    newport = writable / "work" / "DeltaPorts" / "ports" / "devel" / "thing" / "newport"
    newport.mkdir(parents=True)
    (newport / "pkg-plist").write_text("bin/thing\n")
    fake = worker.EnvPaths(env_dir=tmp_path, writable=writable)
    monkeypatch.setattr(worker, "env_paths", lambda env: fake)
    return newport


@pytest.mark.parametrize("path", [
    "/work/DeltaPorts/ports/devel/thing/newport/pkg-plist",
    "/work/DeltaPorts/ports/devel/thing/newport/files/patch-new",
    "/work/DeltaPorts/ports/devel/thing/./newport/pkg-plist",
    "/work/DeltaPorts/ports/devel/thing/dragonfly/../newport/pkg-plist",
])
def test_put_file_refuses_newport(env_dir, path):
    res = worker.put_file("env", path, "bin/thing\nlib/libthing.so\n")
    assert res["ok"] is False
    assert res["blocked_by"] == "newport_upstream"
    assert (env_dir / "pkg-plist").read_text() == "bin/thing\n"
    assert not (env_dir / "files").exists()


def test_edit_file_refuses_newport(env_dir):
    res = worker.edit_file(
        "env", "/work/DeltaPorts/ports/devel/thing/newport/pkg-plist",
        "bin/thing\n", "bin/thing\nlib/libthing.so\n")
    assert res["ok"] is False
    assert res["blocked_by"] == "newport_upstream"
    assert (env_dir / "pkg-plist").read_text() == "bin/thing\n"


def test_the_refusal_says_where_the_fix_goes(env_dir):
    err = worker.put_file(
        "env", "/work/DeltaPorts/ports/devel/thing/newport/pkg-plist",
        "x\n")["error"]
    assert "/work/DeltaPorts/ports/devel/thing/overlay.dops" in err
    assert "every build line" in err
    for way in ("`text` op", "`install_patches`", "`patch apply`",
                "`file remove <path>`"):
        assert way in err


@pytest.mark.parametrize("path", [
    "/work/DeltaPorts/ports/devel/thing/overlay.dops",
    "/work/DeltaPorts/ports/devel/thing/newport-notes.txt",
    "/work/DeltaPorts/ports/devel/newport/Makefile",
])
def test_other_paths_are_not_newport(path):
    assert worker._reject_newport_write(path, "put_file") is None


# --- the escalation names what blocked it ----------------------------------


def test_the_blockers_are_the_conversion_blockers_with_artifacts(tmp_path):
    _port(tmp_path, PORT, newport=True)
    fake = SimpleNamespace(
        assess_dops=lambda env, origin: assess(origin, tmp_path))
    blockers = steps._dops_blockers(fake, "env", "devel/thing")
    assert "legacy_newport_present (newport/)" in blockers
    assert all("valid_dops_present" not in b for b in blockers)


def test_a_state_with_no_blocking_rule_is_named_by_its_rules(tmp_path):
    (tmp_path / "ports" / "devel" / "thing").mkdir(parents=True)
    fake = SimpleNamespace(
        assess_dops=lambda env, origin: assess(origin, tmp_path))
    assert steps._dops_blockers(fake, "env", "devel/thing") == [
        "no_overlay_artifacts"]


def test_an_unreadable_assessment_has_no_blockers():
    def broken(env, origin):
        raise RuntimeError("no env")
    fake = SimpleNamespace(assess_dops=broken)
    assert steps._dops_blockers(fake, "env", "devel/thing") is None


# --- the brief and the guidance say so before the attempt is spent ---------


@pytest.fixture
def brief_env(tmp_path, monkeypatch):
    port = tmp_path / "DeltaPorts" / "ports" / "devel" / "thing"
    port.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda e: SimpleNamespace(deltaports=tmp_path / "DeltaPorts"))
    worker.set_env_target("brief-env", "@2026Q3")
    yield port
    worker.set_env_target("brief-env", None)


@pytest.mark.parametrize("ops", ["", 'target @2026Q3\nmk set A "1"\n'],
                         ids=["header-only", "with-a-line-block"])
def test_the_brief_names_newport_for_a_dport(brief_env, ops):
    (brief_env / "overlay.dops").write_text(DPORT + ops)
    brief = steps._build_line_brief(worker, "brief-env", "devel/thing")
    assert "`type dport`" in brief
    assert "Do not edit files under it" in brief


def test_the_brief_says_nothing_of_newport_for_a_port(brief_env):
    (brief_env / "overlay.dops").write_text(PORT + 'mk set A "1"\n')
    brief = steps._build_line_brief(worker, "brief-env", "devel/thing")
    assert "newport" not in brief


def test_the_scoping_section_covers_dports():
    text = (find_playbooks_dir() / "flow-patch.md").read_text()
    section = " ".join(text.split("## Scoping", 1)[1]
                       .split("\n## ", 1)[0].split())
    assert "A dport (`type dport`) is no exception." in section
    assert "Never edit a file under `newport/`" in section


def test_the_quickref_no_longer_calls_newport_the_whole_port():
    from dportsv3.paths import AGENT_PLAYBOOKS_DIR

    quickref = AGENT_PLAYBOOKS_DIR.parent / "dops_quickref.md"
    text = " ".join(quickref.read_text().split())
    assert "IS the whole port" not in text
    assert "newport/ is the complete port" not in text
    assert "never an edit under `newport/`" in text
