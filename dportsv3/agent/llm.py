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

import logging
import os
import time
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)


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
     "nvidia", "meta"}
)

#: Default base URLs for providers we address by name rather than by an
#: explicit api_base. litellm derives these from the model prefix; the
#: SDK needs to be told.
_PROVIDER_API_BASE = {
    "deepseek": "https://api.deepseek.com",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "meta": "https://api.meta.ai/v1",
}

#: Providers whose model ids carry a namespace of their own.
#: "nvidia/nemotron-3-ultra-550b-a55b" is the id the endpoint wants, not
#: a routing prefix wrapped around "nemotron-..." — the leading segment
#: has to survive into the request. NIM also hosts models under other
#: namespaces (meta/, qwen/, deepseek-ai/); naming the provider
#: explicitly — llm.<role>.provider = "nvidia" — routes those here too,
#: with no api_base to configure.
#:
#: "meta" is here for the opposite reason to "nvidia": Meta's own API
#: takes a bare id ("muse-spark-1.3-contributor", no slash), so it never
#: needs stripping — but "meta/" IS a real namespace on somebody else's
#: host (NIM serves "meta/llama-..."), and letting it become a routing
#: prefix would strip half of those ids and 404 them. Membership here
#: keeps it out of _ROUTING_PREFIXES below, which is what protects them.
_NAMESPACED_MODEL_IDS = frozenset({"nvidia", "meta"})

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


#: Our effort names to Meta's. Their ladder is
#: minimal|low|medium|high|xhigh, ours is none|low|high|max, so only the
#: top end needs a word; anything unlisted passes straight through, which
#: lets an operator write "medium" or "xhigh" directly.
_META_EFFORT = {"max": "xhigh"}

_meta_off_warned = False


def _reasoning_meta(value: str, off: bool) -> dict:
    """Meta Model API takes the plain OpenAI parameter — but muse-spark
    will not switch thinking off.

    ``reasoning_effort="none"`` returns HTTP 400 ``"reasoning_effort"
    does not support "none" with this model.`` That is documented
    (dev.meta.ai/docs/protocols/chat-completions marks "none" as not
    supported by Muse Spark) and was measured against the live endpoint,
    where every other level answered.

    So "off" cannot be honoured, and the choice is to fail the request or
    to buy the cheapest thinking there is. This buys "minimal" — 14
    reasoning tokens on a one-line prompt, against 424 at "xhigh" — and
    warns once, because silently spending reasoning tokens an operator
    switched off is the cost surprise poly-r1g exists to prevent.
    """
    global _meta_off_warned
    if off:
        if not _meta_off_warned:
            _meta_off_warned = True
            _LOG.warning(
                "reasoning is set to %r but this model cannot disable "
                "thinking (reasoning_effort=\"none\" is rejected); "
                "sending \"minimal\" instead, so requests still cost "
                "some reasoning tokens", value,
            )
        return {"reasoning_effort": "minimal"}
    return {"reasoning_effort": _META_EFFORT.get(value, value)}


def _cache_meta(model: str) -> dict:
    """Meta groups requests by ``prompt_cache_key`` so they route to a
    backend already holding their prefix, and takes a retention hint.

    The key is DERIVED, never stored. It is a routing hint rather than a
    handle — there is no object to expire and nothing a saved key could
    refer to — so the model id it is scoped by is enough. Everything
    talking to one model routes together, which at one-job-at-a-time
    volume is exactly what we want: a bundle's attempts share a prefix,
    and so do different ports up to their first divergence.

    Retention is documented as a hint, not a guarantee ("the server may
    evict entries early under load"), and measurement bears that out.
    Probed at 1, 2, 5, 10, 20 and 30 minutes: 98.9% hit through 20
    minutes, then gone at 30 — with an unkeyed control that missed every
    single time. Not a TTL, though: the 10→20m gap was ten minutes and
    hit, the 20→30m gap was ten minutes and missed. It is eviction under
    pressure, so treat a hit as opportunistic and never build anything
    that needs one.

    What it does cover is the case this exists for: attempts within a
    bundle are 1-5 minutes apart, well inside where the cache held. Reuse
    across a longer gap — a later batch, the next session — should be
    assumed absent.

    Sent through ``extra_body`` because the SDK on this platform (1.70.0)
    predates both parameters and rejects them as keyword arguments. That
    is what makes the merge in ``_merge_call_kwargs`` load-bearing rather
    than tidy.
    """
    return {"extra_body": {
        "prompt_cache_key": f"polytropos-{model}",
        "prompt_cache_retention": "24h",
    }}


#: How each provider spells CROSS-REQUEST prompt caching. Absent means
#: "nothing to send": DeepSeek caches automatically with no knob at all
#: (prefix-only from token 0, 64-token chunks), so an unknown field
#: there would be the same 400 _reasoning_meta exists to avoid.
_CACHE_DIALECTS = {"meta": _cache_meta}


def _cache_kwargs(provider: str, model: str) -> dict:
    """Provider knobs for reaching the prompt cache across requests.

    Caching is documented as automatic and prefix-based, which reads as
    "nothing to do". Measured, it is not: the cache is KV state on one
    backend, and without ``prompt_cache_key`` a request is balanced onto
    an arbitrary machine whose cache has never seen our prefix. Same
    prefix, diverging tails, five requests —

        cold, WITH key                 cached=0
        diverging, WITH key            cached=7537
        diverging, NO key              cached=0     <- prefix existed
        diverging, WITH key again      cached=7537  <- proof it existed
        diverging, DIFFERENT key       cached=0

    So the key has to be on every request that wants the cache, not only
    the one that populates it (poly-2w6).

    Worth more than the price break: the tier budget counts
    ``billable = raw - cached``, so a cached prefix is nearly free
    against the BUDGET. Cold-start size predicted budget exhaustion
    almost exactly across eleven ports — the three largest all exhausted,
    six of the seven smallest were all fixed.
    """
    dialect = _CACHE_DIALECTS.get(provider)
    return dialect(model) if dialect else {}


def _merge_call_kwargs(kwargs: dict, extra: dict) -> None:
    """``kwargs.update(extra)``, except ``extra_body`` is merged.

    Both dialect tables can return an ``extra_body``: the DeepSeek
    reasoning form sends ``thinking`` there, nvidia sends
    ``chat_template_kwargs``, and Meta's cache controls go there too
    because the SDK is too old to take them as kwargs. A plain update()
    would let whichever ran second silently drop the other's payload —
    and losing ``thinking: disabled`` would quietly restore the ~50% of
    billable spend reasoning control exists to cut (poly-r1g).
    """
    for key, value in extra.items():
        if (key == "extra_body"
                and isinstance(kwargs.get(key), dict)
                and isinstance(value, dict)):
            kwargs[key] = {**kwargs[key], **value}
        else:
            kwargs[key] = value


#: How each provider spells thinking mode. Anything absent falls back
#: to the DeepSeek form above.
_REASONING_DIALECTS = {"nvidia": _reasoning_nvidia, "meta": _reasoning_meta}


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

# Waits before retry 1, 2, 3, … in seconds. Fast while a flap is the
# likely explanation, then stepping up; the last entry repeats and is
# therefore the ceiling. Also the fallback when settings cannot be read.
_DEFAULT_RETRY_SCHEDULE = "3,3,3,5,5,15"


def is_transient(exc: BaseException, *, provider: str = "",
                 model: str = "") -> bool:
    """True when ``exc`` is worth trying the same request again.

    Conservative by construction: an error nobody recognises is
    terminal, because a retry costs a whole request and a wrong
    "transient" answer turns one failure into several.

    ``provider`` and ``model`` are resolved the same way the request
    itself resolves them, so a caller can hand over whatever it has —
    the provider override, the model name, or both.
    """
    provider = _provider_of(model, provider or None)
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


# --- staged retry, per request -----------------------------------------------
#
# There are two ladders and they are deliberately different.
#
# This one is per REQUEST. It keeps the message list, the worktree and
# every turn already paid for, so a retry here costs one call. That
# makes it cheap enough to be short and frequent: measured on one run,
# 126 calls succeeded while 41 failed in the same window, neighbours
# seconds apart, and the first success after a 429 came 25s later.
#
# The runner's per-JOB ladder (runner.llm_retry_*) throws all of that
# away and restarts the attempt cold, so it is long and rare. A failure
# that reaches it has outlived this one and means the provider is
# actually down, not flapping.
#
# Staged rather than exponential on purpose. Exponential assumes
# contention grows with time; staged encodes the hypothesis the data
# supports — the first few failures are a flap worth retrying fast, and
# something still failing by attempt 4 is an outage worth backing off
# from. The last entry repeats, so it is also the ceiling.
#
# The counter is local to the call, so a success resets it: the next
# request starts at the top of the schedule and never inherits a delay
# earned by an earlier one.

# Indirection so tests do not actually sleep.
_SLEEP = time.sleep


def _setting(name: str, default):
    """One setting, with ``default`` when settings cannot be read.

    Falling back rather than raising is deliberate: a client that cannot
    reach a config file should still retry a flap, not die on the first
    one.
    """
    try:
        from dportsv3 import settings  # noqa: PLC0415

        return settings.get(name)
    except Exception:  # noqa: BLE001
        return default


def _retry_settings() -> tuple[int, list[int]]:
    """(max retries, wait schedule) as configured."""
    max_retries = int(_setting("llm.retry_max", 6))
    raw = str(_setting("llm.retry_backoff_schedule", _DEFAULT_RETRY_SCHEDULE))
    return max(0, max_retries), _parse_schedule(raw)


def _seconds_list(raw: str) -> list[int]:
    """Comma-separated seconds → waits. Garbage entries are dropped
    rather than raising: a typo in the config must not take the agent
    down mid-run."""
    waits = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            seconds = int(float(part))
        except ValueError:
            continue
        # A negative is garbage, not "retry immediately". Dropping it
        # keeps a typo from turning the schedule into a hot loop.
        if seconds >= 0:
            waits.append(seconds)
    return waits


def _parse_schedule(raw: str) -> list[int]:
    """The configured schedule, or the default when it parses to
    nothing. An unreadable schedule is a broken config, not a request to
    turn retry off — ``llm.retry_max = 0`` is how you do that."""
    return _seconds_list(raw) or _seconds_list(_DEFAULT_RETRY_SCHEDULE)


def _retry_waits(max_retries: int, schedule: list[int]) -> list[int]:
    """The wait before each retry. Shorter schedule than ``max_retries``
    means the last entry repeats — that is what makes it the ceiling."""
    if not schedule or max_retries <= 0:
        return []
    return [schedule[min(i, len(schedule) - 1)] for i in range(max_retries)]


def _retry_total_seconds() -> float:
    """Wall-clock bound on one call's whole ladder, request time
    included. Generous against the schedule (which sleeps ~34s by
    default) and tight against a request timeout, so a flap is retried
    and a hang is not."""
    return max(0.0, float(_setting("llm.retry_total_seconds", 180.0)))


def _sdk_max_retries() -> int:
    """The openai SDK's own retry count. 0 by default: complete() owns
    the ladder, and two ladders in series multiply."""
    return max(0, int(_setting("llm.sdk_max_retries", 0)))


def _retry_after_seconds(exc: Exception) -> float | None:
    """The provider's own hint, in seconds, or None.

    Never trusted unclamped — opencode #13591 turned a Retry-After into
    a two-week sleep that no operator could clear. The caller bounds it
    by the schedule's ceiling.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    for key, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        try:
            raw = headers.get(key)
        except Exception:  # noqa: BLE001 — a header bag we do not understand
            return None
        if raw is None:
            continue
        try:
            return float(raw) / divisor
        except (TypeError, ValueError):
            # Retry-After may also be an HTTP-date. We do not parse it;
            # the schedule is a fine answer and cannot be poisoned.
            continue
    return None


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

    A transient failure is retried in place on the staged schedule (see
    ``_retry_waits``) so that one unlucky request does not discard the
    turns already spent. Only ``is_transient`` errors qualify; anything
    terminal is raised on the first try, as before.
    """
    max_retries, schedule = _retry_settings()
    waits = _retry_waits(max_retries, schedule)
    ceiling = max(waits) if waits else 0
    # Wall-clock bound over the whole ladder. The per-retry ceiling does
    # not cover a timeout: at a 300s request timeout, six retries would
    # sit inside one call for 35 minutes — which is how opencode #25041
    # hangs. A slow failure is not a flap, so it gets no ladder.
    deadline = time.monotonic() + _retry_total_seconds()

    attempt = 0
    while True:
        try:
            if _use_openai_sdk(model, custom_llm_provider, api_base):
                return _complete_openai(
                    messages, model=model, tools=tools, api_base=api_base,
                    api_key=api_key, custom_llm_provider=custom_llm_provider,
                    timeout=timeout, temperature=temperature,
                    reasoning=reasoning,
                )
            return _complete_litellm(
                messages, model=model, tools=tools, api_base=api_base,
                api_key=api_key, custom_llm_provider=custom_llm_provider,
                timeout=timeout, temperature=temperature, reasoning=reasoning,
            )
        except Exception as exc:
            if attempt >= len(waits) or not is_transient(
                    exc, provider=custom_llm_provider or "", model=model):
                raise
            delay = waits[attempt]
            hinted = _retry_after_seconds(exc)
            if hinted is not None:
                # Honour the provider, but never above the ceiling.
                delay = min(max(delay, hinted), ceiling)
            if time.monotonic() + delay > deadline:
                raise
            # WARNING, not DEBUG: a silent 30s pause inside one call is
            # exactly the thing an operator needs to see, and this line
            # is the only record that the flap was survived rather than
            # never happening.
            _LOG.warning(
                "llm: retry %d/%d in %ss after transient failure: %s",
                attempt + 1, len(waits), delay, str(exc)[:200],
            )
            _SLEEP(delay)
            attempt += 1


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
    _merge_call_kwargs(kwargs, _reasoning_kwargs(reasoning, provider))
    # SDK path only. litellm 1.65.0 forwards extra_body as a literal
    # top-level JSON key rather than unpacking it (see the module
    # docstring), so sending cache controls down that path would put a
    # junk field on the wire instead of reaching the cache.
    _merge_call_kwargs(kwargs, _cache_kwargs(provider, kwargs["model"]))

    # max_retries is explicit because the SDK's default is 2 and the
    # ladders would otherwise multiply: 6 staged retries over a silent
    # 3-requests-per-call becomes 21 requests at an endpoint that is
    # already overloaded. opencode never overrode it and shipped exactly
    # that (issue #30510). complete() owns the ladder; the SDK owns none.
    client = OpenAI(api_key=api_key, base_url=base,
                    max_retries=_sdk_max_retries())
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
