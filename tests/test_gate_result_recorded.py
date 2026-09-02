"""An observed gate refusal must outlive the attempt that saw it (poly-qkp).

poly-8ni gave the agent ``dsynth_test`` so the loop could reach the
acceptance gate. It reached it and then threw the answer away:
devel_libunwind-20260902-124214Z ran ``dsynth_test`` at turn 22, got a
refusal, spent turns 24-30 diagnosing it, hit ``max_tool_turns`` and never
wrote a report. The orphan rescue lifted ``rebuild_ok=true`` from the
turn-19 ``dsynth_build`` and the bundle resolved ``agent_fixed`` with no
record that the gate had refused it.

Two facts the harness already had, and now keeps:
  * whether ``dsynth_test`` ran and what it said  -> ``gate_ok``
  * whether the loop ended with a real report     -> ``report_complete``

Both are recorded from the event stream rather than read out of the
model's prose, because the failure mode is precisely that the model never
gets a turn to write prose.
"""

from __future__ import annotations

import json


# --- tool_loop says why it stopped ------------------------------------------


def _resp(llm, text="", tool_calls=None, tokens=10):
    return llm.Response(
        text=text,
        tool_calls=tool_calls,
        usage=llm.Usage(prompt_tokens=tokens, completion_tokens=0,
                        total_tokens=tokens),
    )


def test_loop_stop_reports_turn_cap(monkeypatch):
    """The stop that produced the bug: model still calling tools when the
    turn cap lands, so there is no report."""
    from dportsv3.agent import llm, tool_loop, tools

    monkeypatch.setattr(llm, "complete", lambda *a, **k: _resp(
        llm, text="still thinking",
        tool_calls=[llm.ToolCall(id="t", name="env_verify", arguments={})]))
    monkeypatch.setattr(tools, "dispatch", lambda n, a, *, env: {"ok": True})

    events: list[dict] = []
    tool_loop.run([{"role": "user", "content": "x"}], model="m", env="e",
                  max_turns=3, on_event=events.append)

    stops = [e for e in events if e["type"] == "loop_stop"]
    assert len(stops) == 1
    assert stops[0]["reason"] == "turn_cap"
    assert stops[0]["turn"] == 3


def test_loop_stop_reports_text_only(monkeypatch):
    """The healthy stop: the model finished and wrote its report."""
    from dportsv3.agent import llm, tool_loop

    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: _resp(llm, text="## Patch Log\ndone"))

    events: list[dict] = []
    tool_loop.run([{"role": "user", "content": "x"}], model="m", env="e",
                  max_turns=5, on_event=events.append)

    stops = [e for e in events if e["type"] == "loop_stop"]
    assert [s["reason"] for s in stops] == ["text_only"]


def test_loop_stop_reports_token_budget(monkeypatch):
    """Budget stops are not reports either."""
    from dportsv3.agent import llm, tool_loop, tools

    monkeypatch.setattr(llm, "complete", lambda *a, **k: _resp(
        llm, text="mid-thought", tokens=500,
        tool_calls=[llm.ToolCall(id="t", name="env_verify", arguments={})]))
    monkeypatch.setattr(tools, "dispatch", lambda n, a, *, env: {"ok": True})

    events: list[dict] = []
    tool_loop.run([{"role": "user", "content": "x"}], model="m", env="e",
                  max_turns=5, max_tokens=100, on_event=events.append)

    stops = [e for e in events if e["type"] == "loop_stop"]
    assert stops and stops[-1]["reason"] == "token_budget"


# --- attempt_loop keeps the observations ------------------------------------


def _tier(monkeypatch, iterations=1, tokens=0):
    from dportsv3.agent.policy import Tier
    return Tier(name="ASSIST", max_iterations=iterations, max_tokens=tokens)


def test_gate_refusal_survives_a_turn_capped_attempt(monkeypatch):
    """The libunwind shape end to end: green build, refused gate, no
    report. rebuild_ok still true — and the refusal is still recorded."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.llm import Response, Usage

    def fake_run(*args, **kwargs):
        emit = kwargs["on_event"]
        emit({"type": "tool_call", "tool": "dsynth_build",
              "result": {"ok": True, "rebuild_ok": True}})
        emit({"type": "tool_call", "tool": "dsynth_test",
              "result": {"ok": False, "rebuild_ok": False}})
        emit({"type": "loop_stop", "reason": "turn_cap", "turn": 30})
        # No proof block: the attempt never reached its report.
        return (Response(text="Let me understand what --enable-tests does"),
                Usage(prompt_tokens=10, completion_tokens=0, total_tokens=10),
                True)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)

    res = attempt_loop.run(
        payload="p", system_prompt="s", model="m", env="e",
        tier=_tier(monkeypatch),
    )

    # The orphan rescue still fires — that behaviour is unchanged.
    assert res.status == "success"
    # But the gate's verdict is no longer lost.
    assert res.gate_ok is False
    assert res.report_complete is False


def test_gate_pass_is_recorded_too(monkeypatch):
    """Not just failures: a green gate is worth recording as evidence."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.llm import Response, Usage

    def fake_run(*args, **kwargs):
        emit = kwargs["on_event"]
        emit({"type": "tool_call", "tool": "dsynth_test",
              "result": {"ok": True, "rebuild_ok": True}})
        emit({"type": "loop_stop", "reason": "text_only", "turn": 8})
        return (Response(text='## Rebuild Proof (JSON)\n```json\n'
                              '{"rebuild_ok": true}\n```'),
                Usage(prompt_tokens=10, completion_tokens=0, total_tokens=10),
                True)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)

    res = attempt_loop.run(payload="p", system_prompt="s", model="m",
                           env="e", tier=_tier(monkeypatch))

    assert res.gate_ok is True
    assert res.report_complete is True


def test_gate_absent_when_never_run(monkeypatch):
    """None, not False — "did not run the gate" is not "gate refused"."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.llm import Response, Usage

    def fake_run(*args, **kwargs):
        kwargs["on_event"]({"type": "loop_stop", "reason": "text_only",
                            "turn": 4})
        return (Response(text="## Patch Log\nno gate call"),
                Usage(prompt_tokens=10, completion_tokens=0, total_tokens=10),
                False)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)

    res = attempt_loop.run(payload="p", system_prompt="s", model="m",
                           env="e", tier=_tier(monkeypatch))

    assert res.gate_ok is None


def test_caller_on_event_still_receives_everything(monkeypatch):
    """The observer wraps the caller's callback; it must not swallow
    events — the tool trace is built from them."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.llm import Response, Usage

    def fake_run(*args, **kwargs):
        kwargs["on_event"]({"type": "tool_call", "tool": "dsynth_test",
                            "result": {"rebuild_ok": False}})
        kwargs["on_event"]({"type": "loop_stop", "reason": "text_only",
                            "turn": 2})
        return (Response(text="x"),
                Usage(prompt_tokens=1, completion_tokens=0, total_tokens=1),
                False)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)
    seen: list[dict] = []
    attempt_loop.run(payload="p", system_prompt="s", model="m", env="e",
                     tier=_tier(monkeypatch), on_event=seen.append)

    kinds = [e["type"] for e in seen]
    assert "tool_call" in kinds
    assert "loop_stop" in kinds
    # attempt_start/attempt_end are the caller's own, still present.
    assert "attempt_end" in kinds


def test_gate_survives_budget_exhaustion(monkeypatch):
    """Not just the success path. A gate refusal followed by budget
    exhaustion is the combination most worth keeping, and the return
    that carries it is a different one — it was missed on the first
    pass of this change."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.llm import Response, Usage

    def fake_run(*args, **kwargs):
        emit = kwargs["on_event"]
        emit({"type": "tool_call", "tool": "dsynth_test",
              "result": {"ok": False, "rebuild_ok": False}})
        emit({"type": "loop_stop", "reason": "token_budget", "turn": 12})
        return (Response(text="ran out mid-thought"),
                Usage(prompt_tokens=500, completion_tokens=0,
                      total_tokens=500),
                False)

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)

    res = attempt_loop.run(payload="p", system_prompt="s", model="m",
                           env="e", tier=_tier(monkeypatch, tokens=100))

    assert res.status == "budget-exhausted"
    assert res.gate_ok is False
    assert res.report_complete is False


# --- the artifacts ----------------------------------------------------------


def test_proof_artifact_carries_gate_and_report_is_marked(tmp_path,
                                                          monkeypatch):
    """rebuild_proof.json gains gate_ok, and an incomplete patch.md says
    so instead of reading like a considered report."""
    from dportsv3.agent import runner
    from dportsv3.agent.attempt_loop import AttemptInfo, PatchResult

    result = PatchResult(
        status="success",
        final_text="The picture is getting clearer but has a contradiction",
        attempts=[AttemptInfo(attempt=1, tokens=10, rebuild_ok=True)],
        proof={"rebuild_ok": True, "source": "tool_result"},
        gate_ok=False,
        report_complete=False,
    )
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "analysis").mkdir(parents=True)
    monkeypatch.setattr(runner, "artifact_store_put",
                        lambda *a, **kw: None)

    runner._write_patch_audit_harness(bundle_dir, None, result,
                                      "test/model", "devel/foo")

    proof = json.loads((bundle_dir / "analysis" / "rebuild_proof.json")
                       .read_text())
    assert proof["gate_ok"] is False
    assert proof["rebuild_ok"] is True  # oracle unchanged, on purpose

    md = (bundle_dir / "analysis" / "patch.md").read_text()
    assert "Incomplete — no final report" in md
    # The fragment is kept, not discarded: it is the only account of
    # where the attempt had got to.
    assert "The picture is getting clearer" in md


def test_incomplete_with_no_text_says_so(tmp_path, monkeypatch):
    """A budget stop landing after the LLM turn but before tool dispatch
    leaves final_text empty. The marker must not promise commentary it
    then fails to show — security_trousers-20260902-134121Z rendered as a
    header followed by nothing."""
    from dportsv3.agent import runner
    from dportsv3.agent.attempt_loop import AttemptInfo, PatchResult

    result = PatchResult(
        status="budget-exhausted",
        final_text="",
        attempts=[AttemptInfo(attempt=1, tokens=10, rebuild_ok=False)],
        proof=None,
        report_complete=False,
    )
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "analysis").mkdir(parents=True)
    monkeypatch.setattr(runner, "artifact_store_put", lambda *a, **kw: None)

    runner._write_patch_audit_harness(bundle_dir, None, result,
                                      "test/model", "devel/foo")

    md = (bundle_dir / "analysis" / "patch.md").read_text()
    assert "nothing to show" in md
    assert "What follows" not in md
    assert "tool_trace.jsonl" in md


def test_complete_report_is_untouched(tmp_path, monkeypatch):
    """No marker, and no gate_ok key, when there is nothing to say."""
    from dportsv3.agent import runner
    from dportsv3.agent.attempt_loop import AttemptInfo, PatchResult

    result = PatchResult(
        status="success",
        final_text="## Patch Log\nA real report.",
        attempts=[AttemptInfo(attempt=1, tokens=10, rebuild_ok=True)],
        proof={"rebuild_ok": True},
    )
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "analysis").mkdir(parents=True)
    monkeypatch.setattr(runner, "artifact_store_put", lambda *a, **kw: None)

    runner._write_patch_audit_harness(bundle_dir, None, result,
                                      "test/model", "devel/foo")

    md = (bundle_dir / "analysis" / "patch.md").read_text()
    assert "Incomplete" not in md
    assert md.startswith("## Patch Log")
    proof = json.loads((bundle_dir / "analysis" / "rebuild_proof.json")
                       .read_text())
    assert "gate_ok" not in proof
