"""Shared SQLite schema for state.db.

state.db is owned by ``artifact-store`` (the single writer); the tracker
is a read+write consumer under WAL. Defining the schema here keeps both
in sync and makes integration tests easy (import + spin up an in-memory
DB).

The DB is wiped, never migrated — so this is a single clean set of
``CREATE`` statements, not a base schema plus a tail of ``ALTER``
patches. Add a column by editing its table here.
"""

from __future__ import annotations

import sqlite3

# Default build types seeded into the build_types table on first start.
DEFAULT_BUILD_TYPES: tuple[str, ...] = ("test", "release")

# All CREATE TABLE / CREATE INDEX statements for state.db. Idempotent —
# safe to re-run on an existing DB.
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    profile TEXT,
    path TEXT,
    ts_start TEXT,
    ts_end TEXT,
    last_seen_at TEXT,
    build_run_id INTEGER,
    target TEXT
);

-- Issue: one fingerprinted problem grouping many
-- occurrences (bundles). issue_key = short hash of
-- (target, origin, fingerprint); every occurrence carries the same key.
-- Resolution axis: unresolved -> resolving (a fix accepted) -> resolved (a
-- confirm build proved it); muted silences surfacing AND auto-triage. That
-- is the whole stored vocabulary — `regressed` is NOT a state here: a fix
-- that came back is derived on read from green_head_run_id (C3,
-- issue_state.derived_regression), so the badge can never disagree with what
-- the builds actually did. The rollups (times_seen / first_seen / last_seen /
-- latest_bundle_id) are denormalized so the issue list is one cheap
-- read; the artifact-store is the sole writer (single-writer invariant).
CREATE TABLE IF NOT EXISTS issues (
    issue_key        TEXT PRIMARY KEY,
    target           TEXT,
    origin           TEXT NOT NULL,
    fingerprint      TEXT,
    state            TEXT NOT NULL DEFAULT 'unresolved'
                     CHECK (state IN ('unresolved', 'resolving',
                                      'resolved', 'muted')),
    times_seen       INTEGER NOT NULL DEFAULT 0,
    first_seen_at    TEXT,
    last_seen_at     TEXT,
    latest_bundle_id TEXT,
    -- the occurrence whose fix is the issue's deliverable, set when a fix is
    -- accepted (state -> resolving); cleared on reopen / recurrence
    delivery_bundle_id TEXT,
    resolved_at      TEXT,
    -- Legacy. Nothing writes it since C3 made regression a derived read;
    -- the crossing timestamp comes from the boundary-crossing occurrence.
    regressed_at     TEXT,
    muted_at         TEXT,
    muted_by         TEXT,
    reopened_at      TEXT,
    reopened_by      TEXT,
    -- build-confirmed resolution (C1). The `resolving` transition bumps
    -- requested_build_generation; the runner reconcile loop enqueues a
    -- confirm build while requested > last_confirmed; a green build advances
    -- last_confirmed and resolves. building_generation is the single-flight
    -- marker (NULL = no confirm build in flight).
    requested_build_generation      INTEGER NOT NULL DEFAULT 0,
    last_confirmed_build_generation INTEGER NOT NULL DEFAULT 0,
    building_generation             INTEGER,
    -- Green Head (A2): the build_runs ordinal that was current when a
    -- confirm build resolved this issue — a known-good watermark, not a
    -- build of its own. The confirm build itself never appears in
    -- build_runs (dsynth hooks are disabled inside the agent dev-env), so
    -- this records the boundary: a LATER farm build re-emitting this
    -- fingerprint is a genuine regression. C3 derives `regressed` from it,
    -- against runs.build_run_id on the occurrence's run.
    green_head_run_id               INTEGER,
    -- A4: consecutive green confirm builds so far. A single green is
    -- provisional (build flakiness: network, toolchain); the issue only
    -- resolves once this reaches DP_CONFIRM_GREEN_THRESHOLD. Reset to 0 on
    -- a red verdict and on resolve.
    confirm_green_count             INTEGER NOT NULL DEFAULT 0,
    -- Consecutive confirm builds that could not RUN (env gone, no diff to
    -- replay, tracker unreachable) — as opposed to ran-and-failed, which is a
    -- red verdict. Bounded by DP_CONFIRM_MAX_FAILURES so a permanently
    -- unbuildable fix stops retrying and goes to a human instead of parking
    -- the issue in `resolving` forever. Reset on any produced verdict.
    confirm_failure_count           INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
);

-- Occurrence: one dsynth build failure and the agent's repair of it.
-- issue_key links it to its fingerprinted issue (plain column + index,
-- not a hard FK — mirrors jobs.bundle_id; the issue is find-or-created
-- at ingest just before the occurrence is written). result is the raw
-- hook verdict ('failure'); resolution carries the agent/operator
-- disposition (agent_fixed / accepted / merged / rejected / ...).
CREATE TABLE IF NOT EXISTS bundles (
    bundle_id TEXT PRIMARY KEY,
    run_id TEXT,
    origin TEXT,
    flavor TEXT,
    ts_utc TEXT,
    result TEXT,
    path TEXT,
    last_seen_at TEXT,
    target TEXT,
    issue_key TEXT,
    -- normalized first-error fingerprint, computed at ingest (the issue key input)
    error_signature TEXT,
    -- agent/operator disposition; NULL = no disposition yet
    resolution TEXT,
    pre_terminal_resolution TEXT,
    -- dops substrate assessment, written by the runner at triage time
    dops_state TEXT,
    -- independent fix verification (verify-fix orchestrator)
    verification_status TEXT,
    verification_at TEXT,
    verification_applied_diff_sha256 TEXT,
    -- operator accept / reject
    accepted_at TEXT,
    accepted_by TEXT,
    rejected_at TEXT,
    rejection_reason TEXT,
    -- operator take-over (resolution='operator_owned')
    taken_over_at TEXT,
    taken_over_by TEXT,
    -- operator discard (terminal)
    discarded_at TEXT,
    discard_reason TEXT,
    -- terminal-state reopen forensics
    reopened_at TEXT,
    reopened_by TEXT,
    reopened_from TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    state TEXT,
    type TEXT,
    origin TEXT,
    flavor TEXT,
    bundle_dir TEXT,
    created_ts_utc TEXT,
    path TEXT,
    last_error TEXT,
    last_seen_at TEXT,
    target TEXT,
    last_transition_at TEXT,
    retire_reason TEXT,
    -- canonical relation to the occurrence it works on
    bundle_id TEXT,
    -- which runner last transitioned this job (see runners.runner_id).
    -- Recorded only; no code branches on it.
    owner_id TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    bundle_id TEXT,
    relpath TEXT,
    kind TEXT,
    mtime REAL,
    size INTEGER,
    PRIMARY KEY (bundle_id, relpath)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    job_id TEXT,
    bundle_id TEXT,
    stage TEXT,
    message TEXT,
    duration_ms INTEGER,
    extra_json TEXT
);

CREATE TABLE IF NOT EXISTS runner_status (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'unknown',
    job_id TEXT,
    current_stage TEXT,
    started_at TEXT,
    updated_at TEXT,
    extra_json TEXT
);

-- One row per runner process. Distinct from runner_status, which stays a
-- singleton because it is the UI's "what is the runner doing" panel. This
-- table is the forensic record of who did what, and the substrate a future
-- multi-runner lease would build on.
CREATE TABLE IF NOT EXISTS runners (
    runner_id TEXT PRIMARY KEY,
    hostname TEXT,
    pid INTEGER,
    started_at TEXT,
    last_heartbeat_at TEXT,
    stopped_at TEXT
);

CREATE TABLE IF NOT EXISTS env_health_status (
    env TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    probed_at TEXT,
    operator_action TEXT,
    detail_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_context (
    run_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    context_text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    context_rev INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (run_id, origin)
);

CREATE TABLE IF NOT EXISTS user_context_requests (
    run_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    confidence TEXT,
    classification TEXT,
    iteration INTEGER,
    max_iterations INTEGER,
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    last_context_rev_handled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, origin, bundle_id)
);

-- Append-only history of every operator-submitted context for a
-- (run_id, origin). user_context above carries only the current row
-- (overwritten each submission); this preserves each round verbatim.
CREATE TABLE IF NOT EXISTS user_context_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    context_rev INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    text TEXT NOT NULL,
    submitted_by TEXT
);

CREATE TABLE IF NOT EXISTS blob_objects (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_refs (
    bundle_id TEXT NOT NULL,
    relpath TEXT NOT NULL,
    backend TEXT NOT NULL,
    sha256 TEXT,
    fs_path TEXT,
    kind TEXT,
    size INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (bundle_id, relpath)
);

-- Typed job lifecycle. Every transition writes one row; jobs.state is a
-- denormalized cache of the latest, job_events is authoritative.
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    job_id TEXT NOT NULL,
    from_state TEXT,            -- NULL on initial HOOK_ENQUEUED
    to_state TEXT NOT NULL,
    event_name TEXT NOT NULL,   -- one of the JobEvent enum values
    actor TEXT,                 -- "hook", "runner", "runner-<pid>", "tests", ...
    detail_json TEXT
);

-- Operator-triggered verify: the tracker INSERTs a row, the runner polls
-- and enqueues the verify job (keeps the tracker off the runner's queue
-- filesystem). status: 'pending' | 'enqueued' | 'failed'.
CREATE TABLE IF NOT EXISTS verify_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id       TEXT NOT NULL,
    env             TEXT NOT NULL,
    requested_by    TEXT NOT NULL DEFAULT 'operator',
    requested_at    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    job_id          TEXT,
    error           TEXT
);

-- Operator take-over / discard skip lock: subsequent dsynth hooks for a
-- locked (target, origin) produce a tombstone instead of fresh triage.
-- At most one OPEN lock per (target, origin); cleared rows are forensics.
CREATE TABLE IF NOT EXISTS origin_skip_flags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target          TEXT NOT NULL,
    origin          TEXT NOT NULL,
    set_by          TEXT NOT NULL DEFAULT 'operator',
    set_at          TEXT NOT NULL,
    reason          TEXT NOT NULL,
    bundle_id       TEXT,
    cleared_at      TEXT,
    cleared_by      TEXT
);

-- Per-bundle review-request tracking. Append-only; every delivery
-- attempt writes a row. provider_pr_id is the upstream identifier
-- (PR number / MR iid / outbox filename). status: created -> updated ->
-- closed / merged / create_failed. The partial-unique index below keys
-- one open delivery per (provider, branch) — branch encodes
-- (origin, target, signature), so retries of one root cause roll onto
-- one PR and a double-clicked Accept can't open two.
CREATE TABLE IF NOT EXISTS bundle_review_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id       TEXT NOT NULL,
    provider        TEXT NOT NULL,
    provider_pr_id  TEXT,
    url             TEXT,
    branch          TEXT,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'created',
    created_at      TEXT NOT NULL,
    last_synced_at  TEXT,
    error           TEXT,
    operator        TEXT,
    error_signature TEXT,
    note            TEXT,
    diff_sha256     TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_activity_log_ts ON activity_log(ts);
CREATE INDEX IF NOT EXISTS idx_activity_log_bundle
    ON activity_log(bundle_id) WHERE bundle_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_env_health_status_status ON env_health_status(status);
CREATE INDEX IF NOT EXISTS idx_user_context_updated ON user_context(updated_at);
CREATE INDEX IF NOT EXISTS idx_user_context_requests_pending
    ON user_context_requests(status, requested_at);
CREATE INDEX IF NOT EXISTS idx_user_context_history_lookup
    ON user_context_history(run_id, origin, context_rev);
CREATE INDEX IF NOT EXISTS idx_artifact_refs_bundle ON artifact_refs(bundle_id);
CREATE INDEX IF NOT EXISTS idx_artifact_refs_sha ON artifact_refs(sha256);

-- issues
CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state);
CREATE INDEX IF NOT EXISTS idx_issues_origin_target ON issues(origin, target);
CREATE INDEX IF NOT EXISTS idx_issues_last_seen ON issues(last_seen_at);

-- bundles
CREATE INDEX IF NOT EXISTS idx_bundles_target ON bundles(target);
CREATE INDEX IF NOT EXISTS idx_bundles_origin_target_seen
    ON bundles(origin, target, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_bundles_resolution ON bundles(resolution);
CREATE INDEX IF NOT EXISTS idx_bundles_signature_origin
    ON bundles(origin, target, error_signature);
CREATE INDEX IF NOT EXISTS idx_bundles_verification_status
    ON bundles(verification_status);
CREATE INDEX IF NOT EXISTS idx_bundles_issue_key ON bundles(issue_key);

-- jobs
CREATE INDEX IF NOT EXISTS idx_jobs_target ON jobs(target);
CREATE INDEX IF NOT EXISTS idx_jobs_bundle_id ON jobs(bundle_id);

-- runs
CREATE INDEX IF NOT EXISTS idx_runs_target ON runs(target);

-- verify_requests
CREATE INDEX IF NOT EXISTS idx_verify_requests_status
    ON verify_requests(status, requested_at);
CREATE INDEX IF NOT EXISTS idx_verify_requests_bundle
    ON verify_requests(bundle_id, requested_at);

-- origin_skip_flags
CREATE UNIQUE INDEX IF NOT EXISTS uq_origin_skip_flags_open
    ON origin_skip_flags(target, origin) WHERE cleared_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_origin_skip_flags_lookup
    ON origin_skip_flags(target, origin, cleared_at);

-- bundle_review_requests
CREATE INDEX IF NOT EXISTS idx_brr_bundle
    ON bundle_review_requests(bundle_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_brr_open_branch
    ON bundle_review_requests(provider, branch)
    WHERE status NOT IN ('closed', 'merged', 'create_failed');

-- Tracker tables (build tracking) folded into state.db.
CREATE TABLE IF NOT EXISTS build_types (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS build_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    build_type TEXT NOT NULL REFERENCES build_types(name),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    commit_sha TEXT,
    commit_branch TEXT,
    commit_pushed_at TEXT,
    total_expected INTEGER
);

CREATE TABLE IF NOT EXISTS build_results (
    build_run_id INTEGER NOT NULL REFERENCES build_runs(id),
    origin TEXT NOT NULL,
    version TEXT NOT NULL,
    result TEXT NOT NULL,
    log_url TEXT,
    recorded_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded',
    PRIMARY KEY (build_run_id, origin)
);

CREATE TABLE IF NOT EXISTS port_status (
    target TEXT NOT NULL,
    origin TEXT NOT NULL,
    last_attempt_version TEXT,
    last_attempt_result TEXT,
    last_attempt_at TEXT,
    last_attempt_run_id INTEGER REFERENCES build_runs(id),
    last_success_version TEXT,
    last_success_at TEXT,
    last_success_run_id INTEGER REFERENCES build_runs(id),
    PRIMARY KEY (target, origin)
);

CREATE TABLE IF NOT EXISTS tracker_active_env (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    env_name  TEXT,
    set_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_build_runs_target ON build_runs(target);
CREATE INDEX IF NOT EXISTS idx_build_results_origin ON build_results(origin);
CREATE INDEX IF NOT EXISTS idx_port_status_target ON port_status(target);
CREATE INDEX IF NOT EXISTS idx_port_status_failures
    ON port_status(target, last_attempt_result);
CREATE INDEX IF NOT EXISTS idx_build_runs_target_type_started
    ON build_runs(target, build_type, started_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_build_runs_active
    ON build_runs(target, build_type)
    WHERE finished_at IS NULL;
"""

# Every column lives in its CREATE above (a fresh DB needs no patches). These
# ADD COLUMN statements are the non-destructive path for an existing state.db
# that predates a column — run tolerantly by init_db (a duplicate-column error
# is caught). They keep real issue history instead of forcing a wipe.
MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE issues ADD COLUMN requested_build_generation "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE issues ADD COLUMN last_confirmed_build_generation "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE issues ADD COLUMN building_generation INTEGER",
    "ALTER TABLE issues ADD COLUMN green_head_run_id INTEGER",
    "ALTER TABLE issues ADD COLUMN confirm_green_count "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE issues ADD COLUMN confirm_failure_count "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN owner_id TEXT",
)

# One-shot data repair for a state.db written before C3 removed `regressed`
# from the stored vocabulary. Idempotent: after the first run no row matches.
# A pre-existing DB keeps its unconstrained `state` column (SQLite cannot add
# a CHECK by ALTER), so this is what keeps it inside the vocabulary — nothing
# writes `regressed` any more.
_RETIRE_STORED_REGRESSED: tuple[str, ...] = (
    # Resolved and then came back: the resolution axis says resolved, and the
    # derivation reproduces the badge from the occurrences past the boundary.
    "UPDATE issues SET state = 'resolved' "
    "WHERE state = 'regressed' AND resolved_at IS NOT NULL",
    # No resolved_at means it was flipped out of `resolving` by an arriving
    # occurrence — a fix that was accepted but never proved. Calling that
    # `resolved` would claim a resolution that never happened, so it goes back
    # to the open state it effectively had.
    "UPDATE issues SET state = 'unresolved' WHERE state = 'regressed'",
)


def init_db(conn: sqlite3.Connection) -> None:
    """Run schema + seeds on an open connection.

    Called by artifact-store at startup. Sets PRAGMAs first so the rest
    of the call inherits them.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # Enforce FK constraints on the build_* tracker tables. The other
    # cross-table references (jobs.bundle_id, bundles.issue_key) are
    # plain indexed columns, not declared FKs, so they're unaffected.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO build_types(name) VALUES (?)",
        [(name,) for name in DEFAULT_BUILD_TYPES],
    )
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    # Not wrapped in the tolerant try above: these are UPDATEs, and swallowing
    # an OperationalError here would hide a real failure rather than a
    # duplicate column.
    for stmt in _RETIRE_STORED_REGRESSED:
        conn.execute(stmt)
    conn.commit()
