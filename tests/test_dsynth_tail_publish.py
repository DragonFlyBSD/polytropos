"""A running build's last lines reach the job page (poly-pvs2).

The first version of this feature had the TRACKER read the build log. It
could not: resolving one goes through a root-only ``dev-env path`` while
the tracker runs unprivileged, so the tail rendered nothing and said
nothing about why (poly-paee). The runner is root and already has the env
open, so it publishes and the tracker only ever sees bytes.

These tests cover the three seams that arrangement creates: the runner
knowing WHEN to read, the row surviving the trip, and the live poll
refreshing a tail during a build that writes no activity rows at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.agent import runner as rm
from dportsv3.agent import state_store, steps
from dportsv3.common.tools import TAILABLE_TOOLS
from dportsv3.db import presence
from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import job_tail

RUNNER = "builder-A"
JOB = "job-1"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    init_db(c)
    return c


def _tail(**kw):
    base = {
        "job_id": JOB, "tool": "dsynth_build", "text": "cc -o foo\n",
        "lines": 1, "total_bytes": 4096, "skipped": 0,
        "max_bytes": 32768, "log_mtime": 1_700_000_000.0,
    }
    base.update(kw)
    return base


# --- the runner knows when to read ---------------------------------------


@pytest.fixture(autouse=True)
def no_leaked_target():
    rm.clear_tail_target()
    yield
    rm.clear_tail_target()


def test_nothing_is_tailed_until_a_tailable_tool_starts() -> None:
    assert rm._heartbeat_tail() is None


def test_a_job_with_no_env_has_no_tail_rather_than_a_wrong_one() -> None:
    """Without an env there is nothing to resolve the log under. The rule
    the first version used, kept."""
    rm.set_tail_target(None, "devel/glib20", "", JOB, "dsynth_build")

    assert rm._heartbeat_tail() is None


def test_a_log_that_does_not_exist_yet_is_not_an_error() -> None:
    """dsynth writes a log only once a build starts, so "no log" is an
    ordinary state on the way in -- not a fault, and not a tail."""
    rm.set_tail_target("2026Q3", "devel/glib20", "", JOB, "dsynth_build")

    assert rm._heartbeat_tail() is None


def test_the_tail_is_read_fresh_each_tick_not_incrementally(
    monkeypatch, tmp_path: Path,
) -> None:
    """An offset-based read would leave the published row holding only what
    arrived since the last tick -- nothing at all on a quiet build. The
    page wants the newest screenful every time, so offset is -1."""
    seen: list[dict] = []
    log = tmp_path / "glib20.log"
    log.write_text("line one\nline two\n")

    from dportsv3.agent import dsynth_tail

    real = dsynth_tail.read_tail

    def spy(env, origin, **kw):
        seen.append(kw)
        return real(env, origin, **kw)

    monkeypatch.setattr(dsynth_tail, "read_tail", spy)
    monkeypatch.setattr(dsynth_tail, "log_path", lambda *a, **kw: log)
    rm.set_tail_target("2026Q3", "devel/glib20", "", JOB, "dsynth_build")

    first = rm._heartbeat_tail()
    second = rm._heartbeat_tail()

    assert [k["offset"] for k in seen] == [-1, -1]
    assert first["text"] == second["text"] == "line one\nline two\n"
    assert first["job_id"] == JOB and first["tool"] == "dsynth_build"


def test_a_read_that_raises_costs_the_tail_not_the_heartbeat(
    monkeypatch,
) -> None:
    """This runs on the heartbeat thread, and the same call carries the
    liveness the UI uses to tell a working runner from a dead one."""
    from dportsv3.agent import dsynth_tail

    monkeypatch.setattr(
        dsynth_tail, "read_tail",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("gone")),
    )
    rm.set_tail_target("2026Q3", "devel/glib20", "", JOB, "dsynth_build")

    assert rm._heartbeat_tail() is None


# --- the dispatcher starts and stops it ----------------------------------


def _dispatcher(calls: list) -> steps.PatchEventDispatcher:
    return steps.PatchEventDispatcher(
        queue_root=None, job_id=JOB, origin="devel/glib20",
        activity_log=lambda *a, **kw: None,
        looks_env_suspicious=lambda r: False,
        invalidate_health_cache=lambda *a, **kw: None,
        summarize_tool_call=lambda *a: "",
        set_tail_target=calls.append,
    )


@pytest.mark.parametrize("tool", sorted(TAILABLE_TOOLS))
def test_a_tailable_tool_starting_begins_publishing(tool: str) -> None:
    calls: list = []
    _dispatcher(calls)({"type": "tool_start", "tool": tool, "attempt": 1})

    assert calls == [tool]


def test_a_quick_tool_starting_does_not() -> None:
    """A grep returns in milliseconds and has nothing to say meanwhile;
    greps outnumber builds by a wide margin."""
    calls: list = []
    _dispatcher(calls)({"type": "tool_start", "tool": "grep", "attempt": 1})

    assert calls == []


def test_a_tool_returning_stops_publishing() -> None:
    """Cleared for ANY tool: the loop dispatches serially, so any
    completion means the build we were tailing is over."""
    calls: list = []
    d = _dispatcher(calls)
    d({"type": "tool_start", "tool": "dsynth_build", "attempt": 1})
    d({"type": "tool_call", "tool": "dsynth_build", "attempt": 1,
       "result": {"ok": True}})

    assert calls == ["dsynth_build", None]


def test_the_hook_is_optional_and_a_raising_one_is_survivable() -> None:
    """Every existing test constructs this dispatcher without the hook,
    and a tail is a nicety on the path every tool call takes."""
    plain = steps.PatchEventDispatcher(
        queue_root=None, job_id=JOB, origin="o",
        activity_log=lambda *a, **kw: None,
        looks_env_suspicious=lambda r: False,
        invalidate_health_cache=lambda *a, **kw: None,
        summarize_tool_call=lambda *a: "",
    )
    plain({"type": "tool_start", "tool": "dsynth_build", "attempt": 1})

    def boom(_tool):
        raise RuntimeError("nope")

    angry = _dispatcher([])
    angry.set_tail_target = boom
    angry({"type": "tool_start", "tool": "dsynth_build", "attempt": 1})


# --- the row, and what clears it -----------------------------------------


def test_the_tail_rides_the_heartbeat(conn) -> None:
    presence.apply(conn, {"event": "register", "runner_id": RUNNER,
                          "hostname": "h", "pid": 1})
    presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER,
                          "tail": _tail()})

    got = job_tail(conn, JOB)
    assert got["text"] == "cc -o foo\n"
    assert got["tool"] == "dsynth_build"
    assert got["mtime"] == 1_700_000_000.0


def test_a_heartbeat_with_no_tail_clears_the_last_one(conn) -> None:
    """The value is live-only. A tail left behind would be shown against
    whatever the builder did next."""
    presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER,
                          "tail": _tail()})
    presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER})

    assert job_tail(conn, JOB) is None


def test_one_row_per_builder_however_many_builds(conn) -> None:
    """Keyed by runner, not by job: a row per job would grow without
    bound at up to 32 KiB each."""
    for n in range(5):
        presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER,
                              "tail": _tail(job_id=f"job-{n}")})

    assert conn.execute("SELECT COUNT(*) FROM runner_tail").fetchone()[0] == 1


def test_two_builders_do_not_overwrite_each_other(conn) -> None:
    presence.apply(conn, {"event": "heartbeat", "runner_id": "b1",
                          "tail": _tail(job_id="job-1", text="one\n")})
    presence.apply(conn, {"event": "heartbeat", "runner_id": "b2",
                          "tail": _tail(job_id="job-2", text="two\n")})

    assert job_tail(conn, "job-1")["text"] == "one\n"
    assert job_tail(conn, "job-2")["text"] == "two\n"


def test_the_tail_belongs_to_the_job_that_is_running_it(conn) -> None:
    """The builder moved on, so the old job's page gets nothing -- its
    output is its logs/full.log.gz now, not a tail frozen mid-build."""
    presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER,
                          "tail": _tail(job_id="job-1")})
    presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER,
                          "tail": _tail(job_id="job-2")})

    assert job_tail(conn, "job-1") is None
    assert job_tail(conn, "job-2") is not None


def test_a_bad_tail_payload_is_refused(conn) -> None:
    with pytest.raises(ValueError, match="tail must be an object"):
        presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER,
                              "tail": "cc -o foo"})


def test_both_transports_publish_a_tail(monkeypatch, conn) -> None:
    """The protocol carries it, so LocalStore and HttpStore both do."""
    monkeypatch.setattr(rm, "_state_db_conn", conn, raising=False)

    state_store.LocalStore().heartbeat(RUNNER, _tail(text="local\n"))
    assert job_tail(conn, JOB)["text"] == "local\n"

    sent: list = []
    http = state_store.HttpStore()
    monkeypatch.setattr(http, "_post", sent.append)
    http.heartbeat(RUNNER, _tail(text="remote\n"))

    assert sent[0]["tail"]["text"] == "remote\n"
    assert sent[0]["event"] == "heartbeat"


def test_a_heartbeat_without_a_tail_sends_no_tail_key(monkeypatch) -> None:
    """A builder that never tails sends the same bytes it sent before this
    feature existed."""
    sent: list = []
    http = state_store.HttpStore()
    monkeypatch.setattr(http, "_post", sent.append)

    http.heartbeat(RUNNER)

    assert "tail" not in sent[0]


def test_a_stopping_runner_leaves_no_tail_behind(conn) -> None:
    """The job page decides a tool is "still running" from activity rows,
    and a stopped runner leaves those looking exactly like a build in
    progress. So a clean shutdown blanks the tail. (A crash cannot run
    this; there the "last line N ago" ticker is what reveals it.)"""
    presence.apply(conn, {"event": "register", "runner_id": RUNNER,
                          "hostname": "h", "pid": 1})
    presence.apply(conn, {"event": "heartbeat", "runner_id": RUNNER,
                          "tail": _tail()})
    assert job_tail(conn, JOB) is not None

    presence.apply(conn, {"event": "deregister", "runner_id": RUNNER})

    assert job_tail(conn, JOB) is None


def test_deregistering_one_builder_keeps_anothers_tail(conn) -> None:
    presence.apply(conn, {"event": "heartbeat", "runner_id": "b1",
                          "tail": _tail(job_id="job-1")})
    presence.apply(conn, {"event": "heartbeat", "runner_id": "b2",
                          "tail": _tail(job_id="job-2")})

    presence.apply(conn, {"event": "deregister", "runner_id": "b1"})

    assert job_tail(conn, "job-1") is None
    assert job_tail(conn, "job-2") is not None
