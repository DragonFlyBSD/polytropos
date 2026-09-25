"""A font the stylesheet names is a font the tracker ships (poly-x3pg.13).

``--sans`` named Inter from the day the tokens were lifted off the mock, and
nothing served it: no ``@font-face``, no woff2, and no Google Fonts link --
which would not have worked anyway, since the tracker serves on a private LAN
with no egress. So the console rendered in Inter only for viewers who happened
to have it installed, silently, with no error and nothing in a log.

The guard below is the general one rather than "Inter is present": it reads the
families out of the font stacks and requires every non-system face to have an
``@font-face`` whose file exists. Naming a face the next operator does not have
fails here instead of at a side-by-side months later.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.server import create_app

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "dportsv3" / "tracker" / "static"
CSS = STATIC / "progress.css"

# Faces an operating system provides, so naming one ships nothing. Generic
# families and the CSS-wide keywords belong here for the same reason.
SYSTEM_FACES = {
    "sans-serif", "serif", "monospace", "system-ui",
    "ui-sans-serif", "ui-serif", "ui-monospace", "ui-rounded",
    "-apple-system", "blinkmacsystemfont",
    "segoe ui", "sfmono-regular", "sf mono", "consolas", "menlo", "monaco",
    "liberation mono", "dejavu sans mono", "courier new", "roboto",
    "helvetica neue", "helvetica", "arial",
    # keywords a stack may legitimately end on
    "inherit", "initial", "unset", "revert",
}


def _stack(name: str) -> list[str]:
    """The families listed in one custom property, in order."""
    css = CSS.read_text()
    m = re.search(rf"^\s*--{name}:\s*([^;]+);", css, re.M)
    assert m, f"--{name} is not declared in progress.css"
    return [f.strip().strip('"\'') for f in m.group(1).split(",") if f.strip()]


def _font_faces() -> dict[str, str]:
    """family (lowercased) -> the url() its src points at."""
    css = CSS.read_text()
    out: dict[str, str] = {}
    for block in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        fam = re.search(r"font-family:\s*([^;]+);", block)
        src = re.search(r"url\(\s*[\"']?([^\"')]+)", block)
        if fam and src:
            out[fam.group(1).strip().strip('"\'').lower()] = src.group(1).strip()
    return out


@pytest.mark.parametrize("prop", ["sans", "mono"])
def test_every_named_face_is_shipped_or_provided_by_the_os(prop: str) -> None:
    faces = _font_faces()
    for family in _stack(prop):
        if family.lower() in SYSTEM_FACES:
            continue
        assert family.lower() in faces, (
            f"--{prop} names {family!r}, which no OS provides and no "
            f"@font-face in progress.css declares. Either ship it as a woff2 "
            f"under static/ or take it out of the stack -- naming a font and "
            f"not shipping it renders differently on every machine."
        )
        target = STATIC / faces[family.lower()]
        assert target.is_file(), (
            f"@font-face for {family!r} points at {faces[family.lower()]!r}, "
            f"which is not in the static tree"
        )


def test_inter_is_the_first_choice_in_the_sans_stack() -> None:
    # The mock's own stack, and the face the design was drawn in. The rest of
    # the stack is the fallback for the code points the latin subset omits.
    assert _stack("sans")[0] == "Inter"


def test_the_variable_face_covers_every_weight_the_stylesheet_asks_for() -> None:
    """One file, not six.

    The stylesheet asks for 350, 400, 500, 600, 650, 700, 750, 850 and 900.
    A static instance would snap the odd ones to a neighbour; the variable
    range renders them as written.
    """
    block = re.search(
        r"@font-face\s*\{[^}]*font-family:\s*Inter[^}]*\}",
        CSS.read_text(), re.S | re.I)
    assert block, "no @font-face for Inter"
    assert re.search(r"font-weight:\s*100\s+900", block.group(0)), (
        "the Inter face must declare the variable range font-weight: 100 900")
    weights = {
        int(w) for w in re.findall(r"font:\s*(\d{3})\s[^;]*var\(--sans\)",
                                  CSS.read_text())}
    assert weights, "no weighted --sans rules found; the regex has rotted"
    assert min(weights) >= 100 and max(weights) <= 900


def test_the_licence_ships_beside_the_font() -> None:
    # SIL OFL 1.1 requires the licence to travel with the font.
    licence = STATIC / "fonts" / "Inter-LICENSE.txt"
    assert licence.is_file()
    assert "SIL OPEN FONT LICENSE" in licence.read_text().upper()


def test_the_font_is_served(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    init_db(db)
    db.close()
    with TestClient(create_app(path)) as client:
        url = "/static/" + _font_faces()["inter"]
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.content[:4] == b"wOF2", "not a woff2 payload"


def test_no_stylesheet_reaches_for_a_remote_font_host() -> None:
    """The mock's Google Fonts link cannot work here and must not be copied.

    The tracker is reachable on a private LAN with no egress: a stylesheet
    that waits on fonts.googleapis.com blocks first paint for the timeout and
    then renders in the fallback anyway.
    """
    for path in [*STATIC.rglob("*.css"),
                 *(REPO / "dportsv3" / "tracker" / "templates").rglob("*.html")]:
        # Comments may name the host -- the one above this stylesheet's
        # @font-face explains why the mock's link is not copied. What matters
        # is whether anything FETCHES from it.
        text = re.sub(r"/\*.*?\*/|<!--.*?-->|\{#.*?#\}", "",
                      path.read_text(errors="ignore"), flags=re.S)
        for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
            assert host not in text, f"{path} fetches from {host}"
