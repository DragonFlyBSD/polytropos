"""The invariants the shared shell rests on.

Each of these has a specific failure it prevents, and each one had actually
happened in the sheet this replaced: a variable used with a hardcoded
fallback silently ignores the theme; a colour defined only inside a media
block paints one theme's text on the other theme's ground; 12px monospace
for the whole document is why every page read as terminal output.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path("dportsv3/tracker/static/progress.css")
TEMPLATES = sorted(Path("dportsv3/tracker/templates").glob("*.html"))


def _css() -> str:
    return CSS.read_text()


def _defined() -> set[str]:
    return set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", _css(), re.M))


def _block(pattern: str) -> str:
    m = re.search(pattern, _css(), re.S | re.M)
    assert m, f"block not found: {pattern}"
    return m.group(1)


def test_every_token_used_is_defined():
    used = set(re.findall(r"var\((--[a-z0-9-]+)", _css()))
    assert used - _defined() == set()


def test_templates_use_no_undefined_token():
    """An inline style reaching for an undefined name falls through to its
    hardcoded fallback, which is how a page keeps one theme's colours in
    the other. There were 18 of these."""
    defined = _defined()
    stray = {
        (t.name, v)
        for t in TEMPLATES
        for v in re.findall(r"var\((--[a-z0-9-]+)", t.read_text())
        if v not in defined
    }
    assert stray == set()


def test_no_colour_is_defined_only_in_a_dark_block():
    """The default "system" theme stamps nothing on the root element, so a
    token that exists only under [data-theme] or the media query is simply
    absent for most viewers."""
    root = set(re.findall(r"(--[a-z0-9-]+)\s*:", _block(r"^:root \{(.*?)\n\}")))
    media = set(re.findall(r"(--[a-z0-9-]+)\s*:", _block(
        r"@media \(prefers-color-scheme: dark\) \{(.*?)\n\}\n")))
    attr = set(re.findall(r"(--[a-z0-9-]+)\s*:", _block(
        r':root\[data-theme="dark"\] \{(.*?)\n\}')))

    assert media - root == set()
    assert attr - root == set()
    assert media == attr, "the two dark paths must define the same set"


def test_the_stylesheet_carries_no_colour_outside_the_token_block():
    """Every colour goes through a token, or it cannot follow the theme."""
    stray, sel = [], None
    for line in _css().split("\n"):
        m = re.match(r"^([^\s/@}][^{]*)\{", line)
        if m:
            sel = m.group(1).strip()
        # every :root variant is a token block: the bare one and both dark paths
        if sel and sel.startswith(":root"):
            continue
        if re.search(r"#[0-9a-fA-F]{3,8}\b", line):
            stray.append((sel, line.strip()))
    assert stray == []


def test_body_is_not_set_in_monospace():
    """Monospace is for origins, IDs, metrics and evidence. The old sheet set
    the whole document in it."""
    body = _block(r"\nbody \{(.*?)\n\}")
    assert "var(--sans)" in body
    assert "monospace" not in body


@pytest.mark.parametrize("source", ["css", "templates"])
def test_nothing_essential_is_below_twelve_pixels(source):
    texts = [_css()] if source == "css" else [t.read_text() for t in TEMPLATES]
    small = [
        m.group(0)
        for text in texts
        for m in re.finditer(r"font-size:\s*(\d+(?:\.\d+)?)px", text)
        if float(m.group(1)) < 12
    ]
    assert small == []


def _ratio(a: str, b: str) -> float:
    def lum(h: str) -> float:
        h = h.lstrip("#")
        parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b_ = (f(x) for x in parts)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _token(block: str, name: str) -> str:
    m = re.search(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", block)
    assert m, f"{name} not found"
    return m.group(1)


@pytest.mark.parametrize("name", ["--cyan", "--red", "--amber", "--green",
                                  "--violet", "--muted", "--faint", "--text"])
def test_every_accent_clears_aa_on_the_grounds_it_sits_on(name):
    """4.5:1 is AA for normal text. Measured against BOTH the page ground and
    the panel surface, because an accent is used on each."""
    light = _block(r"^:root \{(.*?)\n\}")
    dark = _block(r':root\[data-theme="dark"\] \{(.*?)\n\}')
    for block, grounds in ((light, ("--bg", "--surface")),
                           (dark, ("--bg", "--surface"))):
        fg = _token(block, name)
        for ground in grounds:
            assert _ratio(fg, _token(block, ground)) >= 4.5, (
                f"{name} on {ground}")
