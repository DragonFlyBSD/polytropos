"""The tail follows the port dsynth is on, not the port the job is named for.

poly-quu3. One dsynth invocation can build several ports: dsynth_build adds
the port's patch origin, which for a slave is the MASTER the fix was actually
written into (poly-lt5q). A job for ``print/qt6-pdf`` therefore runs

    dsynth test print/qt6-pdf www/qt6-webengine

and the slave finishes in minutes while the master -- Chromium -- runs for
hours. Tailing the job's own origin pinned the page to a completed log for the
rest of the run, and a healthy build read as hung: an operator reported it
stuck, and so did the first diagnosis of it.
"""

from __future__ import annotations

import inspect
import os
import re
import time
from pathlib import Path

import pytest

from dportsv3.agent import dsynth_tail, runner as rm, steps, worker

SLAVE = "print/qt6-pdf"
MASTER = "www/qt6-webengine"
JOB = "job-1"
STATIC = Path(__file__).resolve().parents[1] / "dportsv3" / "tracker" / "static"


@pytest.fixture(autouse=True)
def no_leaked_target():
    rm.clear_tail_target()
    yield
    rm.clear_tail_target()


@pytest.fixture
def logs(monkeypatch, tmp_path: Path):
    """A log file per origin, with settable mtimes."""
    files = {}

    def write(origin: str, text: str, age_s: float = 0.0):
        path = files.get(origin) or (
            tmp_path / (origin.replace("/", "___") + ".log"))
        path.write_text(text)
        when = time.time() - age_s
        os.utime(path, (when, when))
        files[origin] = path
        return path

    monkeypatch.setattr(
        worker, "_dsynth_log_candidates",
        lambda env, origin: ([files[origin]] if origin in files else []),
    )
    return write


# --- which log the tail follows ------------------------------------------


def test_the_newest_log_across_the_run_is_the_one_dsynth_is_on(logs) -> None:
    """dsynth builds the list serially, so newest-written is current."""
    logs(SLAVE, "SUCCEEDED\n", age_s=600)
    logs(MASTER, "[1/12] compiling\n", age_s=1)

    assert dsynth_tail.building_origin("e", [SLAVE, MASTER]) == MASTER


def test_the_tail_moves_across_when_the_slave_finishes(logs) -> None:
    """The whole bug, in one test.

    While the slave builds it is the newest; once it finishes and the master
    starts writing, the tail follows without anything telling it to.
    """
    logs(MASTER, "", age_s=5)
    logs(SLAVE, "compiling the pdf slice\n", age_s=0)
    assert dsynth_tail.building_origin("e", [SLAVE, MASTER]) == SLAVE

    logs(SLAVE, "SUCCEEDED 00:00:00\n", age_s=30)
    logs(MASTER, "[1/12] QtWebEngineCore\n", age_s=0)
    assert dsynth_tail.building_origin("e", [SLAVE, MASTER]) == MASTER


def test_a_log_from_an_earlier_run_is_not_this_build(logs) -> None:
    """`since` is when the call started; anything older is last time's.

    Without it, a build that has not written yet shows the previous run's
    SUCCEEDED as though it were now -- the same confusion in miniature.
    """
    logs(SLAVE, "SUCCEEDED (yesterday)\n", age_s=86_400)
    since = time.time()

    assert dsynth_tail.building_origin("e", [SLAVE], since=since) is None


def test_a_log_written_a_shade_before_the_call_still_counts(logs) -> None:
    """Coarse mtimes and two clocks read a beat apart."""
    since = time.time()
    logs(MASTER, "compiling\n", age_s=1.0)

    assert dsynth_tail.building_origin("e", [MASTER], since=since) == MASTER


def test_an_origin_with_no_log_is_skipped_not_fatal(logs) -> None:
    logs(MASTER, "compiling\n")

    assert dsynth_tail.building_origin("e", ["x/nothing", MASTER]) == MASTER
    assert dsynth_tail.building_origin("e", ["x/nothing"]) is None


# --- what the heartbeat publishes ----------------------------------------


def test_the_heartbeat_publishes_the_master_not_the_job(logs) -> None:
    logs(SLAVE, "SUCCEEDED 00:00:00\n", age_s=600)
    logs(MASTER, "[1/12] QtWebEngineCore\n", age_s=0)
    rm.set_tail_target("2026Q3", [SLAVE, MASTER], "", JOB, "dsynth_build")

    tail = rm._heartbeat_tail()

    assert tail["origin"] == MASTER
    assert tail["n_origins"] == 2
    assert "QtWebEngineCore" in tail["text"]


def test_a_single_port_run_says_one(logs) -> None:
    logs(MASTER, "compiling\n")
    rm.set_tail_target("2026Q3", [MASTER], "", JOB, "dsynth_build")

    assert rm._heartbeat_tail()["n_origins"] == 1


def test_a_bare_string_origin_is_wrapped_not_iterated() -> None:
    """A string is iterable, and iterating its characters would be silent."""
    rm.set_tail_target("2026Q3", SLAVE, "", JOB, "dsynth_build")

    assert rm._tail_target["origins"] == [SLAVE]


def test_the_target_records_when_the_call_started() -> None:
    before = time.time()
    rm.set_tail_target("2026Q3", [SLAVE], "", JOB, "dsynth_build")

    assert before <= rm._tail_target["since"] <= time.time()


# --- the tail and the build cannot drift apart ---------------------------


def test_the_tail_and_dsynth_resolve_the_same_origin_set() -> None:
    """Both go through invariant_origins, so the rule lives in one place.

    dsynth_build computes `also = invariant_origins(...)[1:]` and then
    `origin_set(origin, also)`, which is that same list. If either side ever
    resolves its ports some other way, the tail can follow a port the build
    is not running -- which is this bead.
    """
    assert "invariant_origins" in inspect.getsource(worker.dsynth_build)
    assert "invariant_origins" in inspect.getsource(steps.PatchAttemptStep.run)


def test_the_relation_probe_is_bounded() -> None:
    """It runs on the MAIN thread at tool_start now, inside a try/except
    that catches raises and not hangs. A build gets no timeout; a probe
    must."""
    src = inspect.getsource(worker.probe_port_relation)
    assert "timeout=RELATION_PROBE_TIMEOUT" in src
    assert worker.RELATION_PROBE_TIMEOUT > 0


# --- the page says which port ---------------------------------------------


def test_the_tailbar_names_the_port_and_counts_the_run() -> None:
    tmpl = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
            / "templates" / "_tool_tail.html").read_text()
    assert "tail.origin" in tmpl
    # only when there is more than one, or every single-port build gains a
    # line saying "1 ports in this run"
    assert re.search(r"tail\.n_origins\s*>\s*1", tmpl)


def test_one_duration_format_for_both_live_clocks() -> None:
    """The tail clock stopped at minutes: a two-and-a-half-hour stall read
    "161m39s", which is a number an operator has to stop and decode."""
    js = (STATIC / "agentic-job.js").read_text()
    assert js.count("function shortDuration") == 1
    assert js.count("shortDuration(") >= 3   # the definition plus both clocks
    body = js.split("function shortDuration", 1)[1].split("\n}", 1)[0]
    assert '"h"' in body and '"m"' in body and '"s"' in body
