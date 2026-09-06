"""The delivery preflight, kept fresh enough to put on a page.

``delivery.preflight.check()`` has run at startup since it was written,
and its findings went to the log and nowhere else. The pipeline's health
strip wants exactly what it reports -- token readable, clone present and
clean, outbox writable, against the account that actually delivers -- so
this holds one reading and says when it was taken.

WHY NOT JUST CALL check() ON EVERY RENDER
-----------------------------------------
It reads as filesystem work and is not. ``_check_clone_state`` shells out
twice, and ``git status --porcelain`` on a 20,000-file tree costs 26-31 ms
and rewrites the index -- a write against the operator's clone, on a page
that is polled.

The stronger reason is that it would lie. ``deliver()`` holds the clone
for the whole provider call, and during it the tree is deliberately on the
feature branch with the diff applied. An unsynchronised preflight would
report "clone_dir is on 'fix/...', not 'master'" and "has uncommitted
changes" -- two warnings describing a delivery working correctly. A health
strip that goes amber every time the system does its job teaches operators
to ignore it.

So: refresh on a throttle, skip the refresh while a delivery holds the
clone, and stamp every reading with when it was taken so a strip can say
"as of" rather than implying now.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger(__name__)

#: Worst-first, so the strip's overall level is a max over the findings.
_SEVERITY: dict[str, int] = {"ok": 0, "warn": 1, "error": 2}


@dataclass(frozen=True)
class PreflightReport:
    """One reading of the delivery preflight, and when it was taken."""

    findings: list[Any] = field(default_factory=list)
    #: ISO-8601 UTC. None only for the empty report served before the
    #: first check has ever run, which no page should reach -- create_app
    #: takes a reading at startup.
    checked_at: str | None = None
    #: ``ok`` / ``warn`` / ``error``: the worst level among the findings.
    #: ``ok`` for an empty report, because "nothing to say" is not a fault.
    level: str = "ok"
    #: True when this reading was served rather than retaken because a
    #: delivery held the clone. The strip says so instead of going amber.
    deferred: bool = False

    @property
    def healthy(self) -> bool:
        return self.level == "ok"


def _level_of(findings: list[Any]) -> str:
    worst = "ok"
    for finding in findings:
        level = str(getattr(finding, "level", "ok"))
        if _SEVERITY.get(level, 0) > _SEVERITY.get(worst, 0):
            worst = level
    return worst


_LOCK = threading.Lock()
_REPORT = PreflightReport()
_LAST_CHECK_MONOTONIC: float = 0.0
_HAS_CHECKED = False


def reset() -> None:
    """Forget the cached reading, so the next call re-checks.

    Process-global state, so a test that asserts refresh behaviour has to
    control it the way it would any module global.
    """
    global _REPORT, _LAST_CHECK_MONOTONIC, _HAS_CHECKED
    with _LOCK:
        _REPORT = PreflightReport()
        _LAST_CHECK_MONOTONIC = 0.0
        _HAS_CHECKED = False


def _interval() -> int:
    from dportsv3 import settings  # noqa: PLC0415

    try:
        return max(0, int(settings.get("tracker.preflight_refresh_seconds")))
    except Exception:  # noqa: BLE001 — a page render must not die on config
        return 0


def _clone_is_busy() -> bool:
    try:
        from dportsv3.delivery.orchestrator import (  # noqa: PLC0415
            clone_is_busy,
        )
        return clone_is_busy()
    except Exception:  # noqa: BLE001 — no delivery configured is not busy
        return False


def current(*, force: bool = False) -> PreflightReport:
    """The delivery preflight, re-checked if the reading is old enough.

    Never raises and never blocks: every failure below becomes a reading
    rather than an exception, because this is called to render a strip on
    a page and a health check that takes the page down with it is worse
    than no health check.

    Refresh is skipped, and the previous reading returned marked
    ``deferred``, while a delivery holds the clone -- see the module
    docstring. Skipped rather than waited on: the caller is a request
    thread and a delivery takes seconds.
    """
    global _REPORT, _LAST_CHECK_MONOTONIC, _HAS_CHECKED

    interval = _interval()
    now = time.monotonic()
    with _LOCK:
        fresh_enough = (
            _HAS_CHECKED
            and not force
            and interval
            and (now - _LAST_CHECK_MONOTONIC) < interval
        )
        if fresh_enough:
            return _REPORT
        if _HAS_CHECKED and _clone_is_busy():
            # Serve what we had and say why. Re-checking now would report
            # the in-flight delivery's own branch and diff as faults.
            _REPORT = PreflightReport(
                findings=_REPORT.findings,
                checked_at=_REPORT.checked_at,
                level=_REPORT.level,
                deferred=True,
            )
            return _REPORT
        # Claim the slot before checking, so a burst of concurrent renders
        # does not each start their own git subprocesses.
        _LAST_CHECK_MONOTONIC = now
        _HAS_CHECKED = True

    findings = _check()
    report = PreflightReport(
        findings=findings,
        checked_at=datetime.now(timezone.utc).isoformat(),
        level=_level_of(findings),
    )
    with _LOCK:
        _REPORT = report
    return report


def _check() -> list[Any]:
    """Run the preflight, turning any failure into a finding.

    The import is local because the tracker must install and run without
    the delivery extra: a host that never delivers should not fail to
    render a page over a module it does not have.
    """
    try:
        from dportsv3.delivery import preflight  # noqa: PLC0415

        return list(preflight.check())
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("delivery preflight could not run: %s", exc)
        try:
            from dportsv3.delivery.preflight import Finding  # noqa: PLC0415

            return [Finding("error", f"preflight could not run: {exc}")]
        except Exception:  # noqa: BLE001
            return []
