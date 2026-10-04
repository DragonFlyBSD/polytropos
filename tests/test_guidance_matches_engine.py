"""What the patch playbooks and the quickref say must be what the engine does.

Each test pins the corrected sentence and runs the behaviour it describes,
so a later edit to either side shows up here. The Makefiles are synthetic
and the build-line names are labels, so nothing reads the real tree.

The oracle is off: the default "local" profile runs bmake when one is on
PATH, and bmake fails on a synthetic Makefile that includes <bsd.port.mk>.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import worker
from dportsv3.engine.api import apply_dsl, build_plan
from dportsv3.paths import AGENT_PLAYBOOKS_DIR

HEAD = 'port devel/thing\ntype port\nreason "fixture"\n'
PLAIN = "PORTNAME=\tthing\n\n.include <bsd.port.mk>\n"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _flow() -> str:
    return _flat((AGENT_PLAYBOOKS_DIR / "flow-patch.md").read_text())


def _quickref() -> str:
    """The quickref as the agent receives it."""
    return _flat(worker.dops_reference("any-env")["content"])


def _apply(tmp_path, body, target="@main", makefile=None, name="port"):
    """Apply HEAD + body in a fresh port root; return (result, Makefile)."""
    root = tmp_path / name
    root.mkdir()
    if makefile is not None:
        (root / "Makefile").write_text(makefile)
    result = apply_dsl(HEAD + body, source_path=None, port_root=root,
                       target=target, oracle_profile="off")
    text = (root / "Makefile").read_text() if makefile is not None else None
    return result, text


def _op_codes(result) -> list[str]:
    """An op's codes are on its row; result.diagnostics is apply-level."""
    return [d.code for row in result.op_results for d in row.diagnostics]


def test_scope_is_not_offered_as_a_cure_for_an_ambiguous_match(tmp_path):
    flow = _flow()
    assert "disambiguation lever" not in flow
    assert "Narrow it with scope" not in flow
    assert "a `target` block does not change that" in flow
    twice = "PORTNAME=\tthing\nFOO=\ta\nFOO=\tb\n\n.include <bsd.port.mk>\n"
    result, _ = _apply(tmp_path, 'target @main\nmk set FOO "z"\n',
                       makefile=twice)
    assert not result.ok
    assert "E_APPLY_AMBIGUOUS_MATCH" in _op_codes(result)


ANGIE = (
    "PORTNAME=\tangie\n"
    ".if ${FLAVOR} == a\n"
    "V=\t1\n"
    "PORTREVISION=\t${R}\n"
    ".  endif\n"
    ".if ${FLAVOR} == b\n"
    "V=\t1\n"
    "PORTREVISION=\t${R}\n"
    ".  else\n"
    "X=\t2\n"
    ".endif\n"
    "\n.include <bsd.port.mk>\n"
)


def test_a_widened_from_string_resolves_a_repeated_assignment(tmp_path):
    """www/angie's shape: the line and the line above it both repeat, so
    only a from string extended with the following line occurs once."""
    assert "occurs once in the file" in _flow()

    def replace(from_, name):
        body = (
            "target @any\n"
            f'text replace-once file Makefile from "{from_}" to "X"\n'
        )
        return _apply(tmp_path, body, makefile=ANGIE, name=name)

    line = "PORTREVISION=\\t${R}"
    result, _ = replace(line, "line")
    assert not result.ok
    assert "E_APPLY_AMBIGUOUS_MATCH" in _op_codes(result)
    result, _ = replace("V=\\t1\\n" + line, "above")
    assert not result.ok
    assert "E_APPLY_AMBIGUOUS_MATCH" in _op_codes(result)
    result, text = replace(line + "\\n.  else", "widened")
    assert result.ok, _op_codes(result)
    assert text.count("PORTREVISION=\t${R}") == 1


@pytest.mark.parametrize("rel", [
    "dragonfly/patch-x.c", "dragonfly/@main/patch-x.c", "overlay.dops",
])
def test_the_patch_steps_do_not_forbid_writing_under_ports(rel):
    assert "to `ports/<origin>/` it is not" not in _flow()
    path = f"/work/DeltaPorts/ports/devel/thing/{rel}"
    assert worker._reject_dports_write(path) is None
    assert worker._reject_makefile_dragonfly_authoring(path) is None
    assert worker._reject_orphan_dops_write(path) is None
    refused = worker._reject_makefile_dragonfly_authoring(
        "/work/DeltaPorts/ports/devel/thing/Makefile.DragonFly")
    assert refused is not None and refused["ok"] is False


@pytest.mark.parametrize("policy", ["", " on-missing error",
                                    " on-missing warn", " on-missing noop"])
def test_mk_set_inserts_an_absent_variable_whatever_on_missing_says(
    tmp_path, policy,
):
    quickref = _quickref()
    assert "fail if not found" not in quickref
    assert ("on-missing only matters if the Makefile itself is missing"
            in quickref)
    result, text = _apply(tmp_path, f'target @any\nmk set NEWVAR "x"{policy}\n',
                          makefile=PLAIN)
    assert result.ok
    assert [r.status for r in result.op_results] == ["applied"]
    assert result.diagnostics == []
    assert _op_codes(result) == []
    assert "NEWVAR= x" in text.splitlines()
    result, _ = _apply(tmp_path, 'target @any\nmk set NEWVAR "x"\n',
                       name="nomakefile")
    assert not result.ok
    assert "E_APPLY_MISSING_SUBJECT" in _op_codes(result)


def test_on_missing_guidance_keeps_error(tmp_path):
    quickref = _quickref()
    assert "idempotent across targets" not in quickref
    assert "file remove files/patch-stale on-missing warn" not in quickref
    assert "Keep the default" in quickref
    scoping = _flow().split("## Scoping", 1)[1].split(" ## ", 1)[0]
    assert "Leave `on-missing` at its default, `error`" in scoping
    op = 'target @any\ntext replace-once file Makefile from "NOPE" to "x"'
    result, _ = _apply(tmp_path, op + "\n", makefile=PLAIN, name="default")
    assert not result.ok
    assert "E_APPLY_MISSING_SUBJECT" in _op_codes(result)
    result, _ = _apply(tmp_path, op + " on-missing warn\n", makefile=PLAIN,
                       name="warn")
    assert result.ok
    assert [r.status for r in result.op_results] == ["skipped"]
    assert "W_APPLY_ON_MISSING_WARN" in _op_codes(result)


def test_the_comma_list_names_exactly_its_lines(tmp_path):
    """@2099Q1 stands for a line branched later, which the list omits."""
    assert ("`target @2026Q3,@main` (no space) is exactly those two"
            in _quickref())
    body = 'target @2026Q3,@main\nmk set FOO "x"\n'
    for target, lands in (("@main", True), ("@2026Q3", True),
                          ("@2099Q1", False)):
        result, text = _apply(tmp_path, body, target=target, makefile=PLAIN,
                              name=target.lstrip("@"))
        assert result.ok, (target, _op_codes(result))
        assert ("FOO= x" in text.splitlines()) is lands, target
    planned = build_plan(HEAD + 'target @2026Q3, @main\nmk set FOO "x"\n', None)
    assert not planned.ok
    assert "E_PARSE_EXPECTED_NEWLINE" in [d.code for d in planned.diagnostics]
