"""One answer to "which port is this row about", for every view that asks.

poly-1v7l, second pass. The first fix taught the TAIL about dsynth's
FLAVOR=$ORIGIN sentinel and corrected the job page's heading -- and left
five other renderings of the same value untouched, including the one the
bead quoted verbatim:

    Origin   print/qt6-pdf @ print/qt6-pdf

which is _job_summary.html, not the heading. Measured on the builder the
day after: 802 of 1480 jobs and 476 of 581 bundles carry the sentinel, and
105 bundles carry a real flavour that must still render. Six templates
each spelled the condition themselves; one of them, agentic_bundles.html,
prints the flavour in a column of its own, so for 476 bundles that column
read "print/qt6-pdf".

build_results and port_status are NOT affected: they go through
_port_identity on the way in. These two tables are written by other paths
and are normalised on the way out, which is what port_label/real_flavor
are for.
"""

from __future__ import annotations

import pytest

from dportsv3.tracker.db import port_label, real_flavor


# --- the sentinel ---------------------------------------------------------

@pytest.mark.parametrize("flavor", ["print/qt6-pdf", "@print/qt6-pdf"])
def test_a_flavor_that_is_the_origin_is_not_a_flavor(flavor: str) -> None:
    assert real_flavor("print/qt6-pdf", flavor) == ""
    assert port_label("print/qt6-pdf", flavor) == "print/qt6-pdf"


def test_a_real_flavor_is_kept_and_shown() -> None:
    assert real_flavor("devel/glib20", "bootstrap") == "bootstrap"
    assert port_label("devel/glib20", "bootstrap") == "devel/glib20 @ bootstrap"


def test_no_flavor_at_all_is_just_the_origin() -> None:
    assert real_flavor("editors/vim", "") == ""
    assert port_label("editors/vim", "") == "editors/vim"
    assert port_label("editors/vim") == "editors/vim"


def test_an_origin_carrying_its_own_flavor_is_split() -> None:
    """The other spelling of the same fact, and _port_identity already
    accepted it -- so the label is the same either way round."""
    assert real_flavor("devel/glib20@bootstrap", "") == "bootstrap"
    assert port_label("devel/glib20@bootstrap", "") == "devel/glib20 @ bootstrap"
    assert port_label("devel/glib20@bootstrap", "bootstrap") == (
        port_label("devel/glib20", "bootstrap"))


def test_the_separator_is_the_caller_s(and_=None) -> None:
    """The job heading sets it tight inside a <code>; the summary tables
    want it spaced. One rule, two looks, rather than two conditions."""
    assert port_label("devel/glib20", "bootstrap", "@") == "devel/glib20@bootstrap"


def test_a_disagreement_is_shown_not_raised_and_not_hidden() -> None:
    """_port_identity raises when an origin suffix and a flavour column
    disagree. A view is the wrong place to discover that and a worse
    place to swallow it, so both parts stay on screen."""
    label = port_label("devel/glib20@bootstrap", "default")

    assert "glib20" in label and "default" in label


def test_nothing_blows_up_on_empty_or_missing() -> None:
    """These come straight off a DB row, so None is a real input."""
    assert port_label("", "") == ""
    assert port_label(None, None) == ""          # type: ignore[arg-type]
    assert real_flavor(None, None) == ""         # type: ignore[arg-type]


# --- no template spells the rule itself any more --------------------------

def test_no_template_reimplements_the_sentinel_check() -> None:
    """The point of the helper. Six templates each had their own
    condition and they had already drifted -- five rendered " @ " and one
    "@", and the first pass at this bead fixed exactly one of them.
    """
    from pathlib import Path

    templates = Path(__file__).resolve().parents[1] / "dportsv3" / "tracker" / "templates"
    import re

    # A flavour handed TO the helper is the point; one read raw is the bug.
    call = re.compile(r"(port_label|real_flavor)\([^)]*\)")

    def raw(line: str) -> bool:
        return any(name in call.sub("", line)
                   for name in ("job.flavor", "bundle.flavor"))

    offenders = {
        p.name: [ln.strip() for ln in p.read_text().splitlines() if raw(ln)]
        for p in templates.glob("*.html")
    }
    offenders = {k: v for k, v in offenders.items() if v}

    assert offenders == {}, (
        "a template is reading a raw flavour again; use port_label() or "
        "real_flavor() so it cannot disagree with the others"
    )


# --- and the rendered page, because source checks miss the cascade --------

def test_the_rendered_job_page_shows_no_port_twice() -> None:
    """The check above reads templates; this one reads a page.

    poly-atq9 is the argument for both: a stylesheet assertion could not
    see another rule winning, and a template assertion cannot see a
    template it did not think to look at. The first pass at this bead
    fixed the heading and left the summary table, and every source-level
    check still passed.
    """
    import sqlite3
    from pathlib import Path
    import tempfile

    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dportsv3.db.schema import init_db
    from dportsv3.tracker.server import create_app

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.db"
        db = sqlite3.connect(str(path))
        db.row_factory = sqlite3.Row
        init_db(db)
        db.execute(
            "INSERT INTO jobs (job_id, state, type, origin, flavor, target, "
            "created_ts_utc) VALUES ('j-1', 'done', 'patch', 'print/qt6-pdf', "
            "'print/qt6-pdf', '@2026Q3', 't0')")
        db.execute(
            "INSERT INTO jobs (job_id, state, type, origin, flavor, target, "
            "created_ts_utc) VALUES ('j-2', 'done', 'patch', 'devel/glib20', "
            "'bootstrap', '@2026Q3', 't0')")
        db.commit()
        db.close()

        with TestClient(create_app(path)) as client:
            sentinel = client.get("/agentic/jobs/j-1").text
            flavored = client.get("/agentic/jobs/j-2").text

    assert "print/qt6-pdf @ print/qt6-pdf" not in sentinel
    assert "print/qt6-pdf@print/qt6-pdf" not in sentinel
    assert "print/qt6-pdf" in sentinel, "the origin itself went missing"
    # The other half: a real flavour must still reach the page.
    assert "devel/glib20 @ bootstrap" in flavored
