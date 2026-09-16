"""Old tool output is masked in the request, never dropped (poly-hnk3).

``tool_loop.run`` re-sent every tool result for the whole attempt, so each
prompt-cache eviction re-billed all of it: misc/py-onnx went from 28k
tokens at T1 to 649k at T138. A first fix left whole old exchanges out of
the request; that deletes the agent's actions and its failed turns, which
no published harness does.

What replaced it is observation masking. JetBrains measured it on
SWE-bench as effective as LLM summarization, and cheaper
(arXiv 2508.21433); Anthropic's clear_tool_uses does the same server-side.
These tests hold it to:

- every assistant message and tool call is sent, only old *results* mask;
- a masked result keeps ok/rebuild_ok, so a failed build stays visibly
  failed;
- ``messages`` keeps everything, because session_dump persists it;
- masking is batched and placeholders never change, so the cached prefix
  holds between masks;
- dops_reference, note and results under 512 bytes are never masked, and
  a mask forgets get_file's scroll-back dedup;
- a placeholder never says to repeat a build or an edit.

And because this model keeps almost none of its reasoning in the
conversation (py-onnx: 0 of 138 turns), a ``note`` tool keeps the *why*,
and the retry hand-over carries notes from every attempt.
"""

from __future__ import annotations

import json

import pytest

from dportsv3.agent import attempt_loop, llm, tool_loop, tools, worker


def _drive(monkeypatch, *, turns, keep_turns, batch, result_bytes=2000,
           tool_for=None, args_for=None, result_for=None, events=None):
    sent: list[list[dict]] = []
    state = {"turn": 0}
    tool_for = tool_for or (lambda t: "get_file")
    args_for = args_for or (lambda t: {"path": f"/f{t}"})

    def fake_complete(messages, **kwargs):
        sent.append([dict(m) for m in messages])
        state["turn"] += 1
        t = state["turn"]
        if t > turns:
            return llm.Response(text="done")
        return llm.Response(text="", tool_calls=[
            llm.ToolCall(id=f"c{t}", name=tool_for(t), arguments=args_for(t))])

    def fake_dispatch(name, arguments, *, env):
        worker._READ_CACHE[("e", json.dumps(arguments), 0, 0)] = "sha"
        if result_for is not None:
            return result_for(name, arguments)
        return {"ok": True, "content": "x" * result_bytes}

    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(tools, "dispatch", fake_dispatch)
    messages = [{"role": "system", "content": "S" * 2000},
                {"role": "user", "content": "the task"}]
    tool_loop.run(messages, model="m", env="e", max_turns=turns + 1,
                  context_keep_turns=keep_turns, context_mask_batch=batch,
                  on_event=events.append if events is not None else None)
    return messages, sent


def _masked(msg: dict) -> bool:
    return msg["role"] == "tool" and "omitted" in json.loads(msg["content"])


@pytest.fixture(autouse=True)
def _clean_read_cache():
    worker.reset_attempt_caches()
    yield
    worker.reset_attempt_caches()


# --- what is masked, and what never is ---------------------------------------

def test_nothing_is_masked_inside_the_window(monkeypatch) -> None:
    messages, sent = _drive(monkeypatch, turns=10, keep_turns=10, batch=0)
    assert sent[-1] == messages


def test_zero_keep_turns_disables_it(monkeypatch) -> None:
    messages, sent = _drive(monkeypatch, turns=60, keep_turns=0, batch=0)
    assert sent[-1] == messages


def test_old_results_are_masked_and_every_call_is_kept(monkeypatch) -> None:
    messages, sent = _drive(monkeypatch, turns=40, keep_turns=10, batch=0)
    last = sent[-1]
    assert len(last) == len(messages), "nothing is dropped"
    for sent_msg, real in zip(last, messages):
        if real["role"] != "tool":
            assert sent_msg == real, "assistant turns and the head are verbatim"
    results = [m for m in last if m["role"] == "tool"]
    assert all(_masked(m) for m in results[:-10])
    assert not any(_masked(m) for m in results[-10:]), "the window stays whole"


def test_a_masked_build_is_still_visibly_failed(monkeypatch) -> None:
    def result_for(name, arguments):
        if name == "dsynth_build":
            return {"ok": False, "rebuild_ok": False, "stdout_tail": "y" * 4000}
        return {"ok": True, "content": "x" * 2000}

    _, sent = _drive(monkeypatch, turns=30, keep_turns=10, batch=0,
                     tool_for=lambda t: "dsynth_build" if t == 1 else "get_file",
                     result_for=result_for)
    first = next(m for m in sent[-1] if m["role"] == "tool")
    body = json.loads(first["content"])
    assert body["ok"] is False and body["rebuild_ok"] is False
    assert "omitted" in body and "y" * 50 not in first["content"]


def test_the_record_keeps_every_output(monkeypatch) -> None:
    messages, _ = _drive(monkeypatch, turns=40, keep_turns=10, batch=0)
    assert not any(_masked(m) for m in messages if m["role"] == "tool")


def test_dops_reference_and_notes_are_never_masked(monkeypatch) -> None:
    names = {1: "dops_reference", 2: "note"}
    _, sent = _drive(monkeypatch, turns=40, keep_turns=5, batch=0,
                     tool_for=lambda t: names.get(t, "get_file"),
                     args_for=lambda t: {"text": "why"} if t == 2 else {"path": f"/f{t}"})
    by_name = {m["name"]: m for m in sent[-1] if m["role"] == "tool"
               and m["name"] in ("dops_reference", "note")}
    assert not _masked(by_name["dops_reference"])
    assert not _masked(by_name["note"])


def test_small_results_are_never_masked(monkeypatch) -> None:
    """A short diagnostic costs less than the placeholder that hides it."""
    def result_for(name, arguments):
        if name == "validate_dops":
            return {"ok": False, "stderr_tail": "E_SYNTAX overlay.dops:3:1"}
        return {"ok": True, "content": "x" * 2000}

    _, sent = _drive(monkeypatch, turns=40, keep_turns=10, batch=0,
                     tool_for=lambda t: "validate_dops" if t == 1 else "get_file",
                     result_for=result_for)
    first = next(m for m in sent[-1] if m["role"] == "tool")
    assert first["name"] == "validate_dops" and not _masked(first)
    assert "E_SYNTAX" in first["content"]


@pytest.mark.parametrize("name, says", [
    ("get_file", "Repeat the call"),
    ("dsynth_log", "Repeat the call"),
    ("dsynth_build", "dsynth_log reads the build log again"),
    ("edit_file", "get_file shows the file"),
    ("put_file", "get_file shows the file"),
])
def test_a_placeholder_says_how_to_get_the_output_back(name, says) -> None:
    text = tool_loop._placeholder(
        {"role": "tool", "name": name, "content": json.dumps({"ok": True, "x": "y" * 900})})
    assert says in text


@pytest.mark.parametrize("name", ["dsynth_build", "edit_file", "put_file",
                                  "make_patch", "install_patches", "genpatch"])
def test_a_placeholder_never_says_to_repeat_a_change(name) -> None:
    """Repeating dsynth_build rebuilds; repeating an edit applies it twice."""
    text = tool_loop._placeholder(
        {"role": "tool", "name": name, "content": json.dumps({"ok": True, "x": "y" * 900})})
    assert "Repeat the call" not in text


# --- cache behaviour ------------------------------------------------------------

def test_masking_waits_for_a_batch(monkeypatch) -> None:
    events: list[dict] = []
    _drive(monkeypatch, turns=60, keep_turns=10, batch=20_000,
           result_bytes=2000, events=events)
    masks = [e for e in events if e["type"] == "observations_masked"]
    assert masks and all(e["masked_results"] >= 9 for e in masks), (
        "each mask should cover a batch, not one result at a time"
    )
    assert len(masks) <= 60 // 9


def test_the_prefix_holds_between_masks(monkeypatch) -> None:
    events: list[dict] = []
    _, sent = _drive(monkeypatch, turns=120, keep_turns=10, batch=30_000,
                     events=events)
    mask_turns = {e["turn"] for e in events if e["type"] == "observations_masked"}
    assert mask_turns
    for k in range(1, len(sent)):
        if k + 1 in mask_turns:
            continue
        assert sent[k][:len(sent[k - 1])] == sent[k - 1], (
            f"turn {k + 1} changed the prefix without masking anything"
        )


def test_zero_batch_masks_every_turn(monkeypatch) -> None:
    events: list[dict] = []
    _drive(monkeypatch, turns=30, keep_turns=10, batch=0, events=events)
    masks = [e for e in events if e["type"] == "observations_masked"]
    assert len(masks) == 30 - 10


def test_a_placeholder_never_changes_once_written(monkeypatch) -> None:
    _, sent = _drive(monkeypatch, turns=60, keep_turns=10, batch=0)
    first_tool = next(i for i, m in enumerate(sent[-1]) if m["role"] == "tool")
    versions = {req[first_tool]["content"] for req in sent
                if len(req) > first_tool and _masked(req[first_tool])}
    assert len(versions) == 1


# --- side effects and telemetry ------------------------------------------------

def test_a_mask_forgets_the_read_dedup(monkeypatch) -> None:
    seen: list[int] = []
    real = worker.reset_attempt_caches

    def spy():
        seen.append(len(worker._READ_CACHE))
        real()

    monkeypatch.setattr(worker, "reset_attempt_caches", spy)
    events: list[dict] = []
    _drive(monkeypatch, turns=40, keep_turns=10, batch=10_000, events=events)
    masks = [e for e in events if e["type"] == "observations_masked"]
    assert masks and len(seen) == len(masks)
    assert all(n > 0 for n in seen)


def test_masks_are_reported(monkeypatch) -> None:
    events: list[dict] = []
    _drive(monkeypatch, turns=40, keep_turns=10, batch=10_000, events=events)
    e = next(e for e in events if e["type"] == "observations_masked")
    assert e["masked_results"] > 0
    assert e["bytes_after"] < e["bytes_before"]


def test_a_read_repeated_after_masking_is_reported_once(monkeypatch) -> None:
    """Whether masking makes the agent re-read is the measurement that
    decides if summaries are ever needed. A repeated build is not."""
    events: list[dict] = []

    def tool_for(t):
        return "dsynth_build" if t in (2, 30, 31) else "get_file"

    def args_for(t):
        if t in (2, 30, 31):
            return {"origin": "misc/py-onnx"}
        return {"path": "/same" if t in (1, 25, 26) else f"/f{t}"}

    _drive(monkeypatch, turns=32, keep_turns=10, batch=0,
           tool_for=tool_for, args_for=args_for, events=events)
    repeats = [e for e in events if e["type"] == "repeat_after_mask"]
    assert [(e["tool"], e["args"], e["turn"]) for e in repeats] == [
        ("get_file", {"path": "/same"}, 25)]


# --- notes ----------------------------------------------------------------------

def test_the_note_tool_is_offered_and_accepted() -> None:
    spec = next(s for s in tools.schemas() if s["function"]["name"] == "note")
    assert spec["function"]["parameters"]["required"] == ["text"]
    assert "note" in tools.patch_tool_names()
    assert tools.dispatch("note", {"text": "tar: no such distfile"},
                          env="e") == {"ok": True}


def test_an_empty_note_is_refused() -> None:
    assert tools.dispatch("note", {"text": "  "}, env="e")["ok"] is False


def test_the_operator_log_shows_the_note() -> None:
    from dportsv3.agent.runner import _summarize_tool_call

    text = "patch-absl read /dev/null because genpatch ran before dupe; " * 3
    line = _summarize_tool_call("note", {"text": text}, {"ok": True})
    assert line.startswith(text.strip()[:200]) and line.endswith(" ok")


def test_the_prompt_asks_for_a_note_after_a_failed_build() -> None:
    from dportsv3.agent import prompts
    assert "note(text)" in prompts.PATCH_SYSTEM


def test_notes_from_every_attempt_reach_the_hand_over(monkeypatch) -> None:
    from dportsv3.agent.policy import Tier

    handovers: list[str] = []

    def fake_run(messages, *, on_event=None, attempt_idx=1, **kwargs):
        if attempt_idx > 1:
            handovers.append(messages[-1]["content"])
        on_event({"type": "tool_call", "attempt": attempt_idx, "turn": 1,
                  "tool": "note",
                  "args": {"text": f"attempt {attempt_idx}: patch read /dev/null"},
                  "result": {"ok": True}, "duration_ms": 1})
        return llm.Response(text="no proof"), llm.Usage(), False

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)
    attempt_loop.run("payload", tier=Tier(name="AUTO", max_iterations=3,
                                          max_tokens=0),
                     env="e", model="m")
    assert len(handovers) == 2
    assert "attempt 1: patch read /dev/null" in handovers[0]
    assert "attempt 1: patch read /dev/null" in handovers[1], (
        "notes accumulate; the tool log handed over is only the last attempt's"
    )
    assert "attempt 2: patch read /dev/null" in handovers[1]


def test_a_long_note_is_clipped_in_the_hand_over() -> None:
    msg = attempt_loop._failure_context_message(
        1, "no proof", notes=["a" * 5000, "short"])["content"]
    assert "a" * attempt_loop._MAX_NOTE_CHARS + "…" in msg
    assert "a" * (attempt_loop._MAX_NOTE_CHARS + 1) not in msg
    assert "- short\n" in msg


# --- wiring ---------------------------------------------------------------------

def test_the_patch_flow_reads_both_settings(monkeypatch) -> None:
    from dportsv3 import settings
    from dportsv3.agent import patch
    from dportsv3.agent.policy import Tier

    seen: dict = {}

    def fake_run(messages, **kwargs):
        seen.update(kwargs)
        return llm.Response(text="no proof"), llm.Usage(), False

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)
    patch.run("payload", tier=Tier(name="AUTO", max_iterations=1, max_tokens=0),
              env="e", model="m", max_tool_turns=3)
    assert seen["context_keep_turns"] == int(settings.get("llm.patch.context_keep_turns")) > 0
    assert seen["context_mask_batch"] == int(settings.get("llm.patch.context_mask_batch"))


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
    d({"type": "observations_masked", "attempt": 1, "turn": 40,
       "masked_results": 12, "bytes_before": 180_000, "bytes_after": 150_000})
    assert rows == [("observations_masked",
                     "A1.T40 masked 12 old tool result(s): 180000 -> 150000 bytes")]
