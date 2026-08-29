"""The agent reads the tree the way the builder does (poly-cx6).

``bsd.default-versions.mk`` derives a few ``*_DEFAULT`` knobs from what
is installed rather than from what the tree declares. ``PERL5_DEFAULT``
is the one that stops the agent::

    .  if !exists(${LOCALBASE}/bin/perl) || (!defined(_PORTS_ENV_CHECK) \\
        && defined(PACKAGE_BUILDING))
    PERL5_DEFAULT?=     5.42                    # the tree's declaration
    .  elif !defined(PERL5_DEFAULT)
    _PERL5_FROM_BIN!=   ${LOCALBASE}/bin/perl -e 'printf "%vd\\n", $$^V;'
    PERL5_DEFAULT:=     ${_PERL5_FROM_BIN:R}    # whatever is installed

The dev-env's /usr/local comes from the official mirror, which trails the
quarterly tree — measured on hardware, the mirror's newest perl was 5.40
against a tree declaring 5.42, so no amount of re-provisioning helps.
``perl5.mk:56`` then sets ``IGNORE= Invalid perl5 version <installed>``.

An IGNOREd port has no real targets at all. ``bsd.port.mk`` replaces
fourteen of them — check-sanity, fetch, checksum, extract, patch,
configure, all, build, install, reinstall, test, package, stage,
restage — with ``echo the reason; exit 1``. So the agent cannot unpack a
distfile to re-cut a patch, and NO_DEPENDS is never even reached: there
is no dependency resolution to suppress, because there is no real
extract target.

dsynth sets PACKAGE_BUILDING in every builder's make.conf
(``addbuildenv("PACKAGE_BUILDING", "yes", BENV_MAKECONF)``), which is
why the same tree builds there and IGNOREs in the chroot. Setting it is
alignment rather than a bypass: the framework itself stops objecting.

Measured on hardware, glib20 in a 2026Q3 env:

    plain                   PERL5_DEFAULT=5.36  IGNORE=Invalid perl5 version 5.36
    PACKAGE_BUILDING=yes    PERL5_DEFAULT=5.42  IGNORE=

and with both flags a real ``make extract`` unpacked 2357 files, after
which ``dupe`` and ``genpatch`` produced a canonical patch.
"""

from __future__ import annotations

import subprocess

import pytest

from dportsv3.agent import worker


def _capture(monkeypatch, *, rc: int = 0, probe_stdout: str = "") -> list[str]:
    payloads: list[str] = []

    def fake(env, *argv, **kw):
        cmd = argv[-1]
        payloads.append(cmd)
        if "-V WRKDIR" in cmd:
            return subprocess.CompletedProcess(
                argv, 0, "/work/obj/work\n/work/obj/work/foo-1.0\n", "")
        if "-V IGNORE" in cmd or "-V EXTRACT_DEPENDS" in cmd:
            return subprocess.CompletedProcess(argv, 0, probe_stdout, "")
        return subprocess.CompletedProcess(argv, rc, "", "")

    monkeypatch.setattr(worker, "_exec", fake)
    return payloads


# --- the flag is passed -----------------------------------------------------

@pytest.mark.parametrize(
    "fn", [worker.make_extract, worker.make_patch], ids=["extract", "patch"]
)
def test_the_build_target_declares_package_building(monkeypatch, fn) -> None:
    payloads = _capture(monkeypatch)
    fn("env", "devel/foo")
    assert "PACKAGE_BUILDING=yes" in payloads[0]


def test_it_reaches_make_as_a_variable_not_an_export(monkeypatch) -> None:
    """``.if defined(PACKAGE_BUILDING)`` reads make variables; an
    exported shell variable of the same name would not be seen."""
    payloads = _capture(monkeypatch)
    worker.make_extract("env", "devel/foo")
    payload = payloads[0]
    assert payload.index("make ") < payload.index("PACKAGE_BUILDING=yes")
    assert "export PACKAGE_BUILDING" not in payload


# --- it does not replace NO_DEPENDS -----------------------------------------

@pytest.mark.parametrize(
    "fn", [worker.make_extract, worker.make_patch], ids=["extract", "patch"]
)
def test_both_flags_are_set_together(monkeypatch, fn) -> None:
    """They clear different walls and neither subsumes the other.

    Measured on hardware with `make -n extract-depends`: with
    PACKAGE_BUILDING alone, do-depends.sh still runs once with
    dp_DEPENDS_TARGET="install" — the recursion into a read-only
    /usr/local that NO_DEPENDS exists to stop. With both, zero
    invocations.
    """
    payloads = _capture(monkeypatch)
    fn("env", "devel/foo")
    assert "NO_DEPENDS=yes" in payloads[0]
    assert "PACKAGE_BUILDING=yes" in payloads[0]


def test_the_variable_queries_are_left_alone(monkeypatch) -> None:
    """``make -V`` runs no targets, and the WRKDIR/WRKSRC answers are
    the same either way (verified against the tree on hardware, along
    with PATCHDIR, PATCH_LIST, EXTRACT_ONLY, DISTFILES and DISTNAME)."""
    payloads = _capture(monkeypatch)
    worker.make_extract("env", "devel/foo")
    query = next(p for p in payloads if "-V WRKDIR" in p)
    assert "PACKAGE_BUILDING" not in query


# --- what must not regress --------------------------------------------------

def test_a_genuinely_ignored_port_is_still_blocking(monkeypatch) -> None:
    """PACKAGE_BUILDING clears an IGNORE that came from a version skew,
    not IGNORE as such. A port the framework still refuses — including
    the NO_PACKAGE and MANUAL_PACKAGE_BUILD cases this flag *adds*, which
    the farm also refuses — must still abort the attempt (poly-n78)."""
    _capture(monkeypatch, rc=1, probe_stdout="is marked as broken: bad\n")
    out = worker.make_extract("env", "devel/foo")
    assert out["blocking"] is True
    assert out["ignore_reason"] == "is marked as broken: bad"


# --- the reasoning stays with the code --------------------------------------

def test_the_constant_records_why(monkeypatch) -> None:
    import inspect

    src = inspect.getsource(worker)
    head = src[:src.index('PACKAGE_BUILDING = ')]
    for phrase in ("PERL5_DEFAULT", "dsynth", "IGNORE", "bypass"):
        assert phrase in head, f"the rationale does not mention {phrase!r}"


def test_defined_once_used_at_both_call_sites(monkeypatch) -> None:
    import inspect

    src = inspect.getsource(worker)
    assert src.count('PACKAGE_BUILDING = "PACKAGE_BUILDING=yes"') == 1
    assert src.count("{PACKAGE_BUILDING}") == 2


@pytest.mark.parametrize(
    "fn", [worker.make_extract, worker.make_patch], ids=["extract", "patch"]
)
def test_both_entry_points_mention_it(fn) -> None:
    assert "PACKAGE_BUILDING" in fn.__doc__
