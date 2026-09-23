"""The running build's log, read by byte offset (poly-qqx9.8).

worker.dsynth_log reads the whole file and keeps the last N lines, which
is right for a model asking "why did this fail" and wrong for a page
asking every three seconds "what is new". These are the cases that
difference creates.
"""

from __future__ import annotations

import pytest

from dportsv3.tracker import dsynth_tail
from dportsv3.tracker.render import (
    attach_tool_tail,
    group_activity_into_cards,
    running_tailable_tool,
)


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "devel___llvm19@default.log"
    path.write_text("".join(f"line {i}\n" for i in range(1, 501)))
    monkeypatch.setattr(dsynth_tail, "log_path", lambda *a, **k: path)
    return path


def test_the_first_poll_gets_the_newest_screenful_not_the_whole_build(log):
    out = dsynth_tail.read_tail("e", "devel/llvm19", offset=-1, max_bytes=200)
    assert out["ok"] is True
    assert out["eof"] is True
    assert "line 500" in out["text"]
    assert "line 1\n" not in out["text"]
    # It says how much it passed over rather than pretending that was all.
    assert out["skipped"] > 0
    assert out["offset"] == log.stat().st_size


def test_a_second_poll_returns_only_what_arrived_since(log):
    first = dsynth_tail.read_tail("e", "devel/llvm19", offset=-1)
    with log.open("a") as fh:
        fh.write("line 501\nline 502\n")
    second = dsynth_tail.read_tail("e", "devel/llvm19", offset=first["offset"])
    assert second["text"] == "line 501\nline 502\n"
    assert second["skipped"] == 0


def test_nothing_new_is_an_empty_read_not_a_re_read(log):
    first = dsynth_tail.read_tail("e", "devel/llvm19", offset=-1)
    again = dsynth_tail.read_tail("e", "devel/llvm19", offset=first["offset"])
    assert again["text"] == ""
    assert again["eof"] is True


def test_a_replaced_log_restarts_from_its_end(log):
    """A new build writes a shorter file; an offset past its end would
    otherwise read nothing forever."""
    far_past_the_end = log.stat().st_size + 10_000
    out = dsynth_tail.read_tail("e", "devel/llvm19", offset=far_past_the_end)
    assert out["ok"] is True
    assert "line 500" in out["text"]


def test_a_long_gap_is_capped_and_says_so(log):
    """A page left open through a 44-minute build asks for megabytes."""
    out = dsynth_tail.read_tail("e", "devel/llvm19", offset=0, max_bytes=100)
    assert len(out["text"].encode()) <= 100
    assert out["skipped"] > 0
    assert out["eof"] is True


def test_the_line_cap_trims_the_display_without_moving_the_offset(log):
    out = dsynth_tail.read_tail("e", "devel/llvm19", offset=-1, max_lines=3)
    assert out["text"].count("\n") <= 3
    assert out["offset"] == log.stat().st_size


def test_a_partial_first_line_is_dropped_not_shown_as_output(log):
    """A mid-file read almost always starts halfway through a line."""
    out = dsynth_tail.read_tail("e", "devel/llvm19", offset=-1, max_bytes=45)
    for line in out["text"].splitlines():
        assert line.startswith("line ")


def test_the_mtime_is_the_liveness_signal(log):
    out = dsynth_tail.read_tail("e", "devel/llvm19", offset=-1)
    assert out["mtime"] == pytest.approx(log.stat().st_mtime)


def test_no_log_yet_is_an_ordinary_state_not_a_fault(monkeypatch):
    """dsynth writes one only once a build starts."""
    monkeypatch.setattr(dsynth_tail, "log_path", lambda *a, **k: None)
    out = dsynth_tail.read_tail("e", "devel/llvm19")
    assert out["ok"] is False
    assert "no log" in out["error"]
    assert out["text"] == ""


def test_an_unreadable_log_reports_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(dsynth_tail, "log_path",
                        lambda *a, **k: tmp_path / "gone.log")
    out = dsynth_tail.read_tail("e", "devel/llvm19")
    assert out["ok"] is False
    assert "read failed" in out["error"]


# --- which row the tail belongs to ----------------------------------------


def _cards(tool, running=True):
    rows = [
        {"id": 1, "stage": "llm_turn", "ts": "t",
         "extra": {"attempt": 1, "turn": 1}, "message": "", "duration_ms": None},
        {"id": 2, "stage": "tool_start", "ts": "t",
         "extra": {"attempt": 1, "turn": 1, "tool": tool, "call_id": "c1"},
         "message": "", "duration_ms": None},
    ]
    if not running:
        rows.append({"id": 3, "stage": f"tool:{tool}", "ts": "t",
                     "extra": {"attempt": 1, "turn": 1, "ok": True,
                               "call_id": "c1"},
                     "message": "done", "duration_ms": 10})
    return group_activity_into_cards(rows)


def test_only_a_long_tool_gets_a_tail():
    """grep returns in milliseconds and has nothing to say meanwhile."""
    assert running_tailable_tool(_cards("dsynth_build")) is not None
    assert running_tailable_tool(_cards("grep")) is None


def test_a_finished_build_gets_no_tail():
    """The row collapses to its duration and rc when the tool returns."""
    assert running_tailable_tool(_cards("dsynth_test", running=False)) is None


def test_the_tail_hangs_on_the_row_producing_it():
    cards = _cards("dsynth_test")
    attach_tool_tail(cards, {"ok": True, "text": "LINK libLLVM.so"})
    assert cards[0]["tools"][0]["tail"]["text"] == "LINK libLLVM.so"


def test_attaching_nothing_leaves_the_row_alone():
    cards = _cards("dsynth_test")
    attach_tool_tail(cards, None)
    assert "tail" not in cards[0]["tools"][0]


# --- the endpoint ----------------------------------------------------------


def test_the_endpoint_refuses_an_unknown_job(tmp_path):
    import sqlite3  # noqa: PLC0415

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from dportsv3.db.schema import init_db  # noqa: PLC0415
    from dportsv3.tracker.server import create_app  # noqa: PLC0415

    db = tmp_path / "state.db"
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row; init_db(c)
    c.execute(
        "INSERT INTO jobs (job_id, state, type, origin, flavor, bundle_dir,"
        " created_ts_utc, path, last_seen_at) "
        "VALUES ('j1','patching','patch','devel/foo','','','t','','t')")
    c.commit(); c.close()
    with TestClient(create_app(db)) as client:
        assert client.get("/api/jobs/nope/dsynth-tail").status_code == 404
        # A job with no recorded dev_env has no environment to resolve a
        # log under, and says so rather than guessing one.
        body = client.get("/api/jobs/j1/dsynth-tail").json()
        assert body["ok"] is False
        assert "dev_env" in body["error"]
