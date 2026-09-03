"""poly-b05: re-prime the prompt cache before a retry.

poly-2w6 put ``prompt_cache_key`` on every request and the retry cold
start did not move. The reason is that the provider matches only the
MOST-RECENTLY-USED prefix for a key: attempt 1 grows its conversation to
~110k tokens, so that chain becomes the match target, and attempt 2 —
which shares only the opening ``[system, payload]`` — matches nothing.
The opening is still cached; an exact repeat of it hits.

So repeat it. Measured by replaying a real 30-turn transcript, two arms
on separate keys:

    no re-prime, attempt 2 turn 1  prompt=31269 cached=    0 -> 31439 billable
    re-prime (max_tokens=1)        prompt=31172 cached=31153 ->    20 billable
    then attempt 2 turn 1          prompt=31269 cached=31217 ->   323 billable
"""

from __future__ import annotations

import pytest

from dportsv3.agent import attempt_loop, llm
from dportsv3.agent.llm import Response, Usage


# --- who gets a re-prime at all ---------------------------------------

def test_meta_supports_the_prompt_cache():
    assert llm.supports_prompt_cache(
        "muse-spark-1.3-contributor", custom_llm_provider="meta")


@pytest.mark.parametrize("provider", ["deepseek", "nvidia", "openai"])
def test_providers_without_a_dialect_do_not(provider):
    """No key to make current, so nothing to re-prime."""
    assert not llm.supports_prompt_cache("m", custom_llm_provider=provider)


def test_the_litellm_backend_never_supports_it(monkeypatch):
    """litellm 1.65.0 sends extra_body as a literal top-level field, so
    the key never reaches the endpoint however the dialect spells it —
    a re-prime down that path would be a full-price request buying
    nothing."""
    monkeypatch.setattr(llm, "_backend", lambda: "litellm")
    assert not llm.supports_prompt_cache("m", custom_llm_provider="meta")


# --- what prime_cache puts on the wire --------------------------------

def _capture_openai(monkeypatch, *, usage=None, raises=None):
    """Stand in for _complete_openai, recording its kwargs."""
    calls: list[dict] = []

    def _fake(messages, **kw):
        calls.append({"messages": messages, **kw})
        if raises is not None:
            raise raises
        return Response(text="", usage=usage or Usage())

    monkeypatch.setattr(llm, "_complete_openai", _fake)
    return calls


def test_the_completion_is_capped_at_one_token(monkeypatch):
    """The reply is discarded. Measured: reasoning does not run past the
    cap — the arm above emitted exactly one output token, which is what
    makes the re-prime cost ~20 tokens rather than a full generation."""
    calls = _capture_openai(monkeypatch)
    llm.prime_cache([{"role": "system", "content": "s"}],
                    model="muse-spark-1.3", custom_llm_provider="meta")
    assert calls[0]["max_tokens"] == 1


def test_it_sends_the_messages_it_was_given(monkeypatch):
    calls = _capture_openai(monkeypatch)
    opening = [{"role": "system", "content": "s"},
               {"role": "user", "content": "p"}]
    llm.prime_cache(opening, model="muse-spark-1.3",
                    custom_llm_provider="meta")
    assert calls[0]["messages"] == opening


def test_the_request_carries_the_cache_key(monkeypatch):
    """The whole point: a re-prime that did not carry the key would be
    load-balanced to an arbitrary backend and make nothing current."""
    captured: dict = {}

    class _FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            raise RuntimeError("stop after capture")

    class _FakeClient:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": _FakeCompletions()})()

    import openai
    monkeypatch.setattr(openai, "OpenAI", _FakeClient)

    # prime_cache swallows the RuntimeError; the capture is the assertion.
    assert llm.prime_cache(
        [{"role": "user", "content": "hi"}],
        model="muse-spark-1.3-contributor", api_base="https://example/v1",
        api_key="k", custom_llm_provider="meta") is None

    assert captured["max_tokens"] == 1
    assert captured["extra_body"]["prompt_cache_key"].startswith("polytropos-")


def test_the_timeout_is_capped_regardless_of_the_attempt_timeout(monkeypatch):
    """A throwaway on the critical path must cost seconds if it hangs,
    not the attempt's ten minutes."""
    calls = _capture_openai(monkeypatch)
    llm.prime_cache([{"role": "user", "content": "hi"}],
                    model="muse-spark-1.3", custom_llm_provider="meta",
                    timeout=600)
    assert calls[0]["timeout"] == llm._PRIME_TIMEOUT


def test_a_shorter_timeout_is_honoured(monkeypatch):
    calls = _capture_openai(monkeypatch)
    llm.prime_cache([{"role": "user", "content": "hi"}],
                    model="muse-spark-1.3", custom_llm_provider="meta",
                    timeout=5)
    assert calls[0]["timeout"] == 5


def test_it_returns_the_usage_so_the_caller_can_charge_it(monkeypatch):
    _capture_openai(monkeypatch, usage=Usage(
        prompt_tokens=31172, completion_tokens=1,
        total_tokens=31173, cached_tokens=31153))
    usage = llm.prime_cache([{"role": "user", "content": "hi"}],
                            model="muse-spark-1.3",
                            custom_llm_provider="meta")
    assert usage.billable_tokens == 20


# --- best-effort, which is the non-negotiable part --------------------

def test_a_failure_is_swallowed(monkeypatch):
    """It is an optimisation; nothing may depend on it."""
    _capture_openai(monkeypatch, raises=RuntimeError("endpoint on fire"))
    assert llm.prime_cache([{"role": "user", "content": "hi"}],
                           model="muse-spark-1.3",
                           custom_llm_provider="meta") is None


def test_a_provider_without_a_dialect_makes_no_request(monkeypatch):
    calls = _capture_openai(monkeypatch)
    assert llm.prime_cache([{"role": "user", "content": "hi"}],
                           model="deepseek-v4-pro",
                           custom_llm_provider="deepseek") is None
    assert calls == []


def test_empty_messages_make_no_request(monkeypatch):
    calls = _capture_openai(monkeypatch)
    assert llm.prime_cache([], model="muse-spark-1.3",
                           custom_llm_provider="meta") is None
    assert calls == []


def test_there_is_no_retry_ladder(monkeypatch):
    """complete()'s staged retries exist to protect turns already spent.
    Spending minutes of them on a request whose whole value is being
    cheap would defeat the purpose."""
    slept: list[int] = []
    monkeypatch.setattr(llm, "_SLEEP", slept.append)
    calls = _capture_openai(
        monkeypatch, raises=RuntimeError("503 Service Unavailable"))
    llm.prime_cache([{"role": "user", "content": "hi"}],
                    model="muse-spark-1.3", custom_llm_provider="meta")
    assert len(calls) == 1
    assert slept == []


# --- the attempt loop -------------------------------------------------

class _Tier:
    max_iterations = 2
    max_tokens = 120_000


def _drive(monkeypatch, *, primed, tool_loop_usage=None, on_event=None):
    """Run two failing attempts, recording prime_cache and tool_loop."""
    primes: list[dict] = []

    def _fake_prime(messages, **kw):
        primes.append({"messages": messages, **kw})
        return primed

    runs: list[dict] = []

    def _fake_tool_loop(messages, **kw):
        runs.append({"messages": list(messages), **kw})
        return (Response(text="no proof here"),
                tool_loop_usage or Usage(prompt_tokens=1000,
                                         completion_tokens=100,
                                         total_tokens=1100),
                False)

    monkeypatch.setattr(attempt_loop.llm, "prime_cache", _fake_prime)
    monkeypatch.setattr(attempt_loop.tool_loop, "run", _fake_tool_loop)
    monkeypatch.setattr(attempt_loop.worker, "reset_attempt_caches",
                        lambda: None)
    monkeypatch.setattr(attempt_loop.worker, "reset_attempt_workspace",
                        lambda *a, **k: {})

    result = attempt_loop.run(
        "PAYLOAD", tier=_Tier(), env="e", model="muse-spark-1.3",
        custom_llm_provider="meta", system_prompt="SYSTEM",
        on_event=on_event,
    )
    return primes, runs, result


def test_attempt_one_is_never_re_primed(monkeypatch):
    """There is nothing to displace yet, and the opening is about to be
    sent for real anyway."""
    primes, runs, _ = _drive(monkeypatch, primed=None)
    assert len(runs) == 2
    assert len(primes) == 1  # before attempt 2 only


def test_the_re_prime_sends_attempt_ones_opening(monkeypatch):
    """Not attempt 2's messages: those carry the failure-context tail,
    which is new text the cache has never seen. The shared prefix is the
    opening, and an exact repeat of it is what hits."""
    primes, _, _ = _drive(monkeypatch, primed=None)
    assert primes[0]["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "PAYLOAD"},
    ]


def test_the_re_prime_carries_the_same_tool_schemas(monkeypatch):
    """A request that omitted them would not be what attempt 1 opened
    with."""
    from dportsv3.agent import tools
    primes, runs, _ = _drive(monkeypatch, primed=None)
    assert primes[0]["tools"] == tools.schemas(only=None)


def test_the_re_prime_inherits_the_provider_wiring(monkeypatch):
    primes, _, _ = _drive(monkeypatch, primed=None)
    assert primes[0]["model"] == "muse-spark-1.3"
    assert primes[0]["custom_llm_provider"] == "meta"


def test_its_cost_is_charged_to_the_budget(monkeypatch):
    """~20 billable when it works and a full cold start when it does
    not — which is exactly the leak the budget has to be able to see."""
    primed = Usage(prompt_tokens=31172, completion_tokens=1,
                   total_tokens=31173, cached_tokens=31153)
    _, runs, result = _drive(monkeypatch, primed=primed)
    # attempt 1 spent 1100 billable; the re-prime adds 20.
    assert result.usage.billable_tokens == 1100 + 20 + 1100
    # and the retry is handed a budget that already knows about it.
    assert runs[1]["max_tokens"] == 120_000 - 1100 - 20


def test_a_skipped_re_prime_changes_nothing(monkeypatch):
    _, runs, result = _drive(monkeypatch, primed=None)
    assert result.usage.billable_tokens == 2200
    assert runs[1]["max_tokens"] == 120_000 - 1100


def test_it_emits_an_event_carrying_the_gain(monkeypatch):
    """The gain per run, rather than inferred from the bead that
    measured it: if a provider change kills the trick, cached drops to
    zero here and the row says so."""
    events: list[dict] = []
    primed = Usage(prompt_tokens=31172, completion_tokens=1,
                   total_tokens=31173, cached_tokens=31153)
    _drive(monkeypatch, primed=primed, on_event=events.append)

    reprimes = [e for e in events if e.get("type") == "cache_reprime"]
    assert len(reprimes) == 1
    assert reprimes[0]["attempt"] == 2
    assert reprimes[0]["cached_tokens"] == 31153
    assert reprimes[0]["billable_tokens"] == 20


def test_no_event_when_no_re_prime_happened(monkeypatch):
    events: list[dict] = []
    _drive(monkeypatch, primed=None, on_event=events.append)
    assert not [e for e in events if e.get("type") == "cache_reprime"]


def test_a_raising_event_callback_does_not_break_the_loop(monkeypatch):
    def _boom(ev):
        raise RuntimeError("callback exploded")

    primed = Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)
    _, runs, result = _drive(monkeypatch, primed=primed, on_event=_boom)
    assert len(runs) == 2
    assert result.status == "needs-help"


# --- the operator-visible row ----------------------------------------

def test_the_activity_dispatcher_logs_the_re_prime():
    from dportsv3.agent.steps import PatchEventDispatcher

    rows: list[tuple] = []

    dispatcher = PatchEventDispatcher(
        queue_root=None, job_id="j", origin="devel/nspr",
        activity_log=lambda root, kind, msg, **kw: rows.append((kind, msg, kw)),
        looks_env_suspicious=lambda res: False,
        invalidate_health_cache=lambda: None,
        summarize_tool_call=lambda *a: "",
    )
    dispatcher({
        "type": "cache_reprime", "attempt": 2, "prompt_tokens": 31172,
        "cached_tokens": 31153, "completion_tokens": 1,
        "billable_tokens": 20,
    })

    assert len(rows) == 1
    kind, msg, kw = rows[0]
    assert kind == "cache_reprime"
    assert "cached=31153" in msg
    assert "billable=20" in msg
    assert kw["extra"]["attempt"] == 2


# --- the two ways this could have broken the attempt below it ---------

def test_a_settings_failure_is_still_a_no_op(monkeypatch):
    """`supports_prompt_cache` reads settings, so the contract only
    holds if that read is inside the guard too."""
    def _boom():
        raise RuntimeError("no config dir")

    monkeypatch.setattr(llm, "_backend", _boom)
    assert llm.prime_cache([{"role": "user", "content": "hi"}],
                           model="muse-spark-1.3",
                           custom_llm_provider="meta") is None


def test_a_missed_re_prime_that_eats_the_budget_stops_the_attempt(monkeypatch):
    """A re-prime that misses costs a whole cold start. tool_loop reads
    max_tokens=0 as "no cap", so spending the remainder down to exactly
    zero would uncap the retry instead of stopping it."""
    class _Tight:
        max_iterations = 2
        max_tokens = 2200  # attempt 1 spends 1100, leaving 1100

    primes: list[dict] = []
    runs: list[dict] = []

    def _fake_prime(messages, **kw):
        primes.append(kw)
        # a full miss: prompt billed in full, nothing cached
        return Usage(prompt_tokens=1100, completion_tokens=0,
                     total_tokens=1100)

    def _fake_tool_loop(messages, **kw):
        runs.append(kw)
        return (Response(text="no proof"),
                Usage(prompt_tokens=1000, completion_tokens=100,
                      total_tokens=1100),
                False)

    monkeypatch.setattr(attempt_loop.llm, "prime_cache", _fake_prime)
    monkeypatch.setattr(attempt_loop.tool_loop, "run", _fake_tool_loop)
    monkeypatch.setattr(attempt_loop.worker, "reset_attempt_caches",
                        lambda: None)
    monkeypatch.setattr(attempt_loop.worker, "reset_attempt_workspace",
                        lambda *a, **k: {})

    result = attempt_loop.run(
        "PAYLOAD", tier=_Tight(), env="e", model="muse-spark-1.3",
        custom_llm_provider="meta", system_prompt="SYSTEM",
    )

    assert len(primes) == 1
    assert len(runs) == 1  # attempt 2 never ran
    assert result.status == "budget-exhausted"
