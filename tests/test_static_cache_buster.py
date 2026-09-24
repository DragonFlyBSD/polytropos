"""Every static file moves the cache-buster, not just the CSS (poly-3o3o).

Found deploying poly-pvs2. ``static_v`` hashed progress.css alone while the
JS tags interpolated the same token, so a JS-only change left the URL
byte-identical, every warm browser cache kept the old script, and the
server's new payload was silently ignored by it. No error anywhere -- the
feature just did not happen.
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
TEMPLATES = REPO / "dportsv3" / "tracker" / "templates"


def _static_v(tmp_path: Path, static_dir: Path | None = None) -> str:
    """The token the app computes, read off a rendered page."""
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    init_db(db)
    db.close()
    app = create_app(path)
    if static_dir is not None:
        app.state.static_dir = static_dir
    with TestClient(app) as client:
        body = client.get("/agentic").text
    found = re.search(r"progress\.css\?v=([0-9a-f]+)", body)
    assert found, "no versioned stylesheet on the page"
    return found.group(1)


def test_every_versioned_asset_uses_the_token() -> None:
    """A tag that versions nothing is the bug in a smaller form."""
    for name in ("_base.html", "agentic_job.html",
                 "agentic_job_transcript.html"):
        text = (TEMPLATES / name).read_text()
        for asset in re.findall(r"path='(progress\.css|[\w-]+\.js)'\)\}\}([^\"']*)",
                                text):
            assert "?v=" in asset[1], f"{name}: {asset[0]} is unversioned"


def _hash_tree(root: Path) -> str:
    """What the token is supposed to be: every file's path and bytes."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def test_the_token_is_the_hash_of_the_whole_static_tree(
    tmp_path: Path,
) -> None:
    """THE regression, pinned as a contract rather than by re-running the
    loop: the token the app serves must equal the hash of EVERY static
    file. Hashing any subset -- progress.css alone, as it was -- fails
    here, and that subset is what let a JS-only change ship invisibly.
    """
    assert _static_v(tmp_path) == _hash_tree(STATIC)


def test_a_js_only_change_moves_the_token(tmp_path: Path) -> None:
    """What a JS-only deploy looks like. Under the old behaviour the token
    did not move, the URL stayed byte-identical, and every warm browser
    cache kept the previous script."""
    shadow = tmp_path / "static"
    shadow.mkdir()
    for src in STATIC.iterdir():
        if src.is_file():
            (shadow / src.name).write_bytes(src.read_bytes())
    before = _hash_tree(shadow)

    js = shadow / "agentic-job.js"
    js.write_text(js.read_text() + "\n// one more line\n")

    assert _hash_tree(shadow) != before


def test_the_token_still_covers_the_css(tmp_path: Path) -> None:
    """The original behaviour, kept: this is what it was written for."""
    shadow = tmp_path / "static"
    shadow.mkdir()
    for src in STATIC.iterdir():
        if src.is_file():
            (shadow / src.name).write_bytes(src.read_bytes())
    before = _hash_tree(shadow)

    css = shadow / "progress.css"
    css.write_text(css.read_text() + "\n/* x */\n")

    assert _hash_tree(shadow) != before


def test_a_rename_moves_the_token(tmp_path: Path) -> None:
    """The relative path is hashed as well as the bytes, so a rename or a
    deletion counts as a change even when every byte is still present."""
    root = tmp_path / "static"
    root.mkdir()
    (root / "one.js").write_text("x")
    before = _hash_tree(root)

    (root / "one.js").rename(root / "two.js")

    assert _hash_tree(root) != before


def test_the_page_still_carries_a_token(tmp_path: Path) -> None:
    assert re.fullmatch(r"[0-9a-f]{10}", _static_v(tmp_path))
