"""An op no build line ever sees is an error: E_SEM_DEAD_OP (poly-7pwa.3).

An op is dead when, on every build line it runs on, a later op on that line
replaces or removes the file it leaves its result in, and no op in between
reads that file. @any runs before every T op, so an @any+@T pair is the
normative override and is allowed; a second writer in the same block is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3.engine.api import apply_dsl, build_plan, check_dsl

HEAD = 'port devel/thing\ntype port\nreason "fixture"\n'  # lines 1-3


def _m(src: str, dst: str = "files/x") -> str:
    return f"file materialize dragonfly/{src} -> {dst}\n"


def _dead(text: str):
    result = build_plan(HEAD + text, None)
    dead = [d for d in result.diagnostics
            if d.code == "E_SEM_DEAD_OP"]
    return result, dead


# -- allowed -----------------------------------------------------------------

ALLOWED = {
    "any-below-main":
        "target @main\n" + _m("b") + "target @any\n" + _m("a"),
    "any-then-main":
        "target @any\n" + _m("a") + "target @main\n" + _m("b"),
    "comma-default-override":
        "target @main,@2026Q3\n" + _m("a") + "target @main\n" + _m("b"),
    "per-target-convention":
        "target @2026Q3\n" + _m("q3") + "target @main\n" + _m("main"),
    "any-write-main-remove":
        "target @any\n" + _m("a") + "target @main\nfile remove files/x\n",
    "main-write-any-remove":
        "target @main\n" + _m("b") + "target @any\nfile remove files/x\n",
    "one-comma-statement":
        "target @main,@2026Q3\n" + _m("a"),
    "copy-reads-between":
        "target @any\n" + _m("a") + "file copy files/x -> files/y\n"
        + _m("b"),
    "patch-between":
        "target @any\n" + _m("a") + "patch apply dragonfly/p.diff\n"
        + _m("b"),
    "edit-after-write":
        "target @any\n" + _m("a")
        + 'text replace-once file files/x from "A" to "B"\n',
    "mk-set-twice":
        'target @any\nmk set FOO "a"\nmk set FOO "b"\n',
}


@pytest.mark.parametrize("text", list(ALLOWED.values()), ids=list(ALLOWED))
def test_a_writer_that_lands_somewhere_is_allowed(text: str) -> None:
    result, dead = _dead(text)

    assert dead == []
    assert result.ok, result.diagnostics


def test_the_override_resolves_by_scope_on_each_build_line(
    tmp_path: Path,
) -> None:
    overlay_dir = tmp_path / "overlay"
    (overlay_dir / "dragonfly").mkdir(parents=True)
    (overlay_dir / "dragonfly" / "a").write_text("ANY\n")
    (overlay_dir / "dragonfly" / "b").write_text("MAIN\n")
    # The @any block is written LAST, but runs first on every line.
    text = HEAD + "target @main\n" + _m("b") + "target @any\n" + _m("a")

    got = {}
    for line in ("@main", "@2026Q3"):
        port = tmp_path / line.lstrip("@")
        (port / "files").mkdir(parents=True)
        result = apply_dsl(
            text, source_path=overlay_dir / "overlay.dops", port_root=port,
            target=line, oracle_profile="off",
        )
        assert result.ok, result.diagnostics
        got[line] = (port / "files" / "x").read_text()

    assert got == {"@main": "MAIN\n", "@2026Q3": "ANY\n"}


# -- rejected ----------------------------------------------------------------

VICTIMS = {
    "materialize": _m("a"),
    "copy": "file copy files/y -> files/x\n",
    "remove": "file remove files/x\n",
    "text": 'text replace-once file files/x from "U" to "E"\n',
}
KILLERS = {
    "materialize": _m("b"),
    "copy": "file copy files/z -> files/x\n",
    "remove": "file remove files/x\n",
}


@pytest.mark.parametrize(
    "victim, killer",
    [(v, k) for v in VICTIMS for k in KILLERS],
    ids=[f"{v}-{k}" for v in VICTIMS for k in KILLERS],
)
def test_a_later_writer_on_every_line_kills_the_earlier_op(
    victim: str, killer: str
) -> None:
    result, dead = _dead("target @any\n" + VICTIMS[victim] + KILLERS[killer])

    assert not result.ok
    assert len(dead) == 1
    assert dead[0].line == 5


def test_two_any_writers_name_both_lines_and_every_build_line() -> None:
    _, dead = _dead("target @any\n" + _m("a") + _m("b"))

    assert len(dead) == 1
    assert "line 5 (file materialize) never takes effect" in dead[0].message
    assert ("line 6 writes files/x after it on every build line"
            in dead[0].message)


def test_a_target_op_then_a_comma_op_covering_its_line_is_dead() -> None:
    _, dead = _dead(
        "target @main\n" + _m("b") + "target @main,@2026Q3\n" + _m("a")
    )

    assert [d.line for d in dead] == [5]
    assert "line 7 writes files/x after it on @main" in dead[0].message


def test_a_comma_op_killed_on_each_line_is_reported_once_naming_both() -> None:
    _, dead = _dead(
        "target @main,@2026Q3\n" + _m("a")
        + "target @main\n" + _m("b")
        + "target @2026Q3\n" + _m("c")
    )

    assert len(dead) == 1
    message = dead[0].message
    assert "line 7 writes files/x after it on @main" in message
    assert "line 9 writes files/x after it on @2026Q3" in message
    assert "If one of those later ops was meant" in message


@pytest.mark.parametrize("dst", ["./files/x", "files//x", "files/../files/x"])
def test_a_respelled_path_is_the_same_file(dst: str) -> None:
    _, dead = _dead("target @any\n" + _m("a") + _m("b", dst))

    assert [d.line for d in dead] == [5]


def test_unrelated_target_blocks_do_not_multiply_the_report() -> None:
    _, dead = _dead(
        "target @any\n" + _m("a") + _m("b")
        + 'target @main\nmk set A "1"\ntarget @2026Q3\nmk set A "2"\n'
    )

    assert len(dead) == 1


def test_the_message_gives_the_route_out() -> None:
    _, dead = _dead(
        "target @main\n" + _m("b") + "target @main,@2026Q3\n" + _m("a")
    )

    assert len(dead) == 1
    message = dead[0].message
    assert "so no build line ever sees its result" in message
    assert "If that later op was meant for another build line" in message
    assert "Otherwise delete line 5" in message
    assert "under its own 'target @main' line" in message


def test_an_op_appended_into_the_last_block_names_the_append_as_suspect(
) -> None:
    _, dead = _dead(
        "target @2026Q3\n" + _m("@2026Q3/p", "dragonfly/p")
        + "target @main\n" + _m("@main/p", "dragonfly/p")
        + _m("p", "dragonfly/p")
    )

    assert [d.line for d in dead] == [7]
    assert "line 8 writes dragonfly/p after it on @main" in dead[0].message
    assert "move it into that line's target block" in dead[0].message


def test_a_dead_op_over_several_lines_is_named_by_its_whole_extent() -> None:
    _, dead = _dead(
        "target @any\n"
        "text line-insert-after file files/x \\\n"
        'anchor "A" \\\n'
        'line "B"\n'
        + _m("a")
    )

    assert [d.line for d in dead] == [5]
    message = dead[0].message
    assert "lines 5-7 (text line-insert-after) never takes effect" in message
    assert "Otherwise delete lines 5-7" in message


@pytest.mark.parametrize(
    "between",
    ['mk set FOO "x"\n', 'text replace-once file files/y from "A" to "B"\n'],
    ids=["mk-op", "edit-of-another-file"],
)
def test_an_op_on_another_file_in_between_does_not_save_the_first_write(
    between: str,
) -> None:
    _, dead = _dead("target @any\n" + _m("a") + between + _m("b"))

    assert [d.line for d in dead] == [5]


def test_an_mk_op_before_a_whole_makefile_write_is_dead() -> None:
    _, dead = _dead('target @any\nmk set FOO "a"\n' + _m("mk", "Makefile"))

    assert [d.line for d in dead] == [5]


def test_dsl_check_reports_it_too() -> None:
    result = check_dsl(HEAD + "target @any\n" + _m("a") + _m("b"))

    assert [d.code for d in result.diagnostics] == ["E_SEM_DEAD_OP"]


# -- the real tree -----------------------------------------------------------


def test_no_overlay_in_the_tree_has_a_dead_op(delta_ports):
    dead = [
        f"{overlay.parent.relative_to(delta_ports)}:{d.line}"
        for overlay in sorted(delta_ports.glob("*/*/overlay.dops"))
        for d in build_plan(overlay.read_text(), overlay).diagnostics
        if d.code == "E_SEM_DEAD_OP"
    ]
    assert dead == []


def test_an_append_to_a_real_overlays_last_block_kills_that_lines_op(
    multi_line_ports,
):
    """The positive control: the zero above is a check that can fire.

    For each multi-line port whose file ends in a one-line block T,
    re-append its last materialize whose src is under dragonfly/@T/.
    The copy lands in the same block, so the original never takes
    effect: one E_SEM_DEAD_OP, at the original. The same op under its
    own `target @any` line is the override, and is allowed.
    """
    checked = []
    for port in multi_line_ports:
        if len(port.last_lines) != 1:
            continue
        line = port.last_lines[0]
        own = [(s, d) for s, d in port.scoped(line)
               if s.startswith(f"dragonfly/{line}/")]
        if not own:
            continue
        src, dst = own[-1]
        text = port.overlay.read_text()
        original = next(
            op.span.line_start
            for op in build_plan(text, port.overlay).plan.ops
            if op.target == line and op.payload.get("src") == src
            and op.payload.get("dst") == dst
        )
        sep = "" if text.endswith("\n") else "\n"
        appended = f"file materialize {src} -> {dst}\n"
        result = build_plan(text + sep + appended, port.overlay)
        assert [d.line for d in result.diagnostics
                if d.code == "E_SEM_DEAD_OP"] == [original], port.origin
        result = build_plan(text + sep + "target @any\n" + appended,
                            port.overlay)
        assert result.ok is True, (port.origin, result.diagnostics)
        checked.append(port.origin)
    if not checked:
        pytest.skip("no multi-line port ends in a one-line block "
                    "with a payload of its own")
