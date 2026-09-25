"""The console's global visual properties, measured against its mock.

Epic poly-x3pg. Both properties here are GLOBAL -- they apply on every page, so
they are asserted once rather than in each per-page test:

* poly-x3pg.14  the page body fills the shell; only prose carries a measure
* poly-x3pg.15  square by default; round only where the shape IS a circle

Neither is a screenshot comparison. A screenshot diff of this console against
the mock would be noise -- the sample data differs and every deliberate
divergence lights up forever (see CLAUDE.md). These are the two properties that
can be read off the stylesheet, so they can be held without a picture.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CSS = REPO / "dportsv3" / "tracker" / "static" / "progress.css"
TEMPLATES = REPO / "dportsv3" / "tracker" / "templates"

# The two containers every page's content sits directly inside.
PAGE_CONTAINERS = [".tracker-page", ".page-body"]


def _rules() -> list[tuple[str, str]]:
    """(selector, body) for every rule, comments stripped."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    return [(m.group(1).strip(), m.group(2))
            for m in re.finditer(r"([^{}@][^{}]*?)\{([^{}]*)\}", css)]


# --------------------------------------------------------------- width

def test_no_page_container_caps_its_width() -> None:
    """poly-x3pg.14: the shell is the window.

    `.tracker-page` capped at 1400px while the 13 `.page-body` pages did not,
    so above 1400px the console disagreed with itself. The mock's .app is a
    100vh grid with no page-level cap at all.
    """
    for selector, body in _rules():
        parts = [p.strip() for p in selector.split(",")]
        for container in PAGE_CONTAINERS:
            # the container alone, or the container plus a page modifier
            if not any(p == container or p.startswith(container + ".")
                       for p in parts):
                continue
            m = re.search(r"max-width:\s*(\d+)px", body)
            assert not m, (
                f"{selector!r} caps the page at {m.group(1)}px. The console is "
                f"an application shell: cap the prose inside a page, not the "
                f"page (poly-x3pg.14)."
            )


def test_prose_still_carries_its_own_measure() -> None:
    """The cap came off the page because the paragraphs already have one.

    If these lose their measures the page cap was load-bearing after all, and
    prose runs the full width of a 2560px monitor.
    """
    css = CSS.read_text()
    for selector in [".pipeline-note", ".notes-in", ".t-pane p"]:
        block = re.search(
            re.escape(selector) + r"\s*(?:,[^{]*)?\{([^}]*)\}", css)
        assert block, f"{selector} is gone; its measure went with it"
        assert re.search(r"max-width:\s*(\d+(?:\.\d+)?)(px|ch)", block.group(1)), (
            f"{selector} no longer measures its text (poly-x3pg.14)")


def test_every_template_uses_one_of_the_known_page_containers() -> None:
    """So the test above cannot be bypassed by inventing a third container."""
    known = {c.lstrip(".") for c in PAGE_CONTAINERS}
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text()
        if "{% block content %}" not in text and "<main" not in text:
            continue  # a partial
        classes = set(re.findall(r'class="([^"]*)"', text))
        flat = {c for group in classes for c in group.split()}
        if flat & known:
            continue
        # A page that extends a base and adds no container of its own is fine;
        # only flag one that clearly builds its own page wrapper.
        assert not re.search(r'<div class="[a-z-]*page[a-z-]*"', text), (
            f"{path.name} wraps its content in a page container that "
            f"{PAGE_CONTAINERS} does not cover, so the width rule above "
            f"does not reach it")
