"""The agent's make calls skip dependency resolution (poly-0oe).

A dsynth builder installs the port's dependencies out of the repo dsynth
built itself, so inside a builder every version matches the tree. The
dev-env chroot has no such repo — its /usr/local comes from a base
provisioned once from the official mirror, null-mounted read-only and
shared across envs, so nothing can install a tree-matched package into
it.

``*-depends`` resolve with ``DEPENDS_TARGET=install``, which walks into
each unsatisfied dependency and builds it. Against a mismatched
/usr/local that recursion dies several ports deep, before do-extract
ever runs. Measured on hardware: extract died on
gobject-introspection -> bison -> m4 -> texinfo -> help2man ->
p5-Locale-gettext -> gettext-runtime, none of which the agent needs to
unpack a distfile.

bsd.port.mk guards every depends type on one variable::

    .for deptype in PKG EXTRACT PATCH FETCH BUILD LIB RUN TEST
    ${deptype:tl}-depends:
    .  if defined(${deptype}_DEPENDS) && !defined(NO_DEPENDS)

Verified against a real ports tree: with NO_DEPENDS=yes,
``extract-depends`` emits zero commands where it otherwise runs
do-depends.sh.
"""

from __future__ import annotations

import subprocess

import pytest

from dportsv3.agent import worker


def _capture(monkeypatch, *, rc: int = 0, probe_stdout: str = "") -> list[str]:
    """Record every shell payload; answer the -V queries plausibly."""
    payloads: list[str] = []

    def fake(env, *argv, **kw):
        cmd = argv[-1]
        payloads.append(cmd)
        if "-V WRKDIR" in cmd:
            return subprocess.CompletedProcess(
                argv, 0, "/work/obj/work\n/work/obj/work/foo-1.0\n", "")
        if "-V IGNORE" in cmd or "-V EXTRACT_DEPENDS" in cmd:
            return subprocess.CompletedProcess(argv, 0, probe_stdout, "")
        return subprocess.CompletedProcess(argv, rc, "tar: truncated\n", "")

    monkeypatch.setattr(worker, "_exec", fake)
    return payloads


# --- the flag is actually passed -------------------------------------------

def test_extract_skips_dependency_resolution(monkeypatch) -> None:
    payloads = _capture(monkeypatch)
    worker.make_extract("env", "devel/foo")
    assert "NO_DEPENDS=yes" in payloads[0]


def test_patch_skips_dependency_resolution(monkeypatch) -> None:
    """do-patch runs patch-depends too, and applies patches with the
    base `patch`. Same reasoning, same flag."""
    payloads = _capture(monkeypatch)
    worker.make_patch("env", "devel/foo")
    assert "NO_DEPENDS=yes" in payloads[0]


def test_the_flag_reaches_make_not_just_the_shell(monkeypatch) -> None:
    """It has to be a make variable assignment on the command line —
    an exported environment variable of the same name would not be
    seen by `.if defined(NO_DEPENDS)`."""
    payloads = _capture(monkeypatch)
    worker.make_extract("env", "devel/foo")
    payload = payloads[0]
    assert payload.index("make ") < payload.index("NO_DEPENDS=yes")
    assert "export NO_DEPENDS" not in payload


# --- what it must not disturb ----------------------------------------------

def test_the_wrksrc_query_is_left_alone(monkeypatch) -> None:
    """`make -V` evaluates variables and runs no targets, so the flag
    would be noise there — and NO_DEPENDS changes none of WRKSRC,
    PATCHDIR, PATCH_LIST, EXTRACT_ONLY or DISTFILES (verified against
    a real ports tree)."""
    payloads = _capture(monkeypatch)
    worker.make_extract("env", "devel/foo")
    query = next(p for p in payloads if "-V WRKDIR" in p)
    assert "NO_DEPENDS" not in query


def test_extract_still_targets_the_compose_root(monkeypatch) -> None:
    payloads = _capture(monkeypatch)
    res = worker.make_extract("env", "devel/foo")
    assert all("$DPORTS_COMPOSE_ROOT" in p for p in payloads)
    assert all("/work/DPorts" not in p for p in payloads)
    assert res["wrksrc"] == "/work/obj/work/foo-1.0"


def test_an_ignored_port_is_still_blocking(monkeypatch) -> None:
    """poly-n78's abort must survive: NO_DEPENDS does not make an
    IGNOREd port buildable, and the agent must still stop."""
    _capture(monkeypatch, rc=1, probe_stdout="Invalid perl5 version 5.36\n")
    out = worker.make_extract("env", "devel/foo")
    assert out["blocking"] is True
    assert out["ignore_reason"] == "Invalid perl5 version 5.36"


# --- the missing-unarchiver diagnostic -------------------------------------

def test_a_failure_names_the_extract_depends_we_skipped(monkeypatch) -> None:
    """The one real cost of the flag: a port needing a genuine
    unarchiver now fails inside do-extract. Untreated that reads as a
    corrupt distfile, so say which dependency was skipped."""
    payloads: list[str] = []

    def fake(env, *argv, **kw):
        cmd = argv[-1]
        payloads.append(cmd)
        if "-V IGNORE" in cmd:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "-V EXTRACT_DEPENDS" in cmd:
            return subprocess.CompletedProcess(
                argv, 0, "zstd:archivers/zstd\n", "")
        return subprocess.CompletedProcess(argv, 1, "tar: unknown format\n", "")

    monkeypatch.setattr(worker, "_exec", fake)
    out = worker.make_extract("env", "devel/foo")

    assert out["ok"] is False
    assert out["extract_depends"] == "zstd:archivers/zstd"
    assert "zstd:archivers/zstd" in out["summary"]
    assert "environment problem" in out["summary"]
    assert "blocking" not in out, "a missing tool is not an IGNORE verdict"


def test_a_port_with_no_extract_depends_gets_no_such_claim(monkeypatch) -> None:
    """Most ports declare none. Blaming a dependency that does not
    exist would send the agent chasing the environment instead of the
    real extract failure."""
    _capture(monkeypatch, rc=1, probe_stdout="")
    out = worker.make_extract("env", "devel/foo")
    assert out["ok"] is False
    assert "extract_depends" not in out
    assert "blocking" not in out


def test_the_ignore_verdict_wins_over_the_depends_hint(monkeypatch) -> None:
    """An IGNOREd port may also declare EXTRACT_DEPENDS. IGNORE is the
    framework's own verdict and the one that must abort the attempt."""
    def fake(env, *argv, **kw):
        cmd = argv[-1]
        if "-V IGNORE" in cmd:
            return subprocess.CompletedProcess(argv, 0, "needs perl 5.42\n", "")
        if "-V EXTRACT_DEPENDS" in cmd:
            return subprocess.CompletedProcess(argv, 0, "zstd:archivers/zstd\n", "")
        return subprocess.CompletedProcess(argv, 1, "boom\n", "")

    monkeypatch.setattr(worker, "_exec", fake)
    out = worker.make_extract("env", "devel/foo")
    assert out["blocking"] is True
    assert "extract_depends" not in out


def test_a_probe_that_itself_fails_is_not_treated_as_an_answer(
    monkeypatch,
) -> None:
    """`make -V` can fail for the same reason the extract did. A
    non-zero probe means no information, not an empty declaration."""
    def fake(env, *argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "garbage\n", "")

    monkeypatch.setattr(worker, "_exec", fake)
    out = worker.make_extract("env", "devel/foo")
    assert "blocking" not in out
    assert "extract_depends" not in out


# --- the reasoning stays with the code -------------------------------------

def test_the_constant_records_why(monkeypatch) -> None:
    """This is a non-obvious flag on a build command. Whoever finds it
    next needs the reason without re-deriving it from dsynth's source."""
    import inspect
    src = inspect.getsource(worker)
    head = src[:src.index("NO_DEPENDS = ")]
    for phrase in ("dsynth", "DEPENDS_TARGET=install", "read-only",
                   "unarchiver"):
        assert phrase in head, f"the rationale does not mention {phrase!r}"


def test_no_depends_is_defined_once(monkeypatch) -> None:
    """Two call sites, one constant — so a change reaches both."""
    import inspect
    src = inspect.getsource(worker)
    assert src.count('NO_DEPENDS = "NO_DEPENDS=yes"') == 1
    assert src.count("{NO_DEPENDS}") == 2


@pytest.mark.parametrize("fn", [worker.make_extract, worker.make_patch])
def test_both_entry_points_mention_it_in_their_docstring(fn) -> None:
    assert "NO_DEPENDS" in fn.__doc__


# --- clean is guarded by a different variable ------------------------------

def test_clean_does_not_walk_into_dependencies(monkeypatch) -> None:
    """bsd.port.mk's `clean` pulls in `limited-clean-depends`, which runs
    `make clean` in every port in the dependency graph. _clean_port_workdir
    was only ever asked to remove one WRKDIR, and the walk goes looking for
    ports the dev-env's package universe cannot satisfy."""
    payloads = _capture(monkeypatch)
    worker._clean_port_workdir("env", "devel/foo")
    assert "NOCLEANDEPENDS=yes" in payloads[0]


def test_clean_is_guarded_by_nocleandepends_not_no_depends(
    monkeypatch,
) -> None:
    """Verified by dry-run against a real ports tree: `make NO_DEPENDS=yes
    clean` still runs limited-clean-depends; NOCLEANDEPENDS=yes is what
    drops it. Two variables, and only one works here."""
    payloads = _capture(monkeypatch)
    worker._clean_port_workdir("env", "devel/foo")
    assert "NO_DEPENDS=yes" not in payloads[0]
