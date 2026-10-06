"""The guidance states poly-7pwa.27's table, and its one edit recipe works.

S5 ("new work defaults to @any; scoping answers a measured divergence")
was replaced: the agent builds one line and can never measure a
divergence, so its @any fixes reached lines nothing built. The guidance
must not steer back to @any, and the recipe it gives for sharing another
line's op must produce what it says.
"""

from __future__ import annotations

import pytest

from dportsv3.agent.playbooks import find_playbooks_dir
from dportsv3.engine.api import build_plan

FLOW = find_playbooks_dir() / "flow-patch.md"


def _scoping() -> str:
    text = FLOW.read_text()
    body = text.split("## Scoping", 1)[1]
    return " ".join(body.split("\n## ", 1)[0].split())


def test_the_old_default_is_gone():
    text = " ".join(FLOW.read_text().split())
    assert "Most fixes are universal" not in text
    assert "Keep them in the `@any` scope" not in text


def test_the_scoping_section_states_the_table():
    sec = _scoping()
    assert "The port had no overlay ops when the job started" in sec
    assert "`target <your line>` block" in sec
    assert "Do not edit or delete an `@any` op" in sec
    assert "goes to an operator" in sec
    assert "never the shared file" in sec


HEAD = 'port devel/thing\ntype port\nreason "fixture"\n'


@pytest.mark.parametrize("tail", ["", 'mk set AFTER "q3"\n'],
                         ids=["op-is-last", "op-in-the-middle"])
def test_sharing_another_lines_op_in_place(tail):
    """`target <its line>,<your line>` above it, `target <its line>` below:
    the op runs on both, and the rest of that block stays on its line."""
    shared = 'mk set V "1"\n'
    text = (HEAD + "target @2026Q3\n" 'mk set BEFORE "q3"\n'
            "target @2026Q3,@main\n" + shared +
            "target @2026Q3\n" + tail)
    planned = build_plan(text, None)
    assert planned.ok, [d.code for d in planned.diagnostics]
    by_line: dict[str, set[str]] = {}
    for op in planned.plan.ops:
        by_line.setdefault(op.target, set()).add(op.payload["name"])
    assert "V" in by_line["@main"] and "V" in by_line["@2026Q3"]
    assert "BEFORE" not in by_line["@main"]
    assert "AFTER" not in by_line.get("@main", set())
