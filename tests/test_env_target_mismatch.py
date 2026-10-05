"""A job runs only in an env of its own build line (poly-7pwa.14).

A dev-env composes one build line (state.target). Hook-created jobs name no
env, so before this change every job ran in whichever env was selected,
whatever line it was for. Since poly-p2ve an unpinned job routes to the
env of its own line; it is held at claim time only when several envs
compose that line and none is selected, kept across a restart while held,
and a confirm build waits the same way without spending its budget. A
verify request for another line fails with a reason, and a job pinned to an
env of another line is refused at job start.

Every build line here is a stub value: no test reads DeltaPorts.
Names this change adds are read with getattr and stubbed with
raising=False, so the file collects and runs on the tree before it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from dportsv3.agent import env_resolver, lifecycle, runner, worker
from dportsv3.db.schema import init_db

ENV_TARGET_MISMATCH = getattr(lifecycle.JobEvent, "ENV_TARGET_MISMATCH", None)
_REAL_ENV_COMPOSE_TARGET = getattr(env_resolver, "env_compose_target", None)

Q3 = "@2026Q3"
MAIN = "@main"
NOW = "2026-10-04T00:00:00+00:00"
OLD = "2000-01-01T00:00:00+00:00"


class _Stop(Exception):
    """Raised by a stub where the job would go on to real work."""


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "state.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_db(c)
    monkeypatch.setattr(runner, "_state_db_conn", c)
    monkeypatch.setattr(runner, "_state_db_lock", threading.Lock())
    yield c
    c.close()


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "queue"
    for sub in ("pending", "inflight", "done", "failed"):
        (q / sub).mkdir(parents=True)
    return q


def _host(monkeypatch, envs: dict, selected: str | None = None) -> None:
    """The envs on this host, {name: line or exception}, and the one
    selected. An env not named here cannot be read."""
    def _compose(env):
        value = envs.get(env, LookupError(f"environment not found: {env}"))
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(env_resolver, "env_compose_target", _compose,
                        raising=False)
    monkeypatch.setattr(env_resolver, "list_available_envs",
                        lambda: tuple(sorted(envs)))
    _select(monkeypatch, selected)


def _select(monkeypatch, env: str | None) -> None:
    monkeypatch.setattr(runner, "_CLI_ENV_DEFAULT", env)
    monkeypatch.setattr(runner, "_GATE_RESOLVE_CACHE", None)


def _job_row(conn, job_id: str):
    return conn.execute(
        "SELECT state, retire_reason, dev_env FROM jobs WHERE job_id = ?",
        (job_id,)).fetchone()


def _activity(conn, stage: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM activity_log WHERE stage = ? ORDER BY id",
        (stage,)).fetchall()


# --- the claim-time hold ------------------------------------------------------


def _seed_db_job(conn, *, job_id: str, target: str, bundle_id: str = "b1",
                 origin: str = "lang/rust", profile: str | None = "2026Q3",
                 job_type: str = "triage", old: bool = False) -> None:
    """A hook-created job as the HTTP half leaves it: no file in pending/."""
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, profile, target) "
        "VALUES ('run-1', ?, ?)", (profile, target))
    conn.execute(
        "INSERT OR IGNORE INTO bundles (bundle_id, run_id, origin, flavor, "
        "target) VALUES (?, 'run-1', ?, '', ?)", (bundle_id, origin, target))
    conn.execute(
        "INSERT INTO jobs (job_id, state, type, origin, flavor, "
        "created_ts_utc, target, bundle_id, path, last_transition_at) "
        "VALUES (?, 'queued', ?, ?, '', ?, ?, ?, '/elsewhere', ?)",
        (job_id, job_type, origin, NOW, target, bundle_id,
         OLD if old else NOW))
    conn.commit()


def _pending(queue: Path, name: str, **meta) -> Path:
    fields = {"type": "triage", "profile": "2026Q3", "origin": "lang/rust",
              "flavor": "", "bundle_id": "b1", "run_id": "run-1"}
    fields.update(meta)
    path = queue / "pending" / name
    path.write_text("".join(f"{k}={v}\n" for k, v in fields.items()))
    return path


def test_a_db_job_for_another_line_routes_to_its_line_s_env(
        conn, queue, monkeypatch):
    # poly-p2ve: nothing to select; the job runs in the env of its line.
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _seed_db_job(conn, job_id="j1.job", target=Q3)

    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"
    assert runner.resolve_env(runner.parse_job_file(batch[0])) == "q3"


def test_a_db_job_waits_while_several_envs_compose_its_line(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3a": Q3, "q3b": Q3}, selected="main")
    _seed_db_job(conn, job_id="j1.job", target=Q3)

    assert runner.claim_next_job_batch(queue) is None
    assert _job_row(conn, "j1.job")["state"] == "queued"
    assert runner._idle_stage() == (
        "waiting: queued jobs cannot route to an env of their line: "
        "1 for @2026Q3 (2 envs compose it (q3a, q3b); select one)")

    _select(monkeypatch, "q3b")
    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"
    assert runner.resolve_env(runner.parse_job_file(batch[0])) == "q3b"


def test_a_line_whose_env_is_broken_waits(conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    monkeypatch.setattr(runner, "_cached_health_broken",
                        lambda env=None: env == "q3")
    _seed_db_job(conn, job_id="j1.job", target=Q3)

    assert runner.claim_next_job_batch(queue) is None
    assert "env q3 is broken" in runner._idle_stage()


def test_a_held_db_job_survives_a_runner_restart(conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3a": Q3, "q3b": Q3}, selected="main")
    _seed_db_job(conn, job_id="j1.job", target=Q3, old=True)
    assert runner.claim_next_job_batch(queue) is None

    reap = getattr(runner, "_reap_stale_queued_at_startup", None)
    assert reap is not None
    assert reap(queue, 3600) == []
    row = _job_row(conn, "j1.job")
    assert (row["state"], row["retire_reason"]) == ("queued", None)

    _select(monkeypatch, "q3a")
    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"


@pytest.mark.parametrize("seed", [
    {"target": "@2026Q3-editors_vim"},      # a line no env here composes
    {"target": Q3, "job_type": "patch"},    # the DB claim takes only triage
    {"target": Q3, "profile": None},        # never claimed without a profile
], ids=["no-env-composes-it", "patch-row", "no-profile"])
def test_a_stale_row_the_db_claim_never_takes_is_still_reaped(
        conn, queue, monkeypatch, seed):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _seed_db_job(conn, job_id="j1.job", old=True, **seed)

    reap = getattr(runner, "_reap_stale_queued_at_startup", None)
    assert reap is not None
    assert reap(queue, 3600) == ["j1.job"]
    row = _job_row(conn, "j1.job")
    assert (row["state"], row["retire_reason"]) == ("dead", "runner_restart")


def test_a_pending_file_for_an_ambiguous_line_is_not_claimed(conn, queue,
                                                            monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3a": Q3, "q3b": Q3}, selected="main")
    path = _pending(queue, "j1.job", target=Q3)

    assert runner.claim_next_job_batch(queue) is None
    assert path.exists()


def test_a_pending_file_for_another_line_is_claimed(conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _pending(queue, "j1.job", target=Q3)

    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"


def test_a_pinned_job_is_not_held(conn, queue, monkeypatch):
    # Its env can never change, so a hold would last forever; the
    # job-start check refuses it instead.
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _pending(queue, "j1.job", target=Q3, dev_env="main")

    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"


def test_a_job_for_the_selected_line_is_claimed(conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _pending(queue, "j1.job", target=MAIN)

    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"


def test_a_target_no_env_here_composes_is_not_held(conn, queue, monkeypatch):
    # An old hook conf with no target derives @<profile>; holding it would
    # hold it forever. It is claimed, for the job-start refusal.
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _pending(queue, "j1.job", target="@2026Q3-editors_vim")

    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"


def test_nothing_is_held_when_the_selected_env_s_line_cannot_be_read(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": LookupError("unreadable"), "q3": Q3},
          selected="main")
    _pending(queue, "j1.job", target=Q3)

    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"


# --- confirm builds -------------------------------------------------------------


def _resolving_issue(conn, *, target: str, key: str = "k1",
                     bundle: str = "b1") -> None:
    conn.execute(
        "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
        "delivery_bundle_id, requested_build_generation, "
        "last_confirmed_build_generation, building_generation, "
        "confirm_green_count, confirm_failure_count, updated_at) "
        "VALUES(?,?,'lang/rust',?, 'resolving', ?, 1, 0, NULL, 0, 0, ?)",
        (key, target, f"fp-{key}", bundle, NOW))
    conn.execute(
        "INSERT INTO bundles(bundle_id, origin, target, issue_key, ts_utc, "
        "result, resolution, verification_status) "
        "VALUES(?, 'lang/rust', ?, ?, ?, 'failure', 'accepted', 'verified')",
        (bundle, target, key, NOW))
    conn.commit()


def _confirm_jobs(queue: Path) -> list[Path]:
    return sorted((queue / "pending").glob("*confirm.job"))


def test_a_confirm_for_an_ambiguous_line_waits_and_costs_no_budget(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3a": Q3, "q3b": Q3}, selected="main")
    _resolving_issue(conn, target=Q3)

    for _ in range(4):
        runner.process_build_requests(queue)

    assert _confirm_jobs(queue) == []
    row = conn.execute(
        "SELECT building_generation, confirm_failure_count, state "
        "FROM issues WHERE issue_key = 'k1'").fetchone()
    assert tuple(row) == (None, 0, "resolving")


def test_a_confirm_for_another_line_is_pinned_to_that_line_s_env(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _resolving_issue(conn, target=Q3)

    runner.process_build_requests(queue)
    jobs = _confirm_jobs(queue)
    assert len(jobs) == 1
    assert "dev_env=q3" in jobs[0].read_text().splitlines()

    _select(monkeypatch, "main")
    assert runner.resolve_env(runner.parse_job_file(jobs[0])) == "q3"


def test_a_confirm_for_the_selected_line_is_enqueued_as_before(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _resolving_issue(conn, target=MAIN)

    runner.process_build_requests(queue)
    assert len(_confirm_jobs(queue)) == 1


def test_a_confirm_for_a_line_no_env_here_composes_is_not_held(
        conn, queue, monkeypatch):
    # Left to the verify-fix backstop and the existing confirm budget.
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    _resolving_issue(conn, target="@2026Q2")

    runner.process_build_requests(queue)
    assert len(_confirm_jobs(queue)) == 1


def test_a_job_pinned_to_a_broken_env_waits_until_it_is_repaired(
        conn, queue, monkeypatch):
    # Claimed, it would be retired ENV_BROKEN; before poly-p2ve the global
    # health pause kept it queued.
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected=None)
    broken = {"q3"}
    monkeypatch.setattr(runner, "_cached_health_broken",
                        lambda env=None: env in broken)
    path = _pending(queue, "j1.job", type="patch", target=Q3, dev_env="q3")

    assert runner.claim_next_job_batch(queue) is None
    assert path.exists()
    assert "env q3 is broken" in runner._idle_stage()

    broken.clear()
    batch = runner.claim_next_job_batch(queue)
    assert batch is not None and batch[0].name == "j1.job"


def test_a_job_without_a_target_waits_while_several_envs_exist(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected=None)
    path = _pending(queue, "j1.job", target="")

    assert runner.claim_next_job_batch(queue) is None
    assert path.exists()
    assert "(no target)" in runner._idle_stage()

    _select(monkeypatch, "main")
    assert runner.claim_next_job_batch(queue) is not None


# --- the gate (poly-p2ve) --------------------------------------------------------


def test_two_lines_and_nothing_selected_watch_both_envs(monkeypatch):
    # Before poly-p2ve this paused the runner: "2 dev-envs exist; select one".
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected=None)
    assert runner._watched_envs(None) == ["main", "q3"]


def test_an_ambiguous_line_s_envs_are_not_watched(monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3a": Q3, "q3b": Q3}, selected=None)
    assert runner._watched_envs(None) == ["main"]


def test_no_env_anywhere_watches_nothing(monkeypatch):
    _host(monkeypatch, {}, selected=None)
    assert runner._watched_envs(None) == []


def test_unreadable_lines_fall_back_to_the_selected_env(monkeypatch):
    _host(monkeypatch, {"main": LookupError("unreadable")}, selected="main")
    assert runner._watched_envs("main") == ["main"]


# --- verify requests ------------------------------------------------------------


def _fixed_bundle_and_request(conn, *, env: str, target: str = Q3) -> int:
    conn.execute(
        "INSERT INTO bundles (bundle_id, run_id, origin, flavor, ts_utc, "
        "result, target, path, last_seen_at, resolution) "
        "VALUES ('b1', '', 'lang/rust', '', ?, 'failure', ?, '', ?, "
        "'agent_fixed')", (NOW, target, NOW))
    cur = conn.execute(
        "INSERT INTO verify_requests (bundle_id, env, requested_by, "
        "requested_at, status) VALUES ('b1', ?, 'operator', ?, 'pending')",
        (env, NOW))
    conn.commit()
    return cur.lastrowid


def test_a_verify_request_for_another_build_line_never_starts(
        conn, queue, monkeypatch):
    from dportsv3.tracker import fix_state

    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    req_id = _fixed_bundle_and_request(conn, env="main")

    runner.process_verify_requests(queue)

    assert list((queue / "pending").glob("*-verify.job")) == []
    request = dict(conn.execute(
        "SELECT * FROM verify_requests WHERE id = ?", (req_id,)).fetchone())
    assert request["status"] == "failed"
    assert Q3 in request["error"] and "composes @main" in request["error"]
    bundle = dict(conn.execute(
        "SELECT * FROM bundles WHERE bundle_id = 'b1'").fetchone())
    assert bundle["verification_status"] is None
    state = fix_state.verify_state(bundle, request)
    assert state.key == fix_state.VERIFY_NOT_STARTED
    assert request["error"] in state.detail
    assert fix_state.fix_status(bundle).key == "needs_review"


def test_a_verify_request_for_its_own_line_is_enqueued(conn, queue,
                                                       monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    req_id = _fixed_bundle_and_request(conn, env="q3")

    runner.process_verify_requests(queue)

    assert len(list((queue / "pending").glob("*-verify.job"))) == 1
    assert conn.execute("SELECT status FROM verify_requests WHERE id = ?",
                        (req_id,)).fetchone()[0] == "enqueued"


# --- the job-start check --------------------------------------------------------


def _claimed(conn, job_id: str, *, start, target: str = Q3,
             job_type: str = "patch", origin: str = "lang/rust") -> None:
    conn.execute(
        "INSERT INTO jobs (job_id, state, type, origin, target, bundle_id) "
        "VALUES (?, 'queued', ?, ?, ?, 'b1')",
        (job_id, job_type, origin, target))
    conn.commit()
    lifecycle.apply(conn, job_id, lifecycle.JobEvent.CLAIM)
    if start is not None:
        lifecycle.apply(conn, job_id, start)


def _patch_jobs(conn, queue: Path, *, target: str) -> tuple[Path, Path]:
    lead = queue / "inflight" / "j1.job"
    sib = queue / "inflight" / "j2.job"
    for path in (lead, sib):
        path.write_text(f"type=patch\ntarget={target}\n")
        _claimed(conn, path.name, start=lifecycle.JobEvent.PATCH_START,
                 target=target)
    return lead, sib


def _stub_patch_path(monkeypatch, *, checkout_ok: bool = True) -> dict:
    """Stub everything process_patch_job does past the check. Returns
    what each stub saw."""
    seen: dict = {"checkout": [], "payload": 0}

    def _checkout(**kw):
        seen["checkout"].append(kw.get("env"))
        return checkout_ok

    def _payload(*a, **k):
        seen["payload"] += 1
        raise _Stop()

    monkeypatch.setattr(runner, "_maybe_skip_locked_origin", lambda **kw: None)
    monkeypatch.setattr(runner, "_maybe_skip_muted_issue", lambda **kw: None)
    monkeypatch.setattr(runner, "_checkout_bundle_branch_for_job", _checkout)
    monkeypatch.setattr(runner, "build_patch_payload", _payload)
    return seen


def _run_patch(queue, lead, siblings, job):
    try:
        return runner.process_patch_job(queue, lead, siblings, job, None, None)
    except _Stop:
        return None


def test_a_patch_job_for_another_build_line_is_refused_before_its_worktree(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    lead, sib = _patch_jobs(conn, queue, target=Q3)
    seen = _stub_patch_path(monkeypatch)

    result = _run_patch(queue, lead, [sib], {
        "origin": "lang/rust", "bundle_id": "b1", "target": Q3,
        "dev_env": "main"})

    assert result is not None
    success, status = result
    assert success is False and status.startswith("env_target_mismatch")
    assert seen["checkout"] == [] and seen["payload"] == 0
    for job_id in ("j1.job", "j2.job"):
        row = _job_row(conn, job_id)
        assert (row["state"], row["retire_reason"]) == (
            "dead", "env_target_mismatch")
    rows = _activity(conn, "patch_refused_env_target_mismatch")
    assert len(rows) == 1
    assert Q3 in rows[0]["message"] and MAIN in rows[0]["message"]
    extra = json.loads(rows[0]["extra_json"])
    assert (extra["job_target"], extra["env"], extra["env_target"]) == (
        Q3, "main", MAIN)


@pytest.mark.parametrize("envs,start", [
    ({"main": MAIN, "q3": Q3},
     "the job is pinned to env main; run it in env q3, which composes "
     "@2026Q3"),
    ({"main": MAIN},
     "no env on this host composes @2026Q3: create one with "
     "'dportsv3 dev-env create --target @2026Q3'"),
], ids=["another-env-composes-it", "no-env-composes-it"])
def test_the_refusal_names_the_way_out(conn, queue, monkeypatch, envs, start):
    _host(monkeypatch, envs, selected="main")
    lead, _ = _patch_jobs(conn, queue, target=Q3)
    _stub_patch_path(monkeypatch)

    _run_patch(queue, lead, [], {"origin": "lang/rust", "bundle_id": "b1",
                                  "target": Q3, "dev_env": "main"})

    rows = _activity(conn, "patch_refused_env_target_mismatch")
    assert len(rows) == 1
    hint = json.loads(rows[0]["extra_json"])["hint"]
    assert hint.startswith(start)
    assert "--env" not in hint and "retry" not in hint.lower()


def test_a_patch_job_for_the_env_s_own_build_line_proceeds(conn, queue,
                                                          monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    lead, _ = _patch_jobs(conn, queue, target=MAIN)
    seen = _stub_patch_path(monkeypatch, checkout_ok=False)

    result = _run_patch(queue, lead, [], {
        "origin": "lang/rust", "bundle_id": "b1", "target": MAIN})

    # worktree_unavailable is the checkout stub's answer, not the check's.
    assert seen["checkout"] == ["main"]
    assert result == (False, "worktree_unavailable")


def test_an_env_whose_line_cannot_be_read_is_let_through(conn, queue,
                                                        monkeypatch):
    _host(monkeypatch, {"main": LookupError("unreadable")}, selected="main")
    lead, _ = _patch_jobs(conn, queue, target=Q3)
    seen = _stub_patch_path(monkeypatch, checkout_ok=False)

    result = _run_patch(queue, lead, [], {
        "origin": "lang/rust", "bundle_id": "b1", "target": Q3})

    assert seen["checkout"] == ["main"]
    assert result == (False, "worktree_unavailable")


@pytest.mark.parametrize("job_target", ["", MAIN])
def test_the_scope_cache_holds_the_env_s_line(conn, queue, monkeypatch,
                                              job_target):
    _host(monkeypatch, {"main": MAIN}, selected="main")
    lead, _ = _patch_jobs(conn, queue, target=job_target)
    _stub_patch_path(monkeypatch)
    monkeypatch.setattr(worker, "_TARGET_CACHE", {})
    job = {"origin": "lang/rust", "bundle_id": "b1", "target": job_target}

    _run_patch(queue, lead, [], job)

    assert worker.peek_env_target("main") == MAIN
    assert job["target"] == job_target


def test_the_env_checked_at_start_is_the_env_the_job_ends_in(conn, queue,
                                                            monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    lead = queue / "inflight" / "j1.job"
    lead.write_text("type=patch\norigin=lang/rust\nbundle_id=b1\n"
                    f"target={MAIN}\n")
    _claimed(conn, "j1.job", start=None, target=MAIN)

    def _checkout(**kw):
        _select(monkeypatch, "q3")      # the operator moves the selection
        return False

    dropped: list = []
    monkeypatch.setattr(runner, "_maybe_skip_locked_origin", lambda **kw: None)
    monkeypatch.setattr(runner, "_maybe_skip_muted_issue", lambda **kw: None)
    monkeypatch.setattr(runner, "_checkout_bundle_branch_for_job", _checkout)
    monkeypatch.setattr(runner, "_drop_bundle_branch_for_job",
                        lambda **kw: dropped.append(kw["env"]))

    runner.process_job(queue, lead, [], False, None)

    assert dropped == ["main"]


def test_a_refused_job_records_the_env_that_refused_it(conn, queue,
                                                      monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    lead, _ = _patch_jobs(conn, queue, target=Q3)
    _stub_patch_path(monkeypatch)

    _run_patch(queue, lead, [], {"origin": "lang/rust", "bundle_id": "b1",
                                  "target": Q3, "dev_env": "main"})

    row = _job_row(conn, "j1.job")
    assert (row["dev_env"], row["retire_reason"]) == (
        "main", "env_target_mismatch")


def test_a_triage_job_for_another_build_line_is_refused_before_the_model(
        conn, queue, monkeypatch):
    _host(monkeypatch, {"main": MAIN, "q3": Q3}, selected="main")
    lead = queue / "inflight" / "j1.job"
    lead.write_text(f"type=triage\ntarget={Q3}\n")
    _claimed(conn, "j1.job", start=lifecycle.JobEvent.TRIAGE_START,
             target=Q3, job_type="triage")
    built: list = []

    class _Orchestrator:
        def __init__(self, *a, **k):
            built.append("orchestrator")

        def run(self, *a, **k):
            raise _Stop()

    def _payload(*a, **k):
        built.append("payload")
        return "payload"

    monkeypatch.setattr(runner, "_maybe_skip_locked_origin", lambda **kw: None)
    monkeypatch.setattr(runner, "_maybe_skip_muted_issue", lambda **kw: None)
    monkeypatch.setattr(runner, "build_triage_payload", _payload)
    monkeypatch.setattr("dportsv3.agent.step.Orchestrator", _Orchestrator)

    try:
        runner.process_triage_job(queue, lead, [], {
            "origin": "lang/rust", "bundle_id": "b1", "target": Q3,
            "dev_env": "main"}, None, None)
    except _Stop:
        pass

    assert built == []
    row = _job_row(conn, "j1.job")
    assert (row["state"], row["retire_reason"]) == (
        "dead", "env_target_mismatch")
    assert len(_activity(conn, "triage_refused_env_target_mismatch")) == 1


# --- verify-fix ----------------------------------------------------------------


def _verify(env: str, calls: list):
    from dportsv3 import verify_fix

    def _get_json(url, timeout=10):
        return {"bundle_id": "b-1", "origin": "lang/rust", "target": Q3}

    def _ab(env_name, origin, *, diff_path=None, **kw):
        calls.append(env_name)
        return {"ok": True, "env": env_name, "origin": origin,
                "applied_diff_sha256": None, "apply_exit": 0,
                "reapply_exit": 0, "dsynth_exit": 0, "log_path": None}

    return verify_fix.run_verify_fix(
        bundle_id="b-1", env=env, tracker_url="http://t",
        _get_json=_get_json,
        _get_bytes=lambda url, timeout=20: b"--- a/x\n+++ b/x\n",
        _post_json=lambda url, body, timeout=10: {"ok": True},
        _put_artifact=lambda *a, **k: None,
        _apply_and_build=_ab,
    )


def test_verify_fix_refuses_an_env_of_another_build_line(monkeypatch):
    from dportsv3 import verify_fix

    _host(monkeypatch, {"main": MAIN, "q3": Q3})
    calls: list = []
    with pytest.raises(verify_fix.VerifyFixError) as exc:
        _verify("main", calls)
    assert Q3 in str(exc.value) and "composes @main" in str(exc.value)
    assert calls == []


def test_verify_fix_builds_in_an_env_of_the_bundle_s_line(monkeypatch):
    # The env's name says nothing; its line is what is compared.
    _host(monkeypatch, {"main": Q3})
    calls: list = []
    assert _verify("main", calls).ok is True
    assert calls == ["main"]


# --- names: the lifecycle event, the counters, the comparator ------------------


def test_a_mismatched_patch_job_retires_dead_with_its_own_reason(conn):
    _claimed(conn, "j1.job", start=lifecycle.JobEvent.PATCH_START)
    assert ENV_TARGET_MISMATCH is not None
    assert lifecycle.apply(conn, "j1.job", ENV_TARGET_MISMATCH) is (
        lifecycle.JobState.DEAD)
    row = _job_row(conn, "j1.job")
    assert (row["state"], row["retire_reason"]) == (
        "dead", "env_target_mismatch")


def test_the_refusal_is_not_counted_as_a_failed_patch_attempt(conn):
    # A regression guard: decision.py does not change.
    from dportsv3.agent.decision import PortHistory

    assert ENV_TARGET_MISMATCH is not None
    _claimed(conn, "j1.job", start=lifecycle.JobEvent.PATCH_START)
    lifecycle.apply(conn, "j1.job", ENV_TARGET_MISMATCH)
    _claimed(conn, "j2.job", start=lifecycle.JobEvent.PATCH_START)
    lifecycle.apply(conn, "j2.job", lifecycle.JobEvent.PATCH_GAVE_UP)

    history = PortHistory.load(conn, Q3, "lang/rust", 24)
    assert history.failed_patch_attempts == 1


def test_the_pipeline_counts_a_refusal_as_interrupted_not_failed(conn):
    from dportsv3.tracker.agentic_queries import job_outcome_counts

    assert ENV_TARGET_MISMATCH is not None
    _claimed(conn, "j1.job", start=lifecycle.JobEvent.PATCH_START)
    lifecycle.apply(conn, "j1.job", ENV_TARGET_MISMATCH)

    dead = job_outcome_counts(conn)["dead"]
    assert (dead["interrupted"], dead["failed"]) == (1, 0)


def _save_env(name: str, target: str) -> None:
    from dports_dev_env.config import load_config
    from dports_dev_env.state import (
        STATE_SCHEMA, EnvironmentState, RepoState, RuntimeState, SourceState,
    )
    from dports_dev_env.store import EnvironmentStore

    store = EnvironmentStore(load_config())
    store.save(EnvironmentState(
        schema=STATE_SCHEMA, name=name, backend="chroot", target=target,
        origin="", status="ready", created_at=NOW, updated_at=NOW,
        root_dir=store.root_dir(name), writable_dir=store.writable_dir(name),
        provisioned_base_id="base",
        repos=RepoState("master", "main", "master", "master"),
        source=SourceState("/delta", "/tool"),
        runtime=RuntimeState("/distfiles", "off"),
    ))


def test_env_compose_target_reads_the_env_state(set_setting, tmp_path):
    assert _REAL_ENV_COMPOSE_TARGET is not None
    set_setting("dev_env.envs_dir", str(tmp_path / "envs"))
    _save_env("q3", "@2026Q3")
    _save_env("main", "main")

    assert _REAL_ENV_COMPOSE_TARGET("q3") == "@2026Q3"
    assert _REAL_ENV_COMPOSE_TARGET("main") == "@main"
    with pytest.raises(LookupError):
        _REAL_ENV_COMPOSE_TARGET("absent")


@pytest.mark.parametrize("env,target,expected", [
    ("main", Q3, MAIN),
    ("main", "main", None),
    ("main", "", None),
    ("gone", Q3, None),
], ids=["differs", "bare-target-matches", "no-target", "unreadable-env"])
def test_other_build_line_names_the_env_s_line_only_when_it_differs(
        monkeypatch, env, target, expected):
    _host(monkeypatch, {"main": MAIN})
    other_build_line = getattr(env_resolver, "other_build_line", None)
    assert other_build_line is not None
    assert other_build_line(env, target) == expected
