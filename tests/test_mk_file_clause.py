"""mk ops take ``file <path>``, and refuse a value an include replaces.

Every mk executor resolved ``Makefile`` and nothing else, so a variable a
port sets in a file it includes from its own directory could only be
edited with text ops -- x11-servers/xlibre-server keeps its dependencies
in Makefile.common. Worse, an mk op on such a variable wrote ``VAR= value``
above the include, the include's ``VAR=`` replaced it, and apply reported
success: multimedia/avidemux's ``mk add USES "alias"`` never reached the
build (poly-bz7n.2).

Every apply here runs with the oracle off: these synthetic Makefiles are
not real ports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3.agent.scope_check import any_overrides
from dportsv3.engine import emit
from dportsv3.engine.api import apply_dsl, build_plan

HEAD = 'port a/b\ntype port\nreason "fixture"\n'

MAKEFILE = (
    "PORTNAME=\tx\n"
    "PORTVERSION=\t1.0\n"
    "\n"
    '.include "${.CURDIR}/Makefile.common"\n'
    ".include <bsd.port.mk>\n"
)

COMMON = (
    "USES=\t\tcmake pkgconfig\n"
    "BUILD_DEPENDS+=\tfoo>0:devel/foo\n"
    "\n"
    "post-patch:\n"
    "\t@${TRUE}\n"
)


def _port(tmp_path: Path, files: dict[str, str], name: str = "port") -> Path:
    port = tmp_path / name
    port.mkdir(parents=True)
    for rel, text in files.items():
        (port / rel).parent.mkdir(parents=True, exist_ok=True)
        (port / rel).write_text(text)
    return port


def _apply(port: Path, ops: str, *, target: str = "@main"):
    return apply_dsl(
        HEAD + ops, port_root=port, target=target, oracle_profile="off",
    )


def _errors(result) -> list[tuple[str, str]]:
    return [
        (d.code, d.message)
        for row in result.op_results
        for d in row.diagnostics
        if d.severity == "error"
    ]


def _payloads(ops: str) -> list[tuple[str, dict]]:
    result = build_plan(HEAD + ops, None)
    assert result.ok, result.diagnostics
    return [(op.kind, op.payload) for op in result.plan.ops]


# --- grammar and plan -------------------------------------------------------

EVERY_MK_OP = [
    ('mk set X "v" file F.mk', "mk.var.set"),
    ('mk eval X "${X:Na}" file F.mk', "mk.var.eval"),
    ('mk shell X "echo v" file F.mk', "mk.var.shell"),
    ("mk unset X file F.mk on-missing warn", "mk.var.unset"),
    ('mk add X "t" file F.mk on-missing warn', "mk.var.token_add"),
    ('mk remove X "t" file F.mk on-missing warn', "mk.var.token_remove"),
    ("mk bump PORTREVISION by 2 file F.mk", "mk.var.bump"),
    ("mk ensure-include bsd.port.options.mk file F.mk", "mk.include.ensure"),
    ('mk disable-if condition "${A}" contains "B" file F.mk on-missing warn',
     "mk.block.disable"),
    ('mk replace-if from "${A}" to "${B}" file F.mk', "mk.block.replace_condition"),
    ("mk block set condition \"${A}\" contains \"B\" file F.mk <<'MK'\nX=1\nMK",
     "mk.block.set"),
    ("mk target set post-patch file F.mk <<'MK'\n\t@true\nMK", "mk.target.set"),
    ("mk target append post-patch file F.mk <<'MK'\n\t@true\nMK",
     "mk.target.append"),
    ("mk target remove post-patch file F.mk on-missing warn", "mk.target.remove"),
    ("mk target rename a -> b file F.mk", "mk.target.rename"),
]


@pytest.mark.parametrize("line, kind", EVERY_MK_OP, ids=[k for _, k in EVERY_MK_OP])
def test_every_mk_op_takes_a_file(line, kind):
    [(planned_kind, payload)] = _payloads(line + "\n")
    assert planned_kind == kind
    assert payload["file"] == "F.mk"


@pytest.mark.parametrize("line, kind", EVERY_MK_OP, ids=[k for _, k in EVERY_MK_OP])
def test_without_a_file_the_plan_is_what_it_was(line, kind):
    [(_, payload)] = _payloads(line.replace(" file F.mk", "") + "\n")
    assert "file" not in payload


def test_file_comes_before_on_missing():
    result = build_plan(HEAD + 'mk add X "t" on-missing warn file F.mk\n', None)
    assert not result.ok
    assert [d.code for d in result.diagnostics] == ["E_PARSE_EXPECTED_NEWLINE"]


def test_a_variable_named_file_is_still_a_variable():
    assert _payloads("mk unset file\n") == [("mk.var.unset", {"name": "file"})]
    assert _payloads('mk add USES file\n') == [
        ("mk.var.token_add", {"name": "USES", "value": "file"})
    ]


EMITTED = [
    emit.mk_set("X", "v", file="F.mk"),
    emit.mk_eval("X", "${X:Na}", file="F.mk"),
    emit.mk_shell("X", "echo v", file="F.mk"),
    emit.mk_unset("X", on_missing="warn", file="F.mk"),
    emit.mk_add("X", "t", on_missing="warn", file="F.mk"),
    emit.mk_remove("X", "t", on_missing="warn", file="F.mk"),
    emit.mk_bump("PORTREVISION", by=2, file="F.mk"),
    emit.mk_ensure_include("bsd.port.options.mk", file="F.mk"),
    emit.mk_disable_if("${A}", contains="B", on_missing="warn", file="F.mk"),
    emit.mk_replace_if("${A}", "${B}", contains="B", file="F.mk"),
    emit.mk_block_set("${A}", ["X=1"], contains="B", file="F.mk"),
    emit.mk_target_set("post-patch", ["\t@true"], file="F.mk"),
    emit.mk_target_append("post-patch", ["\t@true"], file="F.mk"),
    emit.mk_target_remove("post-patch", on_missing="warn", file="F.mk"),
    emit.mk_target_rename("a", "b", on_missing="warn", file="F.mk"),
]


@pytest.mark.parametrize("line", EMITTED)
def test_emit_writes_the_file_and_it_plans_back(line):
    [(_, payload)] = _payloads(line + "\n")
    assert payload["file"] == "F.mk"


def test_emit_without_a_file_is_unchanged():
    assert emit.mk_add("X", "t") == 'mk add X "t"'
    assert emit.mk_target_set("t", ["\t@true"]) == "mk target set t <<'MK'\n\t@true\nMK"


# --- apply ------------------------------------------------------------------

def test_an_op_with_a_file_edits_that_file(tmp_path):
    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": COMMON})
    result = _apply(port, (
        'mk add USES "alias" file Makefile.common\n'
        'mk remove BUILD_DEPENDS "foo>0:devel/foo" file Makefile.common\n'
        "mk target append post-patch file Makefile.common <<'MK'\n"
        "\t@${ECHO_MSG} done\n"
        "MK\n"
    ))
    assert result.ok, _errors(result)
    assert (port / "Makefile").read_text() == MAKEFILE
    assert (port / "Makefile.common").read_text() == (
        "USES= cmake pkgconfig alias\n"
        "\n"
        "post-patch:\n"
        "\t@${TRUE}\n"
        "\t@${ECHO_MSG} done\n"
    )


SUBJECT = (
    "X=\t\told\n"
    "Y+=\t\tt\n"
    "PORTREVISION=\t1\n"
    ".if ${A}\n"
    "Z=\t1\n"
    ".endif\n"
    "post-patch:\n"
    "\t@${TRUE}\n"
)

APPLY_EACH = [
    'mk set X "new"',
    'mk eval X "${X} more"',
    'mk shell W "echo w"',
    "mk unset X",
    'mk add Y "u"',
    'mk remove Y "t"',
    "mk bump PORTREVISION",
    "mk ensure-include bsd.port.options.mk",
    'mk disable-if condition "${A}"',
    'mk replace-if from "${A}" to "${B}"',
    "mk block set condition \"${A}\" <<'MK'\nZ=\t2\nMK",
    "mk target set post-patch <<'MK'\n\t@${ECHO_MSG} set\nMK",
    "mk target append post-patch <<'MK'\n\t@${ECHO_MSG} more\nMK",
    "mk target remove post-patch",
    "mk target rename post-patch -> pre-patch",
]


def _with_file(op: str) -> str:
    head, sep, rest = op.partition(" <<")
    return f"{head} file Makefile.common{sep}{rest}"


@pytest.mark.parametrize("op", APPLY_EACH, ids=[o.split("\n")[0] for o in APPLY_EACH])
def test_every_mk_op_applies_to_its_file(tmp_path, op):
    """With the file, the op changes Makefile.common and leaves the
    Makefile alone; without it, the op changes the Makefile instead."""
    files = {"Makefile": SUBJECT, "Makefile.common": SUBJECT}
    port = _port(tmp_path, files, "with")
    result = _apply(port, _with_file(op) + "\n")
    assert result.ok, _errors(result)
    assert (port / "Makefile").read_text() == SUBJECT
    with_file = (port / "Makefile.common").read_text()
    assert with_file != SUBJECT

    port = _port(tmp_path, files, "without")
    assert _apply(port, op + "\n").ok
    assert (port / "Makefile.common").read_text() == SUBJECT
    assert (port / "Makefile").read_text() == with_file


def test_file_makefile_is_the_default(tmp_path):
    a = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": COMMON}, "a")
    b = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": COMMON}, "b")
    assert _apply(a, 'mk set PORTREVISION "1"\n').ok
    assert _apply(b, 'mk set PORTREVISION "1" file Makefile\n').ok
    assert (a / "Makefile").read_text() == (b / "Makefile").read_text()


def test_add_creates_with_plus_equals_in_an_included_file(tmp_path):
    """The including file may have set the variable before the include;
    `=` in the included file would throw that away."""
    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": COMMON})
    result = _apply(port, 'mk add RUN_DEPENDS "bar>0:devel/bar" file Makefile.common\n')
    assert result.ok, _errors(result)
    assert "RUN_DEPENDS+= bar>0:devel/bar\n" in (port / "Makefile.common").read_text()

    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": COMMON}, "mk")
    assert _apply(port, 'mk add RUN_DEPENDS "bar>0:devel/bar"\n').ok
    assert "RUN_DEPENDS= bar>0:devel/bar\n" in (port / "Makefile").read_text()


@pytest.mark.parametrize("path", ["/etc/Makefile.common", "../other/Makefile.common"])
def test_a_path_outside_the_port_root_is_refused(tmp_path, path):
    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": COMMON})
    result = _apply(port, f'mk add USES "alias" file {path}\n')
    assert [code for code, _ in _errors(result)] == ["E_APPLY_INVALID_PATH"]


def test_a_missing_file_is_named(tmp_path):
    port = _port(tmp_path, {"Makefile": MAKEFILE})
    result = _apply(port, 'mk add USES "alias" file Makefile.common\n')
    assert ("E_APPLY_MISSING_SUBJECT", "Makefile.common does not exist") in [
        (d.code, d.message)
        for row in result.op_results for d in row.diagnostics
    ]


# --- dead-op rule -----------------------------------------------------------

def test_removing_the_file_an_mk_op_edits_kills_it():
    result = build_plan(HEAD + (
        'mk add USES "alias" file Makefile.common\n'
        "file remove Makefile.common\n"
    ), None)
    assert [d.code for d in result.diagnostics] == ["E_SEM_DEAD_OP"]

    result = build_plan(HEAD + (
        'mk add USES "alias"\n'
        "file remove Makefile.common\n"
    ), None)
    assert result.ok, result.diagnostics


# --- the guard: an include that replaces the op's value -----------------------

@pytest.mark.parametrize("operator", ["=", ":=", "!="])
def test_an_included_replacing_assignment_refuses_the_op(tmp_path, operator):
    common = f"USES{operator}\tcmake\n"
    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": common})
    result = _apply(port, 'mk add USES "alias"\n')
    [(code, message)] = _errors(result)
    assert code == "E_APPLY_MK_OVERRIDDEN"
    assert f"USES{operator} in Makefile.common:1" in message
    assert "add 'file Makefile.common'" in message
    assert (port / "Makefile").read_text() == MAKEFILE


@pytest.mark.parametrize(
    "common",
    ["USES+=\tcmake\n", "USES?=\tcmake\n", ".if ${X}\nUSES=\tcmake\n.endif\n"],
    ids=["+=", "?=", "inside .if"],
)
def test_an_include_that_keeps_the_value_passes(tmp_path, common):
    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": common})
    result = _apply(port, 'mk add USES "alias"\n')
    assert result.ok, _errors(result)


@pytest.mark.parametrize(
    "ops",
    [
        'mk set USES "alias"\n',
        'mk eval USES "${USES} alias"\n',
        'mk shell USES "echo alias"\n',
    ],
    ids=["set", "eval", "shell"],
)
def test_every_assignment_writing_op_is_guarded(tmp_path, ops):
    """eval and shell land before the last include; the include of
    Makefile.common is earlier here only when it is the last one."""
    makefile = (
        "PORTNAME=\tx\n\n"
        '.include "${.CURDIR}/Makefile.common"\n'
    )
    port = _port(tmp_path, {"Makefile": makefile, "Makefile.common": "USES=\tcmake\n"})
    assert [c for c, _ in _errors(_apply(port, ops))] == ["E_APPLY_MK_OVERRIDDEN"]


def test_an_include_before_the_written_line_is_not_checked(tmp_path):
    """mk eval lands before the last include, after Makefile.common here, so
    Makefile.common's USES= comes first and the op's value stands."""
    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": "USES=\tcmake\n"})
    result = _apply(port, 'mk eval USES "${USES} alias"\n')
    assert result.ok, _errors(result)


def test_a_rewritten_assignment_is_guarded_too(tmp_path):
    makefile = (
        "PORTNAME=\tx\nPORTREVISION=\t1\n\n"
        '.include "${.CURDIR}/Makefile.common"\n'
        ".include <bsd.port.mk>\n"
    )
    port = _port(
        tmp_path, {"Makefile": makefile, "Makefile.common": "PORTREVISION=\t4\n"}
    )
    for ops in ('mk set PORTREVISION "2"\n', "mk bump PORTREVISION\n"):
        assert [c for c, _ in _errors(_apply(port, ops))] == [
            "E_APPLY_MK_OVERRIDDEN"
        ]


def test_nested_and_relative_includes_are_followed(tmp_path):
    common = 'BUILD_DEPENDS+=\tfoo>0:devel/foo\n.include "Makefile.inc"\n'
    port = _port(tmp_path, {
        "Makefile": MAKEFILE,
        "Makefile.common": common,
        "Makefile.inc": "USES=\tcmake\n",
    })
    [(code, message)] = _errors(_apply(port, 'mk add USES "alias"\n'))
    assert code == "E_APPLY_MK_OVERRIDDEN"
    assert "USES= in Makefile.inc:1" in message


def test_the_guard_applies_inside_an_included_file(tmp_path):
    common = 'BUILD_DEPENDS+=\tfoo>0:devel/foo\n.include "${.PARSEDIR}/Makefile.inc"\n'
    port = _port(tmp_path, {
        "Makefile": MAKEFILE,
        "Makefile.common": common,
        "Makefile.inc": "USES=\tcmake\n",
    })
    [(code, message)] = _errors(
        _apply(port, 'mk add USES "alias" file Makefile.common\n')
    )
    assert code == "E_APPLY_MK_OVERRIDDEN"
    assert "Makefile.inc:1" in message


@pytest.mark.parametrize(
    "include",
    [
        '"${.CURDIR}/../other/Makefile.common"',
        '"${MASTERDIR}/Makefile.common"',
        "<bsd.port.options.mk>",
    ],
    ids=["outside the port root", "other variable", "framework"],
)
def test_includes_the_guard_cannot_read_are_not_followed(tmp_path, include):
    makefile = f"PORTNAME=\tx\n\n.include {include}\n.include <bsd.port.mk>\n"
    port = _port(tmp_path, {"Makefile": makefile}, "port")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "Makefile.common").write_text("USES=\tcmake\n")
    result = _apply(port, 'mk add USES "alias"\n')
    assert result.ok, _errors(result)


def test_an_include_inside_if_is_not_followed(tmp_path):
    makefile = (
        "PORTNAME=\tx\n\n"
        '.if ${X}\n.include "${.CURDIR}/Makefile.common"\n.endif\n'
        ".include <bsd.port.mk>\n"
    )
    port = _port(tmp_path, {"Makefile": makefile, "Makefile.common": "USES=\tcmake\n"})
    assert _apply(port, 'mk add USES "alias"\n').ok


def test_the_guard_reads_what_earlier_ops_staged(tmp_path):
    """A text op that turns the include's USES= into USES+= runs first, so
    by the time mk add runs nothing replaces its value."""
    port = _port(tmp_path, {"Makefile": MAKEFILE, "Makefile.common": "USES=\tcmake\n"})
    result = _apply(port, (
        'text replace-once file Makefile.common from "USES=" to "USES+="\n'
        'mk add USES "alias"\n'
    ))
    assert result.ok, _errors(result)


def test_avidemux_shape(tmp_path):
    """multimedia/avidemux on freebsd-ports main 9ff6dfff4e55: no USES in
    the Makefile, USES= then USES+= in Makefile.common."""
    makefile = (
        "PORTNAME=\tavidemux\n\n"
        '.include "${.CURDIR}/Makefile.common"\n\n'
        ".include <bsd.port.post.mk>\n"
    )
    common = (
        "USES=\t\tcmake:noninja pkgconfig iconv gmake\n"
        "USES+=\t\tdos2unix gnome\n"
        "CXXFLAGS+=\t-I${LOCALBASE}/include\n"
        "\n"
        ".include <bsd.port.pre.mk>\n"
    )
    port = _port(tmp_path, {"Makefile": makefile, "Makefile.common": common})
    result = _apply(port, 'mk add USES "alias"\nmk add CXXFLAGS "-fpermissive"\n')
    assert [c for c, _ in _errors(result)] == ["E_APPLY_MK_OVERRIDDEN"]

    port = _port(
        tmp_path, {"Makefile": makefile, "Makefile.common": common}, "fixed"
    )
    result = _apply(port, (
        'mk add USES "alias" file Makefile.common\n'
        'mk add CXXFLAGS "-fpermissive"\n'
    ))
    assert result.ok, _errors(result)
    assert "USES+= alias\n" in (port / "Makefile.common").read_text()


# --- scope_check -------------------------------------------------------------

def test_an_op_in_another_file_does_not_override_the_any_op():
    before = HEAD + 'target @any\nmk set X "1"\n'
    after = before + 'target @main\nmk set X "2" file Makefile.common\n'
    assert any_overrides(before, after, "@main") == ()

    after = before + 'target @main\nmk set X "2"\n'
    assert len(any_overrides(before, after, "@main")) == 1
