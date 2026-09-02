"""``mk eval OPTIONS_*`` must land before the options are computed (poly-cpd).

``exec_mk_var_eval`` appended its assignment before the *last* include. In a
terminal-only port that is ``.include <bsd.port.mk>`` and the placement is
right: options are computed afterwards, so the assignment counts.

In a *sandwiched* port — one including ``bsd.port.options.mk`` or
``bsd.port.pre.mk`` partway down — the last include is still the trailing
``bsd.port.mk``, so the assignment landed *after* ``PORT_OPTIONS`` had
already been derived and did nothing. Silently: no warning, no failed op,
just filtering that never applied. poly-cpd measured ~79 overlays in that
state.

Confirmed on hardware before this fix: devel/libunwind's overlay declares
``mk eval OPTIONS_DEFINE`` ahead of its ``mk set TESTS_CONFIGURE_ON``
helpers, and compose emitted them the other way round — helpers at lines
43-44, the include at 45, ``OPTIONS_DEFINE:=`` at 55. DragonFly's
``Mk/bsd.sanity.mk`` then failed check-sanity with "The following options
helpers are incorrectly set after bsd.port.options.mk and are ineffective:
TESTS_CONFIGURE_OFF TESTS_CONFIGURE_ON".
"""

from __future__ import annotations

from pathlib import Path

from dportsv3.engine.apply import apply_plan
from dportsv3.engine.models import Plan, PlanOp


def _eval_op(name: str, value: str, op_id: str = "op-1") -> PlanOp:
    return PlanOp(id=op_id, target="@main", kind="mk.var.eval",
                  payload={"name": name, "value": value})


def _apply(tmp_path: Path, ops: list[PlanOp]):
    return apply_plan(Plan(port="category/name", ops=ops),
                      port_root=tmp_path, target="@main",
                      dry_run=False, oracle_profile="off")


# --- the bug ----------------------------------------------------------------


def test_options_eval_lands_before_the_options_include(tmp_path: Path) -> None:
    """The libunwind shape: an OPTIONS_* eval in a sandwiched port."""
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "PORTNAME= libunwind\n"
        "OPTIONS_DEFINE= DOCS\n"
        "TESTS_CONFIGURE_ON= --enable-tests\n"
        ".include <bsd.port.options.mk>\n"
        "\n"
        "post-install:\n"
        "\t@${ECHO}\n"
        ".include <bsd.port.mk>\n"
    )

    assert _apply(tmp_path, [
        _eval_op("OPTIONS_DEFINE", "${OPTIONS_DEFINE} TESTS")]).ok

    lines = makefile.read_text().splitlines()
    assign = lines.index("OPTIONS_DEFINE:= ${OPTIONS_DEFINE} TESTS")
    options_include = lines.index(".include <bsd.port.options.mk>")
    assert assign < options_include, (
        "the assignment must precede the include that consumes it:\n"
        + "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines))
    )
    # And it must still come after the upstream value it expands, or `:=`
    # would capture an empty one.
    assert lines.index("OPTIONS_DEFINE= DOCS") < assign


def test_pre_mk_also_closes_the_window(tmp_path: Path) -> None:
    """bsd.port.pre.mk includes bsd.port.options.mk, so it counts too."""
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "OPTIONS_DEFAULT= STUNNEL\n"
        ".include <bsd.port.pre.mk>\n"
        ".include <bsd.port.mk>\n"
    )

    assert _apply(tmp_path, [
        _eval_op("OPTIONS_DEFAULT", "${OPTIONS_DEFAULT:NSTUNNEL}")]).ok

    lines = makefile.read_text().splitlines()
    assert (lines.index("OPTIONS_DEFAULT:= ${OPTIONS_DEFAULT:NSTUNNEL}")
            < lines.index(".include <bsd.port.pre.mk>"))


def test_earliest_options_include_wins(tmp_path: Path) -> None:
    """With both, the first one is where PORT_OPTIONS is derived."""
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "OPTIONS_DEFINE= A\n"
        ".include <bsd.port.options.mk>\n"
        ".include <bsd.port.pre.mk>\n"
        ".include <bsd.port.mk>\n"
    )

    assert _apply(tmp_path, [_eval_op("OPTIONS_DEFINE", "${OPTIONS_DEFINE} B")]).ok

    lines = makefile.read_text().splitlines()
    assert (lines.index("OPTIONS_DEFINE:= ${OPTIONS_DEFINE} B")
            < lines.index(".include <bsd.port.options.mk>"))


def test_two_options_evals_keep_their_declared_order(tmp_path: Path) -> None:
    """libunwind sets OPTIONS_DEFINE then OPTIONS_DEFAULT; both move, and
    the overlay's order has to survive the move."""
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "OPTIONS_DEFINE= A\n"
        ".include <bsd.port.options.mk>\n"
        ".include <bsd.port.mk>\n"
    )

    assert _apply(tmp_path, [
        _eval_op("OPTIONS_DEFINE", "${OPTIONS_DEFINE} TESTS", "op-1"),
        _eval_op("OPTIONS_DEFAULT", "", "op-2"),
    ]).ok

    lines = makefile.read_text().splitlines()
    first = lines.index("OPTIONS_DEFINE:= ${OPTIONS_DEFINE} TESTS")
    second = lines.index("OPTIONS_DEFAULT:= ")
    assert first < second < lines.index(".include <bsd.port.options.mk>")


# --- what must NOT change ---------------------------------------------------


def test_terminal_only_port_is_untouched(tmp_path: Path) -> None:
    """96 of the 193 overlays are terminal-only and already correct. The
    fix must not move those — this is the pre-existing behaviour, byte for
    byte."""
    makefile = tmp_path / "Makefile"
    makefile.write_text("OPTIONS_DEFAULT= STUNNEL\n\n.include <bsd.port.mk>\n")

    assert _apply(tmp_path, [
        _eval_op("OPTIONS_DEFAULT", "${OPTIONS_DEFAULT:NSTUNNEL}")]).ok

    assert makefile.read_text() == (
        "OPTIONS_DEFAULT= STUNNEL\n"
        "\n"
        "OPTIONS_DEFAULT:= ${OPTIONS_DEFAULT:NSTUNNEL}\n"
        ".include <bsd.port.mk>\n"
    )


def test_non_options_eval_stays_where_it_was(tmp_path: Path) -> None:
    """The narrow scope is the point. A non-OPTIONS assignment can
    legitimately want to read what the framework computed, so moving it
    would break evals that work today."""
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "CFLAGS= -O2\n"
        ".include <bsd.port.options.mk>\n"
        ".include <bsd.port.mk>\n"
    )

    assert _apply(tmp_path, [_eval_op("CFLAGS", "${CFLAGS} -Wall")]).ok

    lines = makefile.read_text().splitlines()
    assert (lines.index("CFLAGS:= ${CFLAGS} -Wall")
            > lines.index(".include <bsd.port.options.mk>"))


def test_no_include_at_all_still_appends_at_eof(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text("OPTIONS_DEFINE= A\n")

    assert _apply(tmp_path, [_eval_op("OPTIONS_DEFINE", "${OPTIONS_DEFINE} B")]).ok

    assert makefile.read_text() == (
        "OPTIONS_DEFINE= A\n"
        "OPTIONS_DEFINE:= ${OPTIONS_DEFINE} B\n"
    )
