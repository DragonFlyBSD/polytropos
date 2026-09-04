"""Patch flow — thin wrapper over attempt_loop."""

from __future__ import annotations

from . import attempt_loop, prompts, tools
from .attempt_loop import PatchResult


def run(
    payload: str,
    *,
    tier,
    env: str,
    model: str,
    api_base: str | None = None,
    api_key: str | None = None,
    custom_llm_provider: str | None = None,
    timeout: int = 600,
    max_tool_turns: int | None = None,
    on_event=None,
    origin: str | None = None,
    session_dump=None,
    reasoning: str | None = None,
) -> PatchResult:
    """Run the patch agent for one bundle. Returns the PatchResult.

    The runner is responsible for persisting the result to the bundle
    (patch.md, rebuild_proof.json, changes.diff, audit JSON).

    ``on_event`` is a callback invoked with structured dicts as the
    loop progresses: ``attempt_start``, ``tool_call``, ``attempt_end``.
    Used by the runner for live activity-log writes and to build a
    tool-trace artifact. Exceptions inside the callback are swallowed.

    The patch agent edits ``ports/<origin>/overlay.dops`` directly in
    dops DSL (``put_file`` + ``validate_dops`` + ``dops_reference``)
    plus the build-loop tools — the surface returned by
    :func:`tools.patch_tool_names`.
    """
    if max_tool_turns is None:
        # Read here rather than as a default argument so the value is
        # resolved per call, not once at import: settings are cached per
        # process and a default would freeze whatever was loaded first.
        # An explicit argument still wins — the manual harnesses and the
        # tests pass their own (poly-lvw).
        from dportsv3 import settings  # noqa: PLC0415 — import cycle
        max_tool_turns = int(settings.get("runner.max_tool_turns"))

    return attempt_loop.run(
        payload,
        tier=tier,
        env=env,
        model=model,
        api_base=api_base,
        api_key=api_key,
        custom_llm_provider=custom_llm_provider,
        timeout=timeout,
        max_tool_turns=max_tool_turns,
        on_event=on_event,
        origin=origin,
        system_prompt=prompts.PATCH_SYSTEM,
        tool_whitelist=tools.patch_tool_names(),
        session_dump=session_dump,
        reasoning=reasoning,
    )
