"""Stopping when a tool reports a wall the agent cannot clear (poly-n78).

devel/glib20 on x6: ``make_extract`` failed with "Invalid perl5 version
5.36" — the ports framework IGNOREing the port because the environment's
installed perl predates the tree's. With no WRKSRC there is nothing to
dupe and nothing to genpatch, so the procedure the playbook prescribes
was unavailable.

The agent kept going for 44 more tool calls, spent the whole 120K
billable budget, and emitted a hand-edited patch that did not apply. Its
own words at the turn it gave up on the procedure:

    "The make_extract failed with an environment error ... Let me
     investigate the available sources to regenerate the patch without
     it."

That is sound behaviour for an agent with nowhere to report a broken
environment. The fix is to give it somewhere: a failed precondition ends
the attempt with a verdict an operator sees, instead of degrading into
improvisation that looks like a fix.
"""

from __future__ import annotations

import subprocess

import pytest


# --- make_extract tells an IGNORE apart from a broken extract --------------

def _fake_exec(monkeypatch, extract_rc, ignore_stdout):
    """Stub the two subprocess calls make_extract makes on failure."""
    from dportsv3.agent import worker

    calls: list[str] = []

    def fake(env, *argv):
        cmd = argv[-1]
        calls.append(cmd)
        if "-V IGNORE" in cmd:
            return subprocess.CompletedProcess(argv, 0, ignore_stdout, "")
        return subprocess.CompletedProcess(
            argv, extract_rc, "===>  x-1.0 Invalid perl5 version 5.36.\n", "")

    monkeypatch.setattr(worker, "_exec", fake)
    return calls


def test_an_ignored_port_is_reported_as_blocking(monkeypatch) -> None:
    from dportsv3.agent import worker
    _fake_exec(monkeypatch, 1, "Invalid perl5 version 5.36\n")

    out = worker.make_extract("env", "devel/x")

    assert out["ok"] is False
    assert out["blocking"] is True
    assert out["ignore_reason"] == "Invalid perl5 version 5.36"
    assert "environment problem" in out["summary"]


def test_the_framework_is_asked_rather_than_the_error_text_matched(
    monkeypatch,
) -> None:
    """IGNORE reasons are free text set by any Uses/*.mk. Grepping the
    build output for known phrases would miss every reason nobody has
    seen yet; `make -V IGNORE` is the framework's own answer."""
    from dportsv3.agent import worker
    calls = _fake_exec(monkeypatch, 1, "some reason nobody has hardcoded\n")

    out = worker.make_extract("env", "devel/x")

    assert any("-V IGNORE" in c for c in calls), "IGNORE was never queried"
    assert out["ignore_reason"] == "some reason nobody has hardcoded"


def test_an_extract_failure_with_no_ignore_is_not_blocking(monkeypatch) -> None:
    """A genuine extract failure — bad checksum, missing distfile — is
    the agent's problem to work, not an environment verdict."""
    from dportsv3.agent import worker
    _fake_exec(monkeypatch, 1, "")

    out = worker.make_extract("env", "devel/x")

    assert out["ok"] is False
    assert "blocking" not in out


# --- the tool loop stops ---------------------------------------------------

def _blocking_loop(monkeypatch, result):
    from dportsv3.agent import llm, tools

    def fake_complete(*args, **kwargs):
        return llm.Response(
            text="",
            tool_calls=[llm.ToolCall(id="tc-1", name="make_extract",
                                     arguments={"origin": "devel/x"})],
            usage=llm.Usage(prompt_tokens=90, completion_tokens=10,
                            total_tokens=100),
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(tools, "dispatch", lambda n, a, *, env: result)


def test_a_blocking_result_ends_the_loop(monkeypatch) -> None:
    from dportsv3.agent import tool_loop
    _blocking_loop(monkeypatch, {"ok": False, "blocking": True,
                                 "ignore_reason": "Invalid perl5 version 5.36"})
    events: list[dict] = []
    messages = [{"role": "user", "content": "x"}]

    with pytest.raises(tool_loop.EnvironmentBlocked) as caught:
        tool_loop.run(messages, model="m", env="e", on_event=events.append)

    assert caught.value.tool == "make_extract"
    assert "perl5" in caught.value.reason
    assert [e["type"] for e in events].count("environment_blocked") == 1


def test_the_tool_result_is_recorded_before_the_loop_stops(monkeypatch) -> None:
    """The session dump has to show what the model was told. Raising
    before appending would lose the one message that explains the stop."""
    from dportsv3.agent import tool_loop
    _blocking_loop(monkeypatch, {"ok": False, "blocking": True,
                                 "ignore_reason": "r"})
    messages = [{"role": "user", "content": "x"}]

    with pytest.raises(tool_loop.EnvironmentBlocked):
        tool_loop.run(messages, model="m", env="e")

    assert messages[-1]["role"] == "tool"
    assert messages[-1]["name"] == "make_extract"


def test_the_spend_so_far_is_carried_out(monkeypatch) -> None:
    """Otherwise the attempt's tokens vanish from the audit and a
    blocked run looks free."""
    from dportsv3.agent import tool_loop
    _blocking_loop(monkeypatch, {"ok": False, "blocking": True,
                                 "ignore_reason": "r"})

    with pytest.raises(tool_loop.EnvironmentBlocked) as caught:
        tool_loop.run([{"role": "user", "content": "x"}], model="m", env="e")

    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 100


def test_a_non_blocking_failure_does_not_stop_the_loop(monkeypatch) -> None:
    """Ordinary tool failures are the agent's to recover from."""
    from dportsv3.agent import llm, tool_loop, tools

    turns: list[int] = []

    def fake_complete(*args, **kwargs):
        turns.append(1)
        if len(turns) == 1:
            return llm.Response(
                text="",
                tool_calls=[llm.ToolCall(id="t", name="make_extract",
                                         arguments={})],
                usage=llm.Usage(prompt_tokens=10, completion_tokens=1,
                                total_tokens=11))
        return llm.Response(text="done", usage=llm.Usage(
            prompt_tokens=10, completion_tokens=1, total_tokens=11))

    monkeypatch.setattr(llm, "complete", fake_complete)
    monkeypatch.setattr(tools, "dispatch",
                        lambda n, a, *, env: {"ok": False, "rc": 1})

    response, _usage, _seen = tool_loop.run(
        [{"role": "user", "content": "x"}], model="m", env="e")
    assert response.text == "done"


# --- the attempt ends with a verdict ---------------------------------------

def test_attempt_loop_reports_environment_blocked(monkeypatch) -> None:
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.llm import Usage
    from dportsv3.agent.policy import Tier

    def fake_run(*args, **kwargs):
        raise attempt_loop.tool_loop.EnvironmentBlocked(
            "Invalid perl5 version 5.36", "make_extract",
            Usage(prompt_tokens=90, completion_tokens=10, total_tokens=100))

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)

    result = attempt_loop.run(
        "payload",
        tier=Tier(name="AUTO", max_iterations=3, max_tokens=100_000),
        env="e", model="m",
    )

    assert result.status == "environment-blocked"
    assert "make_extract" in result.final_text
    assert "Invalid perl5 version 5.36" in result.final_text
    assert result.usage.total_tokens == 100, "spend was dropped"


def test_the_remaining_attempts_are_not_spent(monkeypatch) -> None:
    """The point of the bead: one wall, one report — not three attempts
    improvising around it until the budget is gone."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.policy import Tier

    calls: list[int] = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        raise attempt_loop.tool_loop.EnvironmentBlocked("r", "make_extract")

    monkeypatch.setattr(attempt_loop.tool_loop, "run", fake_run)

    attempt_loop.run(
        "payload",
        tier=Tier(name="AUTO", max_iterations=5, max_tokens=100_000),
        env="e", model="m",
    )

    assert len(calls) == 1, f"tried {len(calls)} attempts against the same wall"


def test_the_verdict_says_it_is_not_a_port_problem(monkeypatch) -> None:
    """An operator reading the handoff has to know where to look. The
    failure this replaces produced a patch diff, which pointed at the
    port."""
    from dportsv3.agent import attempt_loop
    from dportsv3.agent.policy import Tier

    monkeypatch.setattr(
        attempt_loop.tool_loop, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            attempt_loop.tool_loop.EnvironmentBlocked("r", "make_extract")))

    result = attempt_loop.run(
        "payload", tier=Tier(name="AUTO", max_iterations=2, max_tokens=1000),
        env="e", model="m")

    assert "operator" in result.final_text.lower()
    assert "not a different fix for the port" in result.final_text
