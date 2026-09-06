"""The delivery preflight, held fresh enough to put on a page (poly-0e02.10).

check() has run at startup since it was written and its findings went to
the log and nowhere else. Surfacing them is the easy half. The bead's
suggestion -- re-run it on every render, "it is filesystem checks, not
network" -- is what these tests exist to rule out: it shells out to git
twice, and while a delivery holds the clone it would report that
delivery's own feature branch and applied diff as two faults.
"""

from __future__ import annotations

import threading

import pytest

from dportsv3.tracker import preflight_status


class _Finding:
    """Stands in for delivery.preflight.Finding, which needs the delivery
    extra installed. Only .level and .detail are read."""

    def __init__(self, level: str, detail: str = "") -> None:
        self.level = level
        self.detail = detail


@pytest.fixture(autouse=True)
def _clean() -> None:
    preflight_status.reset()
    yield
    preflight_status.reset()


@pytest.fixture
def checks(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record every call to the underlying check, and control its result."""
    calls: list[int] = []
    result = [_Finding("ok", "delivery provider is 'github'")]

    def fake_check() -> list:
        calls.append(1)
        return list(result)

    monkeypatch.setattr(preflight_status, "_check", fake_check)
    return [calls, result]


@pytest.fixture
def busy(monkeypatch: pytest.MonkeyPatch):
    """Control whether a delivery is holding the clone."""
    state = {"busy": False}
    monkeypatch.setattr(
        preflight_status, "_clone_is_busy", lambda: state["busy"],
    )
    return state


# --- the reading ----------------------------------------------------------


def test_a_reading_says_when_it_was_taken(checks, busy) -> None:
    """The strip has to be able to say "as of", not imply now."""
    report = preflight_status.current()

    assert report.checked_at
    assert report.checked_at.endswith("+00:00")


def test_the_level_is_the_worst_finding(checks, busy) -> None:
    checks[1][:] = [_Finding("ok"), _Finding("error"), _Finding("warn")]

    assert preflight_status.current().level == "error"
    assert not preflight_status.current(force=True).healthy


def test_all_ok_is_healthy(checks, busy) -> None:
    checks[1][:] = [_Finding("ok"), _Finding("ok")]

    assert preflight_status.current().healthy


def test_no_findings_is_not_a_fault(checks, busy) -> None:
    """An empty report means the check had nothing to say, which is not
    the same as something being wrong."""
    checks[1][:] = []

    assert preflight_status.current().level == "ok"


def test_a_check_that_raises_becomes_a_reading(
    monkeypatch: pytest.MonkeyPatch, busy,
) -> None:
    """A health strip that takes the page down with it is worse than no
    health strip, so a preflight that blows up becomes an error finding.
    This drives the real _check, not a stand-in."""
    preflight = pytest.importorskip("dportsv3.delivery.preflight")

    def boom(**_kw) -> list:
        raise RuntimeError("clone unreadable")

    monkeypatch.setattr(preflight, "check", boom)

    report = preflight_status.current()

    assert report.level == "error"
    assert "clone unreadable" in report.findings[0].detail
    assert report.checked_at


def test_a_host_without_the_delivery_extra_still_renders(
    monkeypatch: pytest.MonkeyPatch, busy,
) -> None:
    """The tracker installs and runs without delivery. Importing it to
    draw a health strip must not be what breaks the page."""
    import builtins

    real_import = builtins.__import__

    def no_delivery(name, *args, **kwargs):
        if name.startswith("dportsv3.delivery"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_delivery)

    report = preflight_status.current()

    assert report.findings == []
    assert report.level == "ok"


# --- the throttle ---------------------------------------------------------


def test_a_second_render_reuses_the_reading(checks, busy, set_setting) -> None:
    set_setting("tracker.preflight_refresh_seconds", 300)

    preflight_status.current()
    preflight_status.current()
    preflight_status.current()

    assert len(checks[0]) == 1


def test_force_re_checks(checks, busy, set_setting) -> None:
    set_setting("tracker.preflight_refresh_seconds", 300)

    preflight_status.current()
    preflight_status.current(force=True)

    assert len(checks[0]) == 2


def test_zero_re_checks_every_time(checks, busy, set_setting) -> None:
    """The escape hatch, for an operator who wants the live answer and
    accepts what it costs."""
    set_setting("tracker.preflight_refresh_seconds", 0)

    preflight_status.current()
    preflight_status.current()

    assert len(checks[0]) == 2


def test_concurrent_renders_do_not_each_start_their_own_check(
    checks, busy, set_setting,
) -> None:
    """The slot is claimed before the check runs, so a burst of renders on
    the threadpool does not spawn a burst of git subprocesses."""
    set_setting("tracker.preflight_refresh_seconds", 300)
    start = threading.Barrier(8)

    def render() -> None:
        start.wait()
        preflight_status.current()

    threads = [threading.Thread(target=render) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(checks[0]) == 1


# --- the clone is busy ----------------------------------------------------


def test_a_delivery_in_flight_defers_the_refresh(
    checks, busy, set_setting,
) -> None:
    """deliver() holds the clone for the whole provider call, and during it
    the tree is on the feature branch with the diff applied. Re-checking
    then would report a delivery working correctly as two warnings."""
    set_setting("tracker.preflight_refresh_seconds", 0)
    preflight_status.current()
    assert len(checks[0]) == 1

    busy["busy"] = True
    report = preflight_status.current()

    assert len(checks[0]) == 1
    assert report.deferred


def test_a_deferred_reading_keeps_what_it_had(
    checks, busy, set_setting,
) -> None:
    """Deferred is not "unknown": the previous answer is still the best
    one available, and its checked_at already says how old it is."""
    set_setting("tracker.preflight_refresh_seconds", 0)
    checks[1][:] = [_Finding("warn", "clone_dir has uncommitted changes")]
    first = preflight_status.current()

    busy["busy"] = True
    deferred = preflight_status.current()

    assert deferred.level == first.level == "warn"
    assert deferred.checked_at == first.checked_at
    assert [f.detail for f in deferred.findings] == [
        "clone_dir has uncommitted changes",
    ]


def test_the_first_ever_check_runs_even_if_the_clone_is_busy(
    checks, busy, set_setting,
) -> None:
    """There is nothing to defer to. A strip with no reading at all is
    worse than one taken at an awkward moment."""
    set_setting("tracker.preflight_refresh_seconds", 0)
    busy["busy"] = True

    report = preflight_status.current()

    assert len(checks[0]) == 1
    assert not report.deferred


def test_the_refresh_resumes_once_the_delivery_finishes(
    checks, busy, set_setting,
) -> None:
    set_setting("tracker.preflight_refresh_seconds", 0)
    preflight_status.current()
    busy["busy"] = True
    preflight_status.current()

    busy["busy"] = False
    report = preflight_status.current()

    assert len(checks[0]) == 2
    assert not report.deferred


def test_clone_is_busy_reads_the_lock_the_delivery_takes() -> None:
    """The hint has to come from the real lock, or it is decorative."""
    orchestrator = pytest.importorskip(
        "dportsv3.delivery.orchestrator",
    )

    assert not orchestrator.clone_is_busy()
    with orchestrator._CLONE_LOCK:
        assert orchestrator.clone_is_busy()
    assert not orchestrator.clone_is_busy()
