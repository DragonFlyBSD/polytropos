"""Thinking mode is reachable on both backends (poly-r1g).

The SDK path came first, because py311-litellm is 1.65.0. That version
rejects ``thinking`` and ``reasoning_effort`` for deepseek outright, and
sends ``extra_body`` as a literal top-level JSON key instead of
unpacking it — captured off the wire against a local sink::

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
is a direct saving. litellm 1.93.2 is the first release that maps
``reasoning_effort="none"`` to thinking disabled, and py312-litellm is
1.102.1, so the litellm path sends it too where litellm has a mapping.
The SDK path stays for the dialects litellm's mapping loses (see the
llm module docstring).
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
        def __init__(self, api_key=None, base_url=None, max_retries=None):
            sink["_api_key"] = api_key
            sink["_base_url"] = base_url
            sink["_max_retries"] = max_retries
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


# --- the litellm path -------------------------------------------------------

def _litellm_call(monkeypatch, *, version, supported=("reasoning_effort",),
                  model="deepseek/deepseek-v4-pro", reasoning="none"):
    """Run complete() down the litellm path and return what it sent.

    The fake resolves providers the way litellm does: the prefix names
    the provider and the rest is the model.
    """
    sent: dict = {}
    asked: dict = {}

    class _FakeLitellm:
        @staticmethod
        def get_llm_provider(model, custom_llm_provider=None):
            provider, _, bare = model.partition("/")
            return bare, custom_llm_provider or provider, None, None

        @staticmethod
        def get_supported_openai_params(model, custom_llm_provider=None):
            asked.update(model=model, provider=custom_llm_provider)
            return list(supported)

        @staticmethod
        def completion(**kwargs):
            sent.update(kwargs)
            raise RuntimeError("stop here — we only care about the kwargs")

    import sys
    monkeypatch.setitem(sys.modules, "litellm", _FakeLitellm)
    monkeypatch.setenv("DP_HARNESS_LLM_BACKEND", "litellm")
    monkeypatch.setattr(llm, "_litellm_version", lambda: version)
    monkeypatch.setattr(llm, "_litellm_reasoning_warned", set())

    with pytest.raises(RuntimeError):
        llm.complete([{"role": "user", "content": "x"}],
                     model=model, api_key="k", reasoning=reasoning)
    sent["_asked"] = asked
    return sent


@pytest.mark.parametrize("value,expected", [
    ("none", "none"), ("off", "none"), ("disabled", "none"),
    ("low", "low"), ("high", "high"),
])
def test_a_new_litellm_gets_reasoning_effort(monkeypatch, value, expected):
    """1.93.2 maps reasoning_effort onto each provider's own control, so
    the one OpenAI-style parameter is all this path sends. The off
    spellings become "none", the value litellm maps to off."""
    sent = _litellm_call(monkeypatch, version=(1, 102, 1), reasoning=value)
    assert sent["reasoning_effort"] == expected
    assert "extra_body" not in sent
    assert sent["model"] == "deepseek/deepseek-v4-pro", (
        "the litellm path keeps the prefixed model name it routes on"
    )


def test_support_is_asked_for_the_resolved_provider(monkeypatch):
    """Asked about "deepseek/..." with no provider, litellm answers for
    openai (checked against 1.102.1). So the model is resolved first."""
    sent = _litellm_call(monkeypatch, version=(1, 102, 1))
    assert sent["_asked"] == {"model": "deepseek-v4-pro",
                              "provider": "deepseek"}


@pytest.mark.parametrize("version", [(1, 65, 0), (1, 92, 9), ()])
def test_an_old_litellm_sends_nothing_and_says_so(monkeypatch, caplog,
                                                  version):
    """1.65.0 rejects the params outright, so passing them through would
    turn a request that works into an UnsupportedParamsError, and up to
    1.92.x "none" is silently dropped. An unknown version counts as old.
    Saying so matters: the operator turned thinking off and is paying for
    it anyway."""
    with caplog.at_level("WARNING", logger="dportsv3.agent.llm"):
        sent = _litellm_call(monkeypatch, version=version)
    assert "reasoning_effort" not in sent
    assert "extra_body" not in sent
    assert "1.93.2" in caplog.text


def test_a_provider_without_a_mapping_sends_nothing(monkeypatch, caplog):
    """litellm raises UnsupportedParamsError for a parameter the provider
    has no mapping for, which would fail every request (nvidia_nim, in
    1.102.1)."""
    with caplog.at_level("WARNING", logger="dportsv3.agent.llm"):
        sent = _litellm_call(monkeypatch, version=(1, 102, 1),
                             supported=("temperature",),
                             model="nvidia_nim/nvidia/nemotron-x")
    assert "reasoning_effort" not in sent
    assert "no reasoning control" in caplog.text


def test_the_warning_is_once_per_model(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="dportsv3.agent.llm"):
        _litellm_call(monkeypatch, version=(1, 65, 0))
        warned = set(llm._litellm_reasoning_warned)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                llm.complete([{"role": "user", "content": "x"}],
                             model="deepseek/deepseek-v4-pro", api_key="k",
                             reasoning="none")
    assert warned == {"deepseek/deepseek-v4-pro"}
    assert caplog.text.count("cannot send it") == 1


def test_unset_reasoning_sends_nothing_and_warns_nothing(monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="dportsv3.agent.llm"):
        sent = _litellm_call(monkeypatch, version=(1, 65, 0), reasoning=None)
    assert "reasoning_effort" not in sent
    assert caplog.text == ""


def test_the_installed_version_parses():
    """Whatever is installed here, it reads as a tuple of ints (or ()
    when absent), never raising."""
    found = llm._litellm_version()
    assert isinstance(found, tuple)
    assert all(isinstance(part, int) for part in found)


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


# --- the chat role (poly-91w) ------------------------------------------------

def test_the_chat_role_defaults_to_thinking_off(monkeypatch):
    """The panel renders the reply and nothing else — the chat route
    returns `reply`/`reply_html`/`usage` and never reasoning_content —
    so anything the model thinks is billed at full output rate and
    thrown away. Measured on nemotron: 14-21 completion tokens against
    2 with thinking off, on a one-word prompt."""
    from dportsv3.agent import steps

    monkeypatch.delenv("DP_HARNESS_CHAT_REASONING", raising=False)
    assert steps._reasoning_for("chat") == "none"


def test_the_chat_config_carries_the_reasoning_setting(monkeypatch):
    """Declaring the setting is not enough — it has to reach
    llm.complete, which is what the omission cost us."""
    from dportsv3 import settings
    from dportsv3.tracker.routes import _common

    monkeypatch.setattr(settings, "get_opt", lambda p: {
        "llm.chat.model": "nvidia/nemotron-3-super-120b-a12b",
        "llm.chat.api_base": "",
        "llm.chat.provider": "",
        "llm.chat.reasoning": "none",
    }.get(p, ""))
    monkeypatch.setattr(settings, "read_secret", lambda p: "k")
    cfg = _common._chat_llm_config()
    assert cfg is not None
    assert cfg["reasoning"] == "none"


def test_omitting_the_key_leaves_nvidia_thinking():
    """Why this mattered: no reasoning argument means no dialect runs,
    so nothing reaches extra_body and NVIDIA thinks by default. It does
    NOT hang — that was the original filing's error."""
    assert llm._reasoning_kwargs(None, "nvidia") == {}
    assert llm._reasoning_kwargs("none", "nvidia") == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}
    }


def test_the_sdk_ladder_is_disabled_so_the_two_do_not_multiply(monkeypatch):
    """complete() owns the retry ladder. Leaving the SDK's default of 2
    in place would make every staged retry three requests against an
    endpoint that is already overloaded — opencode issue #30510."""
    sink: dict = {}
    _fake_openai(monkeypatch, sink)
    llm.complete([{"role": "user", "content": "x"}],
                 model="deepseek/deepseek-v4-pro", api_key="k")
    assert sink["_max_retries"] == 0
