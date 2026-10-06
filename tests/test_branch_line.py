"""A new build line starts as a copy of another line's own blocks.

poly-7pwa.25. A quarterly branches from main, so at bring-up it needs every
op @main has outside ``@any`` -- and since poly-7pwa.27 that is where every
fix to a port with ops goes. ``migrate branch-line`` appends one block for
the new line holding a copy of them, with the payload they read under
``dragonfly/@<line>/`` copied to the new line's own folder.

Line names here are labels: @2099Q1 stands for the line being brought up.
"""

from __future__ import annotations

import pytest

from dportsv3.cli import main
from dportsv3.engine.api import apply_dsl
from dportsv3.migration.line_branch import branch_overlay, plan_branch

OVERLAY = (
    'port devel/x\ntype port\nreason "fixture"\n'
    "target @any\n"
    "mk add CFLAGS -DANY\n"
    "file materialize dragonfly/patch-a -> dragonfly/patch-a\n"
    "target @2026Q3,@main\n"
    "# shared by both lines\n"
    "mk add CFLAGS -DSHARED\n"
    "\n"
    "# This comment is the next block's, so it is not copied.\n"
    "target @2026Q3\n"
    'mk set V "q3"\n'
    "file materialize dragonfly/@2026Q3/patch-b -> dragonfly/patch-b\n"
    "target @main\n"
    'mk set V "main"\n'
    "file materialize dragonfly/@main/patch-b -> dragonfly/patch-b\n"
    'text line-remove file pkg-plist exact "bin/old"\n'
)
PAYLOAD = {
    "dragonfly/patch-a": "a\n",
    "dragonfly/@2026Q3/patch-b": "b for q3\n",
    "dragonfly/@main/patch-b": "b for main\n",
}
#: Ops before any `target` line, a `./` source, a continued op, and a
#: heredoc that ends the file with no final newline (poly-fklo.1's case).
EDGES = (
    'port devel/x\ntype port\nreason "fixture"\n'
    "mk add CFLAGS -DANY\n"
    "target @2026Q3\n"
    'mk set V "q3"\n'
    "target @main\n"
    "file materialize ./dragonfly/@main/patch-b -> dragonfly/patch-b\n"
    "text line-remove file pkg-plist \\\n"
    '     exact "bin/old"\n'
    "mk target set post-patch <<'MK'\n"
    "\t@echo main\n"
    "MK"
)
UPSTREAM = {
    "Makefile": "PORTNAME=\tx\nCFLAGS+=\t-O\nV=\tup\n\n.include <bsd.port.mk>\n",
    "pkg-plist": "bin/x\nbin/old\n",
}


def _delta(tmp_path, overlay=OVERLAY, payload=PAYLOAD):
    port = tmp_path / "delta" / "ports" / "devel" / "x"
    for rel, data in {**payload, "overlay.dops": overlay}.items():
        (port / rel).parent.mkdir(parents=True, exist_ok=True)
        (port / rel).write_text(data)
    return tmp_path / "delta", port


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _compose(tmp_path, port, line):
    """The port as ``line`` builds it."""
    out = tmp_path / "out" / f"{line}-{len(list(tmp_path.glob('out/*')))}"
    out.mkdir(parents=True)
    for rel, data in UPSTREAM.items():
        (out / rel).write_text(data)
    overlay = port / "overlay.dops"
    result = apply_dsl(overlay.read_text(), source_path=overlay,
                       port_root=out, target=line, oracle_profile="off")
    assert result.ok, (line, result.diagnostics)
    return _snapshot(out)


@pytest.mark.parametrize("overlay", [OVERLAY, EDGES], ids=["blocks", "edges"])
def test_old_lines_compose_as_before_and_the_new_one_as_from(tmp_path,
                                                             overlay):
    delta, port = _delta(tmp_path, overlay)
    before = {line: _compose(tmp_path, port, line)
              for line in ("@main", "@2026Q3")}

    assert main(["migrate", "branch-line", "--from", "@main", "--new",
                 "@2099Q1", "--delta-root", str(delta), "--write"]) == 0

    for line, tree in before.items():
        assert _compose(tmp_path, port, line) == tree, line
    assert _compose(tmp_path, port, "@2099Q1") == before["@main"]
    # Its own copy of @main's patch, and the old text is untouched.
    assert (port / "dragonfly/@2099Q1/patch-b").read_text() == "b for main\n"
    new = (port / "overlay.dops").read_text()
    assert new.startswith(overlay)
    if overlay is OVERLAY:
        assert "next block's" not in new[len(overlay):]
        assert "# shared by both lines" in new[len(overlay):]


def test_a_patch_apply_source_is_copied_too():
    text = ('port devel/x\ntype port\nreason "fixture"\n'
            "target @main\npatch apply diffs/@main/fix.diff\n")
    new, copies, n = branch_overlay(text, "@main", "@2099Q1")
    assert copies == (("diffs/@main/fix.diff", "diffs/@2099Q1/fix.diff"),)
    assert new.endswith("target @2099Q1\n\npatch apply diffs/@2099Q1/fix.diff\n")


def test_a_port_with_no_block_for_from_is_left_alone():
    q3_only = ('port devel/x\ntype port\nreason "fixture"\n'
               'target @any\nmk add CFLAGS -DANY\n'
               'target @2026Q3\nmk set V "q3"\n')
    assert branch_overlay(q3_only, "@main", "@2099Q1") is None


def _names_the_new_line(port):
    with (port / "overlay.dops").open("a") as f:
        f.write('target @2099Q1\nmk set W "1"\n')


def _payload_missing(port):
    (port / "dragonfly/@main/patch-b").unlink()


def _does_not_plan(port):
    with (port / "overlay.dops").open("a") as f:
        f.write("not an op\n")


def _crlf(port):
    overlay = port / "overlay.dops"
    overlay.write_bytes(overlay.read_bytes().replace(b"\n", b"\r\n"))


def _copy_differs(port):
    (port / "dragonfly/@2099Q1").mkdir()
    (port / "dragonfly/@2099Q1/patch-b").write_text("other\n")


@pytest.mark.parametrize("change, reason", [
    (_names_the_new_line, "already has a block naming @2099Q1"),
    (_payload_missing, "dragonfly/@main/patch-b is missing"),
    (_copy_differs, "dragonfly/@2099Q1/patch-b exists with other content"),
    (_does_not_plan, "the overlay does not plan"),
    (_crlf, "CR line endings"),
])
def test_a_port_that_cannot_be_copied_cleanly_is_skipped(tmp_path, change,
                                                         reason):
    delta, port = _delta(tmp_path)
    change(port)
    before = _snapshot(delta)
    (row,) = plan_branch(delta, "@main", "@2099Q1")
    assert row.skipped and reason in row.detail
    assert main(["migrate", "branch-line", "--from", "@main", "--new",
                 "@2099Q1", "--delta-root", str(delta), "--write"]) == 0
    assert _snapshot(delta) == before


def test_without_write_nothing_changes(tmp_path, capsys):
    delta, port = _delta(tmp_path)
    before = _snapshot(delta)
    assert main(["migrate", "branch-line", "--from", "@main", "--new",
                 "@2099Q1", "--delta-root", str(delta)]) == 0
    out = capsys.readouterr().out
    assert "branch devel/x: 4 ops into target @2099Q1, 1 payload file\n" in out
    assert "dry run" in out
    assert _snapshot(delta) == before


@pytest.mark.parametrize("old, new", [("@main", "@main"), ("@any", "@2099Q1")])
def test_two_real_lines_are_required(tmp_path, old, new):
    delta, _ = _delta(tmp_path)
    assert main(["migrate", "branch-line", "--from", old, "--new", new,
                 "--delta-root", str(delta)]) == 2


def test_every_multi_line_port_in_the_tree_branches(multi_line_ports,
                                                    tmp_path):
    """The real overlays pass the in-code check: each new line plans
    exactly its source line's ops, and no other line changes."""
    for port in multi_line_ports:
        for line in port.lines:
            result = branch_overlay(port.overlay.read_text(), line, "@2099Q1")
            assert not isinstance(result, str), (port.origin, line, result)

