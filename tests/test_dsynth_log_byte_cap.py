"""dsynth_log is capped by bytes, not only lines (poly-9hjm).

Lines are not what costs. Three measured cases pull in different
directions, and the cap has to satisfy all of them:

1. www/firefox, 2026-09-07. 4301 lines of cargo output, each line
   kilobytes long: one call cost 684k billable, and every later cache
   eviction in that attempt re-billed ~860k instead of ~140k.
2. misc/py-onnx, 2026-09-16. Told ``total_lines=10268``, the agent
   re-asked with ``tail_lines=10268`` and got 1.7MB. Its prompt went
   from 147,924 to 645,695 tokens; after a second such read the next
   request was rejected with a 400 and the job ended.
3. devel/libcxx22, 2026-09-08. The opposite failure: 578 check-plist
   error lines ARE the fix, and a partial view cost ~40 turns and ~250k
   billable reconstructing them. So the cap is a default the caller can
   raise, never a ceiling.
"""

from __future__ import annotations

import types

import pytest

from dportsv3.agent import runner as runner_mod
from dportsv3.agent import tools, worker

DEFAULT = worker._MAX_STREAM_BYTES


@pytest.fixture
def logs(tmp_path, monkeypatch):
    d = tmp_path / "writable" / "work" / "dsynth" / "logs"
    d.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda env: types.SimpleNamespace(writable=tmp_path / "writable"),
    )
    return d


def _log(d, lines, name="www___firefox.log"):
    (d / name).write_text("\n".join(lines) + "\n")


# --- the costly shapes are bounded ------------------------------------------

def test_long_lines_are_capped_by_bytes(logs) -> None:
    """firefox: the default 200 lines of 3KB each is ~600KB."""
    _log(logs, [f"{i:05d} Running `CARGO=" + "x" * 3000 for i in range(300)])
    out = worker.dsynth_log("env", "www/firefox")
    assert out["ok"] is True
    assert len(out["tail"].encode()) <= DEFAULT
    assert out["truncated"] is True
    assert out["truncated_by"] == "bytes", (
        "raising tail_lines cannot help here; say which limit bit"
    )
    assert out["tail"].splitlines()[-1].startswith("00299 "), (
        "the error is at the end of a build log; keep the end"
    )


def test_asking_for_every_line_is_still_capped(logs) -> None:
    """py-onnx: sizing a re-read in lines reproduces the 1.7MB read."""
    _log(logs, [f"line {i} " + "y" * 150 for i in range(10268)],
         name="misc___py-onnx@py312.log")
    out = worker.dsynth_log("env", "misc/py-onnx", tail_lines=10268)
    assert len(out["tail"].encode()) <= DEFAULT
    assert out["truncated_by"] == "bytes"
    assert out["total_lines"] == 10268
    assert out["total_bytes"] > 1_500_000


def test_tail_lines_zero_no_longer_means_the_whole_log(logs) -> None:
    """``tail_lines <= 0`` used to skip truncation entirely."""
    _log(logs, ["z" * 500 for _ in range(1000)])
    out = worker.dsynth_log("env", "www/firefox", tail_lines=0)
    assert len(out["tail"].encode()) <= DEFAULT
    assert out["truncated_by"] == "bytes"


def test_a_non_positive_max_bytes_is_the_default_not_unlimited(logs) -> None:
    """Zero must not become the easy way back to an unbounded read."""
    _log(logs, ["z" * 500 for _ in range(1000)])
    for mb in (0, -1):
        out = worker.dsynth_log("env", "www/firefox", max_bytes=mb)
        assert len(out["tail"].encode()) <= DEFAULT
        assert out["max_bytes"] == DEFAULT


# --- the cap is a default, not a ceiling -------------------------------------

def test_a_structured_error_block_can_be_read_whole(logs) -> None:
    """libcxx22: 578 plist errors, larger than the default, read in full
    on a second call sized from the first."""
    errors = [f"Error: Orphaned: include/c++/v1/__cxx03/__algorithm/h{i:04d}.h"
              for i in range(578)]
    _log(logs, ["noise"] * 2000 + errors + ["*** Error code 1", "Stop.",
                                            "FAILED 00:00:01"],
         name="devel___libcxx22.log")
    want = 578 + 3

    first = worker.dsynth_log("env", "devel/libcxx22", tail_lines=want)
    assert first["truncated_by"] == "bytes"
    assert first["requested_bytes"] > DEFAULT

    second = worker.dsynth_log("env", "devel/libcxx22", tail_lines=want,
                               max_bytes=first["requested_bytes"])
    got = second["tail"].splitlines()
    assert got == errors + ["*** Error code 1", "Stop.", "FAILED 00:00:01"]
    assert second["truncated_by"] == "lines", (
        "bytes no longer bind; the only cut left is the noise above"
    )


# --- what a cut looks like ----------------------------------------------------

def test_only_whole_lines_are_returned(logs) -> None:
    src = [f"{i:04d}:" + "w" * (37 + i % 11) for i in range(3000)]
    _log(logs, src)
    out = worker.dsynth_log("env", "www/firefox", tail_lines=3000,
                            max_bytes=10_000)
    got = out["tail"].splitlines()
    assert got == src[-len(got):], "no half line at the top of the cut"
    assert len(out["tail"].encode()) <= 10_000


def test_a_line_longer_than_the_cap_keeps_its_tail(logs) -> None:
    _log(logs, ["short", "A" * 5000 + "THE-END"])
    out = worker.dsynth_log("env", "www/firefox", max_bytes=1000)
    assert len(out["tail"].encode()) <= 1000
    assert out["tail"].endswith("THE-END")
    assert out["truncated_by"] == "bytes"


def test_a_small_log_is_untouched(logs) -> None:
    _log(logs, ["a", "b", "c"])
    out = worker.dsynth_log("env", "www/firefox")
    assert out["tail"] == "a\nb\nc"
    assert out["truncated"] is False
    assert out["truncated_by"] == ""


def test_line_truncation_is_still_reported_as_lines(logs) -> None:
    _log(logs, [f"l{i}" for i in range(500)])
    out = worker.dsynth_log("env", "www/firefox")
    assert len(out["tail"].splitlines()) == 200
    assert out["truncated_by"] == "lines"


# --- the model and the operator can see it -----------------------------------

def test_the_schema_offers_max_bytes_and_states_the_cost() -> None:
    spec = next(s for s in tools.schemas()
                if s["function"]["name"] == "dsynth_log")
    assert "max_bytes" in spec["function"]["parameters"]["properties"]
    desc = spec["function"]["description"]
    assert "requested_bytes" in desc and "tokens" in desc


def test_dispatch_accepts_max_bytes(logs) -> None:
    """dispatch rejects arguments the handler does not declare."""
    _log(logs, ["a", "b"])
    out = tools.dispatch("dsynth_log",
                         {"origin": "www/firefox", "max_bytes": 1000},
                         env="env")
    assert out["ok"] is True, out
    assert out["max_bytes"] == 1000


def test_the_operator_line_reports_bytes() -> None:
    """runner.log said ``lines=10268`` and nothing about the 1.7MB; the
    size had to be reconstructed from the tool trace."""
    line = runner_mod._summarize_tool_call(
        "dsynth_log", {"origin": "misc/py-onnx"},
        {"ok": True, "tail": "ab\ncd", "flavor": "py312"},
    )
    assert "lines=2" in line and "bytes=5" in line
