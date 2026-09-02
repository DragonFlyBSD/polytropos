"""An attempt that never wrote a report must not look like one (poly-qkp).

devel_libunwind-20260902-124214Z hit ``max_tool_turns`` mid-investigation and
never emitted a final response. The orphan rescue lifted ``rebuild_ok=true``
from an earlier tool result, and ``analysis/patch.md`` was written from
whatever commentary accompanied the last tool call — 205 characters of
mid-reasoning, indistinguishable from a considered report.

``tool_loop`` now says why it stopped, and ``report_complete`` follows from
that: only a ``text_only`` stop produced a real report. Recorded by the
harness, because the failure mode is precisely that the model never gets a
turn to say so itself.

The companion gate field is gone with poly-9sw: there is one build
tool again, it runs ``dsynth test``, so ``rebuild_ok`` already means the
port built and passed the gate.
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



# --- the artifacts ----------------------------------------------------------


def test_incomplete_report_is_marked(tmp_path, monkeypatch):
    """An incomplete patch.md says so instead of reading like a
    considered report."""
    from dportsv3.agent import runner
    from dportsv3.agent.attempt_loop import AttemptInfo, PatchResult

    result = PatchResult(
        status="success",
        final_text="The picture is getting clearer but has a contradiction",
        attempts=[AttemptInfo(attempt=1, tokens=10, rebuild_ok=True)],
        proof={"rebuild_ok": True, "source": "tool_result"},
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
    assert proof["rebuild_ok"] is True

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
    """No marker when there is nothing to say."""
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
    assert (bundle_dir / "analysis" / "rebuild_proof.json").exists()


# --- one build, and it is the gate (poly-9sw) --------------------------------


def test_dsynth_build_runs_the_test_subcommand(monkeypatch):
    """There is one build tool and it runs dsynth's `test`, not `build`.

    Two tools meant every successful attempt compiled the port twice:
    `test` force-rebuilds, so it threw away what `build` had just made.
    Measured on devel/level-zero, `test` cost 103% of `build` — the same
    work plus install/deinstall/check-plist — so the second compile
    bought ~3% of extra signal.
    """
    import subprocess
    from dportsv3.agent import tools, worker

    captured = {}

    def fake_exec(env, *argv, cwd="/work/DeltaPorts", input_text=None,
                  timeout=None):
        captured["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    monkeypatch.setattr(worker, "_dsynth_log_path", lambda o: "/tmp/log")
    monkeypatch.setattr(worker, "_dsynth_log_candidates", lambda e, o: [])
    monkeypatch.setattr(worker, "_port_subtree_hash", lambda e, o: "deadbeef")
    monkeypatch.setitem(worker._MATERIALIZE_STATE,
                        ("test-env", "devel/foo"), "deadbeef")

    worker.dsynth_build("test-env", "devel/foo")

    cmd = " ".join(str(a) for a in captured["argv"])
    assert ' test "$1"' in cmd
    assert ' build "$1"' not in cmd
    # And the second tool is gone from the registry entirely.
    assert "dsynth_test" not in tools.names()
    assert not hasattr(worker, "dsynth_test")
