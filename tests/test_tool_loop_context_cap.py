"""The tool loop bounds what it re-sends, not what it records (poly-hnk3).

``tool_loop.run`` appended every assistant turn and tool result and never
removed any, so the request grew for the whole attempt and every prompt-
cache eviction — one per dsynth_build — re-billed all of it:

  www/firefox run 4, attempt 1   78k at T1 -> 185k at T106 -> 348k at T124
  misc/py-onnx 2026-09-16        28k at T1 -> 146k at T130 -> 649k at T138,
                                 after which the next request got a 400

The model is now sent the head the attempt started with plus the newest
whole exchanges that fit. What these tests hold it to:

- ``messages`` keeps every turn. It is what session_dump persists, and the
  py-onnx diagnosis above was only possible because it did.
- A tool call is never sent without its result, nor a result without its
  call — providers reject both.
- Cuts are deep and rare, because prompt caches match on a prefix; between
  cuts each request extends the one before it.
- A cut forgets get_file's "unchanged, scroll back to it" dedup, which
  assumes the bytes are still in the conversation.
"""

from __future__ import annotations

import json

import pytest

from dportsv3.agent import llm, tool_loop, tools, worker


def _drive(monkeypatch, *, turns, result_bytes, cap, calls_per_turn=1,
           events=None):
    sent: list[list[dict]] = []
    state = {"turn": 0}

    def fake_complete(messages, **kwargs):
        sent.append([dict(m) for m in messages])
        state["turn"] += 1
        t = state["turn"]
        if t > turns:
            return llm.Response(text="done")
        return llm.Response(text="", tool_calls=[
            llm.ToolCall(id=f"c{t}-{k}", name="get_file",
                         arguments={"path": f"/f{t}-{k}"})
            for k in range(calls_per_turn)])

    def fake_dispatch(name, arguments, *, env):
        worker._READ_CACHE[("e", arguments["path"], 0, 0)] = "sha"
        return {"ok": True, "content": "x" * result_bytes}

    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(tools, "dispatch", fake_dispatch)
    messages = [{"role": "system", "content": "S" * 2000},
                {"role": "user", "content": "the task"}]
    tool_loop.run(messages, model="m", env="e", max_turns=turns + 1,
                  context_cap=cap, on_event=(events.append if events is not None
                                             else None))
    return messages, sent


def _bytes(msgs):
    return sum(len(json.dumps(m).encode()) for m in msgs)


@pytest.fixture(autouse=True)
def _clean_read_cache():
    worker.reset_attempt_caches()
    yield
    worker.reset_attempt_caches()


def test_under_the_cap_every_turn_is_sent(monkeypatch) -> None:
    messages, sent = _drive(monkeypatch, turns=10, result_bytes=100,
                            cap=10_000_000)
    assert sent[-1] == messages
    assert tool_loop.ELISION_NOTE not in json.dumps(sent[-1])


def test_zero_disables_it(monkeypatch) -> None:
    messages, sent = _drive(monkeypatch, turns=40, result_bytes=5000, cap=0)
    assert sent[-1] == messages


def test_over_the_cap_the_oldest_exchanges_go_and_the_head_stays(monkeypatch) -> None:
    cap = 60_000
    messages, sent = _drive(monkeypatch, turns=60, result_bytes=3000, cap=cap)
    last = sent[-1]
    assert last[0] == messages[0], "the system prompt is never cut"
    assert last[1]["role"] == "user"
    assert last[1]["content"].startswith("the task")
    assert last[1]["content"].endswith(tool_loop.ELISION_NOTE)
    assert "c1-0" not in json.dumps(last), "the oldest exchange is gone"
    assert last[-2:] == messages[-2:], "the newest exchange is always sent"
    assert _bytes(last) <= cap


def test_the_record_keeps_every_turn(monkeypatch) -> None:
    """session_dump persists this list; it must not be the elided one."""
    messages, sent = _drive(monkeypatch, turns=60, result_bytes=3000,
                            cap=60_000)
    assert len([m for m in messages if m["role"] == "assistant"]) == 60
    assert "c1-0" in json.dumps(messages)
    assert tool_loop.ELISION_NOTE not in json.dumps(messages)


def test_a_call_is_never_sent_without_its_results(monkeypatch) -> None:
    _, sent = _drive(monkeypatch, turns=50, result_bytes=2500, cap=50_000,
                     calls_per_turn=3)
    for req in sent:
        ids_called = {tc["id"] for m in req if m["role"] == "assistant"
                      for tc in m.get("tool_calls") or []}
        ids_answered = {m["tool_call_id"] for m in req if m["role"] == "tool"}
        assert ids_called == ids_answered
        # and no result arrives before the call that asked for it
        for i, m in enumerate(req):
            if m["role"] == "tool":
                assert req[i - 1]["role"] in ("assistant", "tool")


def test_roles_still_alternate(monkeypatch) -> None:
    """The note rides on the task message rather than being a message of
    its own: strict chat templates on local endpoints reject two user
    turns in a row."""
    _, sent = _drive(monkeypatch, turns=60, result_bytes=3000, cap=60_000)
    roles = [m["role"] for m in sent[-1]]
    assert roles[:3] == ["system", "user", "assistant"]
    assert "user" not in roles[2:]


def test_cuts_are_rare_and_the_prefix_holds_between_them(monkeypatch) -> None:
    events: list[dict] = []
    _, sent = _drive(monkeypatch, turns=200, result_bytes=3000, cap=80_000,
                     events=events)
    cut_turns = {e["turn"] for e in events if e["type"] == "context_elided"}
    assert 0 < len(cut_turns) <= 200 // 8, (
        "cutting to just under the cap would cut nearly every turn"
    )
    for k in range(1, len(sent)):
        if k + 1 in cut_turns:
            continue
        prev = sent[k - 1]
        assert sent[k][:len(prev)] == prev, (
            f"turn {k + 1} changed the prefix without a cut"
        )


def test_the_note_does_not_change_from_one_cut_to_the_next(monkeypatch) -> None:
    events: list[dict] = []
    _, sent = _drive(monkeypatch, turns=200, result_bytes=3000, cap=80_000,
                     events=events)
    cut_turns = sorted(e["turn"] for e in events
                       if e["type"] == "context_elided")
    assert len(cut_turns) >= 2
    heads = {json.dumps(sent[t - 1][:2]) for t in cut_turns}
    assert len(heads) == 1


def test_a_cut_forgets_the_read_dedup(monkeypatch) -> None:
    seen: list[int] = []
    real_reset = worker.reset_attempt_caches

    def spy():
        seen.append(len(worker._READ_CACHE))
        real_reset()

    monkeypatch.setattr(worker, "reset_attempt_caches", spy)
    events: list[dict] = []
    _drive(monkeypatch, turns=60, result_bytes=3000, cap=60_000, events=events)
    cuts = [e for e in events if e["type"] == "context_elided"]
    assert cuts and len(seen) == len(cuts)
    assert all(n > 0 for n in seen), "it was cleared while holding reads"


def test_the_newest_exchange_stays_even_when_it_alone_is_over(monkeypatch) -> None:
    _, sent = _drive(monkeypatch, turns=5, result_bytes=50_000, cap=20_000)
    last = sent[-1]
    assert last[-1]["role"] == "tool"
    assert last[-2]["role"] == "assistant"
    assert last[-2]["tool_calls"][0]["id"] == "c5-0"


def test_the_cut_is_reported(monkeypatch) -> None:
    events: list[dict] = []
    _drive(monkeypatch, turns=60, result_bytes=3000, cap=60_000, events=events)
    cut = next(e for e in events if e["type"] == "context_elided")
    assert cut["dropped_exchanges"] > 0
    assert cut["bytes_before"] > cut["cap"] >= cut["bytes_after"]
    assert cut["bytes_after"] <= int(cut["cap"] * 0.6) + 20_000


# --- wiring ---------------------------------------------------------------

def test_the_patch_flow_reads_the_setting_and_threads_it(monkeypatch) -> None:
    from dportsv3 import settings
    from dportsv3.agent import attempt_loop, patch

    seen: dict = {}

    def fake_tool_loop_run(messages, **kwargs):
        seen.update(kwargs)
        return llm.Response(text="no proof"), llm.Usage(), False

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_tool_loop_run)
    from dportsv3.agent.policy import Tier
    patch.run("payload", tier=Tier(name="AUTO", max_iterations=1,
                                   max_tokens=0),
              env="e", model="m", max_tool_turns=3)
    assert seen["context_cap"] == int(settings.get("llm.patch.context_cap"))
    assert seen["context_cap"] > 0


def test_the_operator_log_gets_a_line() -> None:
    from dportsv3.agent.steps import PatchEventDispatcher

    rows: list[tuple] = []
    d = PatchEventDispatcher(
        queue_root=None, job_id="j", origin="misc/py-onnx",
        activity_log=lambda q, kind, msg, **kw: rows.append((kind, msg)),
        looks_env_suspicious=lambda r: False,
        invalidate_health_cache=lambda: None,
        summarize_tool_call=lambda *a: "",
    )
    d({"type": "context_elided", "attempt": 1, "turn": 90,
       "dropped_exchanges": 41, "bytes_before": 400_000,
       "bytes_after": 230_000, "cap": 393_216})
    assert rows == [("context_elided",
                     "A1.T90 left 41 old exchange(s) out: 400000 -> 230000 "
                     "bytes (cap 393216)")]
