"""Three fixes from an operator's first look at the rebuilt job detail.

poly-qqx9.17  the tail is a terminal, and opens on the newest line
poly-qqx9.18  a segment too narrow for its label carries none
poly-qqx9.19  a version pill lands on the band it changes

The first is a DIVERGENCE FROM THE DESIGN SOURCE, artifact "Five Ways to
Watch a Job" v9, which the epic names as what this work is measured
against. It carries --term-* tokens, dark in both themes, and uses them
for the tail while using --code-* for a tool's captured stdout -- ten
lines apart in the same specimen. Built with --code-*, the tail was a
code block on a light ground.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dportsv3.tracker.render.attempts import _LABEL_MIN_PCT, attempt_strip

REPO = Path(__file__).resolve().parents[1]
CSS = (REPO / "dportsv3" / "tracker" / "static" / "progress.css").read_text()
JS = (REPO / "dportsv3" / "tracker" / "static" / "agentic-job.js").read_text()
STRIP_TMPL = (REPO / "dportsv3" / "tracker" / "templates"
              / "_attempt_strip.html").read_text()


# --- poly-qqx9.17: a terminal, not a code block --------------------------


def test_the_tail_uses_the_terminal_tokens() -> None:
    block = CSS[CSS.index(".turn-stream .tool-tail"):]
    block = block[:block.index("}")]

    assert "var(--term-fg)" in block and "var(--term-bg)" in block
    assert "--code-" not in block, (
        "--code-* is the tool-output treatment; the design distinguishes "
        "the two deliberately"
    )


def test_the_terminal_tokens_are_dark_in_every_theme() -> None:
    """Three declaration sites, because a viewer has three states: no
    stamp with a light OS, no stamp with a dark OS, and an explicit
    choice. A token defined in only one of them renders the other's text
    on this one's ground."""
    sites = re.findall(r"--term-bg:\s*(#[0-9a-f]{6})", CSS)

    assert len(sites) == 3, f"expected 3 --term-bg declarations, got {sites}"
    for value in sites:
        r, g, b = (int(value[i:i + 2], 16) for i in (1, 3, 5))
        assert (r + g + b) / 3 < 40, f"{value} is not a terminal ground"

    for fg in re.findall(r"--term-fg:\s*(#[0-9a-f]{6})", CSS):
        r, g, b = (int(fg[i:i + 2], 16) for i in (1, 3, 5))
        assert (r + g + b) / 3 > 150, f"{fg} is not legible on a dark ground"


def test_the_tail_opens_on_the_newest_line() -> None:
    """Rendered HTML starts scrolled to the TOP, so without this the first
    paint shows the OLDEST of the last forty lines beside a label reading
    "last line 4s ago". poly-pvs2 did this on the poll swap only."""
    assert "function tailToBottom" in JS
    # Called at module level for the server-rendered first paint...
    assert re.search(r"^tailToBottom\(\);", JS, re.M), (
        "not called on initial load"
    )
    # ...and from the swap, which must not steal a reader's scroll.
    swap = JS[JS.index("data.tail_html"):]
    assert "atBottom" in swap and "tailToBottom(fresh)" in swap


# --- poly-qqx9.18: no label a segment cannot hold ------------------------


def _strip(*durations_ms: int) -> dict:
    """One finished attempt per duration, each a single failed tool so the
    whole span lands in one measurable segment."""
    boundaries, tools, turns = [], [], []
    for i, ms in enumerate(durations_ms, start=1):
        boundaries += [
            {"attempt": i, "stage": "attempt_start",
             "ts": "2026-09-24T10:00:00+00:00"},
            {"attempt": i, "stage": "attempt_end", "rebuild_ok": False,
             "ts": "2026-09-24T10:00:00+00:00"},
        ]
        tools.append({"attempt": i, "tool": "grep", "ok": True,
                      "ms": ms, "n": 1})
        turns.append({"attempt": i, "n": 1, "billable": 10})
    return attempt_strip(boundaries, tools, turns)


def test_a_wide_segment_keeps_its_label() -> None:
    strip = _strip(600_000)
    seg = strip["attempts"][0]["segments"][0]

    assert seg["pct"] == 100.0
    assert seg["label"] is True


def test_a_segment_too_narrow_to_hold_a_label_carries_none() -> None:
    """"55.0s" in a segment a few percent wide was cut to "55" -- .seg
    clips rather than ellipsises -- and read as a quantity in the same
    unit as the "1m52s" beside it."""
    strip = _strip(3_000_000, 55_000)
    narrow = strip["attempts"][1]["segments"][0]

    assert narrow["pct"] < _LABEL_MIN_PCT
    assert narrow["label"] is False


def test_the_value_survives_on_every_segment_however_narrow() -> None:
    """Nothing is lost by staying quiet: the title carries it always."""
    assert 'title="' in STRIP_TMPL
    title = STRIP_TMPL[STRIP_TMPL.index('title="'):]
    title = title[:title.index("\n")]
    assert "dur_ms(s.ms)" in title

    body = STRIP_TMPL[STRIP_TMPL.index("data-detail"):]
    assert "{%- if s.label %}{{ dur_ms(s.ms) }}{% endif -%}" in body


def test_the_threshold_applies_to_running_attempts_too() -> None:
    boundaries = [
        {"attempt": 1, "stage": "attempt_start",
         "ts": "2026-09-24T10:00:00+00:00"},
        {"attempt": 1, "stage": "attempt_end", "rebuild_ok": False,
         "ts": "2026-09-24T11:00:00+00:00"},
        {"attempt": 2, "stage": "attempt_start",
         "ts": "2026-09-24T11:00:00+00:00"},
    ]
    strip = attempt_strip(
        boundaries,
        [{"attempt": 1, "tool": "grep", "ok": True, "ms": 3_600_000, "n": 1}],
        [{"attempt": 1, "n": 1, "billable": 10}],
        now=__import__("datetime").datetime.fromisoformat(
            "2026-09-24T11:01:00+00:00"),
    )
    running = strip["attempts"][1]["segments"][0]

    assert running["kind"] == "run"
    assert running["label"] is False, "1 minute against a 60 minute scale"


# --- poly-qqx9.19: the pill lands on the band ----------------------------


def test_both_link_builders_anchor_at_the_band() -> None:
    """A bare ?attempt=N is a full navigation to the top of a long page,
    and the band is near the bottom -- picking a version looked like being
    thrown off what you were reading. The query param stays, because a
    pinned version has to be linkable into a bead (poly-5tgc UI 8)."""
    pages = (REPO / "dportsv3" / "tracker" / "routes" / "pages.py").read_text()
    api = (REPO / "dportsv3" / "tracker" / "routes"
           / "agentic_api.py").read_text()

    assert '+ "#worktree"' in pages
    assert 'f"?attempt={n}#worktree"' in api


def test_the_anchor_exists_wherever_the_pills_do() -> None:
    """The pills render from _worktree_versions.html, which is included by
    both branches of _worktree.html that can show versions."""
    band = (REPO / "dportsv3" / "tracker" / "templates"
            / "_worktree.html").read_text()

    included = [chunk for chunk in band.split("<section")
                if "_worktree_versions.html" in chunk]
    assert included, "no branch includes the version selector"
    for chunk in included:
        assert 'id="worktree"' in chunk
