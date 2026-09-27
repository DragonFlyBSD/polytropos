"""A compat-mode port's scoped payload is read by no build line correctly.

poly-7pwa.9, and the two lanes fail in OPPOSITE directions.
``compat_dragonfly_files_script_parity`` walks ``dragonfly/`` recursively
keeping the scope in the relative path, so ``dragonfly/@main/patch-x``
composes to that same path -- and ``do-patch`` globs ``dragonfly/patch-*``
non-recursively, so it never applies. ``compat_diff_files_script_parity``
copies nothing: every ``*.diff`` it finds recursively is APPLIED to the one
output tree, so both build lines' framework patches land together.

WHY THIS WARNS RATHER THAN CHANGING BEHAVIOUR, which is what these tests
pin. Not ambiguity -- docs/implementation-plan-v3.md specifies per-target
layering for both lanes, and my first version of this docstring leaned on
"the semantics are ambiguous", which a review showed was not supported. The
real reason is the contract in the function names: ``*_script_parity``. The
legacy generator still runs and is target-blind in exactly these two ways
(``merge.sh`` does ``cp -pr ${DP}/dragonfly``, then ``find ${DP}/diffs -name
'*.diff'`` and applies each), and the compat path's output is the
byte-parity oracle the remaining compat ports are being migrated against.
Diverging from the oracle inside the one path whose contract is parity with
it is worse than the hazard.

The suite pins the present behaviour for a real target directory too, not
only for ``@any``: a compat port composing ``@main`` is expected to produce
``dragonfly/@main/pkg-descr`` at that path.
"""

from __future__ import annotations

import pathlib

import pytest

from dportsv3.compose_discovery import compat_scoped_payload_warnings
from dportsv3.compose_models import ComposePortContext


def _delta_ports() -> pathlib.Path | None:
    from dportsv3.paths import resolve_delta_root

    try:
        root = resolve_delta_root() / "ports"
    except Exception:  # noqa: BLE001
        return None
    return root if root.is_dir() else None


DELTA = _delta_ports()


def _ctx(port: pathlib.Path, mode: str) -> ComposePortContext:
    # dops_path set with mode, as discover_overlay_contexts does -- a
    # dops/None pair is a state the real pipeline cannot produce.
    return ComposePortContext(
        origin="devel/thing",
        path=port,
        dops_path=(port / "overlay.dops") if mode == "dops" else None,
        mode=mode,
    )


@pytest.fixture
def port(tmp_path):
    p = tmp_path / "ports" / "devel" / "thing"
    p.mkdir(parents=True)
    return p


def test_a_compat_port_with_a_scoped_lane_warns(port):
    (port / "dragonfly" / "@main").mkdir(parents=True)
    (port / "dragonfly" / "@main" / "patch-x").write_text("diff\n")
    warnings = compat_scoped_payload_warnings(_ctx(port, "compat"))
    assert len(warnings) == 1
    assert "@main" in warnings[0]


def test_the_warning_says_why_it_matters_and_what_to_do(port):
    (port / "dragonfly" / "@2026Q3").mkdir(parents=True)
    (port / "dragonfly" / "@2026Q3" / "patch-x").write_text("diff\n")
    msg = compat_scoped_payload_warnings(_ctx(port, "compat"))[0]
    # The mechanism, not just the fact.
    assert "globs" in msg and "non-recursively" in msg
    assert "none of it applies" in msg
    # And WHY this is not simply fixed: parity with the generator that runs.
    assert "parity with the legacy generator" in msg
    # The route out.
    assert "overlay.dops" in msg
    assert "file materialize" in msg


def test_a_dops_port_does_not_warn(port):
    """dops mode suppresses the compat copy entirely, so there is no hazard."""
    (port / "dragonfly" / "@main").mkdir(parents=True)
    (port / "dragonfly" / "@main" / "patch-x").write_text("diff\n")
    assert compat_scoped_payload_warnings(_ctx(port, "dops")) == []


def test_the_established_any_lane_does_not_warn(port):
    """`dragonfly/@any/` is the shape this path already supports.

    tests/test_dportsv3_compose.py expects it copied path-preserving, so
    warning about it would be second-guessing a documented behaviour.
    """
    (port / "dragonfly" / "@any").mkdir(parents=True)
    (port / "dragonfly" / "@any" / "pkg-descr").write_text("x\n")
    assert compat_scoped_payload_warnings(_ctx(port, "compat")) == []


def test_a_flat_compat_port_does_not_warn(port):
    (port / "dragonfly").mkdir()
    (port / "dragonfly" / "patch-x").write_text("diff\n")
    assert compat_scoped_payload_warnings(_ctx(port, "compat")) == []


def test_the_diffs_lane_fails_the_opposite_way(port):
    """diffs/ is not copied -- every *.diff is APPLIED to one tree.

    The first version of this said diffs were "copied path-preserving" and
    "stop applying", both false. The bead's original framing was right about
    this lane and its own audit correction retracted it after examining only
    the dragonfly lane.
    """
    (port / "diffs" / "@main").mkdir(parents=True)
    (port / "diffs" / "@main" / "base.diff").write_text("diff\n")
    msg = compat_scoped_payload_warnings(_ctx(port, "compat"), "@2026Q3")[0]
    assert msg.startswith("diffs/")
    assert "APPLIED to this one output tree" in msg
    assert "copied" not in msg
    # And the remedy is patch apply, not file materialize: nothing reads a
    # composed diffs/ directory.
    assert "patch apply diffs/@<target>/X" in msg
    assert "file materialize" not in msg


def test_the_message_distinguishes_this_line_from_another(port):
    """"my fix is inert" and "a sibling's files are here" differ."""
    for t in ("@main", "@2026Q3"):
        (port / "dragonfly" / t).mkdir(parents=True)
        (port / "dragonfly" / t / "patch-x").write_text("diff\n")
    mine = compat_scoped_payload_warnings(_ctx(port, "compat"), "@main")[0]
    assert "@2026Q3 belongs to another build line" in mine
    assert "@main belongs to another" not in mine


def test_both_lanes_warn_separately(port):
    for lane in ("dragonfly", "diffs"):
        (port / lane / "@main").mkdir(parents=True)
        (port / lane / "@main" / "f").write_text("x\n")
    assert len(compat_scoped_payload_warnings(_ctx(port, "compat"))) == 2


def test_an_invalid_target_directory_is_left_to_the_existing_error(port):
    """validate_target_scoped_payloads already rejects a bad @name.

    Two diagnostics for one fault in different words helps nobody.
    """
    (port / "dragonfly" / "@nonsense").mkdir(parents=True)
    (port / "dragonfly" / "@nonsense" / "f").write_text("x\n")
    assert compat_scoped_payload_warnings(_ctx(port, "compat")) == []


def test_a_port_with_no_payload_dirs_is_silent(port):
    assert compat_scoped_payload_warnings(_ctx(port, "compat")) == []


# --------------------------------------------------------------------------
# The real tree: latent means latent
# --------------------------------------------------------------------------


@pytest.mark.skipif(DELTA is None, reason="DeltaPorts checkout not present")
def test_no_port_in_the_tree_warns_today():
    """The claim the bead rests on. If this ever fails, it stopped being latent.

    Both ports with scoped payload carry an overlay.dops, so both compose in
    dops mode and this path never runs for them.
    """
    warned = []
    for lane in ("dragonfly", "diffs"):
        for scoped_dir in DELTA.glob(f"*/*/{lane}/@*"):
            port = scoped_dir.parent.parent
            mode = "dops" if (port / "overlay.dops").is_file() else "compat"
            if compat_scoped_payload_warnings(_ctx(port, mode)):
                warned.append(f"{port.parent.name}/{port.name} ({lane})")
    assert sorted(set(warned)) == [], sorted(set(warned))


@pytest.mark.skipif(DELTA is None, reason="DeltaPorts checkout not present")
def test_the_two_scoped_ports_would_warn_if_they_were_compat():
    """Guards the premise from the other side: the check does fire.

    A test that only asserts silence would pass just as well if the function
    returned [] unconditionally.
    """
    for origin in ("lang/rust", "ports-mgmt/pkg"):
        port = DELTA / origin
        if not (port / "dragonfly").is_dir():
            pytest.skip(f"{origin} no longer keeps a dragonfly lane")
        assert compat_scoped_payload_warnings(_ctx(port, "compat")), origin
        assert compat_scoped_payload_warnings(_ctx(port, "dops")) == []
