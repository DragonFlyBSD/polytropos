"""Fix-review chat is stored against the occurrence it is about (poly-pf4a).

It lived in the operator's localStorage. That restores a reload on the same
machine and nothing else: two operators on the same failing port could not
see each other's questions, and the reasoning that produced a decision was
not attached to the bundle that was decided.

Occurrence-scoped rather than issue-scoped because the chat is seeded with
one bundle's frozen artifacts and that bundle's session dump, so a turn read
beside a different attempt would be answering about evidence that attempt
never produced.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import (
    append_chat_turn,
    clear_chat_turns,
    list_chat_turns,
)
from dportsv3.tracker.server import create_app

TARGET = "@main"
ORIGIN = "devel/thing"


def _seed(db: sqlite3.Connection, art: Path) -> None:
    art.mkdir(parents=True, exist_ok=True)
    db.execute("INSERT INTO runs(run_id, target) VALUES ('r-1', ?)", (TARGET,))
    for bundle_id in ("b-1", "b-2"):
        db.execute(
            "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, result, "
            "target, resolution) VALUES (?, 'r-1', ?, 't1', 'failure', ?, "
            "'agent_fixed')", (bundle_id, ORIGIN, TARGET))
    path = art / "triage.md"
    path.write_text("## Classification\n\nplist-error\n", encoding="utf-8")
    db.execute(
        "INSERT INTO artifact_refs(bundle_id, relpath, backend, fs_path, "
        "kind, size, created_at) VALUES ('b-1', 'analysis/triage.md', 'fs', "
        "?, 'text', ?, 't1')", (str(path), path.stat().st_size))
    db.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    _seed(db, tmp_path / "artifacts")
    db.close()
    return path


@pytest.fixture
def client(db_path: Path) -> TestClient:
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# --- the store ------------------------------------------------------------


def test_turns_come_back_in_the_order_they_were_asked(db_path: Path) -> None:
    conn = _conn(db_path)
    append_chat_turn(conn, "b-1", "user", "why this patch?")
    append_chat_turn(
        conn, "b-1", "assistant", "because of the LDFLAGS override",
        session_relpath="sessions/a1.jsonl",
        artifacts_included=["analysis/triage.md"],
    )

    turns = list_chat_turns(conn, "b-1")
    conn.close()

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "why this patch?"
    assert turns[1]["artifacts_included"] == ["analysis/triage.md"]
    assert turns[1]["session_relpath"] == "sessions/a1.jsonl"


def test_a_conversation_belongs_to_one_occurrence(db_path: Path) -> None:
    """The chat is seeded with this bundle's artifacts and this bundle's
    session dump, so it would be answering about the wrong evidence
    anywhere else."""
    conn = _conn(db_path)
    append_chat_turn(conn, "b-1", "user", "asked about attempt one")

    assert len(list_chat_turns(conn, "b-1")) == 1
    assert list_chat_turns(conn, "b-2") == []
    conn.close()


def test_clearing_removes_the_thread_and_says_how_much(db_path: Path) -> None:
    conn = _conn(db_path)
    append_chat_turn(conn, "b-1", "user", "one")
    append_chat_turn(conn, "b-1", "assistant", "two")

    assert clear_chat_turns(conn, "b-1") == 2
    assert list_chat_turns(conn, "b-1") == []
    conn.close()


def test_an_unreadable_context_marker_does_not_lose_the_turn(
    db_path: Path,
) -> None:
    """artifacts_included is a per-turn record of what the model was
    shown. A turn is worth reading even when that one field is not."""
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO bundle_chat_turns(bundle_id, role, content, created_at, "
        "artifacts_included) VALUES ('b-1', 'assistant', 'hi', 't', '{oops')")

    turns = list_chat_turns(conn, "b-1")
    conn.close()

    assert turns[0]["content"] == "hi"
    assert turns[0]["artifacts_included"] == []


# --- the page -------------------------------------------------------------


def test_the_thread_is_rendered_server_side(
    client: TestClient, db_path: Path, set_setting,
) -> None:
    """So it is there on a fresh browser, and there for the next operator
    looking at the same port. localStorage was neither."""
    set_setting("llm.chat.model", "test-model")
    conn = _conn(db_path)
    append_chat_turn(conn, "b-1", "user", "why the LDFLAGS guard?")
    append_chat_turn(conn, "b-1", "assistant", "because **execinfo**")
    conn.close()

    body = client.get("/agentic/bundles/b-1").text

    assert "why the LDFLAGS guard?" in body
    # The assistant answers in Markdown and is rendered with the same
    # subset the artifact previews use.
    assert "<strong>execinfo</strong>" in body


def test_the_page_no_longer_claims_the_server_saves_nothing(
    client: TestClient, set_setting,
) -> None:
    set_setting("llm.chat.model", "test-model")

    body = client.get("/agentic/bundles/b-1").text

    assert "server saves nothing" not in body
    assert "Kept in this browser only" not in body


# --- the endpoint ---------------------------------------------------------


def test_a_turn_is_stored_once_the_model_has_answered(
    client: TestClient, db_path: Path, set_setting, monkeypatch,
) -> None:
    set_setting("llm.chat.model", "test-model")
    from dportsv3.agent import llm

    monkeypatch.setattr(
        llm, "complete",
        lambda *a, **k: llm.Response(
            text="it needs -lexecinfo",
            usage=llm.Usage(prompt_tokens=10, completion_tokens=5,
                            total_tokens=15),
        ),
    )

    resp = client.post("/api/bundles/b-1/chat", json={"message": "why?"})
    assert resp.status_code == 200, resp.text

    conn = _conn(db_path)
    turns = list_chat_turns(conn, "b-1")
    conn.close()

    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "why?"
    assert turns[1]["content"] == "it needs -lexecinfo"


def test_a_failed_answer_leaves_no_half_thread(
    client: TestClient, db_path: Path, set_setting, monkeypatch,
) -> None:
    """Nothing is written until the model has answered, so a question
    whose answer 502s does not sit in the thread with nothing under it."""
    set_setting("llm.chat.model", "test-model")
    from dportsv3.agent import llm

    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "complete", boom)

    assert client.post(
        "/api/bundles/b-1/chat", json={"message": "why?"},
    ).status_code == 502

    conn = _conn(db_path)
    assert list_chat_turns(conn, "b-1") == []
    conn.close()


def test_the_history_comes_from_the_store_not_the_request(
    client: TestClient, db_path: Path, set_setting, monkeypatch,
) -> None:
    """Otherwise two operators diverge: whoever asks next overwrites the
    thread with whatever their own browser happened to hold."""
    set_setting("llm.chat.model", "test-model")
    from dportsv3.agent import llm

    seen: list[list[dict]] = []

    def capture(messages, **k):
        seen.append(messages)
        return llm.Response(text="ok", usage=llm.Usage())

    monkeypatch.setattr(llm, "complete", capture)

    conn = _conn(db_path)
    append_chat_turn(conn, "b-1", "user", "asked earlier from another browser")
    append_chat_turn(conn, "b-1", "assistant", "answered earlier")
    conn.close()

    client.post("/api/bundles/b-1/chat", json={"message": "and now?"})

    sent = "\n".join(m["content"] for m in seen[0])
    assert "asked earlier from another browser" in sent
    assert "and now?" in sent


def test_an_older_client_sending_a_message_list_still_works(
    client: TestClient, set_setting, monkeypatch,
) -> None:
    """The last user turn in it is the question."""
    set_setting("llm.chat.model", "test-model")
    from dportsv3.agent import llm

    monkeypatch.setattr(
        llm, "complete", lambda *a, **k: llm.Response(
            text="ok", usage=llm.Usage()))

    resp = client.post("/api/bundles/b-1/chat", json={
        "messages": [{"role": "user", "content": "legacy shape"}]})

    assert resp.status_code == 200, resp.text


def test_an_empty_question_is_refused(
    client: TestClient, set_setting,
) -> None:
    set_setting("llm.chat.model", "test-model")

    assert client.post(
        "/api/bundles/b-1/chat", json={"message": "   "},
    ).status_code == 400


def test_clearing_is_an_endpoint(
    client: TestClient, db_path: Path, set_setting,
) -> None:
    set_setting("llm.chat.model", "test-model")
    conn = _conn(db_path)
    append_chat_turn(conn, "b-1", "user", "one")
    conn.close()

    resp = client.delete("/api/bundles/b-1/chat")

    assert resp.status_code == 200
    assert resp.json()["removed"] == 1
    conn = _conn(db_path)
    assert list_chat_turns(conn, "b-1") == []
    conn.close()


def test_clearing_an_unknown_occurrence_404s(
    client: TestClient, set_setting,
) -> None:
    set_setting("llm.chat.model", "test-model")

    assert client.delete("/api/bundles/nope/chat").status_code == 404
