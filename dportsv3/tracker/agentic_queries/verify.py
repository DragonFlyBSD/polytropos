"""Operator-triggered verify requests.

The tracker's ``POST /api/bundles/{id}/verify`` writes a ``verify_requests``
row and the runner's poll loop turns it into a job. Until now nothing read
the table back, so everything it records was invisible: whether a verify was
in flight, which env it was asked to run in, and whether one never started
at all (poly-0e02.6).

The env matters most. ``bundles`` has no env column, so this row is the only
record of where a verification actually ran -- and "verified" means very
little without it.

Reading the row is not enough to know what is happening, because the status
column stops at ``enqueued``: the runner sets pending -> enqueued (or
failed), and the verification result posts back to ``bundles`` without ever
closing the request. So an ``enqueued`` row may be running, finished, or
attached to a job that died. ``job_state`` is joined here so a projection can
tell those apart; :func:`dportsv3.tracker.fix_state.verify_state` does the
telling.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dportsv3.tracker.agentic_queries._util import _maybe, _row_dict

_SELECT = (
    "SELECT v.*, "
    "(SELECT j.state FROM jobs j WHERE j.job_id = v.job_id) AS job_state "
    "FROM verify_requests v WHERE v.bundle_id = ? "
    "ORDER BY v.requested_at DESC, v.id DESC"
)


def latest_verify_request(
    conn: sqlite3.Connection, bundle_id: str,
) -> dict[str, Any] | None:
    """The newest verify request for one occurrence, or None.

    Carries ``job_state`` -- the state of the job the runner created for it,
    NULL while the request is still pending or if the job row is gone.
    """
    return _maybe(conn.execute(_SELECT + " LIMIT 1", (bundle_id,)).fetchone())


def verify_requests_for_bundle(
    conn: sqlite3.Connection, bundle_id: str, *, limit: int = 10,
) -> list[dict[str, Any]]:
    """Every verify asked for on one occurrence, newest first.

    ``bundles`` records only the LAST verification outcome, so the requests
    are the only trace that an earlier one was asked for and in which env.
    """
    rows = conn.execute(
        _SELECT + " LIMIT ?", (bundle_id, max(1, int(limit))),
    ).fetchall()
    return [_row_dict(r) for r in rows]
