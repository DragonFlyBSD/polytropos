"""dsynth writes one build log per flavor (poly-aoi).

Measured on hardware 2026-08-29, devel/glib20. ``dsynth_log`` reported
``lines=0 FAIL`` while the log sat in the directory the agent listed by
hand on the very next turn::

    [tool:dsynth_log] origin=devel/glib20 lines=0 FAIL
    [tool:list_dir]   /work/dsynth/logs                            ok
    [tool:get_file]   /work/dsynth/logs/devel___glib20@bootstrap.log ok

Three separate defects, each enough to mislead on its own:

1. The filename was built as ``<origin with ___>.log``. A flavored port
   has no such file at all — glib20 produces only
   ``devel___glib20@bootstrap.log`` and ``devel___glib20@default.log``.
2. ``dsynth_build`` handed the model ``log_hint`` built the same way, so
   the path it was told to read did not exist either.
3. The runner's operator line read ``result["text"]``, but the tool
   returns ``tail``. ``lines=0`` was therefore printed on every call,
   success included — a working read looked exactly like a missing log,
   which is why this went unnoticed.

Cost on that run: roughly four turns and ~5K billable tokens to route
around a tool that should answer in one.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from dportsv3.agent import runner as runner_mod
from dportsv3.agent import tools, worker


@pytest.fixture
def logs(tmp_path, monkeypatch):
    """A chroot logs dir, wired so worker resolves into it."""
    d = tmp_path / "writable" / "work" / "dsynth" / "logs"
    d.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda env: types.SimpleNamespace(writable=tmp_path / "writable"),
    )
    return d


def _write(d, name, lines):
    (d / name).write_text("\n".join(f"line {i}" for i in range(lines)) + "\n")
    return d / name


# --- the reported failure ---------------------------------------------------

def test_a_flavored_log_is_found(logs) -> None:
    """The exact shape from the host: no unflavored log exists."""
    _write(logs, "devel___glib20@bootstrap.log", 50)
    out = worker.dsynth_log("env", "devel/glib20")
    assert out["ok"] is True
    assert out["flavor"] == "bootstrap"
    assert out["log_path"] == "/work/dsynth/logs/devel___glib20@bootstrap.log"
    assert out["total_lines"] == 50


def test_the_newest_flavor_wins_and_the_others_are_named(logs) -> None:
    """Two flavors, no hint from the caller: pick the most recent, and
    say what else exists so a second call can be exact instead of the
    model guessing filenames."""
    import os

    a = _write(logs, "devel___glib20@bootstrap.log", 10)
    b = _write(logs, "devel___glib20@default.log", 20)
    os.utime(a, (1_000, 1_000))
    os.utime(b, (2_000, 2_000))

    out = worker.dsynth_log("env", "devel/glib20")
    assert out["flavor"] == "default"
    assert sorted(out["available_flavors"]) == ["bootstrap", "default"]


def test_an_explicit_flavor_is_honoured(logs) -> None:
    import os

    a = _write(logs, "devel___glib20@bootstrap.log", 10)
    b = _write(logs, "devel___glib20@default.log", 20)
    os.utime(a, (1_000, 1_000))
    os.utime(b, (2_000, 2_000))

    out = worker.dsynth_log("env", "devel/glib20", flavor="bootstrap")
    assert out["ok"] is True
    assert out["flavor"] == "bootstrap"
    assert out["total_lines"] == 10


def test_an_unflavored_log_still_works(logs) -> None:
    _write(logs, "devel___gperf.log", 5)
    out = worker.dsynth_log("env", "devel/gperf")
    assert out["ok"] is True
    assert out["flavor"] == ""


# --- failures have to be actionable -----------------------------------------

def test_a_missing_log_says_what_to_do(logs) -> None:
    out = worker.dsynth_log("env", "devel/nothing")
    assert out["ok"] is False
    assert "dsynth_build" in out["error"], (
        "a missing log means no build has run; say so rather than "
        "naming a path and leaving the model to guess why"
    )


def test_a_wrong_flavor_lists_the_real_ones(logs) -> None:
    """The model should not have to list_dir to discover this."""
    _write(logs, "devel___glib20@bootstrap.log", 10)
    out = worker.dsynth_log("env", "devel/glib20", flavor="nope")
    assert out["ok"] is False
    assert "devel___glib20@bootstrap.log" in out["error"]
    assert out["available_flavors"] == ["bootstrap"]


# --- the build hint must name a file that exists ----------------------------

def test_dsynth_build_hints_the_log_that_was_written(logs, monkeypatch) -> None:
    """Handing the model an unflavored path it cannot open is what sent
    it exploring with list_dir in the first place."""
    _write(logs, "devel___glib20@bootstrap.log", 10)
    monkeypatch.setattr(
        worker, "_exec",
        lambda env, *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    # Satisfy the stale-compose guard: dsynth_build refuses outright
    # unless materialize_dports succeeded for this origin this attempt.
    monkeypatch.setattr(worker, "_MATERIALIZE_STATE",
                        {("env", "devel/glib20"): "sha"})
    monkeypatch.setattr(worker, "_port_subtree_hash", lambda e, o: "sha")

    out = worker.dsynth_build("env", "devel/glib20")
    assert out["log_hint"] == \
        "/work/dsynth/logs/devel___glib20@bootstrap.log"
    assert out["log_flavors"] == ["bootstrap"]


# --- the operator line must not lie -----------------------------------------

def test_the_operator_line_counts_the_key_the_tool_returns() -> None:
    """`lines=0` was printed on every call because the summariser read
    `text` while the tool returns `tail`. A working read was
    indistinguishable from a missing log in the runner log — which is
    precisely how this survived."""
    line = runner_mod._summarize_tool_call(
        "dsynth_log", {"origin": "devel/glib20"},
        {"ok": True, "tail": "a\nb\nc", "flavor": "bootstrap"},
    )
    assert "lines=3" in line
    assert "devel/glib20@bootstrap" in line


def test_the_operator_line_still_reads_a_failure() -> None:
    line = runner_mod._summarize_tool_call(
        "dsynth_log", {"origin": "devel/glib20"},
        {"ok": False, "tail": "", "error": "no log"},
    )
    assert "lines=0" in line


# --- the schema tells the model the flavor exists ---------------------------

def test_the_tool_schema_exposes_flavor() -> None:
    spec = next(s for s in tools.schemas()
                if s["function"]["name"] == "dsynth_log")
    props = spec["function"]["parameters"]["properties"]
    assert "flavor" in props
    assert "flavor" in spec["function"]["description"]
    assert "origin-with-underscores" not in spec["function"]["description"], (
        "the old description promised a filename shape that is wrong for "
        "every flavored port"
    )


def test_the_hint_lookup_never_breaks_the_build_call(monkeypatch) -> None:
    """Resolving the env can fail outright (no dev-env entry point on
    PATH, for one). The log hint only decorates a result — it must
    degrade to "no logs known" rather than turning dsynth_build into an
    exception, which is exactly what it did on first writing."""
    def boom(env):
        raise RuntimeError("could not locate the dev-env entry point")

    monkeypatch.setattr(worker, "env_paths", boom)
    assert worker._dsynth_log_candidates("env", "devel/glib20") == []

    monkeypatch.setattr(
        worker, "_exec",
        lambda env, *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(worker, "_MATERIALIZE_STATE",
                        {("env", "devel/glib20"): "sha"})
    monkeypatch.setattr(worker, "_port_subtree_hash", lambda e, o: "sha")

    out = worker.dsynth_build("env", "devel/glib20")
    assert out["rebuild_ok"] is True
    assert out["log_hint"] == "/work/dsynth/logs/devel___glib20.log"
    assert out["log_flavors"] == []
