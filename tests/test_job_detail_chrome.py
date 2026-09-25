"""The job page's heading strip and its panels (poly-x3pg.16).

From the epic's first side-by-side against the operations console mock,
2026-09-25. Three findings, all structural rather than cosmetic:

1. no header action row -- the <h1> was the raw queue filename with the live
   badge inside it, Pause was a "[pause]" text link, and Abandon was a button
   at the bottom of a facts table labelled "Abandon job (mark dead)"
2. (the facts table itself is poly-qqx9.14, not this bead)
3. sections had no panel chrome: the working tree had no surface, border or
   header AT ALL, the turn stream was a bare column, and the attempt strip
   had a card but no header, so nothing on screen named the bars

These are template-shape assertions, not a screenshot diff -- see CLAUDE.md on
why the pictures are for a person to read and not for a test to assert on.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "dportsv3" / "tracker" / "templates"
CSS = (REPO / "dportsv3" / "tracker" / "static" / "progress.css").read_text()
JS = (REPO / "dportsv3" / "tracker" / "static" / "agentic-job.js").read_text()

def _read(name: str) -> str:
    return (TEMPLATES / name).read_text()


def _markup(name: str) -> str:
    """A template with its Jinja comments removed.

    The comments here explain what each section REPLACED, quoting the old
    "[pause]" link and "Abandon job (mark dead)" by name, and an assertion
    that those strings are gone must not match the note saying so.
    """
    return re.sub(r"\{#.*?#\}", "", _read(name), flags=re.S)


JOB = _read("agentic_job.html")
JOB_MARKUP = _markup("agentic_job.html")
SUMMARY_MARKUP = _markup("_job_summary.html")
STRIP = (TEMPLATES / "_attempt_strip.html").read_text()
ACTIVITY = (TEMPLATES / "_job_activity.html").read_text()
WORKTREE = (TEMPLATES / "_worktree.html").read_text()


# --- 1. the heading strip ------------------------------------------------


def test_the_page_has_a_heading_strip() -> None:
    assert 'class="page-heading"' in JOB
    # and it is outside the page body, like every other page in this console
    assert JOB.index('class="page-heading"') < JOB.index('class="tracker-page"')


def test_the_heading_carries_a_human_subtitle() -> None:
    """The mock: "patch job for databases/postgresql17-server on @main".

    The id alone is a queue filename; what the job IS lived only in the facts
    panel further down.
    """
    head = JOB.split('class="page-heading"', 1)[1].split("</div>", 1)[0]
    assert "job.type" in head and "job.origin" in head and "job.target" in head


def test_both_controls_are_real_buttons_in_the_heading() -> None:
    actions = JOB.split('class="heading-actions"', 1)[1]
    actions = actions.split("</div>", 1)[0]
    for control in ('id="pause-toggle"', 'id="abandon-btn"'):
        assert control in actions, control
        # the JS binds these by id; the point is that they are buttons now
        assert re.search(r"<button[^>]*" + re.escape(control), actions), control


def test_pause_is_no_longer_a_bracketed_text_link() -> None:
    assert "[pause]" not in JOB_MARKUP
    assert "[pause]" not in re.sub(r"//.*", "", JS)
    assert '"Pause"' in JS and '"Resume"' in JS


def test_abandon_left_the_facts_panel() -> None:
    """poly-qqx9.14 deletes that panel; the control must not go with it."""
    assert "abandon-btn" not in SUMMARY_MARKUP
    assert "Abandon job (mark dead)" not in SUMMARY_MARKUP


# --- 3. panel chrome on every section ------------------------------------


def test_every_section_is_a_panel_with_a_head_that_names_it() -> None:
    for name, tmpl, title in [("attempt strip", STRIP, "Attempts"),
                              ("turn stream", ACTIVITY, "Turns"),
                              ("working tree", WORKTREE, "Working tree")]:
        assert 'class="panel' in tmpl, f"{name} has no panel"
        assert "content-head" in tmpl, f"{name} has no head"
        assert title in tmpl, f"{name}'s head does not name it"


def test_the_heads_are_headings() -> None:
    """The mock draws .content-head as a div; UI-7 wants the outline.

    Where the mock and UI-7 conflict, UI-7 wins -- the epic says so. An <h2>
    with .content-head looks exactly like the mock's div.
    """
    for tmpl in (STRIP, ACTIVITY, WORKTREE):
        assert re.search(r'<h2 class="content-head', tmpl)


def test_each_panel_labels_itself_for_a_screen_reader() -> None:
    for tmpl in (STRIP, ACTIVITY, WORKTREE):
        for m in re.finditer(r'<section class="panel[^>]*>', tmpl):
            assert "aria-labelledby" in m.group(0), m.group(0)


def test_the_sections_stopped_drawing_their_own_chrome() -> None:
    """The panel supplies surface and border now; two rules must not.

    .wf had its own card and .wt-band had nothing at all -- the one section
    on the page sitting directly on the page background.
    """
    wf = re.search(r"\n\.wf \{([^}]*)\}", CSS)
    assert wf, ".wf is gone"
    assert "background" not in wf.group(1) and "border" not in wf.group(1)


def test_the_attempt_panel_says_how_many_of_how_many() -> None:
    assert "attempts_total" in STRIP


def test_content_panel_is_no_longer_a_class_with_no_rule() -> None:
    """Six drawer panels used the mock's name against nothing at all.

    They rendered as bare content with a heading strip floating on the page
    background -- the same defect as the job page's, on another page.
    """
    m = re.search(r"\n\.panel, \.content-panel \{([^}]*)\}", CSS)
    assert m, ".content-panel has no chrome rule"
    assert "background" in m.group(1) and "border" in m.group(1)
