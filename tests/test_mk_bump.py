"""mk bump PORTREVISION: a revision relative to the line's upstream.

An absolute ``mk set PORTREVISION "N"`` pins every build line it applies
to: compose re-seeds the port from upstream and applies the op again, so
every later upstream bump on that line is reverted (poly-7pwa.12).
``mk bump PORTREVISION [by N]`` adds N to the value the line holds when the
op runs, so one ``@any`` op is right on every line, and the same plan gives
the same value on every compose (poly-7pwa.18).

Every apply here runs with the oracle off: the default profile runs bmake
when it is on PATH, and these synthetic Makefiles are not real ports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3.engine import emit
from dportsv3.engine.api import apply_dsl, build_plan

HEAD = 'port a/b\ntype port\nreason "fixture"\n'

MK = (
    "PORTNAME=\tx\nPORTVERSION=\t1.0\n{rev}CATEGORIES=\tdevel\n"
    "\n.include <bsd.port.mk>\n"
)


def _plan(ops: str):
    return build_plan(HEAD + ops, None)


def _apply(tmp_path: Path, ops: str, makefile: str, *, target: str = "@main",
           name: str = "port"):
    port = tmp_path / name
    port.mkdir(parents=True)
    (port / "Makefile").write_text(makefile)
    result = apply_dsl(
        HEAD + ops, port_root=port, target=target, oracle_profile="off",
    )
    return result, (port / "Makefile").read_text()


def _errors(result) -> list[tuple[str, str]]:
    return [
        (d.code, d.message)
        for row in result.op_results
        for d in row.diagnostics
        if d.severity == "error"
    ]


def test_bump_parses_and_plans():
    result = _plan("mk bump PORTREVISION\n")
    assert result.ok, result.diagnostics
    assert [(op.kind, op.payload) for op in result.plan.ops] == [
        ("mk.var.bump", {"name": "PORTREVISION", "by": 1})
    ]

    result = _plan("mk bump PORTREVISION by 3\n")
    assert result.ok, result.diagnostics
    assert [(op.kind, op.payload) for op in result.plan.ops] == [
        ("mk.var.bump", {"name": "PORTREVISION", "by": 3})
    ]


def test_bump_rejects_other_variables():
    result = _plan("mk bump PORTEPOCH\n")
    assert not result.ok
    assert [(d.code, d.message) for d in result.diagnostics] == [
        ("E_SEM_INVALID_OPERATION_STATE", "mk bump supports PORTREVISION only")
    ]


@pytest.mark.parametrize("amount", ["0", "100", "x"])
def test_bump_rejects_bad_amounts(amount):
    result = _plan(f"mk bump PORTREVISION by {amount}\n")
    assert not result.ok
    assert [(d.code, d.message) for d in result.diagnostics] == [
        (
            "E_SEM_INVALID_OPERATION_STATE",
            "mk bump by takes an integer from 1 to 99",
        )
    ]


def test_bump_rejects_a_trailing_on_missing():
    result = _plan("mk bump PORTREVISION on-missing warn\n")
    assert not result.ok
    assert [d.code for d in result.diagnostics] == ["E_PARSE_UNEXPECTED_TOKEN"]
    assert result.diagnostics[0].message.startswith(
        "unexpected token after mk bump"
    )


def test_bump_adds_to_upstream(tmp_path):
    result, out = _apply(
        tmp_path, "mk bump PORTREVISION\n", MK.format(rev="PORTREVISION=\t2\n")
    )
    assert result.ok, _errors(result)
    assert out == MK.format(rev="PORTREVISION= 3\n")


def test_bump_keeps_a_conditional_default(tmp_path):
    """A ?= default stays ?=, so a slave can still override it."""
    result, out = _apply(
        tmp_path, "mk bump PORTREVISION\n", MK.format(rev="PORTREVISION?= 0\n")
    )
    assert result.ok, _errors(result)
    assert out == MK.format(rev="PORTREVISION?= 1\n")


def test_bump_inserts_where_mk_set_would(tmp_path):
    bumped_result, bumped = _apply(
        tmp_path, "mk bump PORTREVISION\n", MK.format(rev=""), name="bump"
    )
    set_result, set_ = _apply(
        tmp_path, 'mk set PORTREVISION "1"\n', MK.format(rev=""), name="set"
    )
    assert bumped_result.ok and set_result.ok
    assert bumped == set_
    assert "PORTREVISION= 1\n" in bumped


def test_bump_refuses_slaves(tmp_path):
    result, out = _apply(
        tmp_path,
        "mk bump PORTREVISION\n",
        "PORTNAME=\tx\nMASTERDIR=\t${.CURDIR}/../y\n\n"
        '.include "${MASTERDIR}/Makefile"\n',
    )
    assert not result.ok
    [(code, message)] = _errors(result)
    assert code == "E_APPLY_BUMP_UNSUPPORTED"
    assert message.startswith("slave port")
    assert "PORTREVISION" not in out


@pytest.mark.parametrize(
    "rev, code, why",
    [
        (
            "PORTREVISION=\t1\nPORTREVISION=\t2\n",
            "E_APPLY_AMBIGUOUS_MATCH",
            "multiple assignments found for PORTREVISION",
        ),
        (
            ".if ${FOO}\nPORTREVISION=\t2\n.endif\n",
            "E_APPLY_BUMP_UNSUPPORTED",
            "PORTREVISION is assigned inside a conditional",
        ),
        (
            "PORTREVISION+=\t2\n",
            "E_APPLY_BUMP_UNSUPPORTED",
            "PORTREVISION uses +=; only = and ?= can be bumped",
        ),
        (
            "PORTREVISION=\t${X} # c\n",
            "E_APPLY_BUMP_UNSUPPORTED",
            "PORTREVISION is '${X}', not an integer",
        ),
    ],
    ids=["ambiguous", "conditional", "operator", "non-integer"],
)
def test_bump_refuses_ambiguous_conditional_operator_and_non_integer(
    tmp_path, rev, code, why
):
    makefile = MK.format(rev=rev)
    result, out = _apply(tmp_path, "mk bump PORTREVISION\n", makefile)
    assert not result.ok
    assert _errors(result) == [(code, why)]
    assert out == makefile


def test_bump_refuses_when_a_quoted_include_may_set_it(tmp_path):
    makefile = (
        "PORTNAME=\tx\n\n"
        '.include "${.CURDIR}/../x/Makefile.common"\n'
        ".include <bsd.port.mk>\n"
    )
    result, out = _apply(
        tmp_path, "mk bump PORTREVISION\n", makefile, name="quoted"
    )
    assert not result.ok
    assert _errors(result) == [
        (
            "E_APPLY_BUMP_UNSUPPORTED",
            "PORTREVISION may be set by an included file "
            '("${.CURDIR}/../x/Makefile.common")',
        )
    ]
    assert out == makefile

    result, out = _apply(
        tmp_path,
        "mk bump PORTREVISION\n",
        "PORTNAME=\tx\n\n.include <bsd.port.mk>\n",
        name="framework",
    )
    assert result.ok, _errors(result)
    assert out == "PORTNAME=\tx\n\nPORTREVISION= 1\n.include <bsd.port.mk>\n"


def test_bumps_add_in_apply_order(tmp_path):
    """@any runs first, then the line's own; both bumps count."""
    ops = "mk bump PORTREVISION\ntarget @main\nmk bump PORTREVISION by 2\n"
    makefile = MK.format(rev="PORTREVISION=\t4\n")
    _, main = _apply(tmp_path, ops, makefile, target="@main", name="main")
    _, q3 = _apply(tmp_path, ops, makefile, target="@2026Q3", name="q3")
    assert main == MK.format(rev="PORTREVISION= 7\n")
    assert q3 == MK.format(rev="PORTREVISION= 5\n")

    result, out = _apply(
        tmp_path,
        'mk set PORTREVISION "5"\nmk bump PORTREVISION\n',
        makefile,
        name="set-then-bump",
    )
    assert result.ok, _errors(result)
    assert out == MK.format(rev="PORTREVISION= 6\n")


def test_bump_is_idempotent_across_composes(tmp_path):
    """Compose applies the plan to a fresh upstream copy every time."""
    makefile = MK.format(rev="PORTREVISION=\t2\n")
    _, first = _apply(tmp_path, "mk bump PORTREVISION\n", makefile, name="1")
    _, second = _apply(tmp_path, "mk bump PORTREVISION\n", makefile, name="2")
    assert first == second == MK.format(rev="PORTREVISION= 3\n")


GDAL = (
    "PORTNAME=\tgdal\nPORTVERSION=\t{version}\n{rev}"
    "CATEGORIES=\tgraphics geography\n"
    "\nCMAKE_OFF=\tBUILD_CSHARP_BINDINGS\nCMAKE_ON=\tAVIF_VERSION_CHECK\n"
    "\n.include <bsd.port.options.mk>\n"
    "\npost-patch:\n\t@${{REINPLACE_CMD}} -e 's|x|y|' ${{WRKSRC}}/f\n"
    "\n.include <bsd.port.mk>\n"
)
GDAL_OPS = (
    'mk set USE_GCC_VERSION "${GCC_DEFAULT}"\n'
    'mk eval CMAKE_ON "${CMAKE_ON:NENABLE_IPO:NGDAL_HIDE_INTERNAL_SYMBOLS}"\n'
    'mk add CMAKE_OFF "ENABLE_IPO GDAL_HIDE_INTERNAL_SYMBOLS"\n'
    'mk add CFLAGS "-DGDAL_DISABLE_FLOAT16"\n'
    'mk add CXXFLAGS "-DGDAL_DISABLE_FLOAT16"\n'
)


def test_gdal_migration_is_byte_identical(tmp_path):
    """The operator's migration of graphics/gdal's live pin changes nothing.

    Upstream (2026-09) had no PORTREVISION on main (3.13.3) and 2 on
    2026Q3 (3.13.1); the live @any ``mk set PORTREVISION "3"`` composes 3
    on both. The additive form composes the same whole Makefile.
    """
    old = GDAL_OPS + 'mk set PORTREVISION "3"\n'
    new = (
        GDAL_OPS
        + "mk bump PORTREVISION\n"
        + "target @main\nmk bump PORTREVISION by 2\n"
    )
    lines = {
        "@main": GDAL.format(version="3.13.3", rev=""),
        "@2026Q3": GDAL.format(version="3.13.1", rev="PORTREVISION=\t2\n"),
    }
    for target, makefile in lines.items():
        r_old, before = _apply(
            tmp_path, old, makefile, target=target, name=f"old{target}"
        )
        r_new, after = _apply(
            tmp_path, new, makefile, target=target, name=f"new{target}"
        )
        assert r_old.ok and r_new.ok, (_errors(r_old), _errors(r_new))
        assert after == before, target
        assert "PORTREVISION= 3\n" in after


def test_emit_renders_bump():
    assert emit.mk_bump("PORTREVISION") == "mk bump PORTREVISION"
    assert emit.mk_bump("PORTREVISION", by=3) == "mk bump PORTREVISION by 3"
    for by in (1, 3):
        result = _plan(emit.mk_bump("PORTREVISION", by=by) + "\n")
        assert result.ok, result.diagnostics
        assert [op.payload for op in result.plan.ops] == [
            {"name": "PORTREVISION", "by": by}
        ]
