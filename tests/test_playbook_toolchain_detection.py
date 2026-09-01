"""Toolchain detection must not depend on the bundle being on disk.

Measured 2026-09-01: every ``toolchain-*.md`` playbook — 10 of the 17 —
was silently dropped from every payload. ``detect_toolchains`` reads
``<bundle_dir>/port/Makefile`` from the filesystem, but the patch path
does not materialize the bundle, so the directory was absent and the
detector returned an empty set. An empty set is indistinguishable from
"no toolchain playbooks matched", so nothing reported it.

The payload itself renders that same Makefile without trouble, because
``read_bundle_text`` falls back to the artifact store. Detection had no
such fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_GEN = Path(__file__).resolve().parents[1]
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))


_REAL_MAKEFILE = (
    "PORTNAME=\tlibunwind\n"
    "USES=\t\tcompiler:c11 cpe libtool pkgconfig\n"
    "GNU_CONFIGURE=\tyes\n"
)


def test_text_detection_finds_what_the_directory_read_would():
    from dportsv3.agent.playbooks import detect_toolchains_from_makefile
    assert detect_toolchains_from_makefile(_REAL_MAKEFILE) == {
        "autoconf", "c", "libtool", "pkg-config",
    }


def test_text_detection_is_quiet_on_nothing():
    from dportsv3.agent.playbooks import detect_toolchains_from_makefile
    assert detect_toolchains_from_makefile("") == set()
    assert detect_toolchains_from_makefile(None) == set()


def test_directory_and_text_detection_agree(tmp_path):
    """The fallback must not be a second, divergent implementation."""
    from dportsv3.agent.playbooks import (
        detect_toolchains, detect_toolchains_from_makefile,
    )
    port = tmp_path / "port"
    port.mkdir()
    (port / "Makefile").write_text(_REAL_MAKEFILE)

    assert detect_toolchains(port) == detect_toolchains_from_makefile(_REAL_MAKEFILE)


def test_missing_directory_still_yields_nothing_on_its_own():
    from dportsv3.agent.playbooks import detect_toolchains
    assert detect_toolchains(Path("/nonexistent/port")) == set()


def test_toolchain_playbooks_fire_once_detection_works():
    """The consequence: with tags, toolchain-* entries enter the payload.

    Without them the selector silently returns the untagged subset, which
    is why this went unnoticed — the payload still looked populated.
    """
    from dportsv3.agent import playbooks
    d = _GEN / "dportsv3" / "agent" / "playbooks"

    without = playbooks.load_playbooks(
        d, role="patch", classification="compile-error", toolchains=set())
    with_tags = playbooks.load_playbooks(
        d, role="patch", classification="compile-error",
        toolchains={"autoconf", "libtool"})

    assert len(with_tags.included) > len(without.included)
    gained = set(with_tags.included) - set(without.included)
    assert all(n.startswith("toolchain-") for n in gained), gained


def test_runner_falls_back_to_the_artifact_store():
    """Wiring: both payload builders must use the text fallback."""
    import inspect
    from dportsv3.agent import runner
    for fn in (runner.build_triage_payload, runner.build_patch_payload):
        src = inspect.getsource(fn)
        assert "detect_toolchains_from_makefile(" in src, fn.__name__
        assert 'read_bundle_text(bundle_dir, job.get("bundle_id"), "port/Makefile")' in src, \
            fn.__name__


def test_selection_row_reports_what_was_detected():
    """An empty set must be visible, not inferred from a count."""
    import inspect
    from dportsv3.agent import runner
    src = inspect.getsource(runner._log_playbook_selection)
    assert "toolchains=" in src
    assert "'none'" in src or '"none"' in src
