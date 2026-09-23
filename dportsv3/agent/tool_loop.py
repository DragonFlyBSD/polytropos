"""Multi-turn LLM-with-tools driver.

One call to ``run(...)`` is a single conversation with the LLM:
- Send messages + tool schemas
- If the LLM emitted ``tool_calls``, dispatch each, append the results
  as ``tool`` messages, and re-call
- Stop when the LLM returns text-only (no tool calls) or when
  ``max_turns`` is hit

The caller (``patch.run`` in step 4) handles attempt-level retries
with fresh failure context; this driver is one inner attempt.
"""

from __future__ import annotations

import json
import logging
import time

from . import llm, tools, worker
from .llm import Response, Usage

log = logging.getLogger(__name__)


# Grace tokens granted after a tool call returned ``rebuild_ok=True``.
# Lets the LLM emit its closing ``## Rebuild Proof (JSON)`` + ``## Patch
# Log`` blocks without busting the attempt budget the moment the build
# went green. Sized to cover the closing turns, not a fresh build cycle.
GRACE_TOKENS_AFTER_REBUILD_OK = 100_000

# Never masked. dops_reference says "call ONCE only", so hiding it would
# make that instruction impossible to follow; a note is the point.
_UNMASKED_TOOLS = frozenset({"dops_reference", "note"})

# Results smaller than this are never masked. On py-onnx they were 46 of
# 160 results but 4% of the bytes, and they are the short diagnostics
# (a validate_dops error, a refused write) that are cheapest to keep.
_MASK_MIN_BYTES = 512

# Reads whose repetition after masking is worth counting. A repeated
# dsynth_build or dsynth_log is how every build round looks anyway.
_REPEAT_TRACKED = frozenset({"get_file", "grep", "list_dir"})

# How to get a masked result back. Only reads may be repeated: repeating
# dsynth_build rebuilds and repeating an edit applies it twice.
_RESTORE_HINT = {
    **dict.fromkeys(("get_file", "grep", "list_dir", "dsynth_log", "emit_diff",
                     "get_effective_overlay", "validate_dops", "env_verify"),
                    "Repeat the call if you need it again."),
    "dsynth_build": "dsynth_log reads the build log again; do not rebuild for it.",
    "edit_file": "get_file shows the file as it is now.",
    "put_file": "get_file shows the file as it is now.",
}


def _placeholder(message: dict) -> str:
    """What a masked tool result becomes. Deterministic, so the prefix
    stays byte-identical from one request to the next."""
    content = message.get("content") or ""
    try:
        original = json.loads(content)
    except (TypeError, ValueError):
        original = None
    kept = {k: original[k] for k in ("ok", "rebuild_ok")
            if isinstance(original, dict) and k in original}
    name = message.get("name")
    hint = _RESTORE_HINT.get(name)
    return json.dumps({
        **kept,
        "omitted": (f"{len(content.encode())} bytes of {name} output omitted "
                    f"to keep the conversation small."
                    + (f" {hint}" if hint else "")),
    })


class EnvironmentBlocked(Exception):
    """A tool reported a condition no further agent work can clear.

    Raised out of the dispatch loop rather than returned, because every
    caller unpacks a fixed tuple and none of them should be able to
    ignore this by accident. The tool result is appended to the message
    history first, so a session dump still shows what the model was told
    before the loop stopped.
    """

    def __init__(self, reason: str, tool: str, usage=None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tool = tool
        # Spent before the condition fired. Without this the caller
        # loses the attempt's accounting and the audit under-reports.
        self.usage = usage


class _ContextWindow:
    """What the model is sent each turn (poly-hnk3).

    Tool results older than the last ``keep_turns`` turns are replaced by
    a short placeholder, unless they are already small; every assistant
    message and tool call stays. That is observation masking, which
    JetBrains measured on SWE-bench as effective as LLM summarization,
    and cheaper (arXiv 2508.21433); it is what Anthropic's
    clear_tool_uses does server-side.

    ``messages`` is never changed: it is the attempt's record, and
    session_dump persists it. Masking is batched: nothing is masked until
    ``batch`` bytes of old output are waiting, because each mask changes
    the prompt from the first masked message on and re-bills everything
    after it.
    """

    def __init__(self, messages: list[dict], keep_turns: int, batch: int) -> None:
        self.messages = messages
        self.keep_turns = keep_turns
        self.batch = batch
        self.head_len = len(messages)
        self._masked: dict[int, str] = {}
        self._masked_reads: set[tuple[str, str]] = set()

    def _maskable(self) -> list[int]:
        if self.keep_turns <= 0:
            return []
        turns = [i for i in range(self.head_len, len(self.messages))
                 if self.messages[i].get("role") == "assistant"]
        if len(turns) <= self.keep_turns:
            return []
        return [i for i in range(self.head_len, turns[-self.keep_turns])
                if self.messages[i].get("role") == "tool"
                and i not in self._masked
                and self.messages[i].get("name") not in _UNMASKED_TOOLS
                and len((self.messages[i].get("content") or "").encode())
                >= _MASK_MIN_BYTES]

    def _view(self) -> list[dict]:
        if not self._masked:
            return self.messages
        return [dict(m, content=self._masked[i]) if i in self._masked else m
                for i, m in enumerate(self.messages)]

    def _read_signature(self, i: int) -> tuple[str, str] | None:
        """The (tool, arguments) of the read tool result ``i`` answered."""
        name = self.messages[i].get("name")
        if name not in _REPEAT_TRACKED:
            return None
        for j in range(i - 1, self.head_len - 1, -1):
            for tc in self.messages[j].get("tool_calls") or []:
                if tc.get("id") == self.messages[i].get("tool_call_id"):
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except (TypeError, ValueError):
                        return None
                    return name, json.dumps(args, sort_keys=True)
        return None

    def request(self) -> tuple[list[dict], dict | None]:
        """The messages to send this turn, and what was just masked, if any."""
        pending = self._maskable()
        waiting = sum(len((self.messages[i].get("content") or "").encode())
                      for i in pending)
        masked = None
        if pending and waiting >= self.batch:
            before = len(json.dumps(self._view()).encode())
            for i in pending:
                self._masked[i] = _placeholder(self.messages[i])
                sig = self._read_signature(i)
                if sig is not None:
                    self._masked_reads.add(sig)
            masked = {
                "masked_results": len(pending),
                "bytes_before": before,
                "bytes_after": len(json.dumps(self._view()).encode()),
            }
        return self._view(), masked

    def repeats_masked_read(self, name: str, arguments: dict | None) -> bool:
        """Whether this call re-asks for a read whose output was masked.

        Forgets it once asked: the new result is in full view again.
        """
        sig = (name, json.dumps(arguments or {}, sort_keys=True))
        if sig in self._masked_reads:
            self._masked_reads.discard(sig)
            return True
        return False


def _assistant_message_from(response: Response) -> dict:
    """Reconstruct the assistant message dict that produced ``response``.

    Needed so the next LLM call sees the tool calls the model made on
    the previous turn (otherwise it has amnesia about its own request).
    Thinking-mode providers (DeepSeek v4-*, some OpenAI-compat relays)
    additionally require ``reasoning_content`` to be echoed back, or
    the next request fails with HTTP 400.
    """
    msg: dict = {"role": "assistant", "content": response.text or ""}
    if response.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments or {}),
                },
            }
            for tc in response.tool_calls
        ]
    if response.reasoning_content:
        msg["reasoning_content"] = response.reasoning_content
    return msg


def run(
    messages: list[dict],
    *,
    model: str,
    env: str,
    api_base: str | None = None,
    api_key: str | None = None,
    custom_llm_provider: str | None = None,
    timeout: int = 120,
    max_turns: int = 12,
    max_tokens: int = 0,
    on_event=None,
    attempt_idx: int = 1,
    tool_whitelist: set[str] | frozenset[str] | None = None,
    reasoning: str | None = None,
    context_keep_turns: int = 0,
    context_mask_batch: int = 0,
) -> tuple[Response, Usage, bool]:
    """Drive the LLM through tool calls until it returns text-only.

    ``messages`` is mutated to include each assistant + tool turn for
    the duration of the loop. ``env`` is the dev-env name; every tool
    call is bound to it.

    Returns ``(response, usage, rebuild_ok_seen)``: the last
    ``Response``, cumulative ``Usage`` across all turns, and a flag set
    when any tool result in this attempt carried ``rebuild_ok=True``
    (the structured success signal from ``dsynth_build``).

    ``response`` is the last one the model produced, which is the final
    report only when the loop stopped because the model went text-only.
    On a turn or budget stop it is whatever commentary accompanied the
    last tool call — a fragment, not a report. Callers must not present
    it as one; a ``loop_stop`` event carries the reason so they can
    tell (poly-qkp).

    Safety caps:
    - ``max_turns``: stop after this many LLM round-trips even if the
      model keeps calling tools. Default 12.
    - ``context_keep_turns``: tool results older than this many turns are
      masked in the request, in batches of ``context_mask_batch`` bytes
      (see ``_ContextWindow``); ``messages`` keeps them. 0 disables.
    - ``max_tokens``: stop when cumulative usage reaches this many
      tokens. 0 (the default) disables the check — the caller is
      expected to pass the remaining attempt-level budget when one
      exists. Once ``rebuild_ok_seen`` flips to True the cap is
      extended by ``GRACE_TOKENS_AFTER_REBUILD_OK`` so the closing
      turns (proof block + patch log) have room to land.
    """
    total = Usage()
    tool_schemas = tools.schemas(only=tool_whitelist)
    final: Response | None = None
    rebuild_ok_seen = False
    window = _ContextWindow(messages, context_keep_turns, context_mask_batch)

    def _stop(reason: str, turn: int) -> None:
        """Emit why the loop ended. Callers use this to tell a real
        report from the last tool call's commentary."""
        if on_event is None:
            return
        try:
            on_event({
                "type": "loop_stop",
                "attempt": attempt_idx,
                "turn": turn,
                "reason": reason,
            })
        except Exception:
            pass  # callback must never break the loop

    def _effective_cap() -> int:
        # Grace only kicks in once a tool result has signalled
        # ``rebuild_ok=True``; before that, the budget is the budget.
        return max_tokens + (GRACE_TOKENS_AFTER_REBUILD_OK if rebuild_ok_seen else 0)

    for turn in range(1, max_turns + 1):
        if max_tokens and total.billable_tokens >= _effective_cap():
            log.warning(
                "tool_loop: token budget exhausted on turn %d (%d >= %d billable)",
                turn, total.billable_tokens, _effective_cap(),
            )
            _stop("token_budget", turn)
            return (final if final is not None else Response(text="")), total, rebuild_ok_seen

        request, masked = window.request()
        if masked is not None:
            # get_file answers a repeated read "unchanged, scroll back" while
            # the bytes are still in the conversation. Some no longer are.
            worker.reset_attempt_caches()
            log.info(
                "tool_loop: turn %d masked %d old tool result(s) (%d -> %d bytes)",
                turn, masked["masked_results"], masked["bytes_before"],
                masked["bytes_after"],
            )
            if on_event is not None:
                try:
                    on_event({"type": "observations_masked",
                              "attempt": attempt_idx, "turn": turn, **masked})
                except Exception:
                    pass  # callback must never break the loop

        response = llm.complete(
            request,
            model=model,
            tools=tool_schemas,
            api_base=api_base,
            api_key=api_key,
            custom_llm_provider=custom_llm_provider,
            timeout=timeout,
            reasoning=reasoning,
        )
        total.add(response.usage)
        final = response

        # Per-turn telemetry. Without this, only the per-attempt
        # totals are visible, which makes it hard to see WHERE the
        # tokens went (typically the prompt grows fast because
        # conversation history compounds with every tool result).
        tools_requested = (
            [tc.name for tc in response.tool_calls] if response.tool_calls else []
        )
        if on_event is not None:
            try:
                on_event({
                    "type": "llm_turn",
                    "attempt": attempt_idx,
                    "turn": turn,
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cached_tokens": response.usage.cached_tokens,
                    "billable_tokens": response.usage.billable_tokens,
                    "tools_requested": tools_requested,
                    "text_only": not response.tool_calls,
                    "cumulative_total_tokens": total.total_tokens,
                    "cumulative_billable_tokens": total.billable_tokens,
                })
            except Exception:
                pass  # callback must never break the loop

        if max_tokens and total.billable_tokens >= _effective_cap():
            log.warning(
                "tool_loop: token budget exhausted after turn %d (%d >= %d billable); "
                "stopping before tool dispatch",
                turn, total.billable_tokens, _effective_cap(),
            )
            if on_event is not None:
                try:
                    on_event({
                        "type": "token_budget_exhausted",
                        "attempt": attempt_idx,
                        "turn": turn,
                        "tokens": total.billable_tokens,
                        "budget": _effective_cap(),
                        "phase": "after_llm_turn",
                        "tools_skipped": tools_requested,
                        "rebuild_ok_seen": rebuild_ok_seen,
                    })
                except Exception:
                    pass
            _stop("token_budget", turn)
            return response, total, rebuild_ok_seen

        if not response.tool_calls:
            log.debug("tool_loop: turn %d returned text-only, stopping", turn)
            _stop("text_only", turn)
            return response, total, rebuild_ok_seen

        log.debug(
            "tool_loop: turn %d issued %d tool call(s): %s",
            turn,
            len(response.tool_calls),
            tools_requested,
        )

        # Echo the assistant's tool-call message back into history so
        # the model has continuity on the next turn.
        messages.append(_assistant_message_from(response))

        for call in response.tool_calls:
            if (on_event is not None
                    and window.repeats_masked_read(call.name, call.arguments)):
                try:
                    on_event({"type": "repeat_after_mask", "attempt": attempt_idx,
                              "turn": turn, "tool": call.name,
                              "args": call.arguments or {}})
                except Exception:
                    pass  # callback must never break the loop
            t0 = time.monotonic()
            # The completion event below is emitted when the tool RETURNS.
            # dsynth_build and dsynth_test run 40+ minutes, so a reader had
            # no way to tell a working runner from a wedged one for the most
            # expensive thing the loop does (poly-qqx9.2). `call_id` pairs
            # this row with its completion; the pair is what gives a phase
            # a start and an end rather than only a duration.
            if on_event is not None:
                try:
                    on_event({
                        "type": "tool_start",
                        "attempt": attempt_idx,
                        "turn": turn,
                        "tool": call.name,
                        "call_id": call.id,
                    })
                except Exception:
                    pass  # callback must never break the loop
            # Defense-in-depth: even though we filtered the schemas
            # the model receives, refuse non-whitelisted tools if the
            # model hallucinates one (or the schema filter has a bug).
            if tool_whitelist is not None and call.name not in tool_whitelist:
                result = {
                    "ok": False,
                    "error": (
                        f"tool {call.name!r} is not allowed in this flow; "
                        f"available tools: {sorted(tool_whitelist)}"
                    ),
                }
            else:
                result = tools.dispatch(
                    call.name, call.arguments, env=env,
                )
            duration_ms = int((time.monotonic() - t0) * 1000)
            # The success signal is the dsynth_build tool returning
            # ``rebuild_ok=True``. Lifting it from the structured tool
            # result (rather than waiting for the LLM to restate it in
            # a ## Rebuild Proof block) lets us extend the budget for
            # the closing turns without trusting LLM text shape.
            if (
                not rebuild_ok_seen
                and isinstance(result, dict)
                and result.get("rebuild_ok") is True
            ):
                rebuild_ok_seen = True
                log.info(
                    "tool_loop: rebuild_ok=True observed on turn %d via %s; "
                    "extending cap by %d grace tokens",
                    turn, call.name, GRACE_TOKENS_AFTER_REBUILD_OK,
                )
                if on_event is not None:
                    try:
                        on_event({
                            "type": "rebuild_ok_seen",
                            "attempt": attempt_idx,
                            "turn": turn,
                            "tool": call.name,
                            "grace_tokens": GRACE_TOKENS_AFTER_REBUILD_OK,
                        })
                    except Exception:
                        pass
            if on_event is not None:
                try:
                    on_event({
                        "type": "tool_call",
                        "attempt": attempt_idx,
                        "turn": turn,
                        "tool": call.name,
                        "call_id": call.id,
                        "args": call.arguments or {},
                        "result": result if isinstance(result, dict) else {"value": result},
                        "duration_ms": duration_ms,
                    })
                except Exception:
                    pass  # callback must never break the loop
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(result),
                }
            )
            if isinstance(result, dict) and result.get("blocking"):
                reason = str(result.get("ignore_reason")
                             or result.get("summary") or "").strip()
                log.warning(
                    "tool_loop: %s reported a blocking condition on turn %d; "
                    "ending the attempt: %s", call.name, turn, reason,
                )
                if on_event is not None:
                    try:
                        on_event({
                            "type": "environment_blocked",
                            "attempt": attempt_idx,
                            "turn": turn,
                            "tool": call.name,
                            "reason": reason,
                        })
                    except Exception:
                        pass
                raise EnvironmentBlocked(reason, call.name, total)
    log.warning(
        "tool_loop: hit max_turns=%d without a text-only response", max_turns
    )
    _stop("turn_cap", max_turns)
    return (final if final is not None else Response(text="")), total, rebuild_ok_seen
