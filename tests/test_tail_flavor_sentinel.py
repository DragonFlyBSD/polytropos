"""dsynth's FLAVOR=$ORIGIN is "no flavor", not a flavor called print/qt6-pdf.

poly-1v7l. A job row reading

    origin  print/qt6-pdf
    flavor  print/qt6-pdf

looks like a field filled from the wrong variable and is not: dsynth
exports ``FLAVOR=$ORIGIN`` for a port that has no flavour. The hooks say
so and honour it twice -- ``logfile_for_origin`` and the bundle id in
``hook_pkg_failure`` -- and the tracker learned it in ``_port_identity``.
The tail never did, and a "flavour" containing a ``/`` can match no log
filename, so the match fell through to "newest by mtime" and the page
showed an operator the last lines of a build they were not looking at.

Two halves, and the second is what made it invisible: a flavour that
matches nothing must be a fact, not a licence to show another flavour's
build. It cost real time once already -- during poly-5qtr this was the
competing explanation for a job that looked stuck, and had to be ruled
out by hand before the real cause could be named.
"""

from __future__ import annotations

import types

import pytest

from dportsv3.agent import dsynth_tail, runner as rm, worker

SLAVE = "print/qt6-pdf"
MASTER = "www/qt6-webengine"


@pytest.fixture(autouse=True)
def no_leaked_target():
    rm.clear_tail_target()
    yield
    rm.clear_tail_target()


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """A real chroot logs dir -- the filenames are the thing under test."""
    d = tmp_path / "writable" / "work" / "dsynth" / "logs"
    d.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda env: types.SimpleNamespace(writable=tmp_path / "writable"),
    )

    def write(name: str, text: str = "building\n"):
        path = d / name
        path.write_text(text)
        return path

    write.dir = d
    return write


# --- the sentinel ---------------------------------------------------------


@pytest.mark.parametrize("flavor", [
    "print/qt6-pdf",      # exactly as dsynth exports it
    "@print/qt6-pdf",     # and with the @ the hooks sometimes prepend
])
def test_a_flavor_that_is_the_origin_means_unflavored(flavor: str) -> None:
    assert dsynth_tail.real_flavor(SLAVE, flavor) == ""


def test_the_sentinel_is_recognised_against_a_suffixed_origin() -> None:
    """``origin`` itself can arrive carrying its flavour. The sentinel is
    still the port, so compare against the base as the hooks do."""
    assert dsynth_tail.real_flavor("devel/glib20@bootstrap", "devel/glib20") == ""


def test_a_real_flavor_survives() -> None:
    assert dsynth_tail.real_flavor("devel/glib20", "bootstrap") == "bootstrap"
    assert dsynth_tail.real_flavor("devel/glib20", "@bootstrap") == "bootstrap"


# --- which log gets tailed ------------------------------------------------


def test_the_sentinel_does_not_cost_the_port_its_log(logs) -> None:
    """The reported shape. Before the fix the wanted name was
    ``print___qt6-pdf@print/qt6-pdf.log``, which no directory entry can
    match, so this fell through to newest-by-mtime and was right only by
    accident -- on a port with one log."""
    plain = logs("print___qt6-pdf.log")

    assert dsynth_tail.log_path("e", SLAVE, SLAVE) == plain


def test_a_real_flavor_beats_a_newer_log_of_another_flavor(logs) -> None:
    """The accident ending. Two flavours, the asked-for one older: the
    fallthrough returned the newest, i.e. the wrong build."""
    wanted = logs("devel___glib20@bootstrap.log")
    newer = logs("devel___glib20@default.log")
    import os
    os.utime(wanted, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))

    assert dsynth_tail.log_path("e", "devel/glib20", "bootstrap") == wanted


def test_a_flavor_with_no_log_is_none_not_someone_elses_build(logs) -> None:
    """THE defect. A flavour that matches nothing used to return the
    newest candidate, so the page showed a different flavour's build with
    nothing saying so."""
    logs("devel___glib20@default.log")

    assert dsynth_tail.log_path("e", "devel/glib20", "bootstrap") is None


def test_the_error_names_the_flavor_it_could_not_find(logs) -> None:
    """"no log for devel/glib20" and "no log for devel/glib20@bootstrap"
    are different facts, and only the second explains a page that stays
    empty while a build is plainly running."""
    logs("devel___glib20@default.log")

    out = dsynth_tail.read_tail("e", "devel/glib20", flavor="bootstrap")

    assert out["ok"] is False
    assert "devel/glib20@bootstrap" in out["error"]


def test_an_unflavored_port_with_no_log_says_so_without_a_flavor(logs) -> None:
    out = dsynth_tail.read_tail("e", SLAVE, flavor=SLAVE)

    assert out["ok"] is False
    assert out["error"] == f"dsynth has written no log for {SLAVE} yet"


# --- the sibling run poly-quu3 fixed --------------------------------------


def test_the_sentinel_is_resolved_once_against_the_jobs_own_port() -> None:
    """The regression this fix could easily have caused.

    ``building_origin`` hands the SAME flavour to every origin in the
    run. Left as the sentinel, it reads as a real flavour beside the
    master -- no ``www___qt6-webengine@print/qt6-pdf.log`` can exist --
    so the master would be skipped, the tail would sit on the finished
    slave, and that is exactly the freeze poly-quu3 fixed. Resolving it
    in set_tail_target, against origins[0], is what keeps that from
    happening.
    """
    rm.set_tail_target("e", [SLAVE, MASTER], SLAVE, "job-1", "dsynth_build")
    target = rm._tail_target

    assert target is not None
    assert target["flavor"] == ""
    assert target["origins"] == [SLAVE, MASTER]


def test_the_master_is_still_reachable_under_a_sentinel_flavor(logs) -> None:
    """The same thing one layer down, on real filenames: with the flavour
    resolved, both ports resolve by newest log and the tail moves across
    when the slave finishes."""
    logs("print___qt6-pdf.log")
    master = logs("www___qt6-webengine.log")
    import os
    os.utime(master, (1_800_000_000, 1_800_000_000))

    rm.set_tail_target("e", [SLAVE, MASTER], SLAVE, "job-1", "dsynth_build")
    target = rm._tail_target
    assert target is not None
    flavor = target["flavor"]

    assert dsynth_tail.building_origin("e", [SLAVE, MASTER], flavor) == MASTER


def test_a_real_flavor_still_narrows_the_sibling_search(logs) -> None:
    """Not a regression: a genuine flavour SHOULD exclude a sibling that
    has no log for it, rather than fall back to that sibling's newest."""
    wanted = logs("devel___glib20@bootstrap.log")
    logs("www___qt6-webengine.log")

    assert dsynth_tail.building_origin(
        "e", ["devel/glib20", MASTER], "bootstrap") == "devel/glib20"
    assert dsynth_tail.log_path("e", "devel/glib20", "bootstrap") == wanted
