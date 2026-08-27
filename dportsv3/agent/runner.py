"""dportsv3.agent.runner — process dsynth failure jobs via the harness.

This module is the agent-queue-runner's home. ``scripts/agent-queue-runner``
is a thin shim that calls ``main()``. The ``dportsv3 agent-queue-runner``
subcommand calls ``main()`` directly. Tests can import and call internal
helpers.

Usage:
    agent-queue-runner --queue-root <path> [--once] [--dry-run]

Required env vars (one of triage or patch model must be set for the
relevant job type):
    DP_HARNESS_TRIAGE_MODEL   litellm model string for triage
    DP_HARNESS_PATCH_MODEL    litellm model string for patch
    DP_HARNESS_*_API_KEY      provider API keys
    DP_HARNESS_*_API_BASE     optional custom endpoint
    DP_HARNESS_*_PROVIDER     optional custom_llm_provider override
    DP_HARNESS_POLICY         optional path to agentic-policy.json
    DP_HARNESS_TIMEOUT        triage timeout (default 120)
    DP_HARNESS_PATCH_TIMEOUT  patch timeout (default 600)
    DP_HARNESS_MAX_SNIPPET_ROUNDS  default 5

Job types:
    type=triage (default) — runs dportsv3.agent.triage; classification +
                            confidence drive trust-tier dispatch.
    type=patch            — runs dportsv3.agent.patch with the resolved tier.

Job fields:
    iteration=N           — current fix iteration (1-based)
    max_iterations=N      — max iterations before giving up (default: 3)
    tier=NAME             — pre-resolved trust tier (set by triage step)
    dev_env=NAME          — dev-env for this job; resolved by
                            env_resolver if absent (tracker active
                            env → --env CLI flag → auto-pick if
                            exactly one env exists → refuse)
    previous_bundle=...   — bundle from previous failed attempt
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# lifecycle imports only stdlib (no cycle back to runner), so the
# canonical "actively working" state set is safe to bind at module
# load. Other lifecycle uses stay function-local per this file's
# convention, but this one backs a module-level constant.
from dportsv3 import paths
from dportsv3.agent.lifecycle import ACTIVE_WORK_STATE_VALUES
from dportsv3.engine import emit


class _VerifyBranchUnavailable(Exception):
    """Raised inside the verify dispatch when the fresh
    ``bundle/<id>-verify`` branch couldn't be checked out. Aborts the
    verify run before replay — verify's verdict only means anything if
    the diff was applied against a known-clean base."""


# Max fix iterations before giving up on a port
DEFAULT_MAX_ITERATIONS = 3

DEFAULT_TRACKER_URL = "http://127.0.0.1:8080"

# Default location of agentic-policy.json. Resolution lives in
# ``dportsv3.paths``: the operator's copy under $DPORTSV3_CONFIG_DIR wins,
# then the ``.sample`` template bundled with the package, so a fresh install
# works with no setup. Operator can still override the whole thing via
# DP_HARNESS_POLICY.
#
# Resolved lazily, not at import: $DPORTSV3_CONFIG_DIR is environment, and
# baking it into a module-level constant meant tests (and any caller that
# sets it after import) silently got the value from import time.
def _policy_path() -> str:
    override = os.environ.get("DP_HARNESS_POLICY", "").strip()
    if override:
        # Take the override as given, without resolving the default — an
        # operator pointing at their own file should not be able to trip
        # over a missing packaged template.
        return override
    return str(paths.require_config_file("agentic-policy.json"))

# Heartbeat interval (seconds)
HEARTBEAT_INTERVAL = 5

# How long to wait between dsynth-lock polls when an env is busy.
DSYNTH_LOCK_POLL_SECONDS = 30


# =============================================================================
# State DB connection (for activity logging and runner status)
# =============================================================================

_state_db_conn: sqlite3.Connection | None = None
_state_db_lock = threading.Lock()

# --env NAME from the runner CLI. Trackerless / step-3-of-precedence
# fallback consulted by resolve_env(). main() sets this before any
# job is dispatched.
_CLI_ENV_DEFAULT: str | None = None


def resolve_env(job: dict | None) -> str | None:
    """Per-job env resolution. Wraps env_resolver.resolve_env_for_job
    with the runner's DB connection + CLI flag default.

    Returns the env name, or None if no env could be resolved (the
    caller surfaces "needs_env_selection" to the operator).
    """
    from dportsv3.agent.env_resolver import resolve_env_for_job  # noqa: PLC0415
    r = resolve_env_for_job(job, _state_db_conn, cli_env=_CLI_ENV_DEFAULT)
    return r.env


# Gate-cycle cache for resolve_env(None). The gate runs every poll
# (sub-second) and re-resolves to pick up tracker UI changes
# without a restart. The DB read is cheap but it adds up; a 1-second
# TTL keeps UI changes effectively-immediate while collapsing
# burst-rate polls onto a single read.
_GATE_RESOLVE_CACHE: tuple[float, str | None] | None = None
_GATE_RESOLVE_TTL_SECONDS: float = 1.0


def resolve_env_for_gate() -> str | None:
    """Cached resolve_env(None) for the runner's per-poll gate.

    Cache TTL is :data:`_GATE_RESOLVE_TTL_SECONDS`; UI changes take
    effect within that window. Job-dispatch paths still use the
    uncached :func:`resolve_env` so per-job semantics are exact.
    """
    global _GATE_RESOLVE_CACHE
    import time  # noqa: PLC0415
    now = time.monotonic()
    if _GATE_RESOLVE_CACHE is not None:
        cached_at, cached_val = _GATE_RESOLVE_CACHE
        if now - cached_at < _GATE_RESOLVE_TTL_SECONDS:
            return cached_val
    val = resolve_env(None)
    _GATE_RESOLVE_CACHE = (now, val)
    return val


def _no_env_reason() -> str:
    """Operator-facing reason the runner has no env, for the gate.

    Distinguishes "no envs exist" from "several exist, pick one" from
    "the store could not be read" — three different fixes.
    """
    try:
        r = resolve_env_or_reason(None)
    except Exception as exc:  # never let the gate's message crash the gate
        return f"no dev-env resolved ({type(exc).__name__}: {exc})"
    return r.refusal_reason or "no dev-env resolved"


def resolve_env_or_reason(job: dict | None):
    """Like resolve_env but returns the full EnvResolution so callers
    can surface refusal_reason in their error path."""
    from dportsv3.agent.env_resolver import resolve_env_for_job  # noqa: PLC0415
    return resolve_env_for_job(job, _state_db_conn, cli_env=_CLI_ENV_DEFAULT)
_heartbeat_stop_event = threading.Event()
_heartbeat_thread: threading.Thread | None = None

#: Identity of this runner process, stable for its lifetime. Written to
#: ``runners`` at startup and stamped onto every job this process
#: transitions. Multi-runner is deliberately not supported yet: nothing
#: reads this to decide anything. It exists so that when leases arrive
#: there is already a history of who owned what, and so a second runner
#: on a shared state.db is visible in the table rather than invisible.
#: hostname+pid alone can recycle across reboots, hence the suffix.
_runner_id: str = ""


def runner_id() -> str:
    global _runner_id
    if not _runner_id:
        _runner_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return _runner_id


def register_runner() -> None:
    """Record this process in ``runners``. Best-effort, like every other
    write to the read model."""
    if _state_db_conn is None:
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with _state_db_lock:
            _state_db_conn.execute(
                """INSERT INTO runners
                   (runner_id, hostname, pid, started_at, last_heartbeat_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(runner_id) DO UPDATE SET
                     last_heartbeat_at = excluded.last_heartbeat_at,
                     stopped_at = NULL""",
                (runner_id(), socket.gethostname(), os.getpid(), ts, ts),
            )
            _state_db_conn.commit()
    except Exception as exc:
        print(f"Warning: could not register runner: {exc}", file=sys.stderr)
_current_job_id: str | None = None
_current_stage: str | None = None

# Cached EnvHealth probe result. The runner gate consults this on
# every poll; a fresh probe is taken when (a) the cached entry is
# older than DP_HARNESS_HEALTH_CACHE_SECONDS, or (b)
# invalidate_health_cache() is called explicitly (e.g. when a tool
# result looks env-suspicious mid-job).
_health_cache: dict[str, tuple[float, object]] = {}  # env → (ts, EnvHealth)


def invalidate_health_cache(env: str | None = None) -> None:
    """Drop the cached health probe for ``env`` (or all envs)."""
    if env is None:
        _health_cache.clear()
    else:
        _health_cache.pop(env, None)


def _cached_health_broken(env: str | None = None) -> bool:
    """True iff the cached probe for ``env`` was broken.

    Read directly by ``PatchAttemptStep.run`` (via the
    ``cached_health_broken`` service binding) to route a
    mid-flight job to ENV_BROKEN when the env is known bad.
    Doesn't trigger a probe — purely reads the cache.

    ``env=None`` preserves the legacy "any env" behavior for callers
    that do not have a scoped env, but step code should pass the current
    job env so one broken chroot does not poison another.
    """
    if env is not None:
        cached = _health_cache.get(env)
        if cached is None:
            return False
        return getattr(cached[1], "status", None) == "broken"
    for _ts, eh in _health_cache.values():
        status = getattr(eh, "status", None)
        if status == "broken":
            return True
    return False


def probe_health_cached(env: str, ttl_seconds: int):
    """Return an EnvHealth for ``env``, using the cache when fresh.

    Re-probes when (a) no cached entry exists, (b) the cached entry
    is older than ``ttl_seconds``, or (c) the cache was invalidated
    by ``invalidate_health_cache``. Module-level so tests + the
    runner gate share one implementation.
    """
    from dportsv3.agent import health as health_mod

    now = time.monotonic()
    cached = _health_cache.get(env)
    if cached is not None and (now - cached[0]) < ttl_seconds:
        return cached[1]
    eh = health_mod.check(env)
    _health_cache[env] = (now, eh)
    record_env_health(eh)
    return eh


def stub_unprobed_envs() -> int:
    """Insert a placeholder ``env_health_status`` row for every env
    on disk that isn't already in the table.

    Without this, brand-new envs are invisible to the tracker UI
    (the dropdown sources from this table) until the runner's first
    health probe touches them. Stubbing on runner start closes the
    UI ↔ runner truth gap: any env the runner could auto-pick is
    also visible to the operator. Real probes overwrite the stub.

    Returns the number of stub rows inserted (0 if all envs were
    already in the table or no envs exist).
    """
    if _state_db_conn is None:
        return 0
    from dportsv3.agent.env_resolver import list_available_envs  # noqa: PLC0415
    envs = list_available_envs()
    if not envs:
        return 0
    ts = datetime.now(timezone.utc).isoformat()
    inserted = 0
    try:
        with _state_db_lock:
            for env in envs:
                # INSERT OR IGNORE: real probe rows (with status=ready /
                # degraded / broken) must not be overwritten by stubs.
                cur = _state_db_conn.execute(
                    """INSERT OR IGNORE INTO env_health_status
                       (env, status, probed_at, operator_action,
                        detail_json, updated_at)
                       VALUES (?, 'unprobed', NULL, NULL,
                               '{"checks":[]}', ?)""",
                    (env, ts),
                )
                if cur.rowcount:
                    inserted += 1
            _state_db_conn.commit()
    except Exception as exc:
        print(f"Warning: stub_unprobed_envs failed: {exc}", file=sys.stderr)
    return inserted


def record_env_health(env_health) -> None:
    """Persist the latest EnvHealth snapshot for tracker/UI reads."""
    if _state_db_conn is None or env_health is None:
        return
    env = getattr(env_health, "env", None)
    status = getattr(env_health, "status", None)
    if not env or not status:
        return
    probed_at = getattr(env_health, "probed_at", None)
    operator_action = getattr(env_health, "operator_action", None)
    try:
        detail = env_health.to_dict()
    except Exception:
        detail = {"env": env, "status": status}
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with _state_db_lock:
            _state_db_conn.execute(
                """INSERT INTO env_health_status
                   (env, status, probed_at, operator_action, detail_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(env) DO UPDATE SET
                     status=excluded.status,
                     probed_at=excluded.probed_at,
                     operator_action=excluded.operator_action,
                     detail_json=excluded.detail_json,
                     updated_at=excluded.updated_at""",
                (env, status, probed_at, operator_action, json.dumps(detail), ts),
            )
            _state_db_conn.commit()
    except Exception as exc:
        print(f"Warning: Failed to record env health: {exc}", file=sys.stderr)


def _looks_env_suspicious(result: dict) -> bool:
    """Heuristic: does this tool result look like an env-level failure?

    Used purely to force a health re-probe. Setting the gate state
    is still the probe's job — we never infer broken-ness from a
    tool error directly anymore.
    """
    if not isinstance(result, dict) or result.get("ok") is True:
        return False
    stderr = (result.get("stderr_tail") or "").lower()
    for needle in (
        "missing dragonfly packages",
        "pyproject.toml not found",
        "venv setup failed",
        "no such file or directory: /work/deltaports",
    ):
        if needle in stderr:
            return True
    return False


def get_state_db_path(queue_root: Path) -> Path:
    """Get path to state.db used for lifecycle/status writes."""
    env_db = os.environ.get("DPORTSV3_STATE_DB")
    if env_db:
        return Path(env_db)
    # Queue is at <logs>/evidence/queue/, state.db is at <logs>/evidence/state.db
    return queue_root.parent / "state.db"


def init_state_db(queue_root: Path) -> sqlite3.Connection | None:
    """Initialize connection to state.db, creating + schema-initing the
    file if it doesn't exist yet.

    A first-time runner on a clean host (or after a wipe) must not
    silently disable all DB writes — that produces a runner.log full
    of activity that the tracker UI never sees. Auto-create matches
    artifact-store's behaviour: the schema is idempotent
    (``init_db`` uses CREATE TABLE IF NOT EXISTS + ADD COLUMN
    migrations), so it's safe to run on every startup.

    Returns ``None`` only when the parent dir is missing or
    ``sqlite3.connect`` itself raises — those are real misconfigs the
    operator must fix.
    """
    global _state_db_conn

    db_path = get_state_db_path(queue_root)

    parent = db_path.parent
    if not parent.exists():
        print(
            f"Warning: state.db parent dir {parent} does not exist; "
            "runner lifecycle/status writes disabled. Create the "
            "directory or set DPORTSV3_STATE_DB to a valid path.",
            file=sys.stderr,
        )
        return None

    created = not db_path.exists()
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        print(f"Warning: could not connect to state.db at {db_path}: "
              f"{exc}", file=sys.stderr)
        return None

    # Always run init_db — it's idempotent and applies any pending
    # ALTER TABLE migrations. Cheap, and protects against running
    # against a partially-migrated DB.
    try:
        from dportsv3.db.schema import init_db as _init_schema  # noqa: PLC0415
        _init_schema(conn)
    except Exception as exc:
        print(f"Warning: schema init on state.db at {db_path} failed: "
              f"{exc}; runner lifecycle/status writes disabled",
              file=sys.stderr)
        try:
            conn.close()
        except Exception:
            pass
        return None

    if created:
        print(f"Initialized new state.db at {db_path}", file=sys.stderr)

    _state_db_conn = conn
    return conn


def _tracker_url() -> str:
    return os.environ.get("DPORTSV3_TRACKER_URL", DEFAULT_TRACKER_URL)


def artifact_get(bundle_id: str, relpath: str) -> bytes | None:
    """One artifact's bytes from the tracker, or None.

    There used to be two of these — /v1/artifacts/get on the store's port
    and /api/bundles/../artifacts/.. on the tracker's — tried in order.
    They served the same bytes off the same disk; the fallback existed
    only because there were two services.
    """
    url = (
        f"{_tracker_url()}/v1/artifacts/get"
        f"?bundle_id={urllib.parse.quote(bundle_id)}"
        f"&relpath={urllib.parse.quote(relpath)}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read()
    except Exception:
        return None


def artifact_store_put(bundle_id: str, relpath: str, data: bytes, kind: str | None = None) -> bool:
    url = f"{_tracker_url()}/v1/artifacts/put"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/octet-stream",
        "X-Bundle-Id": bundle_id,
        "X-Relpath": relpath,
    }
    if kind:
        headers["X-Kind"] = kind
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception:
        return False


def bundle_artifact_list(bundle_id: str) -> list[str]:
    url = f"{_tracker_url()}/api/bundles/{urllib.parse.quote(bundle_id)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
        return [a.get("relpath") for a in data.get("artifacts", []) if a.get("relpath")]
    except Exception:
        return []


def _apply_transition(
    job_id: str,
    event,  # dportsv3.agent.lifecycle.JobEvent
    *,
    actor: str = "runner",
    detail: dict | None = None,
) -> bool:
    """Run a lifecycle transition under the runner's DB lock.

    Best-effort observability write: if the state DB isn't available or
    the transition is illegal (mid-race), log + continue. The
    filesystem queue is the source of truth for what processes next;
    the typed jobs table is the UI's read model.
    """
    from dportsv3.agent import lifecycle  # type: ignore[import-not-found]

    if _state_db_conn is None:
        return False
    try:
        with _state_db_lock:
            lifecycle.apply(_state_db_conn, job_id, event, actor=actor,
                            detail=detail, owner=runner_id())
        return True
    except lifecycle.IllegalTransition as exc:
        print(f"Warning: illegal transition {event} on {job_id}: {exc}",
              file=sys.stderr)
        return False
    except Exception as exc:
        print(f"Warning: lifecycle.apply({event}) on {job_id} failed: {exc}",
              file=sys.stderr)
        return False


def _lookup_bundle_target(bundle_id: str | None) -> str:
    """Return ``bundles.target`` for ``bundle_id`` or ``""``.

    Used as the canonical fallback when an enqueue path doesn't carry
    its own target: a triage/patch job for a known bundle should
    inherit the bundle's target. Without this, jobs created under a
    runner whose ``DPORTSV3_TRACKER_TARGET`` env var is unset land
    with ``target=NULL`` while the bundle has the real value, and
    the tracker UI's per-port aggregates (``token_usage_for_port``)
    join-out to zero rows.
    """
    if not bundle_id or _state_db_conn is None:
        return ""
    try:
        with _state_db_lock:
            row = _state_db_conn.execute(
                "SELECT target FROM bundles WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
    except Exception:
        return ""
    if row is None:
        return ""
    val = row["target"] if hasattr(row, "keys") else row[0]
    return val or ""


def _register_new_job(
    job_id: str,
    metadata: dict,
    *,
    actor: str = "runner",
) -> bool:
    """Insert a jobs row + fire the initial HOOK_ENQUEUED event.

    Called by hook-driven enqueue paths (triage + auto-enqueued patch).
    Metadata fields (type, origin, flavor, bundle_dir, created_ts_utc,
    path, target) populate the jobs row; the typed state column is
    set by lifecycle.apply.

    If ``metadata["target"]`` is empty/missing but ``metadata["bundle_id"]``
    is set, resolves the target via ``_lookup_bundle_target`` so the
    job inherits the bundle's target instead of landing NULL.
    """
    from dportsv3.agent import lifecycle  # type: ignore[import-not-found]

    if _state_db_conn is None:
        return False
    target = metadata.get("target") or ""
    if not target:
        target = _lookup_bundle_target(metadata.get("bundle_id"))
    ok = _apply_transition(job_id, lifecycle.JobEvent.HOOK_ENQUEUED,
                           actor=actor, detail=metadata)
    if not ok:
        return False
    # Fill in the metadata columns. lifecycle.apply set jobs.state +
    # jobs.last_transition_at; we add the rest here in a second small
    # transaction.
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _state_db_lock:
            _state_db_conn.execute(
                """UPDATE jobs SET
                       type = COALESCE(?, type),
                       origin = COALESCE(?, origin),
                       flavor = COALESCE(?, flavor),
                       bundle_dir = COALESCE(?, bundle_dir),
                       bundle_id = COALESCE(?, bundle_id),
                       created_ts_utc = COALESCE(?, created_ts_utc),
                       path = COALESCE(?, path),
                       target = COALESCE(NULLIF(?, ''), target),
                       last_seen_at = ?
                   WHERE job_id = ?""",
                (
                    metadata.get("type"),
                    metadata.get("origin"),
                    metadata.get("flavor"),
                    metadata.get("bundle_dir"),
                    metadata.get("bundle_id"),
                    metadata.get("created_ts_utc"),
                    metadata.get("path"),
                    target,
                    now,
                    job_id,
                ),
            )
            _state_db_conn.commit()
        return True
    except Exception as exc:
        print(f"Warning: jobs metadata update for {job_id} failed: {exc}",
              file=sys.stderr)
        return False


def _materialize_bundle(bundle_id: str, dest: Path) -> int:
    """Download all artifacts for ``bundle_id`` into ``dest``.

    Bundles delivered via the artifact-store HTTP path live in state.db,
    not on the filesystem; the harness triage flow (and snippet-extractor)
    need a real directory. Materialize on demand into a tempdir.

    Returns the number of artifacts written. Skips relpaths the store
    can't return (silently — the caller decides whether 0 is an error).
    """
    relpaths = bundle_artifact_list(bundle_id)
    written = 0
    for rel in relpaths:
        data = artifact_get(bundle_id, rel)
        if data is None:
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        written += 1
    return written


def _compute_error_signature(text: str | None) -> str | None:
    """Return the canonical fingerprint of ``logs/errors.txt`` (the
    issue-identity primitive), or ``None`` if the text is missing/empty.

    Thin delegate to :func:`dportsv3.fingerprint.compute_fingerprint` so
    the runner's sticky-signature retry-cap check and the ingest hook
    share one normalization rule — a fingerprint stamped at ingest must
    equal the one the runner would compute, or the two would disagree on
    what "the same failure" is. See that module for the canonicalization.
    """
    from dportsv3.fingerprint import compute_fingerprint  # noqa: PLC0415
    return compute_fingerprint(text)


def _ensure_recent_signatures(target: str, origin: str, window_hours: int) -> None:
    """Backfill ``bundles.error_signature`` for recent same-origin
    failure bundles whose signature is still NULL.

    Lazy population so that Step 6's sticky-signature check has data
    to work with. Idempotent (UPDATE only runs against NULL rows).
    Failures are swallowed — sticky-signature simply doesn't fire if
    we couldn't read the artifact.
    """
    if _state_db_conn is None or not origin:
        return
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max(0, int(window_hours)))
    ).isoformat()
    try:
        with _state_db_lock:
            rows = _state_db_conn.execute(
                """SELECT bundle_id FROM bundles
                   WHERE origin = ? AND result IN ('failure', 'fail')
                     AND error_signature IS NULL
                     AND last_seen_at >= ?
                     AND (target = ?
                          OR (? = '' AND (target IS NULL OR target = '')))""",
                (origin, cutoff, target, target),
            ).fetchall()
    except Exception:
        return
    for row in rows:
        bundle_id = row["bundle_id"] if hasattr(row, "keys") else row[0]
        text = read_bundle_text(None, bundle_id, "logs/errors.txt")
        sig = _compute_error_signature(text)
        if not sig:
            continue
        try:
            with _state_db_lock:
                _state_db_conn.execute(
                    "UPDATE bundles SET error_signature = ? WHERE bundle_id = ?",
                    (sig, bundle_id),
                )
                _state_db_conn.commit()
        except Exception:
            continue


def _load_port_history(target: str, origin: str, window_hours: int):
    """Thin lock-wrapper over PortHistory.load using the runner DB.

    The decision engine's ``PortHistory.load`` does the SQL; this
    helper just supplies ``_state_db_conn`` under ``_state_db_lock``
    so callers don't have to import sqlite3 or know about the lock.

    Backfills error_signature for recent failure bundles first so the
    sticky-signature retry-cap check has data to operate on.
    """
    from dportsv3.agent.decision import PortHistory

    if _state_db_conn is None or not origin:
        return PortHistory.empty(target=target or "", origin=origin or "")
    _ensure_recent_signatures(target or "", origin, window_hours)
    with _state_db_lock:
        return PortHistory.load(_state_db_conn, target or "", origin, window_hours)


def port_bundle_history(origin: str) -> list[dict]:
    # Tracker /api/ports/<origin> returns a flat list (vs. legacy
    # state-server's {"origin", "bundles", "jobs"} shape).
    url = f"{_tracker_url()}/api/ports/{urllib.parse.quote(origin)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
        return data if isinstance(data, list) else data.get("bundles", [])
    except Exception:
        return []


def read_bundle_text(bundle_dir: Path | None, bundle_id: str | None, relpath: str) -> str | None:
    if bundle_dir:
        path = bundle_dir / relpath
        if path.exists():
            return read_file_if_exists(path)
    if bundle_id:
        data = artifact_get(bundle_id, relpath)
        if data is not None:
            return data.decode("utf-8", errors="replace")
    return None


def get_run_profile(run_id: str) -> str:
    if _state_db_conn is None:
        return "unknown"
    try:
        with _state_db_lock:
            row = _state_db_conn.execute(
                "SELECT profile FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row and row["profile"]:
            return row["profile"]
    except Exception:
        pass
    return "unknown"


def get_bundle_flavor(bundle_id: str) -> str:
    if _state_db_conn is None:
        return ""
    try:
        with _state_db_lock:
            row = _state_db_conn.execute(
                "SELECT flavor FROM bundles WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
        if row and row["flavor"]:
            return row["flavor"]
    except Exception:
        pass
    return ""


def get_user_context(run_id: str | None, origin: str | None) -> tuple[str | None, int]:
    """Fetch user context for run_id+origin from state.db."""
    if _state_db_conn is None or not run_id or not origin:
        return None, 0
    try:
        with _state_db_lock:
            row = _state_db_conn.execute(
                """SELECT context_text, context_rev FROM user_context
                   WHERE run_id = ? AND origin = ?""",
                (run_id, origin)
            ).fetchone()
        if not row:
            return None, 0
        return row["context_text"], int(row["context_rev"])
    except Exception:
        return None, 0


def upsert_user_context_request(
    queue_root: Path,
    run_id: str,
    origin: str,
    bundle_id: str,
    classification: str,
    confidence: str,
    iteration: int,
    max_iterations: int,
):
    """Record a request for user context in state.db."""
    if _state_db_conn is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    _, context_rev = get_user_context(run_id, origin)
    try:
        with _state_db_lock:
            row = _state_db_conn.execute(
                """SELECT last_context_rev_handled FROM user_context_requests
                   WHERE run_id = ? AND origin = ? AND bundle_id = ?""",
                (run_id, origin, bundle_id)
            ).fetchone()
            if row:
                _state_db_conn.execute(
                    """UPDATE user_context_requests
                       SET confidence = ?, classification = ?, iteration = ?,
                           max_iterations = ?, requested_at = ?, status = 'pending'
                       WHERE run_id = ? AND origin = ? AND bundle_id = ?""",
                    (confidence, classification, iteration, max_iterations, now, run_id, origin, bundle_id)
                )
            else:
                _state_db_conn.execute(
                    """INSERT INTO user_context_requests
                       (run_id, origin, bundle_id, confidence, classification, iteration,
                        max_iterations, requested_at, status, last_context_rev_handled)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (run_id, origin, bundle_id, confidence, classification, iteration, max_iterations, now, context_rev)
                )
            _state_db_conn.commit()
    except Exception as e:
        print(f"Warning: Failed to write user_context_request: {e}", file=sys.stderr)


def find_latest_bundle_id(run_id: str, origin: str) -> str | None:
    """Find latest bundle_id for run_id+origin from state.db."""
    if _state_db_conn is None:
        return None
    try:
        with _state_db_lock:
            row = _state_db_conn.execute(
                """SELECT bundle_id FROM bundles
                   WHERE run_id = ? AND origin = ?
                   ORDER BY ts_utc DESC LIMIT 1""",
                (run_id, origin),
            ).fetchone()
        if row:
            return row["bundle_id"]
    except Exception:
        return None
    return None


def enqueue_triage_job(
    queue_root: Path,
    bundle_id: str,
    run_id: str,
    origin: str,
    profile: str,
    flavor: str,
    iteration: int,
    max_iterations: int,
    previous_bundle: str | None,
    context_rev: int,
) -> Path:
    """Enqueue a triage job for the given bundle."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    origin_safe = origin.replace("/", "_")
    pid = os.getpid()
    job_name = f"{ts}-{profile}-{origin_safe}-{pid}.job"
    pending_dir = queue_root / "pending"
    job_path = pending_dir / job_name

    target = (
        _lookup_bundle_target(bundle_id)
        or os.environ.get("DPORTSV3_TRACKER_TARGET", "")
    )

    content = [
        "type=triage",
        f"created_ts_utc={ts}",
        f"profile={profile}",
        f"target={target}",
        f"origin={origin}",
        f"flavor={flavor}",
        f"bundle_id={bundle_id}",
        f"run_id={run_id}",
        f"iteration={iteration}",
        f"max_iterations={max_iterations}",
        f"user_context_rev={context_rev}",
    ]
    if previous_bundle:
        content.append(f"previous_bundle={previous_bundle}")

    tmp_path = job_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        f.write("\n".join(content) + "\n")
    tmp_path.rename(job_path)
    _register_new_job(
        job_path.name,
        metadata={
            "type": "triage",
            "origin": origin,
            "flavor": flavor,
            "created_ts_utc": ts,
            "path": str(job_path),
            "target": target,
            "bundle_id": bundle_id,
        },
    )
    return job_path


def process_verify_requests(queue_root: Path) -> None:
    """Reconcile operator-triggered verify requests (Step 11c
    layer-violation cleanup).

    The tracker's ``POST /api/bundles/{id}/verify`` writes a row to
    ``verify_requests`` and returns immediately. This loop scans for
    ``status='pending'`` rows, fetches the bundle metadata from the
    shared state.db, calls :func:`enqueue_verify_job`, and marks the
    request ``enqueued`` (or ``failed`` if enqueue raised). Mirrors
    the ``process_user_context_updates`` shape.

    The runner is the only process that touches the queue
    filesystem, restoring the tracker-is-read-only-for-queue
    invariant. Remote runners (Step 17) can serve this poll over
    the shared state.db without any tracker changes.
    """
    if _state_db_conn is None:
        return
    try:
        with _state_db_lock:
            rows = _state_db_conn.execute(
                """SELECT id, bundle_id, env, requested_by
                   FROM verify_requests
                   WHERE status = 'pending'
                   ORDER BY requested_at ASC"""
            ).fetchall()
    except sqlite3.Error as exc:
        log(queue_root, "WARN",
            f"verify_requests scan failed: {exc}")
        return
    for row in rows:
        req_id = row["id"] if hasattr(row, "keys") else row[0]
        bundle_id = row["bundle_id"] if hasattr(row, "keys") else row[1]
        env = row["env"] if hasattr(row, "keys") else row[2]
        requested_by = (row["requested_by"]
                        if hasattr(row, "keys") else row[3]) or "operator"
        # Look up bundle to get origin + target.
        try:
            with _state_db_lock:
                brow = _state_db_conn.execute(
                    "SELECT origin, target FROM bundles WHERE bundle_id = ?",
                    (bundle_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            log(queue_root, "WARN",
                f"verify request {req_id}: bundle lookup failed: {exc}")
            continue
        if brow is None:
            _mark_verify_request(req_id, "failed",
                                 error=f"bundle {bundle_id} not found")
            continue
        origin = brow["origin"] if hasattr(brow, "keys") else brow[0]
        target = (brow["target"] if hasattr(brow, "keys") else brow[1]) or ""
        try:
            job_path = enqueue_verify_job(
                queue_root, bundle_id=bundle_id, origin=origin,
                target=target, env=env, requested_by=requested_by,
            )
        except Exception as exc:
            log(queue_root, "WARN",
                f"verify request {req_id} enqueue failed: {exc}")
            _mark_verify_request(req_id, "failed", error=str(exc)[:500])
            continue
        _mark_verify_request(req_id, "enqueued", job_id=job_path.name)
        activity_log(
            queue_root, "verify_enqueued_from_request",
            f"verify request {req_id} for {bundle_id} → {job_path.name}",
            job_id=job_path.name,
            extra={"request_id": req_id, "bundle_id": bundle_id,
                   "env": env},
        )


def _mark_verify_request(
    req_id: int, status: str, *,
    job_id: str | None = None, error: str | None = None,
) -> None:
    if _state_db_conn is None:
        return
    try:
        with _state_db_lock:
            _state_db_conn.execute(
                """UPDATE verify_requests
                   SET status = ?, job_id = COALESCE(?, job_id),
                       error = COALESCE(?, error)
                   WHERE id = ?""",
                (status, job_id, error, req_id),
            )
            _state_db_conn.commit()
    except sqlite3.Error:
        pass


def _claim_issue_build(queue_root: Path, issue_key: str,
                       generation: int) -> bool:
    """Atomically CLAIM the confirm build for ``(issue_key, generation)``.

    The single-flight marker is taken BEFORE the job is enqueued, and the
    claim itself decides whether we enqueue at all. Stamping it afterwards
    (the original shape) meant a DB write that failed — e.g. the tracker
    holding the write lock past ``busy_timeout`` — left no marker, logged
    nothing, and the next loop pass (seconds later) enqueued a duplicate
    confirm build: a full dsynth run, wasted.

    Conditional on the intent not having moved (``requested_build_generation``
    still ``generation``) and nothing newer already being in flight, so two
    passes cannot both claim. Returns True when this caller owns the build.
    """
    if _state_db_conn is None:
        return False
    try:
        with _state_db_lock:
            cur = _state_db_conn.execute(
                "UPDATE issues SET building_generation = ? "
                "WHERE issue_key = ? AND requested_build_generation = ? "
                "AND (building_generation IS NULL OR building_generation < ?)",
                (generation, issue_key, generation, generation),
            )
            _state_db_conn.commit()
            return bool(cur.rowcount)
    except sqlite3.Error as exc:
        # Do NOT enqueue on a failed claim — that is exactly how duplicates
        # were produced. Surface it instead of failing silently.
        log(queue_root, "WARN",
            f"could not claim confirm build for {issue_key} "
            f"(gen {generation}): {exc}")
        return False


def sweep_stranded_inflight(queue_root: Path) -> list[str]:
    """Move ``inflight/`` leftovers to ``failed/`` (B3).

    ``reap_orphans`` marks the DB rows DEAD but never touches the files, and
    ``claim_next_job_batch`` only globs ``pending/`` — so a job file stranded
    by a crash is invisible to the runner forever while its row reads dead.

    Moved rather than deleted: the payload is the only record of what the job
    was, and ``failed/`` is where a human already looks. Safe because the
    runner holds an exclusive lock, so nothing in ``inflight/`` belongs to a
    live process.

    Returns the names moved.
    """
    inflight = queue_root / "inflight"
    if not inflight.is_dir():
        return []
    moved: list[str] = []
    for job_path in sorted(inflight.glob("*.job")):
        try:
            write_error_note(
                job_path,
                "stranded in inflight/ by a previous runner; not reprocessed",
            )
            move_job(job_path, "failed")
            moved.append(job_path.name)
        except OSError as exc:
            log(queue_root, "WARN",
                f"could not sweep stranded job {job_path.name}: {exc}")
    return moved


def sweep_stale_worktrees(queue_root: Path, env: str) -> list[str]:
    """Remove per-job worktrees left by a crashed runner (B3).

    Called at startup only, and only while the runner lock is held. That is
    what makes it decidable: no other runner exists, and this one has not
    claimed anything yet, so every ``/work/job-*`` directory is by definition
    a leftover. Nothing is checked for liveness because nothing can be live.

    Single env by design. Sweeping envs this runner is not using would mean
    running ``rm -rf`` inside a tree an operator might be sitting in — the
    lock says nothing about ``dev-env shell``, which does not mark an env
    busy. When a runner owns several envs (poly-s9m) this loops over the set
    it owns, which is still the set it can reason about.

    Returns the worktree directory names removed.
    """
    if not env:
        return []
    from dportsv3.agent import worker  # noqa: PLC0415
    try:
        result = worker.sweep_job_worktrees(env)
    except Exception as exc:
        log(queue_root, "WARN", f"worktree sweep failed for env {env}: {exc}")
        return []
    if not result.get("ok"):
        log(queue_root, "WARN",
            f"worktree sweep failed for env {env}: "
            f"{result.get('error') or result.get('stderr_tail', '')[:200]}")
        return []
    return list(result.get("removed") or [])


def _reset_building_markers(queue_root: Path) -> None:
    """Crash recovery (C2), run once before the main loop.

    A confirm build runs in-process, so a runner that died mid-build left a
    stale in-flight marker. Clearing it lets the level-triggered feed
    re-derive and re-enqueue (requested > confirmed still holds).

    Clearing markers ALONE was not safe: a confirm job sitting in ``pending/``
    survives a restart — ``reap_orphans`` deliberately leaves QUEUED jobs
    claimable, and ``reap_stale_queued`` only reaps rows whose file is gone.
    So the old job stayed claimable AND the feed enqueued a fresh one: two
    builds for one request, and with the A4 threshold of 2 an issue could
    "confirm" on what was effectively a single requested confirmation.

    The two halves are therefore done together: discard queued confirm jobs
    first, then clear the markers, and let the feed re-derive from scratch.
    Discarding is safe precisely because the intent is level-triggered — the
    counters, not the job file, are the source of truth.
    """
    if _state_db_conn is None:
        return
    discarded = 0
    for pending in (queue_root / "pending").glob("*-confirm.job"):
        try:
            _apply_transition(pending.name, JobEvent.REAP_ORPHAN,
                              detail={"reason": "runner restart: queued "
                                                "confirm job re-derived"})
        except Exception:
            pass  # lifecycle bookkeeping is best-effort; the file is what matters
        try:
            pending.unlink()
            discarded += 1
        except OSError as exc:
            log(queue_root, "WARN",
                f"could not discard queued confirm job {pending.name}: {exc}")
    if discarded:
        log(queue_root, "INFO",
            f"discarded {discarded} queued confirm job(s); the reconcile "
            f"loop will re-derive them")
    try:
        with _state_db_lock:
            cur = _state_db_conn.execute(
                "UPDATE issues SET building_generation = NULL "
                "WHERE building_generation IS NOT NULL"
            )
            _state_db_conn.commit()
        if cur.rowcount:
            log(queue_root, "INFO",
                f"cleared {cur.rowcount} stale confirm-build marker(s)")
    except sqlite3.Error as exc:
        log(queue_root, "WARN", f"reset building markers failed: {exc}")


def _confirm_green_threshold() -> int:
    """How many consecutive GREEN confirm builds are required before an issue
    resolves (A4). A single green is provisional — build failures are flaky
    (network, toolchain), and resolving on one green would silently close
    issues that are not actually fixed.

    Same env-knob shape as the neighbouring retry caps
    (``DP_HARNESS_MAX_PATCH_ATTEMPTS`` / ``DP_HARNESS_ATTEMPT_WINDOW_HOURS``).
    Set to 1 to resolve on the first green."""
    return _env_int("DP_CONFIRM_GREEN_THRESHOLD", 2)


def _bump_confirm_green_count(conn: sqlite3.Connection, issue_key: str) -> int:
    """Increment and return the issue's consecutive-green tally (A4). Runs on
    the caller's open transaction."""
    conn.execute(
        "UPDATE issues SET confirm_green_count = confirm_green_count + 1 "
        "WHERE issue_key = ?",
        (issue_key,),
    )
    row = conn.execute(
        "SELECT confirm_green_count FROM issues WHERE issue_key = ?",
        (issue_key,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _reset_confirm_green_count(conn: sqlite3.Connection, issue_key: str) -> None:
    """Clear the consecutive-green tally (A4) — on a red verdict, and after a
    resolve so a later regression cycle starts from zero."""
    conn.execute(
        "UPDATE issues SET confirm_green_count = 0 WHERE issue_key = ?",
        (issue_key,),
    )


def _env_int(name: str, default: int) -> int:
    """A positive integer knob from the environment.

    The confirm loop's knobs all have this shape. Unparseable falls back to
    the default — an operator typo must not take the runner down — and the
    clamp keeps a zero or negative from disabling the very bound the knob
    exists to set, which would read as configured while being off."""
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _confirm_max_failures() -> int:
    """How many consecutive confirm builds may fail to RUN before the issue is
    handed to a human (yt7). Same env-knob shape as the other retry caps."""
    return _env_int("DP_CONFIRM_MAX_FAILURES", 3)


def _confirm_backoff_seconds(failures: int) -> int:
    """How long to hold an issue back after ``failures`` consecutive confirm
    attempts that produced no verdict.

    Exponential in the tally the bound already keeps, so C4 needs no second
    counter: 1st failure waits the base, 2nd twice that, and so on, capped so
    a raised ``DP_CONFIRM_MAX_FAILURES`` cannot push the last retry past the
    point an operator would still be waiting for it.

    The bound alone was not enough. `no dev-env available` and a failed branch
    checkout raise in seconds, so three attempts at loop speed burned the
    whole budget before a transient outage could clear — a handoff written for
    something that would have fixed itself. A slow failure has the opposite
    cost: `confirm verdict could not be recorded` fires only after the build
    ran, so each retry is a whole dsynth run.
    """
    if failures < 1:
        return 0
    base = _env_int("DP_CONFIRM_BACKOFF_SECONDS", 60)
    cap = _env_int("DP_CONFIRM_BACKOFF_MAX_SECONDS", 3600)
    # Shift rather than pow so a large tally cannot build a huge intermediate.
    return min(cap, base << min(failures - 1, 30))


def _next_eligible_at(failures: int) -> str | None:
    """The timestamp :func:`_confirm_backoff_seconds` translates to, or None
    when no delay applies.

    Written in the same ISO-8601 UTC form the feed compares against, because
    that comparison is lexicographic — see ``_normalize_ts`` in the store for
    what mixing timestamp formats in one column cost last time."""
    delay = _confirm_backoff_seconds(failures)
    if delay <= 0:
        return None
    return (datetime.now(timezone.utc)
            + timedelta(seconds=delay)).isoformat()


def _record_confirm_failure(queue_root: Path, issue_key: str, *,
                            reason: str) -> bool:
    """A confirm build could not RUN (env gone, nothing to replay, tracker
    unreachable) — no verdict was produced.

    Clears the in-flight marker so the level-triggered feed can retry WITHOUT
    a runner restart, counts the consecutive failure, and holds the issue back
    for a growing interval (C4) so the retries are spread rather than spent on
    consecutive loop passes. Once ``DP_CONFIRM_MAX_FAILURES`` is reached the
    issue is reopened to ``unresolved``, which puts it back on the worklist
    with the reason attached.

    Both ways an attempt can produce no verdict come here: the job ran and the
    machinery failed, and the enqueue itself failed. The second used to
    release the claim silently and retry forever, uncounted. Only the first
    also writes ``analysis/manual_handoff.md`` — its caller has the bundle to
    write it into; a failed enqueue has no job directory yet.

    The marker used to be left set instead, which parked the issue in
    `resolving` until a human restarted the runner — and for a permanently
    unbuildable fix (bundle with no/empty ``changes.diff``, dev-env absent)
    every restart simply burned another job and the issue never got out.

    Returns True when the retry budget is exhausted (issue reopened)."""
    if _state_db_conn is None:
        return False
    cap = _confirm_max_failures()
    try:
        with _state_db_lock:
            _state_db_conn.execute(
                "UPDATE issues SET confirm_failure_count = "
                "confirm_failure_count + 1, building_generation = NULL "
                "WHERE issue_key = ?",
                (issue_key,),
            )
            row = _state_db_conn.execute(
                "SELECT confirm_failure_count FROM issues WHERE issue_key = ?",
                (issue_key,),
            ).fetchone()
            failures = int(row[0]) if row is not None else 0
            exhausted = failures >= cap
            _state_db_conn.execute(
                "UPDATE issues SET next_eligible_at = ? WHERE issue_key = ?",
                (_next_eligible_at(failures), issue_key),
            )
            if exhausted:
                from dportsv3.tracker.routes.issue_actions import (  # noqa: PLC0415
                    reopen_issue_build_failed,
                )
                now = datetime.now(timezone.utc).isoformat()
                reopen_issue_build_failed(
                    _state_db_conn, issue_key, now=now, actor="runner-confirm",
                    detail=(f"confirm build could not run {failures}x "
                            f"(cap {cap}): {reason}"),
                )
                # Budget spent and the issue reopened: drop the pacing with
                # the tally. Leaving it would silently delay the FIRST build
                # of whatever fix an operator accepts next.
                _state_db_conn.execute(
                    "UPDATE issues SET confirm_failure_count = 0, "
                    "next_eligible_at = NULL WHERE issue_key = ?", (issue_key,),
                )
            _state_db_conn.commit()
    except Exception as exc:
        try:
            _state_db_conn.rollback()
        except Exception:
            pass
        log(queue_root, "WARN",
            f"could not record confirm failure for {issue_key}: {exc}")
        return False
    if exhausted:
        log(queue_root, "ERROR",
            f"confirm build for {issue_key} failed {failures}x (cap {cap}); "
            f"issue reopened for manual handling")
    else:
        log(queue_root, "INFO",
            f"confirm build for {issue_key} failed {failures}/{cap}; "
            f"will retry")
    return exhausted


def _record_confirm_verdict(queue_root: Path, issue_key: str, generation: int,
                            *, ok: bool, requested_by: str,
                            target: str = "",
                            verdict_detail: str = "") -> str:
    """Land a produced confirm-build verdict, in ONE transaction.

    Bookkeeping (A1): advance ``last_confirmed_build_generation`` to the built
    generation and clear the in-flight marker (so the C2 feed stops
    re-enqueuing — including for a persistently-red fix), and emit
    ``issue_confirm_build``.

    Terminal transition, both guarded to ``state='resolving'`` so a muted /
    operator-moved issue is left alone:
      * GREEN (A2) → ``resolved`` with the Green-Head watermark.
      * RED   (A3) → ``unresolved``, delivery history cleared. No agent work
        is re-enqueued; the caller writes ``analysis/manual_handoff.md``.

    Bookkeeping and the transition share the transaction so a crash can't
    leave a verdict recorded with the issue in the wrong state. Only called
    when the build produced a verdict; a machinery gave-up leaves
    ``building_generation`` set (see the confirm dispatch).

    Returns ``"recorded"`` when the verdict was durably committed,
    ``"stale"`` when it was superseded by a newer generation (another job
    already moved this issue on), or ``"error"`` when nothing was written.
    The caller must NOT report success on ``"error"`` — no verdict exists
    and the issue is still marked in-flight."""
    if _state_db_conn is None:
        return "error"
    new_state = None
    terminal_attempted = True
    try:
        with _state_db_lock:
            # Guard against a STALE verdict. Two confirm jobs can exist for
            # one issue (a re-accept bumps the generation mid-build; a
            # restart re-derives a still-queued job), and they can finish out
            # of order. Only a verdict NEWER than what is already confirmed
            # counts: `last_confirmed_build_generation < generation`. Without
            # this the counter walks backwards and the feed re-enqueues an
            # already-built generation (each one a full dsynth run). A
            # malformed job file (generation 0) is rejected by the same test.
            # The in-flight marker is cleared only if it belongs to THIS
            # build or older — never a newer build's marker.
            cur = _state_db_conn.execute(
                "UPDATE issues SET last_confirmed_build_generation = ?, "
                "building_generation = CASE WHEN building_generation > ? "
                "    THEN building_generation ELSE NULL END, "
                # A produced verdict means the machinery works: clear the
                # could-not-run tally (yt7) so unrelated transient failures
                # never accumulate into a false give-up, and the pacing built
                # from it (C4) — an A4 re-request must not inherit a delay
                # earned by an earlier outage.
                "confirm_failure_count = 0, next_eligible_at = NULL "
                "WHERE issue_key = ? AND last_confirmed_build_generation < ?",
                (generation, generation, issue_key, generation),
            )
            if not cur.rowcount:
                _state_db_conn.rollback()
                log(queue_root, "INFO",
                    f"confirm verdict for {issue_key} gen {generation} "
                    f"ignored: stale or already recorded")
                return "stale"
            from dportsv3.artifact_store import emit_event  # noqa: PLC0415
            emit_event(_state_db_conn, "issue_confirm_build",
                       {"issue_key": issue_key, "ok": ok,
                        "requested_build_generation": generation,
                        "requested_by": requested_by})
            now = datetime.now(timezone.utc).isoformat()
            if ok:
                # A4: a single green is provisional. Count consecutive greens
                # and only resolve once the threshold is met; below it, bump
                # the requested generation so the C2 feed schedules another
                # INDEPENDENT confirm build (distinct generation, distinct
                # job) rather than re-reading this one's result.
                threshold = _confirm_green_threshold()
                greens = _bump_confirm_green_count(_state_db_conn, issue_key)
                if greens < threshold:
                    _state_db_conn.execute(
                        "UPDATE issues SET requested_build_generation = "
                        "requested_build_generation + 1, updated_at = ? "
                        "WHERE issue_key = ? AND state = 'resolving'",
                        (now, issue_key),
                    )
                    log(queue_root, "INFO",
                        f"confirm GREEN {greens}/{threshold} for {issue_key} "
                        f"— provisional, requesting another build")
                    terminal_attempted = False
                else:
                    from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
                        green_head_watermark,
                    )
                    from dportsv3.tracker.routes.issue_actions import (  # noqa: PLC0415
                        resolve_issue_build_confirmed,
                    )
                    new_state = resolve_issue_build_confirmed(
                        _state_db_conn, issue_key, now=now,
                        green_head_run_id=green_head_watermark(
                            _state_db_conn, target or ""),
                        actor=f"runner-{requested_by}",
                    )
                    # Reset either way. On success the next cycle starts
                    # fresh; on a guard rejection the issue LEFT `resolving`
                    # mid-build, so the consecutive-green streak is void —
                    # leaving it parked at the threshold would let a future
                    # accept resolve on its very first green.
                    _reset_confirm_green_count(_state_db_conn, issue_key)
            else:
                # Red wipes the provisional greens: confirmation must be
                # consecutive, so a green/red/green sequence never resolves.
                _reset_confirm_green_count(_state_db_conn, issue_key)
                from dportsv3.tracker.routes.issue_actions import (  # noqa: PLC0415
                    reopen_issue_build_failed,
                )
                new_state = reopen_issue_build_failed(
                    _state_db_conn, issue_key, now=now,
                    actor=f"runner-{requested_by}", detail=verdict_detail,
                )
            _state_db_conn.commit()
    except Exception as exc:
        # Catch EVERYTHING, not just sqlite3.Error. This body also runs
        # deferred imports, json serialization (emit_event) and int() casts;
        # any of those raising would otherwise escape with the implicit
        # transaction still OPEN on the shared connection — the next writer's
        # commit would then publish this half-written verdict, and the next
        # `BEGIN IMMEDIATE` (lifecycle.apply) would fail with "cannot start a
        # transaction within a transaction".
        try:
            _state_db_conn.rollback()
        except Exception:
            pass
        log(queue_root, "WARN",
            f"failed to record confirm verdict for {issue_key}: "
            f"{type(exc).__name__}: {exc}")
        return "error"
    if terminal_attempted and new_state is None:
        # A terminal transition was attempted and the guard rejected it: the
        # issue was not in `resolving` (muted / reopened / already resolved
        # between enqueue and verdict). Verdict recorded, transition skipped.
        # NOT logged for a provisional green — that one deliberately stays in
        # `resolving` and is waiting on another build.
        log(queue_root, "INFO",
            f"confirm {'GREEN' if ok else 'RED'} for {issue_key} did not "
            f"transition: issue no longer in `resolving`")
    return "recorded"


def process_build_requests(queue_root: Path) -> None:
    """Reconcile build-confirmed resolution intent (C2).

    Level-triggered, mirroring :func:`process_verify_requests`: each pass
    RE-DERIVES which issues need a confirm build (``issues_needing_build``:
    ``state='resolving'`` AND ``requested_build_generation >
    last_confirmed_build_generation`` AND not already in flight) rather than
    draining a command queue — so a lost enqueue or a runner restart
    self-heals next loop.

    For each, enqueue one ``type=confirm`` job for the accepted fix
    (``delivery_bundle_id``) and stamp ``building_generation`` (single-flight).
    The runner stays the sole queue-fs writer; the tracker wrote only the
    C1 counter. 'env busy' needs no special handling here — the job simply
    waits in the queue behind the gate, and the in-flight marker keeps this
    loop from re-enqueuing it. (In fact the gate stops the loop before this
    runs at all, so a busy env can never cost an issue any retry budget.)

    An attempt that produces no verdict — whether the enqueue failed here or
    the build failed later — is counted and paced by
    :func:`_record_confirm_failure` (C4), so the feed backs off instead of
    re-deriving the same doomed work every pass.
    """
    if _state_db_conn is None:
        return
    from dportsv3.tracker.agentic_queries import issues_needing_build  # noqa: PLC0415
    try:
        with _state_db_lock:
            issues = issues_needing_build(_state_db_conn)
    except sqlite3.Error as exc:
        log(queue_root, "WARN", f"build-requests scan failed: {exc}")
        return
    for issue in issues:
        issue_key = issue.get("issue_key")
        bundle_id = issue.get("delivery_bundle_id")
        origin = issue.get("origin") or ""
        target = issue.get("target") or ""
        generation = issue.get("requested_build_generation")
        if not bundle_id:
            # resolving without a deliverable occurrence: nothing to build.
            # Leave it for an operator; don't spin.
            continue
        # Claim FIRST: the marker gates the enqueue, so a failed claim can
        # never leave an unmarked issue for the next pass to re-enqueue.
        if not _claim_issue_build(queue_root, issue_key, generation):
            continue
        try:
            job_path = enqueue_confirm_build_job(
                queue_root, issue_key=issue_key, bundle_id=bundle_id,
                origin=origin, target=target, generation=generation,
            )
        except Exception as exc:
            # An enqueue that fails is an attempt that produced no verdict,
            # exactly like a build that could not run — so it counts, paces
            # and eventually escalates through the same budget. It used to
            # release the claim silently, which left the feed re-deriving the
            # same work every pass forever: a WARN every few seconds, no
            # counter, and nothing that ever reached a human.
            log(queue_root, "WARN",
                f"confirm build for {issue_key} enqueue failed: {exc}")
            _record_confirm_failure(
                queue_root, issue_key,
                reason=f"enqueue failed: {type(exc).__name__}: {exc}",
            )
            continue
        activity_log(
            queue_root, "confirm_build_enqueued",
            f"confirm build for {issue_key} (gen {generation}) → {job_path.name}",
            job_id=job_path.name,
            extra={"issue_key": issue_key, "bundle_id": bundle_id,
                   "generation": generation},
        )


# Job states that count as "active work in progress for this origin"
# for the operator-retriage duplicate-enqueue guard
# (`_has_active_same_origin_job`). Canonical definition + the full
# rationale for why TRIAGED is excluded (and how this differs from
# the reap-orphans set) live in lifecycle.ACTIVE_WORK_STATES.
_ACTIVE_JOB_STATES = ACTIVE_WORK_STATE_VALUES


def _has_active_same_origin_job(run_id: str, origin: str) -> str | None:
    """Return the job_id of an active same-(run_id, origin) job, or None.

    "Active" matches the JobState values that aren't terminal — anything
    upstream of DONE/DEAD/ESCALATED. Used as a duplicate-enqueue guard
    on operator-triggered retries: we should not enqueue a new triage
    while one is already in flight for the same port in the same run.
    """
    if _state_db_conn is None:
        return None
    placeholders = ",".join("?" for _ in _ACTIVE_JOB_STATES)
    try:
        with _state_db_lock:
            row = _state_db_conn.execute(
                f"""SELECT job_id FROM jobs
                    WHERE origin = ? AND state IN ({placeholders})
                      AND (
                        ? = '' OR target IS NULL OR target = ''
                        OR target = (SELECT target FROM runs WHERE run_id = ?)
                      )
                    ORDER BY last_seen_at DESC LIMIT 1""",
                (origin, *_ACTIVE_JOB_STATES, run_id, run_id),
            ).fetchone()
    except Exception:
        return None
    return row["job_id"] if row else None


def process_user_context_updates(queue_root: Path):
    """Enqueue triage jobs when new user context is provided."""
    if _state_db_conn is None:
        return
    try:
        with _state_db_lock:
            rows = _state_db_conn.execute(
                """SELECT run_id, origin, bundle_id, iteration, max_iterations,
                          last_context_rev_handled
                   FROM user_context_requests WHERE status = 'pending'
                   ORDER BY requested_at ASC"""
            ).fetchall()
        for row in rows:
            run_id = row["run_id"]
            origin = row["origin"]
            last_handled = int(row["last_context_rev_handled"])
            context_text, context_rev = get_user_context(run_id, origin)
            if not context_text or context_rev <= last_handled:
                continue
            # Guard: don't enqueue a duplicate while same-origin work is
            # already in flight. The next sweep will re-evaluate; the
            # request row stays 'pending' so it isn't lost.
            blocker = _has_active_same_origin_job(run_id, origin)
            if blocker:
                activity_log(
                    queue_root, "retriage_blocked",
                    f"Skipping retriage for {origin}: active job {blocker}",
                    extra={"run_id": run_id, "origin": origin,
                           "blocking_job_id": blocker,
                           "context_rev": context_rev},
                )
                continue
            latest_bundle_id = find_latest_bundle_id(run_id, origin)
            if not latest_bundle_id:
                continue
            iteration = int(row["iteration"] or 1)
            max_iterations = int(row["max_iterations"] or DEFAULT_MAX_ITERATIONS)
            previous_bundle = row["bundle_id"]
            profile = get_run_profile(run_id)
            flavor = get_bundle_flavor(latest_bundle_id)
            job_path = enqueue_triage_job(
                queue_root, latest_bundle_id, run_id, origin,
                profile, flavor, iteration, max_iterations, previous_bundle, context_rev,
            )
            activity_log(queue_root, "retriage_enqueued",
                        f"Re-running triage for {origin} after user context",
                        job_id=job_path.name,
                        extra={"run_id": run_id, "origin": origin, "context_rev": context_rev})
            with _state_db_lock:
                _state_db_conn.execute(
                    """UPDATE user_context_requests
                       SET last_context_rev_handled = ?, status = 'retriage_enqueued'
                       WHERE run_id = ? AND origin = ? AND bundle_id = ?""",
                    (context_rev, run_id, origin, row["bundle_id"])
                )
                # Step 28c: clear the transient retry_requested
                # resolution on the originating bundle. Without this,
                # a bundle whose retry was honored would stay at
                # retry_requested forever — the new triage runs on
                # the same bundle and will set the resolution via
                # _EVENT_TO_RESOLUTION on its own terminal events,
                # but only if it reaches one. Clearing here also
                # serves as the observability signal "the runner
                # picked up the operator's request."
                _state_db_conn.execute(
                    """UPDATE bundles
                       SET resolution = NULL
                       WHERE bundle_id = ?
                         AND resolution = 'retry_requested'""",
                    (row["bundle_id"],),
                )
                _state_db_conn.commit()
    except Exception as e:
        print(f"Warning: Failed to process user context updates: {e}", file=sys.stderr)


def activity_log(
    queue_root: Path,
    stage: str,
    message: str,
    job_id: str | None = None,
    duration_ms: int | None = None,
    extra: dict | None = None
):
    """
    Log activity to state.db activity_log table.
    Also updates _current_stage for heartbeat.
    Keeps only last 10 entries.
    """
    global _current_stage
    _current_stage = stage
    
    # Also write to runner.log for backwards compatibility
    log(queue_root, "INFO", f"[{stage}] {message}")
    
    if _state_db_conn is None:
        return
    
    ts = datetime.now(timezone.utc).isoformat()
    extra_json = json.dumps(extra) if extra else None
    
    try:
        with _state_db_lock:
            _state_db_conn.execute(
                """INSERT INTO activity_log (ts, job_id, stage, message, duration_ms, extra_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ts, job_id, stage, message, duration_ms, extra_json)
            )
            
            # Prune to keep only last N entries (N comes from
            # DP_ACTIVITY_LOG_MAX, default 5000). The previous cap of
            # 10 made the UI's activity page useless and dropped
            # tool-call traces before the next page-refresh.
            cap = max(50, int(os.environ.get("DP_ACTIVITY_LOG_MAX", "5000")))
            _state_db_conn.execute(
                """DELETE FROM activity_log WHERE id NOT IN (
                     SELECT id FROM activity_log ORDER BY id DESC LIMIT ?
                   )""",
                (cap,),
            )
            
            _state_db_conn.commit()
    except Exception as e:
        print(f"Warning: Failed to write activity log: {e}", file=sys.stderr)


def update_runner_status(
    status: str,
    job_id: str | None = None,
    stage: str | None = None,
    extra: dict | None = None
):
    """Update runner_status table (singleton row)."""
    global _current_job_id, _current_stage
    
    _current_job_id = job_id
    if stage is not None:
        _current_stage = stage
    
    if _state_db_conn is None:
        return
    
    ts = datetime.now(timezone.utc).isoformat()
    extra_json = json.dumps(extra) if extra else None
    
    try:
        with _state_db_lock:
            # Upsert the singleton row
            _state_db_conn.execute(
                """INSERT INTO runner_status (id, status, job_id, current_stage, started_at, updated_at, extra_json)
                   VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     status = excluded.status,
                     job_id = excluded.job_id,
                     current_stage = excluded.current_stage,
                     started_at = CASE WHEN excluded.job_id != runner_status.job_id THEN excluded.started_at ELSE runner_status.started_at END,
                     updated_at = excluded.updated_at,
                     extra_json = excluded.extra_json""",
                (status, job_id, stage or _current_stage, ts, ts, extra_json)
            )
            _state_db_conn.commit()
    except Exception as e:
        print(f"Warning: Failed to update runner status: {e}", file=sys.stderr)


def _heartbeat_loop():
    """Background thread that updates runner_status.updated_at every 5 seconds."""
    while not _heartbeat_stop_event.is_set():
        if _state_db_conn is not None:
            try:
                ts = datetime.now(timezone.utc).isoformat()
                with _state_db_lock:
                    _state_db_conn.execute(
                        """UPDATE runner_status SET updated_at = ? WHERE id = 1""",
                        (ts,)
                    )
                    _state_db_conn.execute(
                        """UPDATE runners SET last_heartbeat_at = ?
                           WHERE runner_id = ?""",
                        (ts, runner_id()),
                    )
                    _state_db_conn.commit()
            except Exception:
                pass
        
        _heartbeat_stop_event.wait(HEARTBEAT_INTERVAL)


#: Name of the exclusive-startup lock, taken under ``queue_root``.
RUNNER_LOCK_NAME = "runner.lock"

#: Exit code when another runner already holds the lock. Distinct from 1 so
#: a supervisor can tell "already running" from a real startup failure.
EXIT_RUNNER_LOCKED = 3

#: Exit code when the dev-env store cannot be read. Distinct from 1 and 3
#: because the operator action is specific: run as root. See poly-i7y.
EXIT_ENV_UNREADABLE = 4

#: Held open for the process's lifetime. flock is released when the fd
#: closes, so dropping this reference would silently release the lock.
_runner_lock_file = None


def describe_lock_holder() -> str:
    """Best-effort description of whichever runner holds the lock.

    Reads ``runners`` for processes that never recorded a clean shutdown.
    A state.db lives on a local filesystem (sqlite WAL needs shared
    memory), so a competing runner is necessarily on this host and
    ``os.kill(pid, 0)`` settles liveness exactly — no heartbeat-staleness
    guessing. PermissionError means the pid exists but belongs to another
    user, which is still alive.
    """
    if _state_db_conn is None:
        return "another runner (state.db unavailable, cannot identify it)"
    try:
        with _state_db_lock:
            rows = _state_db_conn.execute(
                """SELECT runner_id, hostname, pid, started_at, last_heartbeat_at
                   FROM runners WHERE stopped_at IS NULL
                   ORDER BY last_heartbeat_at DESC""",
            ).fetchall()
    except Exception:
        return "another runner (could not read the runners table)"

    host = socket.gethostname()
    for row in rows:
        runner_id, hostname, pid, started_at, heartbeat = (
            row[0], row[1], row[2], row[3], row[4],
        )
        if hostname != host or not pid:
            continue
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            continue
        except (PermissionError, OSError):
            pass  # exists, just not ours to signal
        return (f"runner {runner_id} (pid {pid}, started {started_at}, "
                f"last heartbeat {heartbeat})")
    return "another runner (no live pid recorded; check for a stale process)"


def acquire_runner_lock(queue_root: Path) -> bool:
    """Take the exclusive runner lock, or report that we could not.

    Multi-runner is not supported. Every startup sweep below is global and
    unqualified — reap_orphans marks all inflight rows DEAD,
    _reset_building_markers discards queued confirm jobs — so a second
    runner corrupts the first's read model and opens the duplicate-enqueue
    guard. Refusing to start is what makes those sweeps sound: hold the
    lock and, by construction, every inflight row is genuinely an orphan.

    flock rather than a lockfile or a mkdir lock (dev-env's CacheLock):
    the kernel releases it when the process dies, so a SIGKILLed runner
    does not block every later restart. That failure mode is exactly the
    crash this lock exists to recover from.

    Scoped to ``queue_root`` because the filesystem queue is the source of
    truth for what processes next. Two runners pointed at different queue
    roots but the same DPORTSV3_STATE_DB would still both start and fight
    over the read model; that is not this lock's job.
    """
    global _runner_lock_file
    lock_path = queue_root / RUNNER_LOCK_NAME
    try:
        handle = open(lock_path, "w")
    except OSError as exc:
        print(f"error: cannot open runner lock {lock_path}: {exc}",
              file=sys.stderr)
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _runner_lock_file = handle
    return True


def release_runner_lock() -> None:
    """Drop the lock. Closing the fd is what releases it."""
    global _runner_lock_file
    if _runner_lock_file is None:
        return
    try:
        _runner_lock_file.close()
    except OSError:
        pass
    _runner_lock_file = None


def deregister_runner() -> None:
    """Stamp ``runners.stopped_at`` on clean shutdown. A crashed runner
    leaves it NULL with a stale heartbeat, which is the distinction that
    matters."""
    if _state_db_conn is None:
        return
    try:
        with _state_db_lock:
            _state_db_conn.execute(
                "UPDATE runners SET stopped_at = ? WHERE runner_id = ?",
                (datetime.now(timezone.utc).isoformat(), runner_id()),
            )
            _state_db_conn.commit()
    except Exception as exc:
        print(f"Warning: could not deregister runner: {exc}", file=sys.stderr)


def start_heartbeat():
    """Start the heartbeat thread."""
    global _heartbeat_thread
    
    if _heartbeat_thread is not None:
        return
    
    _heartbeat_stop_event.clear()
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    _heartbeat_thread.start()


def stop_heartbeat():
    """Stop the heartbeat thread."""
    global _heartbeat_thread
    
    _heartbeat_stop_event.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=2)
        _heartbeat_thread = None


def log(queue_root: Path, level: str, message: str):
    """Log to both stderr and runner.log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {level:5} {message}"
    print(line, file=sys.stderr)
    try:
        with open(queue_root / "runner.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_job_file(path: Path) -> dict:
    """Parse key=value job file into dict."""
    data = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                key, _, value = line.partition("=")
                data[key] = value
    return data


def read_file_if_exists(path: Path, max_bytes: int = 200_000) -> str | None:
    """Read file contents if it exists, truncate if too large."""
    if not path.exists():
        return None
    try:
        content = path.read_text(errors="replace")
        if len(content) > max_bytes:
            content = content[:max_bytes] + "\n[...truncated...]\n"
        return content
    except OSError:
        return None


# find_kedb_dir / load_kedb retired in Step 27b. The replacements
# live in dportsv3.agent.playbooks (find_playbooks_dir,
# load_playbooks) and are imported lazily at call sites to keep
# this module's import surface stable.
#
# Alpha-mode hard cutover: no compatibility shims. The 4 existing
# entries (error-*.md) were retrofitted with explicit triggers; the
# selector's flows-default ([triage, patch]) covers them anyway.


def queue_root_for_log(job: dict | None) -> Path | None:
    """Best-effort queue_root extraction for telemetry from any
    payload-building context. Returns None silently if absent (the
    activity_log call accepts None and downgrades to a no-op)."""
    if not job:
        return None
    qr = job.get("queue_root")
    return Path(qr) if qr else None


def _log_playbook_selection(queue_root, role, origin, selection,
                            job_id: str | None = None):
    """Emit a `playbooks_selected` activity row with included +
    skipped counts so operators see WHY their entry didn't fire.

    ``job_id`` is required for the row to appear under the per-job
    activity query (`tracker get-activity --job ID`). Rows written
    with ``job_id=None`` land in the table with a NULL job_id and
    are invisible to that query — silently swallowing the Step-27
    telemetry signal.

    Best-effort: any failure (no queue_root, write error) silently
    no-ops — telemetry must not break payload assembly.
    """
    if queue_root is None:
        return
    try:
        activity_log(
            queue_root, "playbooks_selected",
            (
                f"{origin or '?'}: role={role} "
                f"included={len(selection.included)} "
                f"skipped={len(selection.skipped)}"
            ),
            job_id=job_id,
            extra={
                "role": role,
                "origin": origin,
                "included": list(selection.included),
                "skipped_count": len(selection.skipped),
                # Cap skipped reasons to avoid bloating the log.
                "skipped_sample": [
                    {"file": f, "reason": r}
                    for f, r in selection.skipped[:8]
                ],
            },
        )
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Triage parsing
# -----------------------------------------------------------------------------
#
# Step 36-7 cutover: ``parse_triage_output`` (regex extraction of
# Classification / Confidence from ``analysis/triage.md``) is gone.
# The patch consumers (``build_patch_payload`` and
# ``steps.PatchAttemptStep.precheck``) now load the typed
# ``TriageResult`` via :func:`dportsv3.agent.phase_result.load_phase_result`
# directly from ``analysis/triage_result.json`` instead of regex-fishing
# fields out of prose.



def build_snippet_feedback(bundle_dir: Path, round_num: int) -> str:
    """Generate feedback section from snippet manifest for agent context."""
    manifest_path = bundle_dir / "analysis" / "snippets" / "manifest.json"
    if not manifest_path.exists():
        return ""
    
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception:
        return ""
    
    rounds = manifest.get("rounds", [])
    if not rounds:
        return ""
    
    # Find the latest round
    latest_round = None
    for r in rounds:
        if r.get("round") == round_num:
            latest_round = r
            break
    
    if not latest_round:
        # Use the last round
        latest_round = rounds[-1]
    
    parts = ["## Snippet Extraction Results", ""]
    parts.append(f"**Round {latest_round.get('round', '?')}** | Source: `{latest_round.get('source', 'unknown')}` | Budget remaining: {latest_round.get('budget_remaining', 0)} bytes")
    parts.append("")
    
    requests = latest_round.get("requests", [])
    if requests:
        parts.append("| Request | Status | Output | Bytes |")
        parts.append("|---------|--------|--------|-------|")
        for req in requests:
            raw = req.get("raw", "?")[:40]
            status = req.get("status", "?")
            output = req.get("output", "-")
            if output and len(output) > 30:
                output = "..." + output[-27:]
            bytes_ = req.get("bytes", 0)
            note = req.get("note", "")
            
            # Add emoji for status
            status_display = {
                "ok": "ok",
                "not_found": "not_found",
                "budget_exceeded": "budget_exceeded",
                "empty": "empty",
            }.get(status, status)
            
            parts.append(f"| `{raw}` | {status_display} | {output or '-'} | {bytes_} |")
            if note:
                parts.append(f"|  | *{note}* | | |")
    
    parts.append("")
    
    # Add summary
    total_rounds = manifest.get("total_rounds", 0)
    max_rounds = int(os.environ.get("DP_HARNESS_MAX_SNIPPET_ROUNDS", "5"))
    remaining_rounds = max_rounds - total_rounds
    
    parts.append(f"**Snippet rounds used:** {total_rounds}/{max_rounds} (remaining: {remaining_rounds})")
    if remaining_rounds <= 0:
        parts.append("**NOTE:** No more snippet rounds available. Work with the information provided.")
    parts.append("")
    
    return "\n".join(parts)


def load_snippets_content(bundle_dir: Path, round_num: int, max_bytes: int = 200_000) -> str:
    """Load extracted snippet contents for inclusion in payload."""
    round_dir = bundle_dir / "analysis" / "snippets" / f"round_{round_num}"
    if not round_dir.exists():
        return ""
    
    parts = ["## Extracted Snippets", ""]
    total_bytes = 0
    
    # Load round manifest for context
    manifest_path = round_dir / "manifest.json"
    if manifest_path.exists():
        try:
            with open(manifest_path) as f:
                round_manifest = json.load(f)
            source_type = round_manifest.get("source", "unknown")
            distfile = round_manifest.get("distfile")
            if distfile:
                parts.append(f"*Source: distfile `{distfile}`*")
            else:
                parts.append(f"*Source: {source_type}*")
            parts.append("")
        except Exception:
            pass
    
    # Walk through subdirectories (source, buildsystem, configure, log)
    for subdir in sorted(round_dir.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        
        for file_path in sorted(subdir.glob("*.txt")):
            if total_bytes >= max_bytes:
                parts.append(f"*[...truncated, budget exceeded...]*")
                break
            
            try:
                content = file_path.read_text(errors="replace")
                remaining = max_bytes - total_bytes
                if len(content) > remaining:
                    content = content[:remaining] + "\n[...truncated...]\n"
                
                # Infer original filename from safe name
                original_name = file_path.stem.replace("_", "/")
                
                parts.append(f"### {subdir.name}/{original_name}")
                parts.append("```")
                parts.append(content)
                parts.append("```")
                parts.append("")
                
                total_bytes += len(content)
            except Exception:
                continue
        
        if total_bytes >= max_bytes:
            break
    
    if total_bytes == 0:
        return ""
    
    return "\n".join(parts)


def build_triage_payload(
    bundle_dir: Path | None,
    playbooks_dir: Path | None = None,
    job: dict | None = None
) -> str:
    """Build the triage prompt from bundle contents.

    Phase 4: assembles via ``dportsv3.agent.context.render_payload``
    over the section roster in ``context.TRIAGE_SECTIONS``. Behavior
    is byte-equivalent to the pre-Phase-4 ``parts.append(...)`` form;
    parity is locked in by ``tests/test_triage_payload_parity.py``.
    """
    from dportsv3.agent.context import ContextCtx, TRIAGE_SECTIONS, render_payload

    job = job or {}
    bundle_id = job.get("bundle_id")
    run_id = job.get("run_id")
    origin = job.get("origin")

    # Pre-load fields sections need without doing I/O at render time.
    sibling_raw = job.get("sibling_bundle_ids", "") or ""
    sibling_ids = [s.strip() for s in sibling_raw.split(",") if s.strip()]

    prior_triage_ids: list[str] = []
    if origin:
        for entry in port_bundle_history(origin):
            bid = entry.get("bundle_id")
            if not bid or bid == bundle_id or bid in sibling_ids:
                continue
            prior_triage_ids.append(bid)
            if len(prior_triage_ids) >= 2:
                break

    user_context_text, _ = get_user_context(run_id, origin)
    # Step 29e: load every operator round so UserContextSection
    # can render history, not just the latest overwrite. The
    # current ``user_context_text`` is kept for the empty-history
    # fallback path (pre-29b submissions, test seeds).
    user_context_history = _load_operator_context_history(run_id, origin)
    # Triage runs BEFORE classification is known — we attach
    # entries that don't require a classification (the entry's
    # `triggers.classifications` is empty / wildcard) or whose
    # triggers don't depend on patch-flow context. Toolchain
    # detection (Step 19a / 27f) feeds `toolchains` so
    # toolchain-*.md playbooks fire for ports whose framework
    # Makefile carries recognizable signals (USES=, GNU_CONFIGURE=).
    from dportsv3.agent.playbooks import (  # noqa: PLC0415
        detect_toolchains, load_playbooks,
    )
    detected_toolchains = detect_toolchains(
        bundle_dir / "port" if bundle_dir else None,
    )
    playbook_selection = load_playbooks(
        playbooks_dir, role="triage", classification=None,
        toolchains=detected_toolchains,
    )
    _log_playbook_selection(queue_root_for_log(job), "triage", origin,
                            playbook_selection,
                            job_id=job.get("job_id"))

    ctx = ContextCtx(
        bundle_dir=bundle_dir,
        bundle_id=bundle_id,
        job=job,
        playbooks_dir=playbooks_dir,
        sibling_bundle_ids=sibling_ids,
        prior_triage_bundle_ids=prior_triage_ids,
        user_context_text=user_context_text or None,
        user_context_history=user_context_history,
        playbooks_text=playbook_selection.text or None,
        read_bundle_text=read_bundle_text,
        bundle_artifact_list=bundle_artifact_list,
        snippet_feedback=build_snippet_feedback,
        snippet_content=load_snippets_content,
    )
    return render_payload(list(TRIAGE_SECTIONS), ctx)


def build_patch_payload(
    bundle_dir: Path | None,
    playbooks_dir: Path | None = None,
    job: dict | None = None
) -> str:
    """Build the patch generation prompt including triage output.

    Phase 4: assembles via ``dportsv3.agent.context.render_payload``
    over the section roster in ``context.PATCH_SECTIONS``. Byte-
    equivalent to the pre-Phase-4 ``parts.append(...)`` form; parity
    locked in by ``tests/test_patch_payload_parity.py``.
    """
    from dportsv3.agent.context import ContextCtx, PATCH_SECTIONS, render_payload

    job = job or {}
    bundle_id = job.get("bundle_id")
    run_id = job.get("run_id")
    origin = job.get("origin")
    target = job.get("target", "") or ""

    # Pre-load sibling list + prior-patch bundle list.
    sibling_raw = job.get("sibling_bundle_ids", "") or ""
    sibling_ids = [s.strip() for s in sibling_raw.split(",") if s.strip()]

    prior_patch_ids: list[str] = []
    if origin:
        for entry in port_bundle_history(origin):
            bid = entry.get("bundle_id")
            if not bid or bid == bundle_id or bid in sibling_ids:
                continue
            prior_patch_ids.append(bid)
            if len(prior_patch_ids) >= 3:
                break

    # Automation-context inputs.
    window_hours = int(os.environ.get("DP_HARNESS_ATTEMPT_WINDOW_HOURS", "2"))
    max_attempts_cap = int(os.environ.get("DP_HARNESS_MAX_PATCH_ATTEMPTS", "3"))
    prior_failures = (
        _load_port_history(target, origin, window_hours).recent_failures
        if origin else 0
    )

    user_context_text, _ = get_user_context(run_id, origin)
    # Step 29e: same history wiring as triage — patch flow also
    # benefits from seeing every operator round, not just the
    # latest overwrite.
    user_context_history = _load_operator_context_history(run_id, origin)
    # Patch flow: classification is known from the prior triage in
    # this bundle. Step 36-5 reads it from the typed
    # ``TriageResult`` written by ``_write_triage_audit_harness``
    # rather than regex-parsing ``analysis/triage.md``.
    triage_classification: str | None = None
    try:
        from dportsv3.agent.phase_result import (  # noqa: PLC0415
            TriageResult, load_phase_result,
        )
        triage = load_phase_result(
            bundle_dir, bundle_id, "triage", TriageResult,
        )
        if triage is not None:
            triage_classification = triage.classification or None
    except Exception:
        # Best-effort: a stale on-disk shape (PhaseResultVersionMismatch)
        # or a missing artifact degrades to "no classification" — the
        # playbook selector falls back to its default set.
        triage_classification = None
    from dportsv3.agent.playbooks import (  # noqa: PLC0415
        detect_toolchains, load_playbooks,
    )
    detected_toolchains = detect_toolchains(
        bundle_dir / "port" if bundle_dir else None,
    )
    playbook_selection = load_playbooks(
        playbooks_dir, role="patch", classification=triage_classification,
        toolchains=detected_toolchains,
    )
    _log_playbook_selection(queue_root_for_log(job), "patch", origin,
                            playbook_selection,
                            job_id=job.get("job_id"))

    ctx = ContextCtx(
        bundle_dir=bundle_dir,
        bundle_id=bundle_id,
        job=job,
        playbooks_dir=playbooks_dir,
        sibling_bundle_ids=sibling_ids,
        prior_patch_bundle_ids=prior_patch_ids,
        user_context_text=user_context_text or None,
        user_context_history=user_context_history,
        playbooks_text=playbook_selection.text or None,
        prior_failure_count=prior_failures,
        window_hours=window_hours,
        max_attempts_cap=max_attempts_cap,
        read_bundle_text=read_bundle_text,
        bundle_artifact_list=bundle_artifact_list,
        snippet_feedback=build_snippet_feedback,
        snippet_content=load_snippets_content,
    )
    return render_payload(list(PATCH_SECTIONS), ctx)



# -----------------------------------------------------------------------------

def move_job(job_path: Path, dest_dir: str) -> Path:
    """Move job file to destination directory (done/failed).
    Also moves any associated .job.error file.
    """
    dest = job_path.parent.parent / dest_dir / job_path.name
    job_path.rename(dest)
    
    # Also move error file if it exists
    error_file = job_path.with_suffix(".job.error")
    if error_file.exists():
        error_dest = dest.with_suffix(".job.error")
        try:
            error_file.rename(error_dest)
        except OSError:
            pass  # Best effort
    
    return dest


def write_error_note(job_path: Path, error: str):
    """Write error note next to failed job."""
    error_path = job_path.with_suffix(".job.error")
    with open(error_path, "w") as f:
        f.write(f"timestamp={datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"error={error}\n")


def dsynth_active(env_name: str, queue_root: Path) -> tuple[bool, str]:
    """Check if a dsynth process is currently running on this host.

    pgrep is the only authority: it tells us if a dsynth process exists
    right now. Process existence is the only state that actually
    matters for "would a second dsynth invocation collide". The
    tracker's ``build_runs.finished_at`` can be stale (operator ^C'd
    dsynth, hook_run_end never ran, row left at NULL forever) and would
    deadlock the runner if we treated it as a gate.

    Returns ``(True, reason)`` when at least one dsynth process is
    seen, ``(False, "")`` otherwise. ``env_name`` is currently unused
    but kept in the signature for the future case where we'd scope
    pgrep to processes inside a specific chroot.

    On systems without pgrep (none in scope today — dfly has it in
    base), this fails open: returns ``(False, "")`` and the runner
    proceeds without a gate.
    """
    # -x matches process basename exactly so we don't false-positive on
    # cmdlines that merely contain "dsynth" (queue paths, log paths, etc.).
    try:
        result = subprocess.run(
            ["pgrep", "-x", "dsynth"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().splitlines()
            return True, f"dsynth process(es) on host: {','.join(pids)}"
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass
    return False, ""


def _job_dedup_key(meta: dict) -> tuple | None:
    """Key for grouping pending jobs that should be processed together.

    Jobs are siblings when they share (type, profile, origin, flavor).
    Returning ``None`` means "can't group this job" — claim it alone.
    """
    jt = meta.get("type")
    profile = meta.get("profile")
    origin = meta.get("origin")
    if not jt or not origin or not profile:
        return None
    return (jt, profile, origin, meta.get("flavor", ""))


def claim_next_job_batch(queue_root: Path) -> tuple[Path, list[Path]] | None:
    """Claim the oldest pending job and any sibling pending jobs.

    Siblings = pending jobs with the same (type, profile, origin,
    flavor) as the lead. All move to inflight/. The lead is the file
    the runner processes; sibling evidence is folded into the payload
    via ``job["sibling_bundle_ids"]``. All members move together to
    done/failed on completion.

    Returns ``(lead_path, sibling_paths)`` or ``None`` if nothing pending.
    """
    pending_dir = queue_root / "pending"
    inflight_dir = queue_root / "inflight"
    jobs = sorted(pending_dir.glob("*.job"))

    for lead_path in jobs:
        try:
            lead_meta = parse_job_file(lead_path)
        except Exception:
            continue
        lead_key = _job_dedup_key(lead_meta)
        candidate_siblings: list[Path] = []
        if lead_key is not None:
            for other in jobs:
                if other == lead_path:
                    continue
                try:
                    other_meta = parse_job_file(other)
                except Exception:
                    continue
                if _job_dedup_key(other_meta) == lead_key:
                    candidate_siblings.append(other)
        # Move lead first; if that races, try the next pending job.
        try:
            lead_dest = inflight_dir / lead_path.name
            lead_path.rename(lead_dest)
        except OSError:
            continue
        from dportsv3.agent.lifecycle import JobEvent
        _apply_transition(lead_dest.name, JobEvent.CLAIM)
        moved_siblings: list[Path] = []
        for s in candidate_siblings:
            try:
                s_dest = inflight_dir / s.name
                s.rename(s_dest)
                moved_siblings.append(s_dest)
            except OSError:
                # Another runner grabbed it or it vanished — skip.
                continue
            _apply_transition(s_dest.name, JobEvent.CLAIM)
        return lead_dest, moved_siblings
    return None


def enqueue_patch_job(
    queue_root: Path,
    job: dict,
    *,
    tier_name: str | None = None,
    dev_env: str | None = None,
):
    """Enqueue a patch job based on completed triage job.

    ``tier_name`` is the trust tier resolved at triage time (AUTO/ASSIST);
    propagating it lets the patch worker use the right budget without
    re-parsing triage.md. ``dev_env`` is the dev-env name the patch
    flow should operate against; omit to let env_resolver pick
    (tracker active env → --env CLI flag → auto-pick).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    origin_safe = job.get("origin", "unknown").replace("/", "_")
    pid = os.getpid()

    job_name = f"{ts}-{job.get('profile', 'unknown')}-{origin_safe}-{pid}-patch.job"

    pending_dir = queue_root / "pending"
    job_path = pending_dir / job_name

    # Inherit iteration from parent job, or start at 1
    iteration = int(job.get("iteration", "1"))
    max_iterations = int(job.get("max_iterations", str(DEFAULT_MAX_ITERATIONS)))

    bundle_id = job.get("bundle_id", "")
    target = (
        job.get("target")
        or _lookup_bundle_target(bundle_id)
        or os.environ.get("DPORTSV3_TRACKER_TARGET", "")
    )

    content = [
        f"type=patch",
        f"created_ts_utc={ts}",
        f"profile={job.get('profile', '')}",
        f"target={target}",
        f"origin={job.get('origin', '')}",
        f"flavor={job.get('flavor', '')}",
        f"bundle_id={bundle_id}",
        f"run_id={job.get('run_id', '')}",
        f"triage_relpath=analysis/triage.md",
        f"iteration={iteration}",
        f"max_iterations={max_iterations}",
    ]
    if tier_name:
        content.append(f"tier={tier_name}")
    if dev_env:
        content.append(f"dev_env={dev_env}")

    # Include previous_bundle if this is a retry
    previous_bundle = job.get("previous_bundle")
    if previous_bundle:
        content.append(f"previous_bundle={previous_bundle}")

    # Atomic write
    tmp_path = job_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        f.write("\n".join(content) + "\n")
    tmp_path.rename(job_path)

    _register_new_job(
        job_path.name,
        metadata={
            "type": "patch",
            "origin": job.get("origin", ""),
            "flavor": job.get("flavor", ""),
            "created_ts_utc": ts,
            "path": str(job_path),
            "target": target,
            "bundle_id": bundle_id,
        },
    )
    return job_path


def enqueue_verify_job(
    queue_root: Path,
    *,
    bundle_id: str,
    origin: str,
    target: str,
    env: str,
    requested_by: str = "operator",
) -> Path:
    """Enqueue a fix-verification job (Step 11c).

    Verify jobs are operator-triggered: the bundle UI's Verify
    button POSTs to a tracker endpoint which calls this. The
    runner's dispatch arm picks the job up and calls
    ``dportsv3.verify_fix.run_verify_fix`` in-process — no
    subprocess, no shell-out.

    Carries ``bundle_id`` so the in-process call has what it
    needs; ``env`` is the operator-chosen dev-env name.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    origin_safe = origin.replace("/", "_")
    pid = os.getpid()
    job_name = f"{ts}-{origin_safe}-{pid}-verify.job"

    pending_dir = queue_root / "pending"
    job_path = pending_dir / job_name

    content = [
        "type=verify",
        f"created_ts_utc={ts}",
        f"bundle_id={bundle_id}",
        f"origin={origin}",
        f"target={target}",
        f"dev_env={env}",
        f"requested_by={requested_by}",
    ]

    tmp_path = job_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        f.write("\n".join(content) + "\n")
    tmp_path.rename(job_path)

    _register_new_job(
        job_path.name,
        metadata={
            "type": "verify",
            "origin": origin,
            "flavor": "",
            "created_ts_utc": ts,
            "path": str(job_path),
            "target": target,
            "bundle_id": bundle_id,
        },
    )
    return job_path


def enqueue_confirm_build_job(
    queue_root: Path,
    *,
    issue_key: str,
    bundle_id: str,
    origin: str,
    target: str,
    generation: int,
    requested_by: str = "reconcile",
) -> Path:
    """Enqueue a confirm-build job (C2).

    The build-confirmed resolution loop (:func:`process_build_requests`)
    calls this when an issue's ``requested_build_generation`` is ahead of
    ``last_confirmed_build_generation``. Mirrors :func:`enqueue_verify_job`:
    the runner's dispatch arm picks the ``type=confirm`` job up and builds
    the accepted fix for ``(target, origin)``, then reports a green/red
    verdict tied back to ``(issue_key, generation)``.

    ``dev_env`` is deliberately omitted — a confirm build is not
    operator-triggered, so the env is resolved at dispatch time
    (``resolve_env``), exactly as auto triage/patch jobs do. ``generation``
    is carried so the verdict can be matched to the exact C1 intent.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    origin_safe = origin.replace("/", "_")
    pid = os.getpid()
    job_name = f"{ts}-{origin_safe}-{pid}-confirm.job"

    pending_dir = queue_root / "pending"
    job_path = pending_dir / job_name

    content = [
        "type=confirm",
        f"created_ts_utc={ts}",
        f"issue_key={issue_key}",
        f"bundle_id={bundle_id}",
        f"origin={origin}",
        f"target={target}",
        f"requested_build_generation={generation}",
        f"requested_by={requested_by}",
    ]

    tmp_path = job_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        f.write("\n".join(content) + "\n")
    tmp_path.rename(job_path)

    _register_new_job(
        job_path.name,
        metadata={
            "type": "confirm",
            "origin": origin,
            "flavor": "",
            "created_ts_utc": ts,
            "path": str(job_path),
            "target": target,
            "bundle_id": bundle_id,
        },
    )
    return job_path



# -----------------------------------------------------------------------------
# Job processing
# -----------------------------------------------------------------------------

def _write_triage_audit_harness(
    bundle_dir: Path | None,
    bundle_id: str | None,
    result,  # dportsv3.agent.triage.TriageResult
    model: str,
) -> None:
    """Step 36-2: write the typed ``TriageResult`` to the bundle.

    The markdown response is already on disk (``triage.run`` writes
    ``analysis/triage.md`` after each LLM round). This adds the
    canonical typed result at ``analysis/triage_result.json`` —
    classification + confidence + root_cause + evidence_excerpt +
    error_signature + tier + token spend + model.

    Replaces the pre-Step-36 ``analysis/triage.json`` audit shape; the
    one downstream consumer (``proposed_fix.build_proposed_fix_ctx``)
    is updated to read the new relpath in the same step.
    """
    from dataclasses import asdict  # noqa: PLC0415
    from dportsv3.agent.markdown import md_section  # noqa: PLC0415
    from dportsv3.agent.phase_result import (  # noqa: PLC0415
        TriageResult, write_phase_result,
    )
    from dportsv3.agent.policy import (  # noqa: PLC0415
        load_policy, tier_for as policy_tier_for,
    )

    # Re-read the markdown the agent just wrote to lift Root Cause +
    # Evidence into the typed result. Same source-of-truth the
    # delivery PR-body builder uses (and the same md_section helper),
    # so they stay in lockstep on prose-section conventions.
    triage_md = read_bundle_text(
        bundle_dir, bundle_id, "analysis/triage.md",
    ) or ""
    errors_text = read_bundle_text(
        bundle_dir, bundle_id, "logs/errors.txt",
    )

    tier_name = "MANUAL"
    try:
        policy_path = _policy_path()
        pol = load_policy(policy_path)
        tier_name = policy_tier_for(
            pol, result.classification, result.confidence,
        ).name
    except Exception:
        # Tier resolution is a derived field; persisting MANUAL on
        # failure is a safe default (operator-only) and keeps the
        # write path side-effect-free.
        pass

    triage_result = TriageResult(
        classification=result.classification,
        confidence=result.confidence,
        root_cause=md_section(triage_md, "Root Cause", max_chars=2000),
        evidence_excerpt=md_section(
            triage_md, "Evidence", max_chars=2000,
        ),
        error_signature=_compute_error_signature(errors_text),
        tier=tier_name,
        classifier_version="triage-v1",
        tokens_prompt=result.usage.prompt_tokens,
        tokens_completion=result.usage.completion_tokens,
        tokens_total=result.usage.total_tokens,
        model=model,
    )

    if bundle_id:
        write_phase_result(bundle_id, "triage", triage_result)
        return
    # bundle_dir-only fallback (legacy / offline-test path). The
    # phase_result write helper only routes through the artifact
    # store; for the dir-only mode we serialize directly to disk
    # using the same shape so future loads off the dir would match.
    if bundle_dir is None:
        raise RuntimeError("bundle_dir or bundle_id required")
    data = (
        json.dumps(asdict(triage_result), indent=2) + "\n"
    ).encode("utf-8")
    out = bundle_dir / "analysis" / "triage_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)


def _finish_orchestrator_run(
    result,                         # OrchestratorResult
    *,
    step_name: str,
    sibling_paths: list[Path],
    failure_event: str,             # JobEvent value to fire on halt
) -> tuple[bool, str]:
    """Extract (success, status_str) from an OrchestratorResult and
    fan the step's lifecycle events out to sibling jobs.

    The orchestrator already fired the step's outcome events for the
    lead job_id. For siblings, we replay the same events here. When
    the orchestrator halted (precheck-fail, run-exception), no events
    fired for the lead either — synthesize ``failure_event`` and fire
    it for lead + siblings.
    """
    from dportsv3.agent.lifecycle import JobEvent
    from dportsv3.agent.step import outcome_events

    def _sibling_detail(s: Path, base_detail: dict) -> dict:
        """Per-sibling detail dict carrying its OWN bundle_id so the
        resolution propagates to the right bundles row."""
        out = dict(base_detail) if base_detail else {}
        try:
            out["bundle_id"] = parse_job_file(s).get("bundle_id", "") or None
        except Exception:
            pass
        return out

    if result.halted:
        # Normal orchestrator halts happen before outcome events are
        # fired. If a future step shape returns an outcome while halting,
        # mirror those events instead of synthesizing a second failure.
        step_result = result.step_by_name(step_name)
        events = outcome_events(step_result.outcome if step_result else None)
        if events:
            detail = (step_result.outcome.detail if step_result and step_result.outcome
                      else {"reason": result.halt_reason})
            for s in sibling_paths:
                sd = _sibling_detail(s, detail)
                for evt in events:
                    _apply_transition(s.name, evt, detail=sd)
            status_str = detail.get("status_str", result.halt_reason)
            return False, status_str or f"{step_name} halted"

        # Lead got no events from the orchestrator; synthesize the
        # catchall failure event.
        evt = JobEvent(failure_event)
        _apply_transition(result.job_id, evt,
                          detail={"reason": result.halt_reason})
        for s in sibling_paths:
            _apply_transition(s.name, evt,
                              detail={"reason": result.halt_reason})
        return False, result.halt_reason or f"{step_name} halted"

    step_result = result.step_by_name(step_name)
    if step_result is None or step_result.outcome is None:
        reason = (step_result.readiness.reason
                  if step_result else f"{step_name} skipped")
        evt = JobEvent(failure_event)
        _apply_transition(result.job_id, evt, detail={"reason": reason})
        for s in sibling_paths:
            _apply_transition(s.name, evt,
                              detail=_sibling_detail(s, {"reason": reason}))
        return False, reason

    outcome = step_result.outcome
    # Sibling fan-out: same events the orchestrator fired for the lead.
    sibling_events = outcome_events(outcome)
    detail = outcome.detail or {}
    for s in sibling_paths:
        sd = _sibling_detail(s, detail)
        for evt in sibling_events:
            _apply_transition(s.name, evt, detail=sd)

    status_str = outcome.detail.get("status_str", "unknown")
    if outcome.status == "failed":
        return False, status_str
    return True, status_str


def _drop_bundle_branch_for_job(
    *,
    queue_root: Path,
    job_id: str,
    env: str | None,
    bundle_id: str | None,
    job_type: str,
    reason: str,
) -> None:
    """Step 30 slice 4: drop the bundle's branch at terminal job
    end. Called from the dispatch after the job's
    success/failure verdict is known.

    Lifecycle: convert success keeps the branch (the next patch
    job for the same bundle_id will reuse it). Convert failure,
    patch end (either outcome), and verify end all drop. The
    delivery.diff (slice 2) has already been captured by the time
    we reach the drop, so the branch's purpose is done.

    Bundles that never run a follow-up job (operator-fired
    convert, MANUAL-escalated retriage) keep their branch until
    the env itself is rebuilt — the "stale branch" case is
    explicitly out of scope per the design discussion.

    Soft-fail: drop failures are logged but never propagate. The
    next bundle's branch creation will tolerate the existence of
    a leftover branch (Slice 1 ``checkout_bundle_branch`` uses
    ``checkout`` on an existing branch).
    """
    if not env or not bundle_id:
        return
    from dportsv3.agent import worker  # noqa: PLC0415
    try:
        # Keep the branch: a bundle's convert->patch chain shares it, and a
        # later job in the chain still wants its commits. Only the worktree
        # goes.
        result = worker.destroy_job_worktree(env, bundle_id, "patch")
    except Exception as exc:
        try:
            activity_log(
                queue_root, "bundle_branch_drop_failed",
                f"{job_type} {job_id}: drop raised: {exc!s}"[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "reason": reason,
                       "exception": str(exc)[:300]},
            )
        except Exception:
            pass
        return
    if not result.get("ok"):
        try:
            activity_log(
                queue_root, "bundle_branch_drop_failed",
                (f"{job_type} {job_id}: "
                 f"{result.get('error') or 'drop failed'}")[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "reason": reason,
                       "branch": result.get("branch")},
            )
        except Exception:
            pass
        return
    if result.get("removed"):
        try:
            activity_log(
                queue_root, "bundle_branch_dropped",
                f"{job_type} {job_id}: dropped {result.get('branch')} "
                f"({reason})",
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "branch": result.get("branch"),
                       "reason": reason},
            )
        except Exception:
            pass


def _checkout_bundle_branch_for_job(
    *,
    queue_root: Path,
    job_id: str,
    env: str | None,
    bundle_id: str | None,
    job_type: str,
) -> bool:
    """Give the job its own worktree of the ports tree before any
    ``worker.*`` call touches the substrate. Returns whether it has one.

    HARD-FAIL. This was soft-fail until B1, on the reasoning that a
    failed checkout only cost isolation — the job would fall back to
    "commits land on whatever branch is current", and bundles that had
    already passed triage would still run. That reasoning died with B1.
    The fallback is no longer a shared tree sitting on some branch:
    ``create_job_worktree`` returns before repointing the link when
    ``worktree add`` fails, so the job runs against whatever
    ``PORTS_DIR`` last pointed at — after a crashed predecessor, that is
    another job's worktree, and the commits land on that job's branch.

    There is no safe tree to fall back to. Pointing the link at
    ``ports-main`` is worse rather than better: worktrees are cut from a
    local branch name (``_resolve_bundle_base_branch``), so commits
    landing there poison the base every future worktree branches from.

    So: no worktree, no job. A job retired ``worktree_unavailable`` can
    be re-enqueued from its bundle; silent cross-job contamination
    cannot be detected at all.

    Returns True as a no-op when env or bundle_id is missing (e.g.
    triage jobs that don't touch the substrate).
    """
    if not env or not bundle_id:
        return True
    from dportsv3.agent import worker  # noqa: PLC0415
    try:
        result = worker.create_job_worktree(env, bundle_id, "patch")
    except Exception as exc:
        try:
            activity_log(
                queue_root, "bundle_branch_checkout_failed",
                f"{job_type} {job_id}: checkout raised: {exc!s}"[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "exception": str(exc)[:300]},
            )
        except Exception:
            pass
        return False
    if not result.get("ok"):
        try:
            activity_log(
                queue_root, "bundle_branch_checkout_failed",
                (f"{job_type} {job_id}: "
                 f"{result.get('error') or 'checkout failed'}")[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "branch": result.get("branch"),
                       "base": result.get("base"),
                       "stderr_tail": result.get("stderr_tail", "")[:300]},
            )
        except Exception:
            pass
        return False
    # Success row. Only emit when we actually did work (created
    # the branch, or switched onto an existing one) — the "already
    # current" no-op path would otherwise flood the activity log on
    # the convert → retriage → patch chain.
    if result.get("created") or not result.get("reused"):
        try:
            activity_log(
                queue_root, "bundle_branch_checkout",
                (f"{job_type} {job_id}: "
                 f"{'created' if result.get('created') else 'switched to'} "
                 f"{result.get('branch')} (base={result.get('base')})"),
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "branch": result.get("branch"),
                       "base": result.get("base"),
                       "created": bool(result.get("created"))},
            )
        except Exception:
            pass
    return True


def _checkout_verify_branch_for_job(
    *,
    queue_root: Path,
    job_id: str,
    env: str | None,
    bundle_id: str | None,
) -> tuple[bool, str | None]:
    """Put the env on a throwaway ``bundle/<id>-verify`` branch cut
    from base for the verify run.

    Verify uses its own branch rather than the patch agent's
    ``bundle/<id>``: changes.diff is the complete canonical artifact,
    and the patch branch may already have been dropped by Slice 4's
    terminal sweep.

    Returns ``(ok, previous_ref)``. Unlike the convert/patch checkout
    helper this is NOT soft-fail: a failed checkout returns
    ``ok=False`` and the caller MUST abort verify rather than replay.
    Verify's entire contract is "does the diff apply cleanly on a
    fresh base" — if we couldn't establish that base, any verdict is
    meaningless and replaying onto whatever branch is current would
    both lie and let cleanup reset the wrong tree. ``previous_ref`` is
    the ref the env was on before (for the end-of-run drop), valid
    only when ``ok``.
    """
    if not env or not bundle_id:
        # A real verify job always carries both; missing them means a
        # malformed job we can't verify — treat as checkout failure.
        return (False, None)
    from dportsv3.agent import worker  # noqa: PLC0415
    try:
        result = worker.create_job_worktree(env, bundle_id, "verify")
    except Exception as exc:
        try:
            activity_log(
                queue_root, "verify_branch_checkout_failed",
                f"verify {job_id}: checkout raised: {exc!s}"[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "exception": str(exc)[:300]},
            )
        except Exception:
            pass
        return (False, None)
    if not result.get("ok"):
        try:
            activity_log(
                queue_root, "verify_branch_checkout_failed",
                (f"verify {job_id}: "
                 f"{result.get('error') or 'checkout failed'}")[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "branch": result.get("branch"),
                       "base": result.get("base"),
                       "stderr_tail": result.get("stderr_tail", "")[:300]},
            )
        except Exception:
            pass
        return (False, None)
    try:
        activity_log(
            queue_root, "verify_branch_checkout",
            (f"verify {job_id}: created {result.get('branch')} "
             f"(base={result.get('base')}, "
             f"prev={result.get('previous_ref')})"),
            job_id=job_id,
            extra={"bundle_id": bundle_id, "env": env,
                   "branch": result.get("branch"),
                   "base": result.get("base"),
                   "previous_ref": result.get("previous_ref")},
        )
    except Exception:
        pass
    # No previous_ref: nothing was moved off a shared checkout, so there is
    # nothing to restore. Kept in the tuple for the caller's shape until B2.
    return (True, None)


def _drop_verify_branch_for_job(
    *,
    queue_root: Path,
    job_id: str,
    env: str | None,
    bundle_id: str | None,
    restore_ref: str | None,
    reason: str,
) -> None:
    """Delete the verify run's ``bundle/<id>-verify`` branch and
    restore the ref the env was on before verify started
    (``restore_ref`` from :func:`_checkout_verify_branch_for_job`).
    Soft-fail; mirrors :func:`_drop_bundle_branch_for_job`."""
    if not env or not bundle_id:
        return
    from dportsv3.agent import worker  # noqa: PLC0415
    try:
        # restore_ref is vestigial: it named the ref to put the shared
        # checkout back on, and there is no shared checkout to restore now —
        # the worktree is thrown away instead. B2 removes the parameter.
        result = worker.destroy_job_worktree(
            env, bundle_id, "verify", drop_branch=True,
        )
    except Exception as exc:
        try:
            activity_log(
                queue_root, "verify_branch_drop_failed",
                f"verify {job_id}: drop raised: {exc!s}"[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "reason": reason, "exception": str(exc)[:300]},
            )
        except Exception:
            pass
        return
    if not result.get("ok"):
        try:
            activity_log(
                queue_root, "verify_branch_drop_failed",
                (f"verify {job_id}: "
                 f"{result.get('error') or 'drop failed'}")[:240],
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "reason": reason,
                       "branch": result.get("branch")},
            )
        except Exception:
            pass
        return
    if result.get("removed"):
        try:
            activity_log(
                queue_root, "verify_branch_dropped",
                (f"verify {job_id}: dropped {result.get('branch')} "
                 f"(restored {result.get('restored_to')}, {reason})"),
                job_id=job_id,
                extra={"bundle_id": bundle_id, "env": env,
                       "branch": result.get("branch"),
                       "restored_to": result.get("restored_to"),
                       "reason": reason},
            )
        except Exception:
            pass


def _maybe_skip_locked_origin(
    *,
    queue_root: Path,
    job: dict,
    job_id: str,
    sibling_paths: list[Path] | None,
    origin: str,
    job_type: str = "triage",
) -> tuple[bool, str] | None:
    """Step 28a / 28-extra: short-circuit a job when the operator
    has staked (target, origin) via take-over.

    Returns None when the origin is not locked (job proceeds
    normally), or a ``(True, status)`` tuple to retire the job
    immediately. Idempotent: a second job for a still-locked
    origin produces the same short-circuit + a fresh activity row
    so each bypass is observable. Best-effort lookup — DB errors
    don't block the job, just log and proceed.

    ``job_type`` ("triage" / "patch" / "convert") selects the
    activity-log stage name so a per-job-type filter on the UI
    can distinguish the three bypass paths. The lifecycle event
    (``SKIP_ORIGIN_LOCKED``) is shared across all three.
    """
    from dportsv3.agent.lifecycle import JobEvent

    if _state_db_conn is None:
        return None
    target = job.get("target") or os.environ.get(
        "DPORTSV3_TRACKER_TARGET", "",
    )
    if not target:
        return None

    try:
        with _state_db_lock:
            from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
                is_origin_skipped,
            )
            lock = is_origin_skipped(_state_db_conn, target, origin)
    except sqlite3.Error as exc:
        log(queue_root, "WARN",
            f"skip-lock lookup failed for {origin}: {exc}; proceeding")
        return None

    if lock is None:
        return None

    locked_bundle = lock.get("bundle_id") or "?"
    set_by = lock.get("set_by") or "?"
    reason = lock.get("reason") or ""
    activity_log(
        queue_root, f"{job_type}_skipped_origin_locked",
        (
            f"{origin}: origin locked by bundle {locked_bundle} "
            f"({set_by}); {job_type} skipped"
        ),
        job_id=job_id,
        extra={
            "origin": origin,
            "target": target,
            "locking_bundle_id": locked_bundle,
            "locked_by": set_by,
            "lock_reason": reason,
            "job_type": job_type,
        },
    )
    # Retire the job DEAD with retire_reason='origin_locked'. The
    # dedicated SKIP_ORIGIN_LOCKED event keeps this case separable
    # from Step 10b's ABANDON (operator hand-killed) in lineage
    # views and the manual queue. Fan out to siblings so a multi-
    # job fanout doesn't leave parallel jobs stuck after this lead
    # bypasses.
    detail = {
        "skipped_because": "origin_locked",
        "locking_bundle_id": locked_bundle,
        "locked_by": set_by,
        "lock_reason": reason,
        "origin": origin,
        "target": target,
        "job_type": job_type,
    }
    _apply_transition(job_id, JobEvent.SKIP_ORIGIN_LOCKED, detail=detail)
    for s in sibling_paths or ():
        _apply_transition(s.name, JobEvent.SKIP_ORIGIN_LOCKED, detail=detail)
    return True, f"origin_locked_by:{locked_bundle}"


def _maybe_skip_muted_issue(
    *,
    queue_root: Path,
    job: dict,
    job_id: str,
    sibling_paths: list[Path] | None,
    origin: str,
    job_type: str = "triage",
) -> tuple[bool, str] | None:
    """WS8: short-circuit a job when the operator has muted its issue.

    Muting an issue means "stop bugging me AND stop spending agent budget
    on it." This is the second half — the runner skips auto-work for a
    muted issue's occurrences, mirroring the origin-lock skip: retire the
    job DEAD with retire_reason='issue_muted', fan out to siblings, log
    the bypass. The bundle row and the issue's rollups (bumped at ingest)
    are left alone — only the triage/patch path short-circuits.

    Returns None to proceed, or ``(True, status)`` to retire immediately.
    Best-effort lookup — DB errors don't block the job.
    """
    from dportsv3.agent.lifecycle import JobEvent

    if _state_db_conn is None:
        return None
    bundle_id = job.get("bundle_id")
    if not bundle_id:
        return None

    try:
        with _state_db_lock:
            from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
                issue_for_bundle,
            )
            issue = issue_for_bundle(_state_db_conn, bundle_id)
    except sqlite3.Error as exc:
        log(queue_root, "WARN",
            f"mute lookup failed for {bundle_id}: {exc}; proceeding")
        return None

    if issue is None or issue.get("state") != "muted":
        return None

    issue_key = issue.get("issue_key") or "?"
    muted_by = issue.get("muted_by") or "?"
    activity_log(
        queue_root, f"{job_type}_skipped_issue_muted",
        f"{origin}: issue {issue_key} muted by {muted_by}; {job_type} skipped",
        job_id=job_id,
        extra={
            "origin": origin,
            "issue_key": issue_key,
            "muted_by": muted_by,
            "bundle_id": bundle_id,
            "job_type": job_type,
        },
    )
    detail = {
        "skipped_because": "issue_muted",
        "issue_key": issue_key,
        "muted_by": muted_by,
        "bundle_id": bundle_id,
        "origin": origin,
        "job_type": job_type,
    }
    _apply_transition(job_id, JobEvent.SKIP_ISSUE_MUTED, detail=detail)
    for s in sibling_paths or ():
        _apply_transition(s.name, JobEvent.SKIP_ISSUE_MUTED, detail=detail)
    return True, f"issue_muted:{issue_key}"


_BOOTSTRAP_REASON = (
    "Step 48 bootstrap: deterministic header; patch authors the body"
)


def _read_status_type(env_name: str, origin: str) -> str | None:
    """Lowercased STATUS token (port/dport/mask/lock) for a port in the
    dev-env, or None when STATUS is absent/empty."""
    from dportsv3.agent import worker
    try:
        p = worker._exec(
            env_name, "/bin/sh", "-c",
            'head -1 "$DELTAPORTS_ROOT/ports/$1/STATUS" 2>/dev/null',
            "_", origin,
        )
    except Exception:
        return None
    token = (p.stdout or "").strip().split()[:1]
    if not token:
        return None
    lowered = token[0].lower()
    return lowered if lowered in {"port", "dport", "mask", "lock"} else None


def _ensure_overlay_or_abort(
    *,
    queue_root: Path,
    job: dict,
    job_path: Path,
    origin: str,
) -> tuple[str, str] | None:
    """Step 48 cutover (Phase B): replace defer-to-convert with a
    deterministic bootstrap-or-abort at a build failure.

    Returns ``None`` to let triage proceed to patch (the port already has
    an ``overlay.dops``, or one was just bootstrapped), or
    ``("abort", reason)`` to route the triage to a manual handoff (the port
    carries non-dport compat artifacts that need a real conversion).

    The convert *agent* is gone: bootstrapping a header overlay is
    deterministic, and the patch agent authors the body. See
    [[project-convert-is-substrate-prerequisite]] (retired).
    """
    from dportsv3.agent import worker
    from dportsv3.agent.overlay_state import bootstrap_decision

    env_resolution = resolve_env_or_reason(job)
    env_name = env_resolution.env
    if not env_name:
        log(queue_root, "WARN",
            f"no dev-env resolved for {origin!r} "
            f"({env_resolution.refusal_reason}); proceeding with triage")
        return None

    try:
        facts = worker.probe_overlay_facts(env_name, origin)
    except Exception as exc:
        log(queue_root, "WARN",
            f"probe_overlay_facts({origin!r}) failed: {exc}; proceeding")
        return None

    status_type = _read_status_type(env_name, origin)
    decision = bootstrap_decision(facts, status_type)

    if decision.action == "proceed":
        return None

    if decision.action == "abort":
        try:
            activity_log(
                queue_root, "triage_compat_abort",
                (
                    f"{origin}: non-dport compat artifacts need offline "
                    f"conversion; escalating to manual ({decision.reason})"
                ),
                job_id=job_path.name,
                extra={"origin": origin, "reason": decision.reason},
            )
        except Exception as exc:
            log(queue_root, "WARN", f"activity_log failed in compat-abort: {exc}")
        return "abort", decision.reason

    # bootstrap: write a header overlay so the port becomes dops; patch
    # authors the body. Drop STATUS when the header now carries the type.
    header = emit.overlay(
        emit.header(
            port=origin, type=decision.overlay_type, reason=_BOOTSTRAP_REASON
        ),
        [],
    )
    overlay_path = f"/work/DeltaPorts/ports/{origin}/overlay.dops"
    res = worker.put_file(env_name, overlay_path, header)
    if res.get("ok") is False:
        log(queue_root, "WARN",
            f"bootstrap put_file failed for {origin!r}: "
            f"{res.get('error', '')[:200]}; escalating to manual")
        return "abort", "bootstrap_write_failed"

    if decision.remove_status:
        try:
            worker._exec(
                env_name, "/bin/sh", "-c",
                'rm -f "$DELTAPORTS_ROOT/ports/$1/STATUS"', "_", origin,
            )
        except Exception as exc:
            log(queue_root, "WARN",
                f"failed to remove STATUS for {origin!r}: {exc}")

    try:
        activity_log(
            queue_root, "triage_overlay_bootstrapped",
            (
                f"{origin}: bootstrapped type={decision.overlay_type} header "
                f"overlay ({decision.reason}); proceeding to patch"
            ),
            job_id=job_path.name,
            extra={
                "origin": origin,
                "overlay_type": decision.overlay_type,
                "removed_status": decision.remove_status,
                "reason": decision.reason,
            },
        )
    except Exception as exc:
        log(queue_root, "WARN", f"activity_log failed in bootstrap: {exc}")
    return None


def process_triage_job(
    queue_root: Path,
    job_path: Path,
    sibling_paths: list[Path],
    job: dict,
    bundle_dir: Path | None,
    playbooks_dir: Path | None,
) -> tuple[bool, str]:
    """Process a triage job by driving TriageStep through the orchestrator.

    Phase 5: TriageStep's ``StepOutcome.next_event`` + ``extra_events``
    encode the lifecycle events to fire on completion. The
    orchestrator fires them for the lead; this wrapper fans the
    same events out to siblings. ``_completion_events_for`` retires.

    Step 20d: before invoking the triage step, check whether the
    port still has legacy overlay artifacts. If so, enqueue a
    convert job and defer this triage to ESCALATED with a clear
    detail — the agent should not spend tokens triaging a port
    whose framework patches are about to be rewritten anyway.
    """
    from dportsv3.agent.step import Orchestrator, StepCtx
    from dportsv3.agent.steps import TriageServices, TriageStep

    origin = job.get("origin", "unknown")
    job_id = job_path.name

    # ---- Step 28a: operator-owned origin short-circuit ------------
    # If the operator has staked (target, origin) via take-over,
    # don't burn cycles on this triage. Mark the job DEAD with a
    # distinct retire_reason so the manual queue and lineage
    # queries can tell "skipped because operator owns this" apart
    # from "actually failed." Emit an activity row so the bypass
    # is observable; reference the bundle that triggered the lock
    # for forensics. The bundle row itself is left alone — the
    # hook's failure record is unchanged; only the triage path
    # short-circuits.
    skipped = _maybe_skip_locked_origin(
        queue_root=queue_root, job=job, job_id=job_id,
        sibling_paths=sibling_paths, origin=origin,
    )
    if skipped is not None:
        return skipped
    # ---------------------------------------------------------------

    # ---- WS8: muted-issue short-circuit ---------------------------
    # If the operator muted this occurrence's issue, don't auto-triage
    # it — muting suppresses surfacing AND agent budget.
    skipped = _maybe_skip_muted_issue(
        queue_root=queue_root, job=job, job_id=job_id,
        sibling_paths=sibling_paths, origin=origin,
    )
    if skipped is not None:
        return skipped
    # ---------------------------------------------------------------

    # ---- Step 20d / Step 36 follow-up: lazy convert hook ----------
    # The substrate defer used to short-circuit triage *before* the
    # LLM ran — convert was then dispatched blind, with no view of
    # what triage would have classified. Post-Step-36 the check
    # moves INTO TriageStep, AFTER the LLM call and the typed
    # ``TriageResult`` (analysis/triage_result.json) write, so
    # convert reads triage's classification + root_cause + evidence
    # via load_phase_result. Wired into services below; nothing to
    # do here at the top of the dispatcher.
    # ---------------------------------------------------------------

    # Seed queue_root + job_id into job so payload-build telemetry
    # (`playbooks_selected` activity row) can find them. parse_job_file
    # doesn't populate either; only the runner's per-job dispatch
    # knows the live queue_root and the dispatcher knows the job_id.
    # Without job_id the activity row lands with NULL job_id and is
    # invisible to `tracker get-activity --job ID`.
    job["queue_root"] = str(queue_root)
    job["job_id"] = job_id
    payload = build_triage_payload(bundle_dir, playbooks_dir, job)

    ctx = StepCtx(
        job_id=job_id,
        job=job,
        queue_root=queue_root,
        apply_transition=_apply_transition,
        activity_log=activity_log,
        db_conn=_state_db_conn,
        env_name=resolve_env(job),
        bundle_dir=bundle_dir,
        bundle_id=job.get("bundle_id"),
        playbooks_dir=playbooks_dir,
    )
    ctx.state["job_path"] = job_path
    ctx.state["payload"] = payload
    ctx.state["origin"] = origin
    ctx.state["policy_path"] = _policy_path()
    ctx.state["services"] = TriageServices(
        materialize_bundle=_materialize_bundle,
        artifact_store_put=artifact_store_put,
        write_error_note=write_error_note,
        write_triage_audit=_write_triage_audit_harness,
        enqueue_patch_job=enqueue_patch_job,
        upsert_user_context_request=upsert_user_context_request,
        update_runner_status=update_runner_status,
        probe_health_cached=probe_health_cached,
        cached_health_broken=_cached_health_broken,
        load_port_history=_load_port_history,
        log=log,
        activity_log=activity_log,
        write_manual_handoff=_write_manual_handoff,
        # Step 48 cutover (Phase B): the substrate check runs INSIDE
        # TriageStep, after the LLM + triage_result.json write. It no
        # longer defers to a convert agent — it deterministically
        # bootstraps a header overlay (→ patch authors the body) or
        # aborts to a manual handoff for non-dport compat residue.
        ensure_overlay_or_abort=lambda *, queue_root, job, job_path, origin: (
            _ensure_overlay_or_abort(
                queue_root=queue_root, job=job, job_path=job_path,
                origin=origin,
            )
        ),
    )

    result = Orchestrator().run(ctx, [TriageStep()])
    success, status = _finish_orchestrator_run(
        result, step_name="triage",
        sibling_paths=sibling_paths,
        failure_event="triage_fail",
    )
    if not success:
        # M4: a triage that terminates via TRIAGE_FAIL (bundle
        # materialization, LLM call, policy load, or orchestrator
        # halt) otherwise leaves the operator with
        # resolution=triage_failed but no explanation artifact. Mirror
        # the convert (H2) and patch handoff writes so every terminal
        # failure carries a manual_handoff. The escalate-manual and
        # convert-defer paths return success=True (and write their own
        # handoff / continue), so they never reach here. Lead bundle
        # only, matching the convert-failure funnel. Best-effort: the
        # writer swallows its own errors.
        _write_manual_handoff(
            bundle_dir, job.get("bundle_id"),
            origin=origin,
            target=job.get("target", "") or "",
            reason="triage_failed",
            reason_detail=status,
            run_id=job.get("run_id") or None,
        )
    return success, status
def _summarize_tool_call(tool: str, args: dict, result: dict) -> str:
    """One-line summary of a tool invocation for the activity log.

    The activity_log's ``message`` column drives the UI's at-a-glance
    view. Keep it short and informative — favor the args/result fields
    a human would look for first.
    """
    args = args or {}
    result = result if isinstance(result, dict) else {"value": result}
    ok = result.get("ok")
    ok_tag = "" if ok is None else (" ok" if ok else " FAIL")
    if tool == "env_verify":
        return f"status={result.get('status', '?')}{ok_tag}"
    if tool in ("get_file", "list_dir"):
        return f"{args.get('path', '')}{ok_tag}"
    if tool == "grep":
        return (
            f"pattern={args.get('pattern', '')!r} path={args.get('path', '')} "
            f"matches={len(result.get('matches') or [])}{ok_tag}"
        )
    if tool == "materialize_dports":
        def _last_nonempty_line(text: str) -> str:
            for ln in reversed((text or "").splitlines()):
                ln = ln.strip()
                if ln:
                    return ln
            return ""
        # Compose tends to print the canonical error last with
        # context above; the tail line is the right one.
        msg_line = (
            _last_nonempty_line(result.get("stderr_tail") or "")
            or _last_nonempty_line(result.get("stdout_tail") or "")
        )
        suffix = f" — {msg_line[:160]}" if (not ok and msg_line) else ""
        return f"origin={args.get('origin', '')}{ok_tag}{suffix}"
    if tool == "validate_dops":
        # Surface the first non-empty line of stderr (or stdout
        # as a fallback) so a failing validate_dops is debuggable
        # from the tracker without spelunking artifacts. The
        # diagnostic format ("ERROR E_*: ... [path:line:col]") is
        # the common case but not the only failure mode — runtime
        # errors before diagnostics flow are also possible.
        def _first_nonempty_line(text: str) -> str:
            for ln in (text or "").splitlines():
                ln = ln.strip()
                if ln:
                    return ln
            return ""
        msg_line = (
            _first_nonempty_line(result.get("stderr_tail") or "")
            or _first_nonempty_line(result.get("stdout_tail") or "")
        )
        suffix = f" — {msg_line[:160]}" if (not ok and msg_line) else ""
        return f"origin={args.get('origin', '')}{ok_tag}{suffix}"
    if tool == "put_file":
        # On failure (sha mismatch / permission), the worker's
        # ``error`` field carries the reason — show it.
        err = (result.get("error") or result.get("stderr_tail") or "")
        err = err.strip().splitlines()
        suffix = f" — {err[0][:120]}" if (not ok and err) else ""
        path = args.get('path', '')
        size = len((args.get('content') or ''))
        return f"{path} ({size} bytes){ok_tag}{suffix}"
    if tool in ("make_extract", "make_patch"):
        return f"origin={args.get('origin', '')}{ok_tag}"
    if tool in ("dupe", "genpatch"):
        return f"path={args.get('path', '')}{ok_tag}"
    if tool == "install_patches":
        n = len(result.get("installed") or [])
        return f"origin={args.get('origin', '')} installed={n}{ok_tag}"
    if tool == "emit_diff":
        diff_len = len(result.get('diff') or '')
        return (
            f"origin={args.get('origin', '')} relpath={args.get('relpath', '')} "
            f"diff_bytes={diff_len}{ok_tag}"
        )
    if tool == "dsynth_build":
        rb = result.get("rebuild_ok")
        return f"origin={args.get('origin', '')} rebuild_ok={rb}{ok_tag}"
    if tool == "dsynth_log":
        return f"origin={args.get('origin', '')} lines={len((result.get('text') or '').splitlines())}{ok_tag}"
    # Fallback: show first arg key=value pair
    if args:
        k, v = next(iter(args.items()))
        return f"{k}={str(v)[:80]}{ok_tag}"
    return f"(no args){ok_tag}"


def _write_tool_trace(
    bundle_dir: Path | None,
    bundle_id: str | None,
    trace_events: list[dict],
) -> None:
    """Persist the structured per-tool trace to the bundle.

    JSONL so it appends naturally and is grep-friendly. One line per
    event (attempt_start / tool_call / attempt_end).
    """
    if not trace_events:
        return
    lines = [json.dumps(ev, default=str) for ev in trace_events]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if bundle_id:
        artifact_store_put(bundle_id, "analysis/tool_trace.jsonl", data, "text")
    elif bundle_dir is not None:
        out = bundle_dir / "analysis" / "tool_trace.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)


def _write_patch_audit_harness(
    bundle_dir: Path | None,
    bundle_id: str | None,
    result,  # dportsv3.agent.attempt_loop.PatchResult
    model: str,
    origin: str = "",
) -> None:
    """Write harness-side outputs to the bundle: patch.md, rebuild_proof.json,
    changes.diff (host-side git diff in the env), and patch_audit.json."""
    text = (result.final_text or "").rstrip() + "\n"
    md_bytes = text.encode("utf-8")
    if bundle_id:
        artifact_store_put(bundle_id, "analysis/patch.md", md_bytes, "text")
    else:
        analysis = bundle_dir / "analysis"
        analysis.mkdir(parents=True, exist_ok=True)
        (analysis / "patch.md").write_bytes(md_bytes)

    # Always emit rebuild_proof.json on terminal states. Without
    # this, budget-exhausted / needs-help bundles produced
    # patch_audit.json but no proof artifact, leaving operators
    # unable to tell "agent gave up cleanly" from "agent crashed
    # mid-attempt" by skimming the artifact list. When the LLM
    # parsed a Rebuild Proof block we write it verbatim; otherwise
    # we synthesize one carrying the terminal status + attempt
    # count so the contract holds.
    if result.proof is not None:
        proof_payload = dict(result.proof)
    else:
        proof_payload = {
            "rebuild_ok": False,
            "status": result.status,
            "reason": (
                "agent gave up: " + result.status
                if result.status != "success"
                else "no rebuild proof parsed despite success status"
            ),
            "attempts": len(result.attempts),
            "synthetic": True,
        }
    # Code-owned metadata: the LLM has no clock and copies the prompt's
    # example (proofs carried the literal example timestamp). Only
    # rebuild_ok is the agent's to assert; stamp the rest authoritatively.
    # The real build command is the env-var-templated form that
    # worker.dsynth_build actually runs (and that proposed_fix gives the
    # operator) — the profile is the chroot's $DPORTS_DSYNTH_PROFILE, left
    # unexpanded so the value is honest and runnable, not a hardcoded guess.
    proof_payload["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    if origin:
        proof_payload["origin"] = origin
        proof_payload["build_command"] = (
            f'dsynth -S -y -p "$DPORTS_DSYNTH_PROFILE" build {origin}'
        )
    # Drop any standalone fabricated profile field; the profile is the env
    # var embedded in build_command, not a guessable concrete name here.
    proof_payload.pop("dsynth_profile", None)
    proof_bytes = (json.dumps(proof_payload, indent=2) + "\n").encode("utf-8")
    if bundle_id:
        artifact_store_put(bundle_id, "analysis/rebuild_proof.json", proof_bytes, "json")
    else:
        (bundle_dir / "analysis" / "rebuild_proof.json").write_bytes(proof_bytes)

    audit = {
        "status": result.status,
        "model": model,
        "tokens_used": {
            "prompt": result.usage.prompt_tokens,
            "completion": result.usage.completion_tokens,
            "total": result.usage.total_tokens,
        },
        "attempts": [
            {"attempt": a.attempt, "tokens": a.tokens, "rebuild_ok": a.rebuild_ok}
            for a in result.attempts
        ],
        "via": "dportsv3.agent.patch",
    }
    audit_bytes = (json.dumps(audit, indent=2) + "\n").encode("utf-8")
    if bundle_id:
        artifact_store_put(bundle_id, "analysis/patch_audit.json", audit_bytes, "json")
    else:
        (bundle_dir / "analysis" / "patch_audit.json").write_bytes(audit_bytes)

    # Step 36-3: typed PatchResult for downstream phases / future
    # tracker UI. patch_audit.json + rebuild_proof.json stay (verify
    # and existing UI consume them); this writes the typed contract
    # alongside.
    from dataclasses import asdict  # noqa: PLC0415
    from dportsv3.agent.phase_result import (  # noqa: PLC0415
        PatchResult as _PatchResultTyped, write_phase_result,
    )
    typed = _PatchResultTyped(
        rebuild_ok=bool(proof_payload.get("rebuild_ok")),
        status=result.status,
        attempts=len(result.attempts),
        tokens_prompt=result.usage.prompt_tokens,
        tokens_completion=result.usage.completion_tokens,
        tokens_total=result.usage.total_tokens,
    )
    if bundle_id:
        write_phase_result(bundle_id, "patch", typed)
    elif bundle_dir is not None:
        out = bundle_dir / "analysis" / "patch_result.json"
        out.write_bytes(
            (json.dumps(asdict(typed), indent=2) + "\n").encode("utf-8")
        )


def _write_changes_diff(bundle_dir: Path | None, bundle_id: str | None, env: str, origin: str) -> None:
    """Capture the bundle branch's full diff vs the env's base branch
    and write to ``analysis/changes.diff``.

    Step 30 slice 5: this is the *canonical* diff artifact — single
    source of truth for delivery, verify-fix replay, the
    proposed_fix recipe, and the agent's prior-attempt context
    sections. Pre-slice-5 ``changes.diff`` was HEAD-relative and
    a sibling ``delivery.diff`` carried the branch-vs-base shape;
    the dual artifact split was wrong (no reader had a legitimate
    need for the HEAD-relative form, and converted bundles lost
    the convert commit's deltas in the HEAD-relative diff).

    Includes both convert commits on the bundle branch and the
    patch agent's uncommitted working-tree edits. Uses
    ``worker._git_diff_against_base`` (with the ``--intent-to-add``
    dance) so freshly-created files surface as additions.

    Best-effort: failures emit a tombstone diff body so the
    operator sees the failure shape rather than getting silent
    delivery breakage downstream.
    """
    try:
        from dportsv3.agent import worker  # type: ignore[import-not-found]
        # Whole-tree (not ports/<origin>) so fixes that correctly land
        # outside the bundle origin — e.g. a slave port whose patch lives
        # in the master's PATCHDIR — are captured instead of vanishing.
        rel = "."
        base = worker._resolve_bundle_base_branch(env)
        # Takes the env, not a host path: the diff runs in-chroot now, because
        # a job's tree may be a linked worktree whose gitdir: pointer only
        # resolves under the chroot's /work/... prefix.
        p = worker._git_diff_against_base(env, base, rel)
        diff_bytes = p.stdout.encode("utf-8")
    except Exception as exc:
        diff_bytes = f"# failed to capture diff: {exc}\n".encode("utf-8")

    if bundle_id:
        artifact_store_put(bundle_id, "analysis/changes.diff", diff_bytes, "text")
    elif bundle_dir:
        out = bundle_dir / "analysis" / "changes.diff"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(diff_bytes)


def _load_operator_context_history(
    run_id: str | None, origin: str | None,
) -> list[dict]:
    """Step 29c: read every operator-submitted context round for
    ``(run_id, origin)`` so ``manual_handoff.md`` can render the
    full operator-side narrative. Returns ``[]`` if the DB isn't
    available, the lookup is unscoped, or no rounds exist."""
    if _state_db_conn is None or not run_id or not origin:
        return []
    try:
        with _state_db_lock:
            from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
                list_user_context_history,
            )
            return list_user_context_history(
                _state_db_conn, run_id, origin,
            )
    except Exception:
        return []


def _write_manual_handoff(
    bundle_dir: Path | None,
    bundle_id: str | None,
    *,
    origin: str,
    target: str,
    reason: str,
    reason_detail: str = "",
    decision_extra: dict | None = None,
    patch_result: object | None = None,
    run_id: str | None = None,
) -> None:
    """Render and persist ``analysis/manual_handoff.md`` for an
    escalated job. Best-effort: failures are swallowed so escalation
    bookkeeping (lifecycle event, user-context-request row) never gets
    blocked by an artifact write."""
    try:
        from dportsv3.agent import manual_handoff  # noqa: PLC0415

        history = _load_operator_context_history(run_id, origin)
        ctx = manual_handoff.build_handoff_ctx(
            origin=origin,
            target=target,
            reason=reason,
            reason_detail=reason_detail,
            bundle_id=bundle_id or "",
            bundle_dir=bundle_dir,
            read_bundle_text=read_bundle_text,
            decision_extra=decision_extra,
            patch_result=patch_result,
            operator_context_history=history,
        )
        body = manual_handoff.render_handoff(ctx).encode("utf-8")
    except Exception as exc:
        print(f"Warning: manual_handoff render failed for {origin}: {exc}",
              file=sys.stderr)
        return

    if bundle_id:
        artifact_store_put(bundle_id, "analysis/manual_handoff.md", body, "text")
    elif bundle_dir is not None:
        out = bundle_dir / "analysis" / "manual_handoff.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)


def _write_proposed_fix(
    bundle_dir: Path | None,
    bundle_id: str | None,
    *,
    origin: str,
    target: str,
    model: str,
    classification: str = "",
    confidence: str = "",
    attempts_max: int = 0,
    patch_result: object | None = None,
) -> None:
    """Render and persist ``analysis/proposed_fix.md`` after a
    successful patch attempt. Best-effort: failures are swallowed so
    the artifact write never blocks the lifecycle event.

    Operator-facing one-pager: what the agent did, the cost, and the
    recipe to land the fix in the operator's own DeltaPorts clone.
    Surfaces as the default bundle preview when
    ``resolution='agent_fixed'``.
    """
    try:
        from dportsv3.agent import proposed_fix  # noqa: PLC0415

        ctx = proposed_fix.build_proposed_fix_ctx(
            origin=origin,
            target=target,
            bundle_id=bundle_id or "",
            bundle_dir=bundle_dir,
            read_bundle_text=read_bundle_text,
            patch_result=patch_result,
            model=model,
            classification=classification,
            confidence=confidence,
            attempts_max=attempts_max,
            tracker_url=_tracker_url(),
        )
        body = proposed_fix.render_proposed_fix(ctx).encode("utf-8")
    except Exception as exc:
        print(f"Warning: proposed_fix render failed for {origin}: {exc}",
              file=sys.stderr)
        return

    if bundle_id:
        artifact_store_put(bundle_id, "analysis/proposed_fix.md", body, "text")
    elif bundle_dir is not None:
        out = bundle_dir / "analysis" / "proposed_fix.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)


def process_patch_job(
    queue_root: Path,
    job_path: Path,
    sibling_paths: list[Path],
    job: dict,
    bundle_dir: Path | None,
    playbooks_dir: Path | None,
) -> tuple[bool, str]:
    """Process a patch job by driving PatchAttemptStep through the orchestrator.

    Phase 5: PatchAttemptStep's ``StepOutcome.next_event`` +
    ``extra_events`` encode the lifecycle events to fire on
    completion. The orchestrator fires them for the lead; this
    wrapper fans the same events out to siblings.
    """
    from dportsv3.agent.lifecycle import JobEvent
    from dportsv3.agent.step import Orchestrator, StepCtx
    from dportsv3.agent.steps import PatchAttemptStep, PatchServices

    origin = job.get("origin", "unknown")
    job_id = job_path.name

    # ---- Step 28-extra: operator-owned origin short-circuit -------
    # If the operator has staked (target, origin) between when this
    # patch job was enqueued and when the runner picked it up, don't
    # burn LLM tokens — retire the job DEAD with
    # retire_reason='origin_locked'. Mirrors the triage-side check.
    skipped = _maybe_skip_locked_origin(
        queue_root=queue_root, job=job, job_id=job_id,
        sibling_paths=sibling_paths, origin=origin,
        job_type="patch",
    )
    if skipped is not None:
        return skipped
    # ---------------------------------------------------------------

    # ---- WS8: muted-issue short-circuit (mute may land mid-flight) --
    skipped = _maybe_skip_muted_issue(
        queue_root=queue_root, job=job, job_id=job_id,
        sibling_paths=sibling_paths, origin=origin,
        job_type="patch",
    )
    if skipped is not None:
        return skipped
    # ---------------------------------------------------------------

    # Step 30 slice 1: give the patch its own worktree, cut from or
    # reusing bundle/<id>. Hard-fail since B1 — without an isolated tree
    # the job would edit whatever PORTS_DIR last pointed at, which after
    # a crashed predecessor is another job's worktree. See
    # _checkout_bundle_branch_for_job for why there is no safe fallback.
    if not _checkout_bundle_branch_for_job(
        queue_root=queue_root, job_id=job_path.name,
        env=resolve_env(job), bundle_id=job.get("bundle_id") or None,
        job_type="patch",
    ):
        detail = {
            "bundle_id": job.get("bundle_id"),
            "origin": origin,
            "job_type": "patch",
            "reason": "worktree_unavailable",
        }
        activity_log(
            queue_root, "patch_skipped_worktree_unavailable",
            f"{origin}: no isolated worktree; patch not run",
            job_id=job_id, extra=detail,
        )
        _apply_transition(job_id, JobEvent.WORKTREE_UNAVAILABLE, detail=detail)
        for s in sibling_paths or ():
            _apply_transition(s.name, JobEvent.WORKTREE_UNAVAILABLE,
                              detail=detail)
        return False, "worktree_unavailable"

    # Step 38a: record the env's compose target so get_effective_overlay
    # can scope-filter overlay.dops by the build line the env targets.
    # Guard env=None so an unresolvable env doesn't stash a junk entry
    # under the None key (the cache miss-fallback is the same `None`,
    # so behavior is unchanged either way — but a clean cache makes
    # debugging easier).
    from dportsv3.agent import worker as _worker  # noqa: PLC0415
    _38a_env = resolve_env(job)
    if _38a_env:
        _worker.set_env_target(_38a_env, job.get("target") or None)

    # Seed queue_root + job_id into job so payload-build telemetry
    # (`playbooks_selected` activity row) can find them. See the same
    # comment in process_triage_job.
    job["queue_root"] = str(queue_root)
    job["job_id"] = job_path.name
    payload = build_patch_payload(bundle_dir, playbooks_dir, job)

    ctx = StepCtx(
        job_id=job_id,
        job=job,
        queue_root=queue_root,
        apply_transition=_apply_transition,
        activity_log=activity_log,
        db_conn=_state_db_conn,
        env_name=resolve_env(job),
        bundle_dir=bundle_dir,
        bundle_id=job.get("bundle_id"),
        playbooks_dir=playbooks_dir,
    )
    ctx.state["job_path"] = job_path
    ctx.state["payload"] = payload
    ctx.state["origin"] = origin
    ctx.state["policy_path"] = _policy_path()
    ctx.state["services"] = PatchServices(
        read_bundle_text=read_bundle_text,
        write_error_note=write_error_note,
        write_patch_audit=_write_patch_audit_harness,
        write_tool_trace=_write_tool_trace,
        write_changes_diff=_write_changes_diff,
        looks_env_suspicious=_looks_env_suspicious,
        invalidate_health_cache=invalidate_health_cache,
        cached_health_broken=_cached_health_broken,
        summarize_tool_call=_summarize_tool_call,
        activity_log=activity_log,
        log=log,
        load_port_history=_load_port_history,
        write_manual_handoff=_write_manual_handoff,
        write_proposed_fix=_write_proposed_fix,
    )

    result = Orchestrator().run(ctx, [PatchAttemptStep()])
    return _finish_orchestrator_run(
        result, step_name="patch",
        sibling_paths=sibling_paths,
        failure_event="patch_gave_up",
    )


def process_job(
    queue_root: Path,
    job_path: Path,
    sibling_paths: list[Path],
    dry_run: bool,
    playbooks_dir: Path | None,
):
    """Process a single job (dispatch based on type).

    Phase 5 Step 4: lifecycle event firing moved into the steps
    themselves (via ``StepOutcome.next_event`` / ``extra_events``).
    The orchestrator fires those for the lead; the step's wrapper
    (``process_triage_job`` / ``process_patch_job``) fans them out
    to siblings. This function is now a pure dispatcher: job-file
    parsing, dry-run handling, START-event firing, filesystem move.

    ``sibling_paths`` are inflight job files claimed alongside the lead
    by ``claim_next_job_batch``.
    """
    from dportsv3.agent.lifecycle import JobEvent

    job = parse_job_file(job_path)
    # Fold sibling bundles into the payload context.
    sibling_bundles: list[str] = []
    for s_path in sibling_paths:
        try:
            s_meta = parse_job_file(s_path)
        except Exception:
            continue
        bid = s_meta.get("bundle_id")
        if bid and bid != job.get("bundle_id"):
            sibling_bundles.append(bid)
    if sibling_bundles:
        job["sibling_bundle_ids"] = ",".join(sibling_bundles)
        log(queue_root, "INFO",
            f"batched {len(sibling_bundles)} sibling job(s) with {job_path.name}: "
            f"{','.join(sibling_bundles)}")
    job_type = job.get("type", "triage")
    bundle_dir_value = job.get("bundle_dir")
    bundle_dir = Path(bundle_dir_value) if bundle_dir_value else None
    bundle_id = job.get("bundle_id")
    origin = job.get("origin", "unknown")
    job_id = job_path.name

    log(queue_root, "INFO", f"processing job {job_path.name}")
    update_runner_status("processing", job_id=job_id, stage=f"{job_type}_start",
                         extra={"origin": origin, "type": job_type})

    # Step 20: convert jobs are port-level — there's no failure
    # bundle attached and they don't need one. Step 11c: verify
    # jobs reference a bundle but don't need bundle_dir
    # materialized (run_verify_fix fetches the diff via tracker).
    if (job_type not in ("convert", "verify")
            and bundle_dir is None and not bundle_id):
        log(queue_root, "ERROR", "missing bundle_id/bundle_dir in job")
        write_error_note(job_path, "missing bundle_id/bundle_dir in job")
        move_job(job_path, "failed")
        update_runner_status("idle", job_id=None, stage=None)
        return
    if bundle_dir is not None and not bundle_dir.exists():
        log(queue_root, "ERROR", f"bundle_dir does not exist: {bundle_dir}")
        write_error_note(job_path, f"bundle_dir does not exist: {bundle_dir}")
        move_job(job_path, "failed")
        update_runner_status("idle", job_id=None, stage=None)
        return

    if dry_run:
        if job_type == "patch":
            payload = build_patch_payload(bundle_dir, playbooks_dir, job)
        else:
            payload = build_triage_payload(bundle_dir, playbooks_dir, job)

        log(queue_root, "INFO",
            f"[dry-run] type={job_type}, would send payload ({len(payload)} bytes)")
        print("=" * 60)
        print(f"JOB TYPE: {job_type}")
        print("=" * 60)
        print(payload)
        print("=" * 60)
        move_job(job_path, "pending")
        update_runner_status("idle", job_id=None, stage=None)
        return

    # Fire the START lifecycle event for the lead + siblings before
    # invoking the step (which fires its own completion events).
    if job_type == "patch":
        start_event = JobEvent.PATCH_START
        _apply_transition(job_path.name, start_event)
        for s in sibling_paths:
            _apply_transition(s.name, start_event)
        success, status = process_patch_job(
            queue_root, job_path, sibling_paths, job, bundle_dir, playbooks_dir,
        )
        # Step 30 slice 4: patch is terminal for the bundle branch
        # — delivery.diff was captured (slice 2) on success, and on
        # failure the branch's state is moot. Drop either way.
        _drop_bundle_branch_for_job(
            queue_root=queue_root, job_id=job_path.name,
            env=resolve_env(job),
            bundle_id=job.get("bundle_id") or None,
            job_type="patch",
            reason="patch_success" if success else "patch_failure",
        )
    elif job_type == "triage":
        start_event = JobEvent.TRIAGE_START
        _apply_transition(job_path.name, start_event)
        for s in sibling_paths:
            _apply_transition(s.name, start_event)
        success, status = process_triage_job(
            queue_root, job_path, sibling_paths, job, bundle_dir, playbooks_dir,
        )
    elif job_type == "verify":
        # Step 11c: operator-triggered fix verification. Calls
        # dportsv3.verify_fix.run_verify_fix() in-process — no
        # subprocess. The job's terminal state is VERIFY_FIX_OK
        # whenever the orchestrator ran end-to-end (regardless of
        # the underlying dsynth verdict, which lives on
        # bundles.verification_status via the Slice 2 endpoint).
        # VERIFY_FIX_GAVE_UP only fires if the orchestrator itself
        # raised (env gone, tracker unreachable, etc.).
        #
        # Note: run_verify_fix raises VerifyFixError (an Exception
        # subclass), not SystemExit, so the except-Exception below
        # catches its caller-recoverable failures without killing
        # the runner process. SystemExit would have escaped the
        # runner main loop and exited the process — that bug was
        # what made the runner silently disappear on the first
        # verify run.
        from dportsv3.verify_fix import run_verify_fix  # noqa: PLC0415

        start_event = JobEvent.VERIFY_FIX_START
        _apply_transition(job_path.name, start_event)
        bundle_id = job.get("bundle_id", "")
        verify_env = job.get("dev_env", "")

        def _flag_bundle_verification_failed() -> None:
            # Flip bundle.verification_status so the UI stops showing
            # "in flight forever" — the orchestrator never got far
            # enough to POST a result to /verification itself.
            if bundle_id and _state_db_conn is not None:
                try:
                    with _state_db_lock:
                        now = datetime.now(timezone.utc).isoformat()
                        _state_db_conn.execute(
                            """UPDATE bundles SET
                                   verification_status = 'verification_failed',
                                   verification_at = ?,
                                   last_seen_at = ?
                               WHERE bundle_id = ?""",
                            (now, now, bundle_id),
                        )
                        _state_db_conn.commit()
                except sqlite3.Error as db_exc:
                    log(queue_root, "WARN",
                        f"failed to mark bundle {bundle_id} "
                        f"verification_failed: {db_exc}")

        # Verify replays changes.diff (the complete branch-vs-base
        # canonical artifact) on a throwaway ``bundle/<id>-verify``
        # branch cut fresh from base. This decouples verify from the
        # patch agent's ``bundle/<id>`` branch, which Slice 4's
        # terminal sweep may already have dropped — and from master,
        # which verify must never build against directly. We record
        # the ref the env was on so the end-of-run drop can restore it.
        checkout_ok, verify_prev_ref = _checkout_verify_branch_for_job(
            queue_root=queue_root, job_id=job_path.name,
            env=verify_env or None,
            bundle_id=bundle_id or None,
        )
        try:
            if not checkout_ok:
                # Abort BEFORE replay. Without a fresh base branch we
                # can't prove the diff applies cleanly, so any verdict
                # would be meaningless — and replaying onto whatever
                # branch is current would also let apply-and-build's
                # cleanup reset the wrong tree.
                raise _VerifyBranchUnavailable()
            result = run_verify_fix(
                bundle_id=bundle_id,
                env=verify_env,
                tracker_url=_tracker_url(),
            )
            success, status = True, (
                "verified" if result.ok else "verification_failed"
            )
            finish_event = JobEvent.VERIFY_FIX_OK
            failed_stage = result.failure_stage()
            detail = {
                "ok": result.ok,
                "failed_stage": failed_stage,
                "apply_exit": result.apply_exit,
                "reapply_exit": result.reapply_exit,
                "dsynth_exit": result.dsynth_exit,
                "applied_diff_sha256": result.applied_diff_sha256,
                "log_path": result.log_path,
                "stderr_tail": result.stderr_tail,
                "posted": result.posted,
                "bundle_id": bundle_id,
                "env": verify_env,
            }
            if result.ok:
                msg = (f"verify OK for {bundle_id} (env={verify_env}, "
                       f"dsynth_exit=0)")
            else:
                # Name which stage failed so the activity row is
                # actionable without opening the log.
                stage_exit = {
                    "apply": result.apply_exit,
                    "reapply": result.reapply_exit,
                    "dsynth": result.dsynth_exit,
                }.get(failed_stage)
                msg = (f"verify FAIL for {bundle_id} (env={verify_env}): "
                       f"{failed_stage} stage exit={stage_exit}")
            try:
                activity_log(
                    queue_root, "verify_complete", msg,
                    job_id=job_path.name, extra=detail,
                )
            except Exception as log_exc:
                log(queue_root, "WARN",
                    f"activity_log failed for verify_complete: {log_exc}")
        except _VerifyBranchUnavailable:
            # Checkout of the fresh verify branch failed (logged by
            # _checkout_verify_branch_for_job). Fail the job (→ DEAD
            # via the shared tail) and mark the bundle as
            # verification_failed so the UI stops showing "in flight".
            success = False
            status = "verify aborted: could not establish fresh verify branch"
            finish_event = JobEvent.VERIFY_FIX_GAVE_UP
            detail = {
                "reason": status,
                "bundle_id": bundle_id,
                "env": verify_env,
            }
            log(queue_root, "ERROR",
                f"verify job {job_path.name} aborted: "
                f"verify-branch checkout failed")
            try:
                activity_log(
                    queue_root, "verify_failed",
                    f"verify ABORTED for {bundle_id}: "
                    f"verify-branch checkout failed",
                    job_id=job_path.name, extra=detail,
                )
            except Exception as log_exc:
                log(queue_root, "WARN",
                    f"activity_log failed for verify abort: {log_exc}")
            _flag_bundle_verification_failed()
        except Exception as exc:
            success, status = False, f"verify_fix raised: {exc}"
            finish_event = JobEvent.VERIFY_FIX_GAVE_UP
            err_msg = str(exc)[:500]
            detail = {
                "reason": err_msg,
                "exception_type": type(exc).__name__,
                "bundle_id": bundle_id,
                "env": verify_env,
            }
            log(queue_root, "ERROR",
                f"verify job {job_path.name} failed: "
                f"{type(exc).__name__}: {err_msg}")
            try:
                activity_log(
                    queue_root, "verify_failed",
                    f"verify FAILED for {bundle_id}: {err_msg}",
                    job_id=job_path.name, extra=detail,
                )
            except Exception as log_exc:
                log(queue_root, "WARN",
                    f"activity_log failed for verify_failed: {log_exc}")
            _flag_bundle_verification_failed()
        _apply_transition(job_path.name, finish_event, detail=detail)
        # The throwaway verify branch is done either way. Drop it and
        # restore the ref the env was on before verify (verify_prev_ref)
        # so we don't strand the env on a deleted branch or on base
        # when the operator had something else checked out.
        _drop_verify_branch_for_job(
            queue_root=queue_root, job_id=job_path.name,
            env=verify_env or None,
            bundle_id=bundle_id or None,
            restore_ref=verify_prev_ref,
            reason="verify_complete" if success else "verify_failure",
        )
    elif job_type == "confirm":
        # A1: build-confirmed resolution. Reuses the verify machinery —
        # run_verify_fix applies the accepted fix (the delivery bundle's
        # changes.diff) and builds it — but records the verdict on the
        # ISSUE (advance the confirm generation), NOT on the bundle. The
        # bundle's verification axis is deliberately left untouched: a
        # no-op _post_json is injected so a red confirm can't flip the
        # already-accepted delivery bundle to verification_failed. Green
        # OR red is a completed job (CONFIRM_GREEN/RED → DONE); only a
        # machinery failure is CONFIRM_GAVE_UP → DEAD.
        #
        # The confirm build runs on a throwaway bundle/<id>-verify branch
        # cut fresh from base (same as verify), so it never builds against
        # master and its cleanup can't reset the wrong tree.
        from dportsv3.verify_fix import run_verify_fix  # noqa: PLC0415

        _apply_transition(job_path.name, JobEvent.CONFIRM_START)
        bundle_id = job.get("bundle_id", "")
        issue_key = job.get("issue_key", "")
        requested_by = job.get("requested_by", "reconcile")
        try:
            generation = int(job.get("requested_build_generation") or 0)
        except (TypeError, ValueError):
            generation = 0
        confirm_env = resolve_env(job) or ""

        def _noop_post(url, body, timeout=10):  # confirm: skip bundle POST
            return {}

        checkout_ok, confirm_prev_ref = False, None
        try:
            if not confirm_env:
                raise RuntimeError("no dev-env available for confirm build")
            checkout_ok, confirm_prev_ref = _checkout_verify_branch_for_job(
                queue_root=queue_root, job_id=job_path.name,
                env=confirm_env, bundle_id=bundle_id or None,
            )
            if not checkout_ok:
                raise RuntimeError(
                    "could not establish fresh confirm/verify branch")
            result = run_verify_fix(
                bundle_id=bundle_id, env=confirm_env,
                tracker_url=_tracker_url(), _post_json=_noop_post,
            )
            failed_stage = result.failure_stage()
            verdict_detail = (
                "" if result.ok else
                f"{failed_stage} stage failed "
                f"(dsynth_exit={result.dsynth_exit}); "
                f"accepted fix from bundle {bundle_id}"
            )
            recorded = _record_confirm_verdict(
                queue_root, issue_key, generation,
                ok=result.ok, requested_by=requested_by,
                target=job.get("target", ""),
                verdict_detail=verdict_detail,
            )
            if recorded == "error":
                # The build ran but its verdict could not be persisted, so
                # the issue is still marked in-flight with nothing recorded.
                # Reporting success here would strand it silently; treat it
                # as a machinery failure so the job lands DEAD and the
                # startup marker sweep can re-derive the work.
                raise RuntimeError(
                    f"confirm verdict for {issue_key} could not be recorded")
            if not result.ok and recorded == "recorded":
                # A3: a red confirm goes to a HUMAN. The accepted fix did not
                # hold, which is a judgement call, so write the operator
                # one-pager instead of re-triaging. Best-effort: the issue is
                # already reopened above, the artifact is context.
                _write_manual_handoff(
                    None, bundle_id or None,
                    origin=origin,
                    target=job.get("target", ""),
                    # manual_handoff.REASON_CONFIRM_RED (same literal style
                    # as the triage_failed call above)
                    reason="confirm_build_red",
                    reason_detail=(
                        f"Confirm build for issue {issue_key} "
                        f"(generation {generation}) failed: {verdict_detail}. "
                        f"Log: {result.log_path or '(not retained)'}"
                    ),
                )
            success = True
            status = "confirm_green" if result.ok else "confirm_red"
            finish_event = (JobEvent.CONFIRM_GREEN if result.ok
                            else JobEvent.CONFIRM_RED)
            detail = {
                "ok": result.ok, "issue_key": issue_key,
                "requested_build_generation": generation,
                "dsynth_exit": result.dsynth_exit,
                "failed_stage": failed_stage,
                "bundle_id": bundle_id, "env": confirm_env,
                "requested_by": requested_by,
            }
            msg = (f"confirm {'GREEN' if result.ok else 'RED'} for "
                   f"{issue_key} (gen {generation}, bundle={bundle_id}, "
                   f"env={confirm_env})")
            try:
                activity_log(queue_root, "confirm_complete", msg,
                             job_id=job_path.name, extra=detail)
            except Exception as log_exc:
                log(queue_root, "WARN",
                    f"activity_log failed for confirm_complete: {log_exc}")
        except Exception as exc:
            # Machinery failure — NO verdict produced (env gone, nothing to
            # replay, tracker unreachable). Clear the in-flight marker and
            # count it, so the level-triggered feed retries WITHOUT needing a
            # runner restart, and a permanently unbuildable fix stops after
            # DP_CONFIRM_MAX_FAILURES instead of parking the issue in
            # `resolving` forever (yt7). Proper per-key backoff is C4 — this
            # is the bound, not the pacing.
            success = False
            status = f"confirm gave up: {type(exc).__name__}: {exc}"[:500]
            finish_event = JobEvent.CONFIRM_GAVE_UP
            exhausted = _record_confirm_failure(
                queue_root, issue_key, reason=f"{type(exc).__name__}: {exc}",
            ) if issue_key else False
            detail = {"reason": status, "issue_key": issue_key,
                      "bundle_id": bundle_id, "env": confirm_env,
                      "requested_build_generation": generation,
                      "retry_budget_exhausted": exhausted}
            log(queue_root, "ERROR",
                f"confirm job {job_path.name} gave up: {status}")
            if exhausted:
                # Budget spent: the issue is reopened, so tell a human why the
                # build could never run — same one-pager as a red verdict.
                _write_manual_handoff(
                    None, bundle_id or None,
                    origin=origin, target=job.get("target", ""),
                    reason="confirm_build_red",
                    reason_detail=(
                        f"Confirm build for issue {issue_key} could not RUN "
                        f"(retry budget exhausted). Last error: {status}"
                    ),
                )
            try:
                activity_log(queue_root, "confirm_failed",
                             f"confirm GAVE UP for {issue_key}: {status}",
                             job_id=job_path.name, extra=detail)
            except Exception as log_exc:
                log(queue_root, "WARN",
                    f"activity_log failed for confirm_failed: {log_exc}")
        _apply_transition(job_path.name, finish_event, detail=detail)
        if confirm_env:
            # Throwaway confirm/verify branch is done either way; drop it
            # and restore the ref the env was on before (mirrors verify).
            _drop_verify_branch_for_job(
                queue_root=queue_root, job_id=job_path.name,
                env=confirm_env, bundle_id=bundle_id or None,
                restore_ref=confirm_prev_ref,
                reason="confirm_complete" if success else "confirm_failure",
            )
    else:
        # Unknown job type — fire the catchall failure event for lead +
        # siblings, write the error note, move everything to failed/.
        success, status = False, f"unknown job type: {job_type}"
        _apply_transition(job_path.name, JobEvent.PATCH_GAVE_UP,
                          detail={"reason": status})
        for s in sibling_paths:
            _apply_transition(s.name, JobEvent.PATCH_GAVE_UP,
                              detail={"reason": status})

    error_msg = (status or "Unknown error")[:500] if not success else None

    if success:
        move_job(job_path, "done")
        for s in sibling_paths:
            try:
                move_job(s, "done")
            except OSError:
                continue
        log(queue_root, "INFO",
            f"moved job + {len(sibling_paths)} sibling(s) to done/")
    else:
        # Write error file for visibility in UI.
        error_file = job_path.with_suffix(".job.error")
        try:
            error_file.write_text(error_msg)
            log(queue_root, "DEBUG", f"wrote error file: {error_file}")
        except OSError as e:
            log(queue_root, "WARN", f"failed to write error file: {e}")

        move_job(job_path, "failed")
        for s in sibling_paths:
            try:
                move_job(s, "failed")
            except OSError:
                continue
        log(queue_root, "ERROR",
            f"moved job + {len(sibling_paths)} sibling(s) to failed/ ({status})")

    update_runner_status("idle", job_id=None, stage=None)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process dsynth failure jobs via the dportsv3.agent harness")
    parser.add_argument("--queue-root", required=True, help="Path to queue directory")
    parser.add_argument("--once", action="store_true", help="Process one job and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print payload without calling the LLM")
    parser.add_argument(
        "--playbooks-dir",
        help="Override the playbooks directory (default: the copy that ships "
             "inside the package)",
    )
    parser.add_argument(
        "--env",
        help=(
            "Default dev-env name when a job doesn't carry one and "
            "the tracker hasn't recorded an active env. Trackerless "
            "escape hatch — for tracker-attached runs prefer setting "
            "the active env in the tracker UI."
        ),
    )
    args = parser.parse_args(argv)

    # Stash the CLI env on the module so handlers reach it via
    # cli_env_default() without threading it through every signature.
    global _CLI_ENV_DEFAULT
    _CLI_ENV_DEFAULT = args.env or None

    queue_root = Path(args.queue_root)

    for subdir in ["pending", "inflight", "done", "failed"]:
        d = queue_root / subdir
        if not d.exists():
            print(f"error: queue directory missing: {d}", file=sys.stderr)
            return 1

    init_state_db(queue_root)
    # poly-i7y: an env store we cannot read is not an empty machine. Every
    # dev-env subcommand calls require_root(), so a runner that cannot
    # enumerate envs cannot exec into one either — it would run jobs with
    # the dsynth-busy gate switched off, which is how buildbase gets
    # corrupted. Refuse before taking the lock, so the refusal costs a
    # live runner nothing.
    from dportsv3.agent.env_resolver import (  # noqa: PLC0415
        list_available_envs_detailed,
    )
    _, env_list_error = list_available_envs_detailed()
    if env_list_error:
        print(f"error: cannot enumerate dev-envs: {env_list_error}",
              file=sys.stderr)
        log(queue_root, "ERROR",
            f"refusing to start: cannot enumerate dev-envs: {env_list_error}")
        return EXIT_ENV_UNREADABLE

    # Populate env_health_status with stub rows for every env on
    # disk so the tracker UI dropdown reflects the runner's view of
    # available envs even before the first health probe fires.
    if _state_db_conn is not None:
        try:
            n = stub_unprobed_envs()
            if n:
                log(queue_root, "INFO",
                    f"stubbed {n} previously-unseen env(s) into env_health_status")
        except Exception as exc:
            log(queue_root, "WARN", f"stub_unprobed_envs failed: {exc}")
    # Before any sweep runs, and before this process records itself in
    # `runners` — a refused runner must not appear as a participant.
    if not acquire_runner_lock(queue_root):
        holder = describe_lock_holder()
        print(f"error: another runner already owns {queue_root}: {holder}",
              file=sys.stderr)
        log(queue_root, "ERROR",
            f"refusing to start: {holder} already owns this queue")
        return EXIT_RUNNER_LOCKED

    register_runner()
    start_heartbeat()

    # Reap any inflight-ish jobs left over from a previous runner
    # instance — they're DEAD-with-runner_restart by definition.
    # Operator can re-enqueue via the bundle if a retry is warranted.
    if _state_db_conn is not None:
        try:
            from dportsv3.agent import lifecycle  # type: ignore[import-not-found]
            with _state_db_lock:
                n = lifecycle.reap_orphans(_state_db_conn, actor=f"runner-{os.getpid()}")
            if n > 0:
                log(queue_root, "INFO",
                    f"reaped {n} orphan job(s) from previous runner instance")
        except Exception as exc:
            log(queue_root, "WARN", f"reap_orphans failed: {exc}")

        # B3: reap_orphans just marked those rows DEAD but left their files
        # in inflight/, where nothing ever looks at them again.
        stranded = sweep_stranded_inflight(queue_root)
        if stranded:
            log(queue_root, "INFO",
                f"swept {len(stranded)} stranded inflight job(s) to failed/: "
                + ", ".join(stranded[:5])
                + ("..." if len(stranded) > 5 else ""))

        # C2 crash recovery: a confirm build runs in-process, so a runner
        # that died mid-build left a stale building_generation marker.
        # Clear them so the level-triggered feed re-derives and re-enqueues.
        _reset_building_markers(queue_root)

        # Step 10a: also reap stale QUEUED rows whose .job file has
        # vanished from pending/ (gated on age + missing-file). Catches
        # the failure mode where a row records a path the host runner
        # never scans (in-chroot paths from earlier deployments) and
        # blocks _has_active_same_origin_job indefinitely.
        try:
            max_age = int(os.environ.get(
                "DPORTSV3_STALE_QUEUED_MAX_AGE_SECONDS", "3600",
            ))
            with _state_db_lock:
                stale = lifecycle.reap_stale_queued(
                    _state_db_conn, queue_root,
                    max_age_seconds=max_age,
                    actor=f"runner-{os.getpid()}",
                )
            if stale:
                log(queue_root, "INFO",
                    f"reaped {len(stale)} stale queued job(s): "
                    + ", ".join(stale[:5])
                    + ("..." if len(stale) > 5 else ""))
        except Exception as exc:
            log(queue_root, "WARN", f"reap_stale_queued failed: {exc}")

    if args.playbooks_dir:
        playbooks_dir = Path(args.playbooks_dir)
        if not playbooks_dir.exists():
            print(
                f"warning: playbooks directory not found: {playbooks_dir}",
                file=sys.stderr,
            )
            playbooks_dir = None
    else:
        from dportsv3.agent.playbooks import find_playbooks_dir  # noqa: PLC0415
        playbooks_dir = find_playbooks_dir()

    triage_model = os.environ.get("DP_HARNESS_TRIAGE_MODEL") or "<unset>"
    patch_model_env = os.environ.get("DP_HARNESS_PATCH_MODEL")
    if patch_model_env:
        patch_model = patch_model_env
    elif triage_model != "<unset>":
        patch_model = f"{triage_model} (fallback from triage)"
    else:
        patch_model = "<unset>"
    playbooks_info = str(playbooks_dir) if playbooks_dir else "none"
    log(queue_root, "INFO",
        f"starting runner (once={args.once}, dry_run={args.dry_run}, "
        f"triage_model={triage_model}, patch_model={patch_model}, "
        f"playbooks={playbooks_info})")
    activity_log(queue_root, "runner_start",
                 f"Runner started (triage={triage_model}, "
                 f"patch={patch_model})")
    update_runner_status("idle", job_id=None, stage=None)

    # Runner-level dev-env for the health probe + dsynth-busy gate.
    # Resolved from the env_resolver with no job context — picks up
    # the tracker active env, --env CLI flag, or auto-picked single
    # env. Re-resolved on each gate tick so UI changes take effect
    # without a runner restart. Empty = no gate (no env to watch);
    # operator gets a one-time WARN at startup.
    runner_env = resolve_env(None) or ""

    # B3: with the lock held and nothing claimed yet, every /work/job-*
    # directory in our env is a leftover from a runner that died mid-job.
    if runner_env:
        swept = sweep_stale_worktrees(queue_root, runner_env)
        if swept:
            log(queue_root, "INFO",
                f"swept {len(swept)} stale worktree(s) in {runner_env}: "
                + ", ".join(swept[:5])
                + ("..." if len(swept) > 5 else ""))
            activity_log(queue_root, "worktrees_swept",
                         f"removed {len(swept)} stale worktree(s) in "
                         f"{runner_env}",
                         extra={"env": runner_env, "removed": swept[:20]})

    if not runner_env:
        log(queue_root, "WARN",
            "no dev-env resolved at runner start; the runner will hold "
            "instead of processing jobs, because without an env there is "
            "no dsynth-busy gate and concurrent dsynth runs corrupt "
            "buildbase. Set an active env in the tracker UI or pass "
            "--env NAME.")

    _last_busy_reason = ""
    _last_health_reason = ""
    _last_no_env_reason = ""
    health_cache_seconds = int(
        os.environ.get("DP_HARNESS_HEALTH_CACHE_SECONDS", "60")
    )

    def _gate_blocked() -> bool:
        nonlocal _last_busy_reason, _last_health_reason, _last_no_env_reason
        # Re-resolve per cycle (cached for 1 s) so operator selection
        # in the tracker UI takes effect without a runner restart.
        runner_env = resolve_env_for_gate() or ""
        # Health probe first. If broken, we pause regardless of
        # dsynth-busy state — there's no point running anything until
        # the env is repaired. Cache keeps this cheap (default 60s);
        # tool errors that look env-suspicious invalidate the cache
        # so a freshly-broken env is detected on the next gate.
        if runner_env:
            eh = probe_health_cached(runner_env, health_cache_seconds)
            if eh.status == "broken":
                reason = (eh.operator_action
                          or f"env {runner_env} status=broken")
                if reason != _last_health_reason:
                    log(queue_root, "INFO",
                        f"runner paused: health broken: {reason}")
                    activity_log(queue_root, "health_broken",
                                 f"env {runner_env} broken; pausing runner",
                                 extra={"operator_action": reason[:500]})
                    _last_health_reason = reason
                update_runner_status(
                    "paused", job_id=None,
                    stage=f"health_broken: {reason[:120]}",
                )
                return True
            if _last_health_reason and eh.status == "ready":
                log(queue_root, "INFO", "runner resumed: health ready")
                activity_log(queue_root, "health_ready",
                             f"env {runner_env} healthy; resuming")
                _last_health_reason = ""

        if not runner_env:
            # poly-i7y: no env means dsynth_active() has nothing to ask
            # about, so the busy gate is not "off" — it is unanswerable.
            # Hold rather than run ungated. Reversible from the tracker
            # UI: selecting an env re-resolves on the next tick.
            reason = _no_env_reason()
            if reason != _last_no_env_reason:
                log(queue_root, "INFO", f"runner paused: {reason}")
                activity_log(queue_root, "no_env",
                             "no dev-env resolved; holding jobs "
                             "(dsynth-busy gate unavailable)",
                             extra={"reason": reason[:500]})
                _last_no_env_reason = reason
            update_runner_status(
                "paused", job_id=None,
                stage=f"no_dev_env: {reason[:120]}",
            )
            return True
        if _last_no_env_reason:
            log(queue_root, "INFO",
                f"runner resumed: dev-env {runner_env} resolved")
            _last_no_env_reason = ""
        busy, reason = dsynth_active(runner_env, queue_root)
        if busy and reason != _last_busy_reason:
            log(queue_root, "INFO", f"runner paused: {reason}")
            update_runner_status("paused", job_id=None, stage=f"waiting: {reason}")
            _last_busy_reason = reason
        elif not busy and _last_busy_reason:
            log(queue_root, "INFO", "runner resumed: dsynth idle")
            update_runner_status("idle", job_id=None, stage="waiting")
            _last_busy_reason = ""
        return busy

    try:
        if args.once:
            if _gate_blocked():
                log(queue_root, "INFO",
                    f"skipping --once: {_current_stage or 'gate blocked'}")
            else:
                batch = claim_next_job_batch(queue_root)
                if batch:
                    lead, siblings = batch
                    process_job(queue_root, lead, siblings, args.dry_run, playbooks_dir)
                else:
                    log(queue_root, "INFO", "no jobs in queue")
        else:
            while True:
                if _gate_blocked():
                    if _last_health_reason:
                        # Don't hammer the chroot probing while the env
                        # is known broken; the cache (default 60s) is
                        # what limits the rate. Sleep aligns with that.
                        time.sleep(health_cache_seconds)
                    else:
                        time.sleep(DSYNTH_LOCK_POLL_SECONDS)
                    continue
                process_user_context_updates(queue_root)
                process_verify_requests(queue_root)
                process_build_requests(queue_root)
                batch = claim_next_job_batch(queue_root)
                if batch:
                    lead, siblings = batch
                    process_job(queue_root, lead, siblings, args.dry_run, playbooks_dir)
                else:
                    update_runner_status("idle", job_id=None, stage="waiting")
                    time.sleep(5)
    except KeyboardInterrupt:
        log(queue_root, "INFO", "shutting down (keyboard interrupt)")
        activity_log(queue_root, "runner_stop", "Runner stopped (keyboard interrupt)")
    finally:
        stop_heartbeat()
        update_runner_status("stopped", job_id=None, stage=None)
        deregister_runner()
        release_runner_lock()

    return 0


if __name__ == "__main__":
    sys.exit(main())
