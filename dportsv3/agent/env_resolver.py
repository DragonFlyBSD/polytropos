"""Per-job dev-env resolution for the runner.

Single helper, single precedence rule, single test surface.
Replaces the prior pattern of every callsite doing
``job.get("dev_env") or os.environ.get("...")``.

Precedence (top wins):

1. ``job.dev_env`` — the job carries its own env. Hook-driven jobs
   are bound to the env where dsynth failed; that env is the right
   answer regardless of operator preference.
2. ``tracker_active_env`` row in state.db — what the operator
   selected in the tracker UI (or via the PUT endpoint).
3. ``--env NAME`` CLI flag at runner startup. The trackerless
   escape hatch.
4. Auto-pick if exactly one env exists on disk. The single-env
   happy path needs no operator action.
5. Refuse: caller decides whether to hold the job (tracker mode,
   wait for UI selection) or hard-exit (trackerless startup).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Iterable

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnvResolution:
    """Outcome of one resolution call."""
    env: str | None        # resolved name, or None if refused
    source: str            # "job" | "tracker" | "cli_flag" | "auto" | "none"
    refusal_reason: str | None = None
    available_envs: tuple[str, ...] = ()
    #: Set when the env list could not be read at all, as opposed to
    #: being genuinely empty. Callers that must not proceed on a guess
    #: check this rather than ``available_envs == ()``.
    enumeration_error: str | None = None


def list_available_envs_detailed() -> tuple[tuple[str, ...], str | None]:
    """Enumerate envs, and say *why* the list is empty when it is.

    Returns ``(names, error)``. ``error`` is non-None only when the store
    could not be read at all. That is a different situation from "no
    envs exist" and takes a different operator action, so the two must
    not collapse into the same empty tuple — see poly-i7y, where a
    non-root runner read the store as empty and ran with the
    dsynth-busy gate switched off.
    """
    # Deferred import of a *declared* dependency (see pyproject). It stays
    # inside the function so importing the agent stays cheap, and so a
    # generator venv that predates the dependency degrades loudly rather
    # than failing at import time.
    try:
        from dports_dev_env.config import load_config  # noqa: PLC0415
        from dports_dev_env.store import EnvironmentStore  # noqa: PLC0415
    except ImportError as exc:
        # A broken install, not an empty machine. Say so — the operator
        # action ("reinstall the generator venv") has nothing to do with
        # the one they'd infer from "no envs exist" ("create an env").
        msg = (f"dports_dev_env is not importable ({exc}) — the generator "
               f"venv is missing its dev-env dependency; reinstall it")
        _log.error("list_available_envs: %s", msg)
        return (), msg

    try:
        config = load_config()
    except Exception as exc:
        msg = (f"the dev-env config could not be loaded "
               f"({type(exc).__name__}: {exc})")
        _log.error("list_available_envs: %s", msg)
        return (), msg

    try:
        store = EnvironmentStore(config)
        names = tuple(sorted(info.name for _, info in store.list_infos()))
    except PermissionError as exc:
        msg = (f"the dev-env store at {config.envs_dir} is not readable "
               f"({exc.strerror}) — every dev-env command requires root, "
               f"so run the runner as root")
        _log.error("list_available_envs: %s", msg)
        return (), msg
    except Exception as exc:
        # Log so a broken config doesn't masquerade as "no envs exist"
        # — symptoms diverge (auto-pick refusal vs. "create one") and
        # the message tells the operator which one they're actually in.
        msg = (f"enumerating envs failed ({type(exc).__name__}: {exc})")
        _log.error("list_available_envs: %s", msg)
        return (), msg

    return names, None


def list_available_envs() -> tuple[str, ...]:
    """Enumerate envs from the filesystem via EnvironmentStore.

    Returns a sorted tuple of env names. Empty tuple if the store can't
    be read; callers that need to tell *why* it was empty want
    :func:`list_available_envs_detailed` instead.
    """
    return list_available_envs_detailed()[0]


def resolve_env_for_job(
    job: dict | None,
    db_conn: sqlite3.Connection | None,
    cli_env: str | None = None,
    *,
    available_envs: Iterable[str] | None = None,
    enumeration_error: str | None = None,
) -> EnvResolution:
    """Resolve the dev-env to use for ``job``.

    ``available_envs`` is the enumerable env list; if not supplied
    we call :func:`list_available_envs_detailed`. Pass an explicit
    value in tests to avoid touching the host filesystem, with
    ``enumeration_error`` to simulate an unreadable store.
    """
    # Step 1: job carries its own env (hook-driven).
    if job is not None:
        job_env = job.get("dev_env") if isinstance(job, dict) else None
        if isinstance(job_env, str) and job_env:
            return EnvResolution(env=job_env, source="job")

    # Step 2: tracker active env.
    if db_conn is not None:
        try:
            # Local import to keep agent package decoupled from
            # tracker imports at module load time.
            from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
                get_active_env,
            )
            active = get_active_env(db_conn)
            if active:
                return EnvResolution(env=active, source="tracker")
        except Exception as exc:
            # Schema not yet migrated, or query raised — fall through
            # to the lower-precedence sources rather than crash the
            # runner. Log at WARN so a persistent failure is visible
            # (operator might expect tracker selection to take effect
            # but the read keeps failing for a real reason).
            _log.warning(
                "env_resolver: tracker active-env read failed "
                "(%s: %s); falling through to lower precedence",
                type(exc).__name__, exc,
            )

    # Step 3: CLI flag passed to the runner at startup.
    if cli_env:
        return EnvResolution(env=cli_env, source="cli_flag")

    # Step 4: auto-pick if exactly one env exists.
    if available_envs is not None:
        envs = tuple(available_envs)
    else:
        envs, enumeration_error = list_available_envs_detailed()
    if len(envs) == 1:
        return EnvResolution(env=envs[0], source="auto",
                             available_envs=envs)

    # Step 5: refuse.
    if enumeration_error:
        # Not an empty machine — an unreadable one. Saying "create an
        # env" here sends the operator at the wrong problem.
        reason = f"could not enumerate dev-envs: {enumeration_error}"
    elif len(envs) == 0:
        reason = (
            "no dev-envs exist; create one with "
            "`dportsv3 dev-env create NAME --target TARGET`"
        )
    else:
        reason = (
            f"{len(envs)} dev-envs exist ({', '.join(envs)}); "
            f"select one in the tracker UI or pass --env NAME to "
            f"the runner / verify-fix CLI"
        )
    return EnvResolution(
        env=None, source="none",
        refusal_reason=reason, available_envs=envs,
        enumeration_error=enumeration_error,
    )
