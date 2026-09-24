"""The seam between the runner and the tracker's state (poly-fij.12).

Every write the runner makes to ``state.db`` is a write that has to cross
a network the day a builder stops sharing a host with the tracker. That
set is a boundary, not a style preference: 31 statements over 10 tables,
and nothing else in the codebase is in it. The tracker keeps its own ~200
writes exactly where they are.

This module is the one interface those writes go through. Two
implementations:

``LocalStore``   today's ``runner._state_db_conn`` and ``_state_db_lock``,
                 byte-for-byte the same SQL and the same error handling.
``HttpStore``    posts to the tracker's ``/v1`` vocabulary, pointed at
                 loopback exactly like the dsynth hooks.

Call sites move through the seam one group at a time with no behaviour
change, tests green throughout, and the implementation is flipped by
config once every group has crossed. That ordering is the whole point:
rewriting 31 call sites as requests in one commit is a cutover with no
safe intermediate state.

WHY THE OPERATIONS ARE NAMED RATHER THAN SQL-SHAPED. A seam that took a
statement and parameters would let ``LocalStore`` work and leave
``HttpStore`` with nothing to implement -- the tracker does not accept
SQL, and it should not. So each method names an intent the tracker can
answer, and the SQL lives in the local implementation only.

WHY THE CONNECTION IS RESOLVED PER CALL. Eight test modules and the demo
driver assign ``runner._state_db_conn`` after import and expect the
runner's writes to land in their sqlite file. A store that captured the
connection when it was built would silently stop seeing those
assignments, and a test asserting on rows would pass while writing
nowhere. ``LocalStore`` therefore reads the module global on every call.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.request
from typing import Protocol

from dportsv3.db import presence


class StateStore(Protocol):
    """What the runner needs from the tracker's state.

    Grouped by meaning rather than by table: a remote runner announcing
    itself does not know or care that presence lives in two tables.
    """

    # --- runner presence ---------------------------------------------

    def register_runner(self, runner_id: str) -> None:
        """Announce this process. Idempotent: a restart re-enrolls the
        same id and clears any ``stopped_at`` from the last run."""

    def heartbeat(self, runner_id: str) -> None:
        """Say this runner is still alive. Called every
        ``HEARTBEAT_INTERVAL`` seconds from a thread that keeps ticking
        while the main thread blocks in ``subprocess.run``."""

    def set_runner_status(
        self,
        status: str,
        job_id: str | None = None,
        stage: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """Record what the runner is doing now."""

    def deregister_runner(self, runner_id: str) -> None:
        """Clean shutdown. A crashed runner leaves this unset with a
        stale heartbeat, which is the distinction that matters."""


#: Which presence failures say something locally. The heartbeat stays
#: silent at 12 ticks a minute; the rest happen at process and job
#: boundaries, where one line is worth having.
_PRESENCE_WARNINGS = {
    "register": "could not register runner",
    "status": "Failed to update runner status",
    "deregister": "could not deregister runner",
    "heartbeat": None,
}


def _register_payload(runner_id: str) -> dict:
    """hostname and pid travel in the payload: the tracker cannot know
    them about a process on another host."""
    return {
        "event": "register",
        "runner_id": runner_id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
    }


def _status_payload(
    status: str,
    job_id: str | None,
    stage: str | None,
    extra: dict | None,
) -> dict:
    from dportsv3.agent import runner  # noqa: PLC0415
    return {
        "event": "status",
        # Unused by the handler while runner_status is the id=1 singleton,
        # and required anyway: it says who called, which is what
        # poly-fij.4 binds a token to.
        "runner_id": runner.runner_id(),
        "status": status,
        "job_id": job_id,
        "stage": stage,
        "extra": extra,
    }


class LocalStore:
    """Today's behaviour, unchanged, behind the seam.

    Builds the same payload ``HttpStore`` sends and hands it to the same
    ``db.presence.apply`` the endpoint calls, so the two transports cannot
    diverge in what they mean -- only in how the payload travels.

    Holds no connection of its own: see the module docstring on why the
    global is read per call.
    """

    def _conn(self):
        from dportsv3.agent import runner  # noqa: PLC0415  (cycle at import)
        return runner._state_db_conn

    def _lock(self) -> threading.Lock:
        from dportsv3.agent import runner  # noqa: PLC0415
        return runner._state_db_lock

    def _apply(self, payload: dict) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            with self._lock():
                presence.apply(conn, payload)
        except Exception as exc:
            warning = _PRESENCE_WARNINGS.get(payload.get("event"))
            if warning:
                print(f"Warning: {warning}: {exc}", file=sys.stderr)

    # --- runner presence ---------------------------------------------

    def register_runner(self, runner_id: str) -> None:
        self._apply(_register_payload(runner_id))

    def heartbeat(self, runner_id: str) -> None:
        self._apply({"event": "heartbeat", "runner_id": runner_id})

    def set_runner_status(
        self,
        status: str,
        job_id: str | None = None,
        stage: str | None = None,
        extra: dict | None = None,
    ) -> None:
        self._apply(_status_payload(status, job_id, stage, extra))

    def deregister_runner(self, runner_id: str) -> None:
        self._apply({"event": "deregister", "runner_id": runner_id})


class HttpStore:
    """The same payloads, posted to the tracker's ``/v1`` vocabulary.

    Pointed at loopback exactly like the dsynth hooks, so a builder that
    stops sharing a host with the tracker changes a URL and nothing else.

    NEVER RAISES, NEVER BLOCKS FOR LONG. This carries telemetry, and a
    tracker that is down, restarting or slow must not slow a build or fail
    one -- which is also what keeps trackerless deployment working: with
    nothing listening, every call here is the same silent no-op that a
    missing ``state.db`` already is.

    The timeout is deliberately SHORTER than the heartbeat interval. A
    call that could outlive its own slot would leave ticks overlapping and
    the thread falling behind the cadence it exists to define.

    It warns where ``LocalStore`` warns and stays silent where that does,
    from the same table. Silence everywhere was the first version and it
    was wrong: a builder pointed at the wrong URL would fail to enroll and
    say nothing, which is the one deployment where a line in the log is
    worth most.
    """

    def __init__(self, url: str | None = None, timeout: float | None = None):
        self._url = url
        self._timeout = timeout

    def _endpoint(self) -> str:
        if self._url:
            return self._url
        from dportsv3.agent import runner  # noqa: PLC0415
        return f"{runner._tracker_url()}/v1/runners/presence"

    def _timeout_seconds(self) -> float:
        if self._timeout is not None:
            return self._timeout
        from dportsv3.agent import runner  # noqa: PLC0415
        # Strictly inside the interval, with room for the request to be
        # torn down before the next tick is due.
        return max(1.0, runner.HEARTBEAT_INTERVAL - 1.0)

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._endpoint(), data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds()):
                pass
        except Exception as exc:
            # Never raises: telemetry does not get to fail a build. The
            # heartbeat also never speaks -- at 12 ticks a minute that is a
            # log nobody can read, and a missed tick costs one tick of
            # liveness the UI already renders as "not running".
            warning = _PRESENCE_WARNINGS.get(payload.get("event"))
            if warning:
                print(f"Warning: {warning}: {exc}", file=sys.stderr)

    # --- runner presence ---------------------------------------------

    def register_runner(self, runner_id: str) -> None:
        self._post(_register_payload(runner_id))

    def heartbeat(self, runner_id: str) -> None:
        self._post({"event": "heartbeat", "runner_id": runner_id})

    def set_runner_status(
        self,
        status: str,
        job_id: str | None = None,
        stage: str | None = None,
        extra: dict | None = None,
    ) -> None:
        self._post(_status_payload(status, job_id, stage, extra))

    def deregister_runner(self, runner_id: str) -> None:
        self._post({"event": "deregister", "runner_id": runner_id})


#: The process-wide store. ``LocalStore`` until a config flip selects
#: ``HttpStore``; swapped wholesale rather than per call site so there is
#: never a run with some writes local and some remote.
_store: StateStore = LocalStore()


def store() -> StateStore:
    return _store


def set_store(new: StateStore) -> StateStore:
    """Install a store, returning the previous one. For the config flip,
    and for tests that want to assert on calls rather than on rows."""
    global _store
    previous = _store
    _store = new
    return previous
