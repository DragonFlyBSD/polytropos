"""poly-p0h: the Meta Model API (muse-spark) as a provider.

Everything here is pinned to behaviour measured against the live
endpoint on 2026-09-02, or to the published parameter table at
dev.meta.ai/docs/protocols/chat-completions — not to inference from an
error message.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import llm


# --- routing ---------------------------------------------------------

def test_bare_id_needs_no_slash_and_survives_untouched():
    """The endpoint's own ids carry no namespace."""
    assert llm._bare_model("muse-spark-1.3-contributor", "meta") == (
        "muse-spark-1.3-contributor"
    )


def test_meta_namespaced_ids_on_other_hosts_keep_their_prefix():
    """The regression adding "meta" to _OPENAI_COMPATIBLE invites: NIM
    serves "meta/llama-...", and if "meta" became a routing prefix those
    ids would lose half their name and 404 — exactly what _bare_model's
    docstring warns about."""
    assert llm._bare_model("meta/llama-3.3-70b", "nvidia") == (
        "meta/llama-3.3-70b"
    )
    assert llm._bare_model("meta/llama-3.3-70b", "") == "meta/llama-3.3-70b"
    assert "meta" not in llm._ROUTING_PREFIXES


def test_nvidia_namespacing_is_unchanged():
    assert llm._bare_model("nvidia/nemotron-3", "nvidia") == (
        "nvidia/nemotron-3"
    )


def test_named_provider_routes_through_the_sdk_without_an_api_base():
    assert llm._use_openai_sdk("muse-spark-1.3-contributor", "meta", None)
    assert llm._PROVIDER_API_BASE["meta"] == "https://api.meta.ai/v1"


def test_an_explicit_api_base_alone_already_routed_before_this_change():
    assert llm._use_openai_sdk(
        "muse-spark-1.3-contributor", None, "https://api.meta.ai/v1",
    )


# --- reasoning dialect -----------------------------------------------

@pytest.fixture(autouse=True)
def _reset_warn():
    llm._meta_off_warned = False
    yield
    llm._meta_off_warned = False


@pytest.mark.parametrize("ours,theirs", [
    ("low", "low"),
    ("high", "high"),
    ("max", "xhigh"),        # our top name, their top name
    ("medium", "medium"),    # theirs, written through unchanged
    ("minimal", "minimal"),
    ("xhigh", "xhigh"),
])
def test_effort_levels_map_to_the_published_ladder(ours, theirs):
    assert llm._reasoning_kwargs(ours, "meta") == {
        "reasoning_effort": theirs
    }


def test_off_becomes_minimal_because_the_model_refuses_none():
    """Measured: reasoning_effort="none" returns 400 '"reasoning_effort"
    does not support "none" with this model.' while every other level
    answers. The DeepSeek fallback would have sent extra_body thinking,
    which this endpoint rejects outright as an unknown parameter."""
    for spelling in ("none", "off", "disabled"):
        llm._meta_off_warned = False
        assert llm._reasoning_kwargs(spelling, "meta") == {
            "reasoning_effort": "minimal"
        }


def test_off_never_sends_the_deepseek_thinking_field():
    kwargs = llm._reasoning_kwargs("none", "meta")
    assert "extra_body" not in kwargs


def test_disabling_warns_once_that_it_costs_tokens_anyway(caplog):
    """Silently spending reasoning tokens the operator switched off is
    the surprise poly-r1g exists to prevent, so say it — but once, not
    per request."""
    import logging
    with caplog.at_level(logging.WARNING, logger="dportsv3.agent.llm"):
        llm._reasoning_kwargs("none", "meta")
        llm._reasoning_kwargs("none", "meta")
        llm._reasoning_kwargs("none", "meta")
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "minimal" in warnings[0]
    assert "cannot disable" in warnings[0]


def test_other_providers_keep_their_own_dialect():
    """The new entry must not leak into the fallback."""
    assert llm._reasoning_kwargs("none", "deepseek") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert llm._reasoning_kwargs("none", "") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert llm._reasoning_kwargs("high", "nvidia") == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}
    }


def test_unset_reasoning_sends_nothing_so_the_model_default_applies():
    """The published default is "model-set": omit the parameter and the
    model decides."""
    assert llm._reasoning_kwargs(None, "meta") == {}
    assert llm._reasoning_kwargs("", "meta") == {}


def test_naming_the_provider_is_what_activates_the_dialect(caplog):
    """The trap: translating the vendor's snippet literally gives an
    api_base and no provider name. That routes through the SDK fine, but
    _provider_of returns "" so the dialect table misses and the DeepSeek
    fallback sends `thinking`, which this endpoint rejects outright.

    llm.<role>.provider = "meta" is what makes it work — and then
    api_base is redundant, because _PROVIDER_API_BASE supplies it.
    """
    api_base_only = llm._provider_of("muse-spark-1.3-contributor", None)
    assert api_base_only == ""
    assert llm._reasoning_kwargs("none", api_base_only) == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }, "api_base alone still sends the field the endpoint 400s on"

    named = llm._provider_of("muse-spark-1.3-contributor", "meta")
    assert named == "meta"
    assert llm._reasoning_kwargs("none", named) == {
        "reasoning_effort": "minimal"
    }
