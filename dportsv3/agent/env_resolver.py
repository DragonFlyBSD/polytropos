"""Per-job dev-env resolution for the runner.

Single helper, single precedence rule, single test surface.
Replaces the prior pattern of every callsite doing
``job.get("dev_env") or os.environ.get("...")``.

A job with a target routes to an env of its build line (poly-p2ve):

1. ``job.dev_env`` -- the job carries its own env: a patch job the
   env its triage ran in, a verify job the env the operator
   picked, a confirm build the env its line routed to. Hook-created
   jobs carry none (hook_common.sh writes no dev_env). An env that
   no longer exists is routed past.
2. The tracker's selection (per builder, then the default), if it
   composes the job's line.
3. ``--env NAME``, if it composes the job's line.
4. The only env that composes the line.
5. Several envs compose it and none is preferred: refuse with
   source ``ambiguous``; the runner holds such jobs.

A job without a target, or for a line no env is known to compose, takes
the precedence below; the job-start check refuses a wrong line
(poly-7pwa.14; ``other_build_line``):

1. ``job.dev_env``.
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


def build_line(target: str | None) -> str:
    """The build line ``target`` names, in its one spelling (``@T``).

    The single normalizer every env-to-job comparison goes through, so a
    bare ``main`` and ``@main`` are the same line. Empty for no target.
    """
    t = (target or "").strip()
    return "@" + t.lstrip("@") if t else ""


def env_compose_target(env: str) -> str:
    """The build line ``env`` composes: ``state.target`` from its env.json.

    A file read through dports_dev_env's store, never a shell-out to
    ``dev-env status``. Raises LookupError when the state cannot be read,
    for whatever reason; callers treat that as "unknown" and never guess.
    ``state.target`` is never empty (dev-env's loader rejects it), so
    there is no empty answer.
    """
    try:
        from dports_dev_env.config import load_config  # noqa: PLC0415
        from dports_dev_env.store import EnvironmentStore  # noqa: PLC0415
    except ImportError as exc:
        raise LookupError(f"dports_dev_env is not importable ({exc})") from exc
    try:
        state = EnvironmentStore(load_config()).load(env)
    except Exception as exc:
        raise LookupError(f"{type(exc).__name__}: {exc}") from exc
    return build_line(state.target)


def other_build_line(env: str | None, target: str | None) -> str | None:
    """``env``'s build line when it differs from ``target``'s, else None.

    None also when either is empty or the env's line cannot be read: only
    a known mismatch is a mismatch (poly-7pwa.14).
    """
    job_line = build_line(target)
    if not env or not job_line:
        return None
    try:
        env_line = env_compose_target(env)
    except LookupError:
        return None
    return env_line if env_line != job_line else None


def envs_by_build_line(
    envs: Iterable[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """``{line: env names}`` over ``envs`` (default: every env on disk).

    An env whose line cannot be read is left out, so a line appears here
    only when some env is known to compose it.
    """
    names = list_available_envs() if envs is None else envs
    out: dict[str, list[str]] = {}
    for name in names:
        try:
            line = env_compose_target(name)
        except LookupError:
            continue
        out.setdefault(line, []).append(name)
    return {line: tuple(found) for line, found in out.items()}


def resolve_env_for_job(
    job: dict | None,
    db_conn: sqlite3.Connection | None,
    cli_env: str | None = None,
    *,
    available_envs: Iterable[str] | None = None,
    enumeration_error: str | None = None,
    env_lines: dict[str, str] | None = None,
) -> EnvResolution:
    """Resolve the dev-env to use for ``job``.

    A job with a target routes to an env of its build line (poly-p2ve):
    its own ``dev_env`` when that env still exists; else the operator's
    selection or ``--env`` if it composes the line; else the one env that
    does. Several envs of the line and no preference among them refuses
    with source ``ambiguous``. When no env is known to compose the line,
    resolution falls back to the precedence in the module docstring and
    the job-start check refuses a wrong line (poly-7pwa.14).

    ``available_envs`` is the enumerable env list; if not supplied
    we call :func:`list_available_envs_detailed`. Pass an explicit
    value in tests to avoid touching the host filesystem, with
    ``enumeration_error`` to simulate an unreadable store, and
    ``env_lines`` ({env: line}) to avoid reading env state.
    """
    job = job if isinstance(job, dict) else None
    line = build_line(job.get("target")) if job is not None else ""
    job_env = job.get("dev_env") if job is not None else None
    job_env = job_env if isinstance(job_env, str) and job_env else None

    if available_envs is not None:
        names: tuple[str, ...] | None = tuple(available_envs)
    elif env_lines is not None:
        names = tuple(env_lines)
    else:
        names = None

    def _names() -> tuple[str, ...]:
        # The same listing envs_by_build_line reads. Empty when the store
        # cannot be read, which nothing below treats as "no env exists".
        nonlocal names
        if names is None:
            names = tuple(list_available_envs())
        return names

    # Step 1: the job carries its own env (patch, verify and confirm
    # jobs; a hook-created job never does). A job with a line whose env is
    # known to be gone -- the listing has envs, and not that one -- routes
    # again rather than fail on a ghost.
    if job_env:
        if not line or not _names() or job_env in _names():
            return EnvResolution(env=job_env, source="job")
        _log.warning("env_resolver: job env %s no longer exists; routing "
                     "by build line %s", job_env, line)

    active = _active_env(db_conn)

    if line:
        if env_lines is None:
            env_lines = {}
            for name in _names():
                try:
                    env_lines[name] = env_compose_target(name)
                except LookupError:
                    continue
        candidates = tuple(n for n in _names() if env_lines.get(n) == line)
        if active in candidates:
            return EnvResolution(env=active, source="tracker",
                                 available_envs=candidates)
        if cli_env in candidates:
            return EnvResolution(env=cli_env, source="cli_flag",
                                 available_envs=candidates)
        if len(candidates) == 1:
            return EnvResolution(env=candidates[0], source="line",
                                 available_envs=candidates)
        if len(candidates) > 1:
            return EnvResolution(
                env=None, source="ambiguous", available_envs=candidates,
                refusal_reason=(
                    f"{len(candidates)} dev-envs compose {line} "
                    f"({', '.join(candidates)}); select one of them in the "
                    f"tracker UI or pass --env NAME"),
            )
        # No env is known to compose the line: the old precedence, and the
        # job-start check refuses it if the env it lands in is wrong.

    # Step 2: tracker active env.
    if active:
        return EnvResolution(env=active, source="tracker")

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


def _active_env(db_conn: sqlite3.Connection | None) -> str | None:
    """The operator's selection: this builder's, else the default."""
    if db_conn is None:
        return None
    try:
        # Local import to keep agent package decoupled from
        # tracker imports at module load time.
        from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
            get_active_env,
        )
        # This builder's own choice first, the deployment default
        # behind it. Without the runner_id a per-builder selection could
        # never take effect (poly-fij.13).
        from dportsv3.agent.runner import runner_id  # noqa: PLC0415
        return get_active_env(db_conn, runner_id()) or None
    except Exception as exc:
        # Schema not yet migrated, or query raised — fall through
        # to the lower-precedence sources rather than crash the
        # runner. Log at WARN so a persistent failure is visible.
        _log.warning(
            "env_resolver: tracker active-env read failed "
            "(%s: %s); falling through to lower precedence",
            type(exc).__name__, exc,
        )
        return None
