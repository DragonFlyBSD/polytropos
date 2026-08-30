"""Thinking mode is reachable through the openai SDK (poly-r1g).

TEMPORARY, along with the code it covers. DragonFly pins py311-litellm
at 1.65.0 and newer versions cannot build here (fastuuid → maturin →
rustc >= 1.89, poly-170). 1.65.0 rejects ``thinking`` and
``reasoning_effort`` for deepseek outright, and sends ``extra_body`` as
a literal top-level JSON key instead of unpacking it — captured off the
wire against a local sink::

    {"model": "deepseek-v4-pro", "messages": [...],
     "extra_body": {"thinking": {"type": "disabled"}}}

DeepSeek sees a field called ``extra_body`` and no ``thinking`` field,
which is why the setting appeared to do nothing. The openai SDK (1.70.0,
already installed) unpacks it correctly::

    wire keys: ['messages', 'model', 'thinking']

Measured on the live API, same prompt, deepseek-v4-pro:

    thinking on (default)  completion=4,601  reasoning=18,056 chars
    thinking disabled      completion=  350  reasoning=     0 chars

Output is billed at full rate and is never cached at generation, so this
is a direct saving. Once py311-litellm reaches >= 1.93.2 — the first
release mapping ``reasoning_effort="none"`` to thinking disabled — the
SDK path and this file should both go away.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import llm


# --- which backend serves a request -----------------------------------------

@pytest.mark.parametrize("model,provider,expect_sdk", [
    ("deepseek/deepseek-v4-pro", None, True),
    ("openai/gpt-x", None, True),
    ("groq/llama-x", None, True),
    ("anthropic/claude-x", None, False),
    ("vertex_ai/gemini-x", None, False),
    ("some-model", "deepseek", True),
    ("claude-x", "anthropic", False),
    ("nvidia/nemotron-3-ultra-550b-a55b", None, True),
])
def test_provider_decides_the_backend(monkeypatch, model, provider, expect_sdk):
    """Only OpenAI-wire providers go to the SDK. Anything whose request
    or response shape litellm has to translate must keep going through
    litellm, or we would be silently dropping that translation."""
    monkeypatch.delenv("DP_HARNESS_LLM_BACKEND", raising=False)
    assert llm._use_openai_sdk(model, provider, None) is expect_sdk


def test_an_explicit_api_base_is_treated_as_openai_compatible(monkeypatch):
    """`custom_llm_provider="openai"` + api_base has always meant an
    OpenAI-compatible relay here."""
    monkeypatch.delenv("DP_HARNESS_LLM_BACKEND", raising=False)
    assert llm._use_openai_sdk("some-model", "openai", "http://relay:8000")


def test_the_backend_can_be_forced_back_to_litellm(monkeypatch):
    """The escape hatch. If the SDK path misbehaves in the field, one
    env var restores the previous behaviour without a deploy."""
    monkeypatch.setenv("DP_HARNESS_LLM_BACKEND", "litellm")
    assert llm._use_openai_sdk("deepseek/deepseek-v4-pro", None, None) is False


def test_the_backend_can_be_forced_to_the_sdk(monkeypatch):
    monkeypatch.setenv("DP_HARNESS_LLM_BACKEND", "openai")
    assert llm._use_openai_sdk("anthropic/claude-x", None, None) is True


# --- the parameter shape ----------------------------------------------------

def test_none_disables_thinking_via_extra_body():
    """`thinking` is documented as an extra_body field; the SDK unpacks
    extra_body into the request body, which is the whole point."""
    assert llm._reasoning_kwargs("none") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }


@pytest.mark.parametrize("alias", ["none", "off", "disabled", "NONE", " none "])
def test_the_off_spellings_all_disable(alias):
    """Operators write this in a shell env file; do not make them guess
    which word we accept."""
    assert llm._reasoning_kwargs(alias)["extra_body"]["thinking"]["type"] == "disabled"


@pytest.mark.parametrize("level", ["low", "high", "max"])
def test_an_effort_level_is_a_normal_parameter(level):
    """reasoning_effort is an ordinary OpenAI-style param — it must NOT
    go in extra_body, or it lands nested and is ignored."""
    assert llm._reasoning_kwargs(level) == {"reasoning_effort": level}


@pytest.mark.parametrize("value", [None, ""])
def test_unset_sends_nothing(value):
    """Unset must mean 'provider default', not 'off'. Anything else
    would change behaviour for callers that never opted in."""
    assert llm._reasoning_kwargs(value) == {}


# --- what the SDK path actually sends ---------------------------------------

class _FakeCompletions:
    def __init__(self, sink):
        self.sink = sink

    def create(self, **kwargs):
        self.sink.update(kwargs)

        class _Msg:
            content = "hi"
            tool_calls = None
            reasoning_content = None

        class _Choice:
            message = _Msg()

        class _Details:
            cached_tokens = 7

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 5
            total_tokens = 105
            prompt_tokens_details = _Details()

        class _Completion:
            choices = [_Choice()]
            usage = _Usage()

        return _Completion()


def _fake_openai(monkeypatch, sink):
    class _Client:
        def __init__(self, api_key=None, base_url=None):
            sink["_api_key"] = api_key
            sink["_base_url"] = base_url
            self.chat = type("C", (), {"completions": _FakeCompletions(sink)})()

    import openai
    monkeypatch.setattr(openai, "OpenAI", _Client)


def test_the_sdk_path_sends_the_bare_model_id(monkeypatch):
    """litellm takes "deepseek/deepseek-v4-pro"; the SDK wants the bare
    id, because base_url already fixes the provider. Sending the
    prefixed name is a 400 from the provider."""
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    llm.complete([{"role": "user", "content": "x"}],
                 model="deepseek/deepseek-v4-pro", api_key="k")
    assert sink["model"] == "deepseek-v4-pro"
    assert sink["_base_url"] == "https://api.deepseek.com"


def test_reasoning_reaches_the_request(monkeypatch):
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    llm.complete([{"role": "user", "content": "x"}],
                 model="deepseek/deepseek-v4-pro", api_key="k",
                 reasoning="none")
    assert sink["extra_body"] == {"thinking": {"type": "disabled"}}


def test_cached_tokens_survive_the_sdk_path(monkeypatch):
    """The budget gate is billable = uncached prompt + completion, so
    losing cached_tokens here would silently inflate every budget
    reading. DeepSeek emits prompt_tokens_details.cached_tokens natively
    (verified against the live API), so no per-provider mapping is
    needed — but it does have to be read."""
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    r = llm.complete([{"role": "user", "content": "x"}],
                     model="deepseek/deepseek-v4-pro", api_key="k")
    assert r.usage.cached_tokens == 7
    assert r.usage.billable_tokens == (100 - 7) + 5


# --- the litellm path must not regress --------------------------------------

def test_litellm_path_never_sees_reasoning(monkeypatch):
    """1.65.0 rejects the params outright, so passing them through would
    turn a request that works today into an UnsupportedParamsError.
    Dropping is deliberate."""
    sent: dict = {}

    class _FakeLitellm:
        @staticmethod
        def completion(**kwargs):
            sent.update(kwargs)
            raise RuntimeError("stop here — we only care about the kwargs")

    import sys
    monkeypatch.setitem(sys.modules, "litellm", _FakeLitellm)
    monkeypatch.setenv("DP_HARNESS_LLM_BACKEND", "litellm")

    with pytest.raises(RuntimeError):
        llm.complete([{"role": "user", "content": "x"}],
                     model="deepseek/deepseek-v4-pro", api_key="k",
                     reasoning="none")

    assert "reasoning" not in sent
    assert "reasoning_effort" not in sent
    assert "extra_body" not in sent
    assert sent["model"] == "deepseek/deepseek-v4-pro", (
        "the litellm path keeps the prefixed model name it routes on"
    )


# --- the temporary framing stays visible ------------------------------------

def test_the_code_says_it_is_temporary():
    """Whoever upgrades py311-litellm needs to find this without
    archaeology — the removal condition is a version number, not a
    judgement call."""
    import inspect

    src = inspect.getsource(llm)
    assert "1.93.2" in src
    assert "TEMPORARY" in src
    assert "poly-170" in src


# --- per-role defaults ------------------------------------------------------

def test_the_roles_default_differently(monkeypatch):
    """Triage classifies against a fixed schema and does not need to
    think; the patch loop reasons about code, so it gets low rather than
    off — a smaller step to walk back if quality drops."""
    from dportsv3.agent import steps

    monkeypatch.delenv("DP_HARNESS_TRIAGE_REASONING", raising=False)
    monkeypatch.delenv("DP_HARNESS_PATCH_REASONING", raising=False)
    assert steps._reasoning_for("triage") == "none"
    assert steps._reasoning_for("patch") == "low"


def test_a_role_can_be_overridden(monkeypatch):
    from dportsv3.agent import steps

    monkeypatch.setenv("DP_HARNESS_PATCH_REASONING", "max")
    assert steps._reasoning_for("patch") == "max"


def test_an_empty_override_means_provider_default(monkeypatch):
    """Setting the var to nothing is how an operator asks for the
    provider default back, distinct from unsetting it (which gives our
    default)."""
    from dportsv3.agent import steps

    monkeypatch.setenv("DP_HARNESS_TRIAGE_REASONING", "  ")
    assert steps._reasoning_for("triage") is None


# --- namespaced providers (poly-ajd) ----------------------------------------

@pytest.mark.parametrize("model,provider,expected", [
    # A routing prefix: base_url already fixes the provider, so it goes.
    ("deepseek/deepseek-v4-pro", "deepseek", "deepseek-v4-pro"),
    # One segment only — an OpenRouter id is itself "vendor/model".
    ("openrouter/deepseek/deepseek-chat", "openrouter", "deepseek/deepseek-chat"),
    # Not a prefix, part of the name. Stripping these is a 404.
    ("nvidia/nemotron-3-ultra-550b-a55b", "nvidia",
     "nvidia/nemotron-3-ultra-550b-a55b"),
    ("meta/llama-4-maverick", "nvidia", "meta/llama-4-maverick"),
    ("qwen/qwen3-coder", "nvidia", "qwen/qwen3-coder"),
    ("deepseek-ai/deepseek-r1", "nvidia", "deepseek-ai/deepseek-r1"),
    ("some-model", "deepseek", "some-model"),
    # An unlisted prefix that names the resolved provider is still a
    # routing prefix — the llm.backend="openai" escape hatch relies on
    # it, and forcing the backend must not also rewrite the model id.
    ("anthropic/claude-x", "anthropic", "claude-x"),
])
def test_only_a_routing_prefix_is_stripped(model, provider, expected):
    """The old rule was "a slash means a litellm prefix". NVIDIA NIM
    disproves it: the namespace IS the id there, and half a model name
    gets a 404 back."""
    assert llm._bare_model(model, provider) == expected


def test_a_namespaced_model_reaches_nim_intact(monkeypatch):
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    monkeypatch.delenv("DP_HARNESS_LLM_BACKEND", raising=False)
    llm.complete([{"role": "user", "content": "x"}],
                 model="nvidia/nemotron-3-ultra-550b-a55b", api_key="k")
    assert sink["model"] == "nvidia/nemotron-3-ultra-550b-a55b"
    assert sink["_base_url"] == "https://integrate.api.nvidia.com/v1"


def test_naming_the_provider_covers_the_models_nim_only_hosts(monkeypatch):
    """NIM serves plenty of models whose namespace is not "nvidia".
    Setting llm.<role>.provider is how those get the right base URL and
    the right thinking dialect, with no api_base to configure."""
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    monkeypatch.delenv("DP_HARNESS_LLM_BACKEND", raising=False)
    llm.complete([{"role": "user", "content": "x"}],
                 model="meta/llama-4-maverick", api_key="k",
                 custom_llm_provider="nvidia", reasoning="none")
    assert sink["model"] == "meta/llama-4-maverick"
    assert sink["_base_url"] == "https://integrate.api.nvidia.com/v1"
    assert sink["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


# --- one dialect per provider (poly-ajd) ------------------------------------

def test_nvidia_disables_thinking_through_the_chat_template():
    """Sending DeepSeek's spelling here fails silently: NIM ignores the
    unknown field and thinks anyway, which is the exact cost poly-r1g
    exists to avoid."""
    assert llm._reasoning_kwargs("none", "nvidia") == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
    }


@pytest.mark.parametrize("level", ["low", "high", "max"])
def test_nvidia_has_no_effort_levels_so_any_level_means_on(level):
    """nemotron's switch is a boolean. Passing reasoning_effort instead
    would be an unknown parameter, not a graceful degrade."""
    assert llm._reasoning_kwargs(level, "nvidia") == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}
    }


def test_an_unknown_provider_keeps_the_deepseek_dialect():
    """The dialect table is opt-in: adding a provider must not change
    what every existing one sends."""
    assert llm._reasoning_kwargs("none", "groq") == \
        llm._reasoning_kwargs("none")


def test_the_two_prefix_tables_cannot_drift():
    """_ROUTING_PREFIXES is derived rather than written out, so a
    provider added to one table cannot be forgotten in the other."""
    assert llm._ROUTING_PREFIXES == (
        llm._OPENAI_COMPATIBLE - llm._NAMESPACED_MODEL_IDS
    )
    assert not (llm._NAMESPACED_MODEL_IDS & llm._ROUTING_PREFIXES)
