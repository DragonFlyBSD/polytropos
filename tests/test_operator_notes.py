"""Telling a running agent something (poly-qqx9.11).

The only control on a running job was Abandon -- watch or kill -- so an
operator who knew early that a line of attack was wrong could spend that
knowledge only by throwing away the attempt budget and the workspace.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import (
    operator_notes_for_job,
    pending_note_count,
    queue_operator_note,
    take_pending_operator_notes,
)
from dportsv3.tracker.render import merge_note_cards, window_cards
from dportsv3.tracker.server import create_app

NOW = "2026-09-23T10:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "state.db"
    c = sqlite3.connect(str(path)); c.row_factory = sqlite3.Row; init_db(c)
    for job_id, state in (("j-live", "patching"), ("j-done", "done")):
        c.execute(
            "INSERT INTO jobs (job_id, state, type, origin, flavor, "
            "bundle_dir, created_ts_utc, path, last_seen_at) "
            "VALUES (?,?,'patch','devel/foo','','',?,'',?)",
            (job_id, state, NOW, NOW))
    c.commit()
    c.close()
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    return c


# --- storage ---------------------------------------------------------------


def test_a_note_is_stored_not_held_in_flight(conn):
    """poly-pf4a is open precisely because the fix chat was not stored."""
    note = queue_operator_note(conn, "j-live", "the path moved upstream")
    assert note["text"] == "the path moved upstream"
    assert note["delivered_at"] is None
    assert operator_notes_for_job(conn, "j-live")[0]["id"] == note["id"]


def test_an_empty_note_is_refused(conn):
    with pytest.raises(ValueError):
        queue_operator_note(conn, "j-live", "   \n  ")


def test_a_note_is_capped_because_it_lands_in_the_model_s_context(conn):
    note = queue_operator_note(conn, "j-live", "x" * 9000)
    assert len(note["text"]) == 2000


def test_claiming_marks_where_the_note_landed(conn):
    queue_operator_note(conn, "j-live", "look at llvm-tblgen19")
    claimed = take_pending_operator_notes(conn, "j-live", attempt=2, turn=7)
    assert [c["text"] for c in claimed] == ["look at llvm-tblgen19"]
    assert claimed[0]["delivered_attempt"] == 2
    assert claimed[0]["delivered_turn"] == 7
    stored = operator_notes_for_job(conn, "j-live")[0]
    assert stored["delivered_at"] is not None


def test_a_claimed_note_is_never_delivered_twice(conn):
    """It costs tokens every time it is delivered."""
    queue_operator_note(conn, "j-live", "once")
    assert len(take_pending_operator_notes(conn, "j-live", 1, 1)) == 1
    assert take_pending_operator_notes(conn, "j-live", 1, 2) == []
    assert pending_note_count(conn, "j-live") == 0


def test_notes_do_not_leak_between_jobs(conn):
    queue_operator_note(conn, "j-live", "for the live one")
    assert take_pending_operator_notes(conn, "j-done", 1, 1) == []


# --- in the stream ---------------------------------------------------------


def _turn(attempt, turn, id):
    return {"kind": "turn", "key": f"t-{attempt}-{turn}", "attempt": attempt,
            "turn": turn, "id": id, "tools": []}


def test_a_delivered_note_sits_above_the_turn_it_landed_in():
    """It was read as that turn was composed; that is where a later
    reader needs to find it."""
    cards = [_turn(1, 3, 9), _turn(1, 2, 6), _turn(1, 1, 3)]
    notes = [{"id": 1, "text": "n", "created_at": NOW, "delivered_at": NOW,
              "delivered_attempt": 1, "delivered_turn": 2}]
    merged = merge_note_cards(cards, notes)
    assert [c["kind"] for c in merged] == ["turn", "note", "turn", "turn"]
    assert merged[2]["turn"] == 2


def test_a_queued_note_goes_to_the_top_and_stays_there():
    """No optimistic silence, and no scrolling out of its own window
    before the agent has read it."""
    cards = [_turn(1, t, t) for t in range(12, 0, -1)]
    notes = [{"id": 1, "text": "n", "created_at": NOW, "delivered_at": None}]
    merged = merge_note_cards(cards, notes)
    assert merged[0]["kind"] == "note"
    windowed = window_cards(merged)
    assert windowed[0]["kind"] == "note"
    assert len(windowed) == 6


def test_a_delivered_note_falls_out_of_the_window_like_any_card():
    cards = [_turn(1, t, t) for t in range(12, 0, -1)]
    notes = [{"id": 1, "text": "n", "created_at": NOW, "delivered_at": NOW,
              "delivered_attempt": 1, "delivered_turn": 1}]
    windowed = window_cards(merge_note_cards(cards, notes))
    assert all(c["kind"] != "note" for c in windowed)


def test_a_note_whose_turn_is_outside_the_window_is_not_lost():
    cards = [_turn(1, 3, 9)]
    notes = [{"id": 1, "text": "n", "created_at": NOW, "delivered_at": NOW,
              "delivered_attempt": 1, "delivered_turn": 99}]
    merged = merge_note_cards(cards, notes)
    assert any(c["kind"] == "note" for c in merged)


# --- the endpoint ----------------------------------------------------------


def test_the_page_queues_a_note_and_shows_it_at_once(db):
    with TestClient(create_app(db)) as client:
        r = client.post("/api/jobs/j-live/notes",
                        data={"text": "the CMake path moved upstream"},
                        follow_redirects=False)
        assert r.status_code == 303
        body = client.get("/agentic/jobs/j-live").text
    assert "operator note" in body
    assert "queued" in body
    assert "the CMake path moved upstream" in body


def test_a_finished_job_has_no_next_turn_to_deliver_on(db):
    with TestClient(create_app(db)) as client:
        r = client.post("/api/jobs/j-done/notes", data={"text": "too late"})
        assert r.status_code == 409
        body = client.get("/agentic/jobs/j-done").text
    # The composer says why rather than offering a control that cannot work.
    assert "no next turn" in body


def test_an_empty_note_is_refused_by_the_endpoint(db):
    with TestClient(create_app(db)) as client:
        assert client.post("/api/jobs/j-live/notes",
                           data={"text": "  "}).status_code == 400


def test_an_unknown_job_is_a_404(db):
    with TestClient(create_app(db)) as client:
        assert client.post("/api/jobs/nope/notes",
                           data={"text": "x"}).status_code == 404


def test_a_json_caller_gets_the_note_back(db):
    with TestClient(create_app(db)) as client:
        r = client.post("/api/jobs/j-live/notes", json={"text": "via fetch"},
                        headers={"accept": "application/json"})
        assert r.status_code == 200
        assert r.json()["note"]["text"] == "via fetch"


# --- delivery, in the loop -------------------------------------------------


def test_the_loop_delivers_a_note_on_the_next_turn(monkeypatch):
    """The seam is the turn, not the attempt boundary: during a 44-minute
    dsynth_test the agent is inside a turn waiting on the tool, and the
    next turn is composed the moment it returns."""
    from dportsv3.agent import llm, tool_loop  # noqa: PLC0415

    seen: list[dict] = []
    calls = {"n": 0}

    def fake_complete(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return llm.Response(
                text="", tool_calls=[llm.ToolCall(
                    id="c1", name="get_file",
                    arguments={"path": "Makefile"})],
                usage=llm.Usage(prompt_tokens=1, completion_tokens=1,
                                total_tokens=2))
        seen.append({"messages": list(messages or [])})
        return llm.Response(text="done", tool_calls=[],
                            usage=llm.Usage(prompt_tokens=1,
                                            completion_tokens=1,
                                            total_tokens=2))

    monkeypatch.setattr(tool_loop.llm, "complete", fake_complete)
    monkeypatch.setattr(
        tool_loop.tools, "dispatch",
        lambda *a, **k: {"ok": True, "content": "x"})

    events: list[dict] = []
    tool_loop.run(
        [{"role": "user", "content": "go"}],
        model="m", env="e", max_turns=4,
        on_event=events.append,
        operator_notes=lambda attempt, turn: [
            {"id": 1, "text": "look at llvm-tblgen19"}],
    )
    # It reached the model on the very next turn...
    prompt = "\n".join(
        str(m.get("content")) for m in seen[0]["messages"])
    assert "look at llvm-tblgen19" in prompt
    # ...as its own message, not folded into a tool result, so the
    # provenance a note needs to keep is kept.
    note_msgs = [m for m in seen[0]["messages"]
                 if m.get("role") == "user" and "operator" in
                 str(m.get("content"))]
    assert len(note_msgs) == 1
    assert all(m.get("role") != "tool" or "llvm-tblgen19" not in
               str(m.get("content")) for m in seen[0]["messages"])
    # And it is on the record.
    delivered = [e for e in events if e.get("type") == "operator_note"]
    assert delivered and delivered[0]["note_id"] == 1


def test_a_broken_note_source_never_breaks_the_loop(monkeypatch):
    from dportsv3.agent import llm, tool_loop  # noqa: PLC0415

    calls = {"n": 0}

    def fake_complete(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return llm.Response(
                text="", tool_calls=[llm.ToolCall(id="c1", name="get_file",
                                                  arguments={})],
                usage=llm.Usage(1, 1, 2))
        return llm.Response(text="done", tool_calls=[], usage=llm.Usage(1, 1, 2))

    monkeypatch.setattr(tool_loop.llm, "complete", fake_complete)
    monkeypatch.setattr(tool_loop.tools, "dispatch", lambda *a, **k: {"ok": True})

    def boom(attempt, turn):
        raise RuntimeError("the state db is gone")

    out, _usage, _ok = tool_loop.run(
        [{"role": "user", "content": "go"}], model="m", env="e",
        max_turns=4, operator_notes=boom)
    assert out.text == "done"
