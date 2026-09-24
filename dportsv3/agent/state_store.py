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
from datetime import datetime, timezone
from typing import Protocol


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class LocalStore:
    """Today's behaviour, unchanged, behind the seam.

    Holds no connection of its own -- see the module docstring on why the
    global is read per call.
    """

    def _conn(self):
        from dportsv3.agent import runner  # noqa: PLC0415  (cycle at import)
        return runner._state_db_conn

    def _lock(self) -> threading.Lock:
        from dportsv3.agent import runner  # noqa: PLC0415
        return runner._state_db_lock

    # --- runner presence ---------------------------------------------

    def register_runner(self, runner_id: str) -> None:
        conn = self._conn()
        if conn is None:
            return
        ts = _now()
        try:
            with self._lock():
                conn.execute(
                    """INSERT INTO runners
                       (runner_id, hostname, pid, started_at, last_heartbeat_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(runner_id) DO UPDATE SET
                         last_heartbeat_at = excluded.last_heartbeat_at,
                         stopped_at = NULL""",
                    (runner_id, socket.gethostname(), os.getpid(), ts, ts),
                )
                conn.commit()
        except Exception as exc:
            print(f"Warning: could not register runner: {exc}", file=sys.stderr)

    def heartbeat(self, runner_id: str) -> None:
        conn = self._conn()
        if conn is None:
            return
        # Silent on failure, unlike its neighbours: this runs 12 times a
        # minute and a warning per tick would bury the log.
        try:
            ts = _now()
            with self._lock():
                conn.execute(
                    """UPDATE runner_status SET updated_at = ? WHERE id = 1""",
                    (ts,),
                )
                conn.execute(
                    """UPDATE runners SET last_heartbeat_at = ?
                       WHERE runner_id = ?""",
                    (ts, runner_id),
                )
                conn.commit()
        except Exception:
            pass

    def set_runner_status(
        self,
        status: str,
        job_id: str | None = None,
        stage: str | None = None,
        extra: dict | None = None,
    ) -> None:
        conn = self._conn()
        if conn is None:
            return
        ts = _now()
        extra_json = json.dumps(extra) if extra else None
        try:
            with self._lock():
                # started_at survives an update that does not change the
                # job, so the UI's elapsed clock measures the job rather
                # than the last status write.
                conn.execute(
                    """INSERT INTO runner_status
                       (id, status, job_id, current_stage, started_at,
                        updated_at, extra_json)
                       VALUES (1, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         status = excluded.status,
                         job_id = excluded.job_id,
                         current_stage = excluded.current_stage,
                         started_at = CASE
                           WHEN excluded.job_id != runner_status.job_id
                           THEN excluded.started_at
                           ELSE runner_status.started_at END,
                         updated_at = excluded.updated_at,
                         extra_json = excluded.extra_json""",
                    (status, job_id, stage, ts, ts, extra_json),
                )
                conn.commit()
        except Exception as e:
            print(f"Warning: Failed to update runner status: {e}",
                  file=sys.stderr)

    def deregister_runner(self, runner_id: str) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            with self._lock():
                conn.execute(
                    "UPDATE runners SET stopped_at = ? WHERE runner_id = ?",
                    (_now(), runner_id),
                )
                conn.commit()
        except Exception as exc:
            print(f"Warning: could not deregister runner: {exc}",
                  file=sys.stderr)


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
