"""A tool row carries its own tail, when something publishes one.

These are the CONSUMER side, and they outlived their producer. The tracker
used to read a running build's log itself, which meant resolving the path
through a root-only `dev-env path` while running unprivileged -- so it never
worked, and it would be wrong anyway once the runner is remote (poly-paee).
The reader was deleted; poly-pvs2 holds the decision about what publishes a
tail instead.

Kept rather than deleted with it: these two functions are pure, they are
what any producer would feed, and the pairing they encode -- only a long
tool gets a tail, and it hangs on the row that is running -- is the part
worth not rediscovering. The deleted reader is at commit 6275522.
"""

from __future__ import annotations

from dportsv3.tracker.render import (
    attach_tool_tail,
    group_activity_into_cards,
    running_tailable_tool,
)


def _cards(tool, running=True):
    rows = [
        {"id": 1, "stage": "llm_turn", "ts": "t",
         "extra": {"attempt": 1, "turn": 1}, "message": "", "duration_ms": None},
        {"id": 2, "stage": "tool_start", "ts": "t",
         "extra": {"attempt": 1, "turn": 1, "tool": tool, "call_id": "c1"},
         "message": "", "duration_ms": None},
    ]
    if not running:
        rows.append({"id": 3, "stage": f"tool:{tool}", "ts": "t",
                     "extra": {"attempt": 1, "turn": 1, "ok": True,
                               "call_id": "c1"},
                     "message": "done", "duration_ms": 10})
    return group_activity_into_cards(rows)


def test_only_a_long_tool_gets_a_tail():
    """grep returns in milliseconds and has nothing to say meanwhile."""
    assert running_tailable_tool(_cards("dsynth_build")) is not None
    assert running_tailable_tool(_cards("grep")) is None


def test_a_finished_build_gets_no_tail():
    """The row collapses to its duration and rc when the tool returns."""
    assert running_tailable_tool(_cards("dsynth_test", running=False)) is None


def test_the_tail_hangs_on_the_row_producing_it():
    cards = _cards("dsynth_test")
    attach_tool_tail(cards, {"ok": True, "text": "LINK libLLVM.so"})
    assert cards[0]["tools"][0]["tail"]["text"] == "LINK libLLVM.so"


def test_attaching_nothing_leaves_the_row_alone():
    cards = _cards("dsynth_test")
    attach_tool_tail(cards, None)
    assert "tail" not in cards[0]["tools"][0]
