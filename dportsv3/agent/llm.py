"""LLM client with a normalized response shape.

Two backends behind one ``complete()``: the ``openai`` SDK for providers
that speak the OpenAI wire format, and litellm for everything else.

THE SPLIT IS TEMPORARY. It exists only because this platform cannot run
a litellm new enough to do the job:

  * DragonFly ships py311-litellm 1.65.0 (2025-03-28) and that is the
    newest package. Upgrading by pip fails — newer litellm pulls
    fastuuid, which needs maturin, which needs rustc >= 1.89 against a
    base with 1.85.1 (poly-170).
  * 1.65.0 rejects `thinking` and `reasoning_effort` outright for
    deepseek, and sends `extra_body` as a literal top-level JSON key
    rather than unpacking it — verified by capturing the wire. So no
    provider-specific parameter can reach the API, and thinking mode
    cannot be turned off. That is ~50% of billable spend.
  * litellm gained the ability to DISABLE deepseek thinking only in
    1.93.2 (2026-08-09). 1.80.10 through 1.92.x added the parameters
    but enable-only.

REMOVE THIS SPLIT once py311-litellm is >= 1.93.2. At that point
`reasoning_effort` is an ordinary parameter litellm maps for every
provider, `_complete_openai` and the `_OPENAI_COMPATIBLE` /
`_PROVIDER_API_BASE` tables stop earning their keep, and `complete()`
goes back to one backend. Tracked on poly-r1g.

The tokenizers stub (needed on DragonFly) is in dportsv3.agent.__init__,
which runs before any module here is loaded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Cache-read (re-billed prefix) tokens, read from
    # usage.prompt_tokens_details.cached_tokens. DeepSeek emits that field
    # natively alongside its own prompt_cache_hit_tokens (verified against
    # the live API), so both backends see the same shape and neither needs
    # a per-provider mapping. Defaults 0 → if a provider/run reports no
    # cache hit, billable_tokens == total and budgeting is unchanged.
    cached_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens

    @property
    def billable_tokens(self) -> int:
        """New (non-cached) work: uncached prompt + completion. The
        per-attempt budget gates on this so re-sending a cached prefix
        every turn doesn't exhaust the budget for no real work."""
        return max(0, self.prompt_tokens - self.cached_tokens) + self.completion_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # Thinking-mode chain-of-thought (DeepSeek's `reasoning_content`,
    # OpenAI o-series reasoning summaries via the same field name on
    # OpenAI-compat backends). None for non-thinking models.
    reasoning_content: str | None = None
    raw: object = None  # opaque litellm ModelResponse for debugging


#: Which client issues the request. "auto" (default) uses the openai SDK
#: for providers that speak the OpenAI wire format and litellm for the
#: rest; "litellm" forces the old path for every provider, which is the
#: escape hatch if the SDK path ever misbehaves.
#:
#: TEMPORARY, with the module docstring: once litellm >= 1.93.2 is
#: installable this knob and the SDK path both go away.
def _backend() -> str:
    from dportsv3 import settings  # noqa: PLC0415 — avoids an import cycle
    return settings.get_str("llm.backend").lower()


#: Providers that speak the OpenAI wire format end to end, so the SDK can
#: talk to them directly. Anything else goes through litellm, which is
#: what translates non-OpenAI request/response shapes.
_OPENAI_COMPATIBLE = frozenset(
    {"deepseek", "openai", "openrouter", "together_ai", "groq", "xai"}
)

#: Default base URLs for providers we address by name rather than by an
#: explicit api_base. litellm derives these from the model prefix; the
#: SDK needs to be told.
_PROVIDER_API_BASE = {"deepseek": "https://api.deepseek.com"}


def _provider_of(model: str, custom_llm_provider: str | None) -> str:
    if custom_llm_provider:
        return custom_llm_provider.strip().lower()
    return model.split("/", 1)[0].strip().lower() if "/" in model else ""


def _use_openai_sdk(model: str, custom_llm_provider: str | None,
                    api_base: str | None) -> bool:
    """True when the openai SDK can serve this request directly."""
    backend = _backend()
    if backend == "litellm":
        return False
    provider = _provider_of(model, custom_llm_provider)
    if backend == "openai":
        return True
    # An explicit api_base without a provider name is an OpenAI-compat
    # endpoint by convention (that is what custom_llm_provider="openai"
    # + api_base has always meant here).
    if api_base and provider in ("", "openai"):
        return True
    return provider in _OPENAI_COMPATIBLE


def _reasoning_kwargs(reasoning: str | None) -> dict:
    """Provider knobs for thinking mode. TEMPORARY — see module docstring.

    Hand-rolled because litellm 1.65.0 cannot express this. litellm
    >= 1.93.2 maps ``reasoning_effort="none"`` to
    ``thinking={"type": "disabled"}`` itself, so this function is
    exactly what that version makes redundant.

    ``"none"`` disables it; ``"low"``/``"high"``/``"max"`` set the effort.
    The ``thinking`` object is documented as an extra_body field
    (api-docs.deepseek.com/guides/thinking_mode) — passing it as a plain
    kwarg is a TypeError — while ``reasoning_effort`` is a normal
    OpenAI-style parameter.
    """
    if not reasoning:
        return {}
    value = reasoning.strip().lower()
    if value in ("none", "off", "disabled"):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"reasoning_effort": value}


def complete(
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    custom_llm_provider: str | None = None,
    timeout: int | None = None,
    temperature: float | None = None,
    reasoning: str | None = None,
) -> Response:
    """Call an LLM provider and return a normalized Response.

    ``messages`` is the OpenAI-style chat list. ``tools`` is the
    OpenAI-style JSON schema list.

    ``custom_llm_provider`` forces a specific provider's code path
    regardless of what the model name looks like. Important when talking
    to OpenAI-compatible third-party endpoints (opencode.ai/zen, Groq,
    Together, …) whose model IDs may contain a native-provider substring
    (``deepseek-*``, ``claude-*``) that a model→provider heuristic would
    otherwise mis-route.

    ``reasoning`` controls thinking mode where the provider supports it:
    ``"none"`` turns it off, ``"low"``/``"high"``/``"max"`` set the
    effort. Measured on deepseek-v4-pro with one prompt, thinking on
    cost 4,601 completion tokens against 350 with it off. Output is
    billed at full rate and is never cached at generation, so this is a
    direct saving rather than a caching artifact. Only the openai-SDK
    backend can deliver it — see the module docstring.
    """
    if _use_openai_sdk(model, custom_llm_provider, api_base):
        return _complete_openai(
            messages, model=model, tools=tools, api_base=api_base,
            api_key=api_key, custom_llm_provider=custom_llm_provider,
            timeout=timeout, temperature=temperature, reasoning=reasoning,
        )
    return _complete_litellm(
        messages, model=model, tools=tools, api_base=api_base,
        api_key=api_key, custom_llm_provider=custom_llm_provider,
        timeout=timeout, temperature=temperature, reasoning=reasoning,
    )


def _complete_openai(
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None,
    api_base: str | None,
    api_key: str | None,
    custom_llm_provider: str | None,
    timeout: int | None,
    temperature: float | None,
    reasoning: str | None,
) -> Response:
    """Talk to an OpenAI-compatible endpoint with the openai SDK."""
    from openai import OpenAI

    provider = _provider_of(model, custom_llm_provider)
    base = api_base or _PROVIDER_API_BASE.get(provider)

    kwargs: dict = {
        # litellm takes "deepseek/deepseek-v4-pro"; the SDK wants the bare
        # model id, because the provider is already fixed by base_url.
        "model": model.split("/", 1)[-1] if "/" in model else model,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    if timeout is not None:
        kwargs["timeout"] = timeout
    if temperature is not None:
        kwargs["temperature"] = temperature
    kwargs.update(_reasoning_kwargs(reasoning))

    client = OpenAI(api_key=api_key, base_url=base)
    return _response_from(client.chat.completions.create(**kwargs))


def _complete_litellm(
    messages: list[dict],
    *,
    model: str,
    tools: list[dict] | None,
    api_base: str | None,
    api_key: str | None,
    custom_llm_provider: str | None,
    timeout: int | None,
    temperature: float | None,
    reasoning: str | None,
) -> Response:
    """Talk to any provider through litellm.

    ``reasoning`` is accepted and ignored here. The pinned 1.65.0 cannot
    deliver it for any provider (it rejects the params outright and sends
    extra_body as a literal key), so silently dropping it is honest —
    raising would break providers this backend otherwise serves fine.
    """
    import litellm

    kwargs: dict = {"model": model, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    if custom_llm_provider:
        kwargs["custom_llm_provider"] = custom_llm_provider
    if timeout is not None:
        kwargs["timeout"] = timeout
    if temperature is not None:
        kwargs["temperature"] = temperature

    return _response_from(litellm.completion(**kwargs))


def _response_from(completion) -> Response:
    """Build our Response from an OpenAI-shaped completion object.

    Shared by both backends: litellm's response object and the openai
    SDK's expose the same attributes we read, so parsing lives in one
    place and the two paths cannot drift.
    """
    choice = completion.choices[0]
    msg = choice.message

    text = msg.content or ""

    tool_calls: list[ToolCall] = []
    raw_calls = getattr(msg, "tool_calls", None) or []
    for call in raw_calls:
        fn = call.function
        arguments = fn.arguments
        if isinstance(arguments, str):
            import json
            try:
                arguments = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                arguments = {"_raw": arguments}
        tool_calls.append(
            ToolCall(id=call.id, name=fn.name, arguments=arguments or {})
        )

    raw_usage = getattr(completion, "usage", None)
    usage = Usage()
    if raw_usage is not None:
        usage.prompt_tokens = getattr(raw_usage, "prompt_tokens", 0) or 0
        usage.completion_tokens = getattr(raw_usage, "completion_tokens", 0) or 0
        usage.total_tokens = getattr(raw_usage, "total_tokens", 0) or 0
        # litellm normalizes cache-read tokens here across providers
        # (DeepSeek prompt_cache_hit_tokens, Anthropic cache_read_input_tokens,
        # OpenAI cached_tokens). May be absent/None → 0.
        details = getattr(raw_usage, "prompt_tokens_details", None)
        usage.cached_tokens = (getattr(details, "cached_tokens", 0) or 0) if details else 0

    # Some thinking-mode providers (DeepSeek's v4-* models, certain
    # OpenAI-compat relays) expose intermediate chain-of-thought as
    # `reasoning_content` on the message object. The upstream API
    # requires it to be echoed back on multi-turn requests.
    reasoning_content = getattr(msg, "reasoning_content", None) or None

    return Response(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        reasoning_content=reasoning_content,
        raw=completion,
    )
