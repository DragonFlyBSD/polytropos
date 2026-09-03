"""poly-2w6: reaching the prompt cache across requests.

Caching is documented as automatic and prefix-based, which reads as
"nothing to do". Measured against the live endpoint it is not: the cache
is KV state on one backend, and a request without ``prompt_cache_key``
is balanced onto an arbitrary machine whose cache has never seen our
prefix. Same prefix, diverging tails:

    cold, WITH key                 cached=0
    diverging, WITH key            cached=7537
    diverging, NO key              cached=0      <- the prefix existed
    diverging, WITH key again      cached=7537   <- proof it existed
    diverging, DIFFERENT key       cached=0
"""

from __future__ import annotations

import pytest

from dportsv3.agent import llm


# --- which providers get cache controls -------------------------------

def test_meta_sends_a_key_and_a_retention_hint():
    kw = llm._cache_kwargs("meta", "muse-spark-1.3-contributor")
    body = kw["extra_body"]
    assert body["prompt_cache_key"] == "polytropos-muse-spark-1.3-contributor"
    assert body["prompt_cache_retention"] == "24h"


def test_the_key_is_stable_across_calls():
    """A key that varied per call would route every request to a
    different backend — worse than sending none."""
    a = llm._cache_kwargs("meta", "muse-spark-1.3")
    b = llm._cache_kwargs("meta", "muse-spark-1.3")
    assert a == b


def test_the_key_is_scoped_by_model():
    """Different models do not share a prefix, so they should not share
    a routing group."""
    a = llm._cache_kwargs("meta", "muse-spark-1.3")["extra_body"]
    b = llm._cache_kwargs("meta", "muse-spark-1.2")["extra_body"]
    assert a["prompt_cache_key"] != b["prompt_cache_key"]


@pytest.mark.parametrize("provider", ["deepseek", "nvidia", "openai", ""])
def test_providers_without_the_concept_send_nothing(provider):
    """DeepSeek caches automatically with no knob; an unknown field is
    the same 400 the reasoning dialect exists to avoid."""
    assert llm._cache_kwargs(provider, "some-model") == {}


# --- the merge, which is the load-bearing part ------------------------

def test_cache_controls_do_not_clobber_deepseek_thinking():
    """Both dialects return an extra_body. A plain update() would drop
    `thinking: disabled` and silently restore the ~50% of billable spend
    reasoning control exists to cut (poly-r1g)."""
    kwargs: dict = {"model": "m"}
    llm._merge_call_kwargs(kwargs, llm._reasoning_kwargs("none", "deepseek"))
    llm._merge_call_kwargs(kwargs, {"extra_body": {"prompt_cache_key": "k"}})

    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert kwargs["extra_body"]["prompt_cache_key"] == "k"


def test_cache_controls_do_not_clobber_nvidia_thinking():
    kwargs: dict = {"model": "m"}
    llm._merge_call_kwargs(kwargs, llm._reasoning_kwargs("high", "nvidia"))
    llm._merge_call_kwargs(kwargs, {"extra_body": {"prompt_cache_key": "k"}})

    assert kwargs["extra_body"]["chat_template_kwargs"] == {
        "enable_thinking": True
    }
    assert kwargs["extra_body"]["prompt_cache_key"] == "k"


def test_the_merge_is_order_independent():
    forward: dict = {}
    llm._merge_call_kwargs(forward, {"extra_body": {"a": 1}})
    llm._merge_call_kwargs(forward, {"extra_body": {"b": 2}})
    backward: dict = {}
    llm._merge_call_kwargs(backward, {"extra_body": {"b": 2}})
    llm._merge_call_kwargs(backward, {"extra_body": {"a": 1}})
    assert forward == backward == {"extra_body": {"a": 1, "b": 2}}


def test_non_extra_body_keys_still_replace():
    """Only extra_body merges; everything else keeps update() semantics
    so a dialect can still override a scalar."""
    kwargs = {"reasoning_effort": "low"}
    llm._merge_call_kwargs(kwargs, {"reasoning_effort": "high"})
    assert kwargs["reasoning_effort"] == "high"


# --- wiring -----------------------------------------------------------

def test_the_sdk_path_sends_both_dialects(monkeypatch):
    """The whole point: a real call must carry the reasoning payload and
    the cache controls together."""
    captured: dict = {}

    class _Resp:
        choices = []
        usage = None

    class _FakeCompletions:
        def create(self, **kw):
            captured.update(kw)
            raise RuntimeError("stop after capture")

    class _FakeClient:
        def __init__(self, **kw):
            self.chat = type("C", (), {"completions": _FakeCompletions()})()

    import openai
    monkeypatch.setattr(openai, "OpenAI", _FakeClient)

    with pytest.raises(RuntimeError, match="stop after capture"):
        llm._complete_openai(
            [{"role": "user", "content": "hi"}],
            model="muse-spark-1.3-contributor", tools=None,
            api_base="https://api.meta.ai/v1", api_key="k",
            custom_llm_provider="meta", timeout=30, temperature=None,
            reasoning="high",
        )

    assert captured["reasoning_effort"] == "high"
    body = captured["extra_body"]
    assert body["prompt_cache_key"].startswith("polytropos-")
    assert body["prompt_cache_retention"] == "24h"


def test_litellm_path_gets_no_cache_controls():
    """litellm 1.65.0 forwards extra_body as a literal top-level JSON
    key instead of unpacking it, so cache controls sent that way would
    be a junk field on the wire rather than a cache hit."""
    import inspect
    src = inspect.getsource(llm._complete_litellm)
    assert "_cache_kwargs" not in src
