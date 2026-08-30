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
`_PROVIDER_API_BASE` / `_REASONING_DIALECTS` tables stop earning their
keep, and `complete()` goes back to one backend. Tracked on poly-r1g.

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
    {"deepseek", "openai", "openrouter", "together_ai", "groq", "xai",
     "nvidia"}
)

#: Default base URLs for providers we address by name rather than by an
#: explicit api_base. litellm derives these from the model prefix; the
#: SDK needs to be told.
_PROVIDER_API_BASE = {
    "deepseek": "https://api.deepseek.com",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}

#: Providers whose model ids carry a namespace of their own.
#: "nvidia/nemotron-3-ultra-550b-a55b" is the id the endpoint wants, not
#: a routing prefix wrapped around "nemotron-..." — the leading segment
#: has to survive into the request. NIM also hosts models under other
#: namespaces (meta/, qwen/, deepseek-ai/); naming the provider
#: explicitly — llm.<role>.provider = "nvidia" — routes those here too,
#: with no api_base to configure.
_NAMESPACED_MODEL_IDS = frozenset({"nvidia"})

#: Leading segments that really are a routing prefix, and so can be
#: dropped once base_url has fixed the provider. Derived, so it cannot
#: drift from the two tables above.
_ROUTING_PREFIXES = _OPENAI_COMPATIBLE - _NAMESPACED_MODEL_IDS


def _provider_of(model: str, custom_llm_provider: str | None) -> str:
    if custom_llm_provider:
        return custom_llm_provider.strip().lower()
    return model.split("/", 1)[0].strip().lower() if "/" in model else ""


def _bare_model(model: str, provider: str = "") -> str:
    """The model id as the endpoint itself wants it.

    litellm addresses a model as "<provider>/<id>" and the SDK does not
    need that prefix, because base_url already fixes the provider. Drop
    it only when the leading segment really is one of those prefixes:
    providers whose own ids are namespaced ("nvidia/nemotron-...",
    "meta/llama-...", "qwen/...") would otherwise lose half their name
    and get a 404 back.

    A segment naming the provider we resolved to counts as a routing
    prefix even when it is not in the table — that is what the
    ``llm.backend = "openai"`` escape hatch used to do for every model,
    and forcing the backend should not also change the model id.
    """
    head, sep, tail = model.partition("/")
    if not sep:
        return model
    head = head.strip().lower()
    if head in _NAMESPACED_MODEL_IDS:
        return model
    return tail if head in _ROUTING_PREFIXES or head == provider else model


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


#: Spellings that mean "do not think".
_REASONING_OFF = frozenset({"none", "off", "disabled"})


def _reasoning_deepseek(value: str, off: bool) -> dict:
    """DeepSeek's spelling, and the fallback for every provider without
    an entry below — which is what they all got before there was a
    table, so no existing provider changes shape.

    The ``thinking`` object is documented as an extra_body field
    (api-docs.deepseek.com/guides/thinking_mode) — passing it as a plain
    kwarg is a TypeError — while ``reasoning_effort`` is a normal
    OpenAI-style parameter.
    """
    if off:
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {"reasoning_effort": value}


def _reasoning_nvidia(value: str, off: bool) -> dict:
    """NVIDIA NIM hands the switch to the chat template, and it is a
    boolean: nemotron thinks or it does not, there are no effort levels.
    So any level means "on" and only the off spellings turn it off."""
    return {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": not off}}
    }


#: How each provider spells thinking mode. Anything absent falls back
#: to the DeepSeek form above.
_REASONING_DIALECTS = {"nvidia": _reasoning_nvidia}


def _reasoning_kwargs(reasoning: str | None, provider: str = "") -> dict:
    """Provider knobs for thinking mode. TEMPORARY — see module docstring.

    Hand-rolled because litellm 1.65.0 cannot express this. litellm
    >= 1.93.2 maps ``reasoning_effort="none"`` to
    ``thinking={"type": "disabled"}`` itself, so this function is
    exactly what that version makes redundant.

    ``"none"`` disables it; ``"low"``/``"high"``/``"max"`` set the
    effort. Providers do not agree on how to say that, so the wire form
    is keyed on ``provider`` — sending the wrong dialect is silent, the
    endpoint just ignores a field it does not know and thinks anyway.
    """
    if not reasoning:
        return {}
    value = reasoning.strip().lower()
    dialect = _REASONING_DIALECTS.get(provider, _reasoning_deepseek)
    return dialect(value, value in _REASONING_OFF)


# --- which failures are worth trying again -----------------------------------

#: Transient by HTTP status, for every provider. Same set pi uses
#: (429/500/502/503/504/524) plus the two the openai SDK already retries
#: on its own, so our classification does not disagree with the client's.
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 524})

#: Statuses that are transient for ONE provider and nothing else.
#:
#: NVIDIA answers an unroutable model with a bare "404 page not found"
#: — Go's http.NotFound, i.e. no route matched — and returns the exact
#: same body for a misspelled id, an id whose vendor prefix a client
#: stripped (openclaw#71552), and a valid id that is momentarily
#: unroutable. Measured here: individual requests 404 while their
#: neighbours seconds away succeed, the model stays listed in
#: /v1/models throughout, and it recovered on its own ~40 minutes
#: later. Nobody else treats 404 as retryable and neither should we in
#: general — this entry is safe only because ``validate_model`` has
#: already proved the name exists, which is what separates "typo" from
#: "unroutable". Do not add a provider here without that guarantee.
_PROVIDER_TRANSIENT_STATUS = {"nvidia": frozenset({404})}

#: Transient regardless of status code. NIM reports an overloaded
#: worker pool this way and the status does not always come with it
#: (pi#6364 had to match the substring for the same reason).
_RETRYABLE_SUBSTRINGS = (
    "resourceexhausted",
    "all workers are busy",
    "service temporarily overloaded",
    "please retry later",
    "you can retry your request",
)

#: Never retryable however transient the wrapper looks — retrying only
#: burns the budget again. Matches pi's non-retryable provider-limit set.
_TERMINAL_SUBSTRINGS = ("insufficient_quota", "invalid_api_key", "billing")


def is_transient(exc: BaseException, *, provider: str = "") -> bool:
    """True when ``exc`` is worth trying the same request again.

    Conservative by construction: an error nobody recognises is
    terminal, because a retry costs a whole request and a wrong
    "transient" answer turns one failure into several.
    """
    text = str(exc).lower()
    if any(s in text for s in _TERMINAL_SUBSTRINGS):
        return False

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status in _RETRYABLE_STATUS:
            return True
        if status in _PROVIDER_TRANSIENT_STATUS.get(provider, frozenset()):
            return True
        # A 4xx we do not recognise is the caller's fault, not the
        # provider's. Saying so here stops the substring pass below
        # from rescuing it on an incidental word match.
        if 400 <= status < 500:
            return False

    if any(s in text for s in _RETRYABLE_SUBSTRINGS):
        return True

    # Timeouts and dropped connections carry no status. Match on the
    # exception type name so this module needs no openai import.
    name = type(exc).__name__
    return name in ("APITimeoutError", "APIConnectionError", "Timeout",
                    "ConnectionError", "ReadTimeout", "ConnectTimeout")


# --- is the configured model actually there? ---------------------------------


def list_models(*, api_base: str | None, api_key: str | None,
                provider: str = "") -> set[str] | None:
    """Model ids the endpoint advertises, or None if it cannot be asked.

    None means "no answer", never "no models" — the caller must not
    read it as the model being absent.
    """
    base = api_base or _PROVIDER_API_BASE.get(provider)
    try:
        from openai import OpenAI  # noqa: PLC0415

        client = OpenAI(api_key=api_key, base_url=base)
        return {m.id for m in client.models.list().data}
    except Exception:  # noqa: BLE001 — an unreachable endpoint is not a verdict
        return None


def validate_model(model: str, *, api_base: str | None, api_key: str | None,
                   provider: str = "") -> str | None:
    """None when the model is usable, else a line explaining what is wrong.

    This is what makes treating a 404 as transient safe: a name that
    does not exist is caught once, at startup, instead of being retried
    forever against an endpoint that will never serve it. It earns its
    keep on every provider though — a typo'd model is the commonest
    configuration mistake there is, and the failure it produces
    otherwise arrives one job at a time.
    """
    advertised = list_models(api_base=api_base, api_key=api_key,
                             provider=provider)
    if advertised is None:
        return None  # could not ask; not a verdict, so not an error
    wanted = _bare_model(model, _provider_of(model, provider or None))
    if wanted in advertised or model in advertised:
        return None
    import difflib  # noqa: PLC0415

    near = difflib.get_close_matches(wanted, sorted(advertised), n=3, cutoff=0.6)
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    return (f"model {model!r} is not offered by this endpoint "
            f"({len(advertised)} available).{hint}")


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
        # Namespaced ids keep their prefix — see _bare_model.
        "model": _bare_model(model, provider),
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    if timeout is not None:
        kwargs["timeout"] = timeout
    if temperature is not None:
        kwargs["temperature"] = temperature
    kwargs.update(_reasoning_kwargs(reasoning, provider))

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
