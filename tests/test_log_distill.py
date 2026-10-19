"""The store-side log distiller and what it feeds (poly-m8m).

errors.txt is what triage reads first and what the issue fingerprint is
taken from. The hook's grep pipeline it replaces had compiler vocabulary
only, so ports failures came back with no candidates, and it cut the file
to its first 200000 bytes, so a tail of long compiler command lines lost
its end. x11-servers/xorg-server's run-depends failure, ten lines from the
end of a 1 MB log, was escalated blind thirteen times.

The fixtures are real dsynth logs from the tracker, gzipped as the store
holds them. The three sibling xorg flavors are cut to their failed phase.
Assertions are on sections, not on the whole file: the tail masked this
bug, so a whole-file grep passed while triage saw nothing.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

import pytest

from dportsv3.fingerprint import compute_fingerprint, normalize_error
from dportsv3.log_distill import (
    LINE_CAP,
    TOTAL_BUDGET,
    distill,
    distill_file,
)

FIXTURES = Path(__file__).parent / "fixtures" / "buildlogs"

XORG = "xorg-server@xorg.run-depends.log.gz"
XORG_FLAVORS = (
    XORG,
    "xorg-server@xvfb.run-depends.tail.log.gz",
    "xorg-server@xnest.run-depends.tail.log.gz",
    "xorg-server@xephyr.run-depends.tail.log.gz",
)
SKLEARN = "py-scikit-learn.patch.log.gz"
GLIB = "glib20.build.log.gz"
PKG = "pkg.package.log.gz"
ONE_PER_SHAPE = (XORG, SKLEARN, GLIB, PKG)

_HEAD = re.compile(r"^==\s.*\s==$")


def _log_lines(name: str) -> list[str]:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as fh:
        return fh.read().splitlines()


def _section(text: str, title: str) -> list[str] | None:
    """The body of the section whose heading starts with ``title``."""
    body: list[str] | None = None
    for line in text.splitlines():
        if _HEAD.match(line):
            if body is not None:
                break
            if line.startswith(f"== {title}"):
                body = []
            continue
        if body is not None:
            body.append(line)
    if body is not None:
        while body and not body[-1]:
            body.pop()
    return body


def _candidates(text: str) -> list[str]:
    """Candidate lines without their "N:" log line number."""
    return [re.sub(r"^\d+:", "", c, count=1)
            for c in _section(text, "First error candidates")]


def _build_log(*phases: tuple[str, list[str], str]) -> list[str]:
    """A dsynth-shaped log: (phase, output lines, closing verdict)."""
    rule = "-" * 79
    out = []
    for name, lines, verdict in phases:
        out += [rule, f"-- Phase: {name}", rule, "", *lines, "", verdict, ""]
    return out


# --- what triage gets to see ----------------------------------------------


def test_a_patch_failure_names_the_failed_hunk():
    errors = distill_file(FIXTURES / SKLEARN)
    assert _candidates(errors)[0] == (
        "1 out of 1 hunks failed--saving rejects to sklearn/utils/fixes.py.rej")
    assert normalize_error(errors) == (
        "1 out of 1 hunks failed--saving rejects to fixes.py.rej")


@pytest.mark.parametrize("name", ONE_PER_SHAPE + XORG_FLAVORS[1:])
def test_generic_failure_markers_are_never_candidates(name):
    """Every failed phase prints these. As the first candidate one would
    be the fingerprint of every failure nothing more specific matched:
    the per-port catch-all under a new name."""
    generic = re.compile(r"\*\*\* Error code|^Stop\.$|stopped making|"
                         r"^FAILED \d\d:\d\d:\d\d")
    hits = [c for c in _candidates(distill_file(FIXTURES / name))
            if generic.search(c)]
    assert hits == []


def test_the_xorg_run_depends_failure_reaches_triage():
    log = _log_lines(XORG)
    errors = distill_file(FIXTURES / XORG)

    phase = log[log.index("-- Phase: run-depends") + 1:]
    while not phase[-1]:
        phase.pop()
    assert _section(errors, "Failed phase: run-depends") == phase
    assert _candidates(errors)[0] == (
        "===>   xorg-server-21.1.24,1 depends on file: "
        "/usr/local/lib/dri/libdril_dri.so - not found")
    last = [line for line in log if line.strip()][-1]
    assert errors.rstrip("\n").splitlines()[-1] == last


def test_the_four_xorg_flavors_are_one_issue():
    """Each flavor names its own package (xorg-vfbserver, xorg-nestserver
    with epoch 2, xephyr). The failure is one."""
    prints = {compute_fingerprint(distill_file(FIXTURES / n))
              for n in XORG_FLAVORS}
    assert len(prints) == 1 and None not in prints
    assert normalize_error(distill_file(FIXTURES / XORG)) == (
        "depends on file: libdril_dri.so - not found")


@pytest.mark.parametrize("name", ONE_PER_SHAPE)
def test_the_failed_phase_and_the_candidates_fit_in_triages_head(name):
    """Triage and the patch agent see the first and last 16 KB
    (runner.context_file_cap); everything else is for a person."""
    errors = distill_file(FIXTURES / name)
    assert _section(errors, "Failed phase") is not None
    assert errors.index("== Error blocks") < 16 * 1024


def test_a_compile_failure_keeps_the_compiler_error_first():
    errors = distill_file(FIXTURES / GLIB)
    assert _candidates(errors)[0] == (
        "../glib/glib-unix.c:1010:10: error: variable 'kf' has initializer "
        "but incomplete type")


# --- the shape of the file -------------------------------------------------


def test_long_lines_are_cut_and_the_file_is_bounded():
    errors = distill_file(FIXTURES / XORG)
    longest = max(len(line) for line in errors.splitlines())
    # A capped line plus its marker, plus a block's "NNNNN-" prefix.
    assert longest <= LINE_CAP + 40
    assert len(errors) <= TOTAL_BUDGET


def test_a_long_failed_phase_keeps_its_end():
    noise = [f"compiling unit {i:05d} " + "x" * 80 for i in range(2000)]
    log = _build_log(("configure", ["ok"], "SUCCEEDED 00:00:01"),
                     ("build", [*noise, "the last thing it said"],
                      "FAILED 00:00:09"))
    phase = _section(distill(log), "Failed phase: build")
    assert phase[0].startswith("[... first ")
    assert phase[-1] == "FAILED 00:00:09"
    assert "the last thing it said" in phase


def test_a_failure_far_from_the_end_still_reaches_errors_txt():
    """bmake keeps other jobs running after the first error, so the error
    can be hundreds of lines before the end."""
    later = [f"[{i}/900] cc -c unit{i}.c" for i in range(400)]
    log = _build_log(("build", ["foo.c:12:3: error: 'X' undeclared", *later],
                      "FAILED 00:01:00"))
    assert _candidates(distill(log)) == ["foo.c:12:3: error: 'X' undeclared"]


def test_a_hit_in_an_earlier_phase_is_not_a_candidate():
    log = _build_log(
        ("configure", ["checking for x.h: No such file or directory"],
         "SUCCEEDED 00:00:02"),
        ("stage", ["something went wrong in a way nothing matches"],
         "FAILED 00:00:01"),
    )
    errors = distill(log)
    assert _candidates(errors) == []
    assert normalize_error(errors) == "failed phase: stage"


def test_a_builds_own_phase_line_does_not_restart_the_scope():
    """Only dsynth's marker, under its rule of dashes, starts a phase."""
    log = _build_log(("build", ["foo.c:1:1: error: real", "-- Phase: fake",
                                "more output"], "FAILED 00:00:03"))
    errors = distill(log)
    assert _section(errors, "Failed phase: build") is not None
    assert _candidates(errors) == ["foo.c:1:1: error: real"]


def test_blocks_separate_groups_like_grep():
    lines = (["a", "b", "x.c:1:1: error: one", "c", "d"]
             + [f"filler {i}" for i in range(10)]
             + ["e", "f", "x.c:9:1: error: two", "g"])
    blocks = _section(distill(lines), "Error blocks")
    assert blocks == [
        "1-a", "2-b", "3:x.c:1:1: error: one", "4-c", "5-d",
        "--",
        "16-e", "17-f", "18:x.c:9:1: error: two", "19-g",
    ]


# --- logs without phase markers keep the old behaviour --------------------


def test_a_log_without_phases_is_searched_whole():
    errors = distill(["===>  Building for vim-9.1",
                      "buffer.c:120:5: error: use of undeclared identifier",
                      "*** Error code 1"])
    assert _section(errors, "Failed phase") is None
    assert _candidates(errors) == [
        "buffer.c:120:5: error: use of undeclared identifier"]


def test_a_log_without_phases_or_candidates_has_no_fingerprint():
    errors = distill(["=> Fetching rustc-1.85.1-src.tar.xz", "fetch: 404"])
    assert compute_fingerprint(errors) is None


# --- the store distills at upsert ------------------------------------------


@pytest.fixture
def store(tmp_path):
    from dportsv3.artifact_store import ArtifactStore
    return ArtifactStore.from_evidence_root(tmp_path / "evidence")


def _upsert(store, bundle_id, **extra):
    payload = {
        "run_id": "r1", "profile": "p", "ts_utc": "2026-10-16T23:39:53Z",
        "bundle_id": bundle_id, "origin": "x11-servers/xorg-server",
        "flavor": "xorg", "result": "failure", "target": "@2026Q3",
    }
    payload.update(extra)
    store.upsert_run_bundle(payload)


def _stored(store, bundle_id, relpath) -> bytes | None:
    found = store.get_artifact(bundle_id, relpath)
    return None if found is None else found[1].read_bytes()


def _sig(store, bundle_id):
    return store.conn.execute(
        "SELECT error_signature FROM bundles WHERE bundle_id = ?",
        (bundle_id,)).fetchone()[0]


def test_the_store_distills_a_full_log_uploaded_first(store):
    store.put_blob("b1", "logs/full.log.gz",
                   (FIXTURES / XORG).read_bytes(), "gzip")
    _upsert(store, "b1", errors_text="cc: error: an older hook's guess\n")

    errors = _stored(store, "b1", "logs/errors.txt").decode()
    assert errors == distill_file(FIXTURES / XORG)
    assert _sig(store, "b1") == compute_fingerprint(errors)


def test_without_a_stored_log_the_sent_text_is_used(store):
    """A hook that has not been updated uploads the log after the upsert
    and sends its own distillation; that is what it fingerprints by."""
    sent = "cc: error: from the hook\n"
    _upsert(store, "b1", errors_text=sent)
    assert _sig(store, "b1") == compute_fingerprint(sent)
    assert _stored(store, "b1", "logs/errors.txt") is None


def test_a_reupsert_does_not_distill_again(store):
    store.put_blob("b1", "logs/full.log.gz",
                   (FIXTURES / SKLEARN).read_bytes(), "gzip")
    _upsert(store, "b1")
    first = _sig(store, "b1")
    _upsert(store, "b1", ts_utc="2026-10-16T23:59:00Z")
    puts = store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'artifact_put' "
        "AND data_json LIKE '%logs/errors.txt%'").fetchone()[0]
    assert puts == 1
    assert _sig(store, "b1") == first


def test_an_unreadable_log_still_records_the_occurrence(store):
    store.put_blob("b1", "logs/full.log.gz", b"\x1f\x8bnot gzip at all",
                   "gzip")
    _upsert(store, "b1")
    assert _sig(store, "b1") is None
    assert _stored(store, "b1", "logs/errors.txt") is None
    assert store.conn.execute(
        "SELECT 1 FROM bundles WHERE bundle_id = 'b1'").fetchone()
