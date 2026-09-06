"""The three facts the command bar carries, held long enough to be free.

Queue, Needs you and Runner sit in the shell, so every page pays for
them. Measured on 20,000 issues (12,800 open): 2.38 ms for the job
counts, 23.99 ms for the worklist bands, 0.01 ms for the runner, 25.84 ms
together -- most of what a Builds render costs, on a page that shows
none of it.

The cheap substitute is dishonest. ``COUNT(*) WHERE state='unresolved'``
takes 0.16 ms and answers 12,000 where the bands say 11,227, because a
runner-owned issue is open and does not need you. A number in the chrome
of every page that is 7% high is worse than one that costs 24 ms.

So it is memoised, on ``tracker.shell_facts_seconds`` -- the same shape
as the delivery sweep and the preflight reading, for the same reason.
A topbar fact is a glance; the page it links to is the authority and
recomputes on arrival.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShellFacts:
    """What the command bar shows, and whether to believe the runner."""

    #: Jobs the runner is holding: queued plus in flight.
    queue: int = 0
    #: Open issues in an operator band -- NOT every open issue. The
    #: runner band and the confirming band are the system's work.
    needs_you: int = 0
    #: Whatever the runner last wrote. It survives the process dying.
    runner_status: str = "unknown"
    #: The heartbeat read (poly-chf), which is what says whether the
    #: status above means anything.
    runner_live: bool = False
    #: Inventory for the status rail. Cheap next to the bands, and
    #: memoised with them anyway.
    targets: int = 0
    issues: int = 0
    occurrences: int = 0
    #: False only for the empty reading served before the first look.
    loaded: bool = False


_LOCK = threading.Lock()
#: Keyed by database path, not a single slot. One process serves one
#: tracker in production, but create_app takes a path and nothing stops
#: two -- the test suite builds dozens -- and an unkeyed memo hands the
#: second one the first one's runner.
_FACTS: dict[str, ShellFacts] = {}
_LAST_MONOTONIC: dict[str, float] = {}
#: One per database, held across the read. Stamping the clock and
#: releasing instead -- which is what the preflight throttle does, where
#: it is correct because it has a "has run at all" flag -- lets every
#: thread in a concurrent burst find an empty slot and compute anyway.
#: Here they wait for the first one and take its answer.
_COMPUTE: dict[str, threading.Lock] = {}


def reset() -> None:
    """Forget every reading, so the next call recomputes.

    Process-global, so a test that asserts freshness controls it the way
    it would any module global.
    """
    with _LOCK:
        _FACTS.clear()
        _LAST_MONOTONIC.clear()


def _interval() -> int:
    from dportsv3 import settings  # noqa: PLC0415

    try:
        return max(0, int(settings.get("tracker.shell_facts_seconds")))
    except Exception:  # noqa: BLE001 — a render must not die on config
        return 0


def _held(key: str, interval: int, force: bool) -> ShellFacts | None:
    facts = _FACTS.get(key)
    if facts is None or force or not interval:
        return None
    if (time.monotonic() - _LAST_MONOTONIC.get(key, 0.0)) >= interval:
        return None
    return facts


def current(db_path: str, *, force: bool = False) -> ShellFacts:
    """The command bar's facts, recomputed if the reading is old enough.

    Never raises: this is called while composing a page, and a header
    that takes the page down with it is worse than a header that says
    nothing.
    """
    key = str(db_path)
    interval = _interval()
    with _LOCK:
        facts = _held(key, interval, force)
        if facts is not None:
            return facts
        compute = _COMPUTE.setdefault(key, threading.Lock())

    with compute:
        # Somebody else may have finished while we waited for the lock.
        with _LOCK:
            facts = _held(key, interval, force)
            if facts is not None:
                return facts
        facts = _read(key)
        with _LOCK:
            _FACTS[key] = facts
            _LAST_MONOTONIC[key] = time.monotonic()
        return facts


def _read(db_path: str) -> ShellFacts:
    from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
        RUNNER_BAND,
        agentic_status,
        distinct_targets,
        runner_is_live,
        runner_status,
        worklist_band_counts,
    )
    from dportsv3.tracker.db import open_db  # noqa: PLC0415

    try:
        conn = open_db(db_path)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("shell facts unavailable: %s", exc)
        return ShellFacts(loaded=True)
    try:
        status = agentic_status(conn)
        jobs = status["jobs"]
        jobs_bundles = status["bundles"]
        bands = worklist_band_counts(conn)
        # Everything except the runner's own work and the confirming
        # band, which is a build's to finish, not an operator's.
        needs_you = sum(
            n for key, n in bands.items()
            if key not in (RUNNER_BAND, "confirming")
        )
        runner = runner_status(conn)
        return ShellFacts(
            queue=int(jobs.get("pending", 0)) + int(jobs.get("inflight", 0)),
            needs_you=needs_you,
            runner_status=str(runner.get("status") or "unknown"),
            runner_live=runner_is_live(conn),
            targets=len(distinct_targets(conn)),
            issues=int(conn.execute(
                "SELECT COUNT(*) FROM issues").fetchone()[0] or 0),
            occurrences=int(jobs_bundles),
            loaded=True,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("shell facts could not be read: %s", exc)
        return ShellFacts(loaded=True)
    finally:
        conn.close()
