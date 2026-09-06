"""Adapter that exposes tracker data in dsynth-progress' JSON shape.

The dsynth-progress UI (``www/example/progress.{html,js,css}``) consumes
two endpoints:

- ``summary.json``      — profile + kickoff + stats + active builders
- ``<NN>_history.json`` — array of build entries, paginated into chunks

Entries carry one field dsynth-progress never had: ``bundle_id``, the
evidence bundle a failed port produced. The lifted UI linked each row to
a ``.log`` file sitting beside the static report; there is no such file
here, so the link is built from the bundle instead.

This module maps the tracker's ``state.db`` rows (``build_runs``,
``build_results``, ``port_status``) into that shape so the lifted UI
runs against tracker data without modification.

Result vocabulary mapping:
- tracker ``success``  → dsynth ``built``
- tracker ``failure``  → dsynth ``failed``
- tracker ``skipped``  → dsynth ``skipped``
- tracker ``ignored``  → dsynth ``ignored``
- (no tracker analog)  → dsynth ``meta``  — left at 0

Chunk size is fixed at 1000 entries per ``<NN>_history.json``, matching
dsynth-progress' own chunking. ``kfiles`` in summary.json is the count
of chunks the UI should fetch.

MEASURED VERSUS DSYNTH SHAPE
----------------------------

Half of dsynth's payload describes a build farm this tracker does not
model. Those fields are still emitted, because the lifted progress.js
writes them straight into the DOM, but they carry no measurement and a
view must render them as absent rather than as a value:

- ``stats.load``, ``stats.swapinfo``   the literal ``"  -"``
- ``stats.pkghour``, ``stats.impulse``, ``stats.meta``   the literal 0
- ``builders[].phase``     always the literal ``"build"``
- ``builders[].elapsed``   always ``" --:--:--"``, and it cannot be
                           computed: enqueue_ports writes recorded_at=''
                           and update_port_status only moves the status, so
                           nothing anywhere records when a port started
                           building (poly-0e02.4)
- ``builders[].lines``     always ``""``
- ``builders[].ID``        a zero-padded index over ports in 'building'
                           state -- there is no per-builder-slot model
- ``entry.ID``             always ``"00"``, same reason
- ``entry.elapsed``, ``entry.duration``   always ``""``; the tracker
                           records when a port finished, not how long
                           it took

Measured, and safe to render as fact: ``profile``, ``kickoff``,
``active``, ``kfiles``, ``stats.elapsed`` (the run's own wall clock),
the outcome counts, and every entry's origin / version / result /
``recorded_at``.

``stats.queued`` is dsynth's meaning of the word -- the whole queue, ie
total_expected -- and NOT the number of rows sitting in 'queued' status.
``stats.in_queue`` and ``stats.in_progress`` are those, added because a
view that filtered on the dsynth name would be wrong by the size of the
build.

The queued rows themselves are deliberately not in this payload: a run
starts with every origin queued, so shipping them would put the whole
tree in summary.json. ``stats.in_queue`` is the count, and the Builds
dashboard (``/?run=N&state=queued``) is the paged list.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dportsv3.tracker.db import BUNDLE_FOR_RESULT_SQL

CHUNK_SIZE = 1000

_RESULT_TO_DSYNTH = {
    "success": "built",
    "failure": "failed",
    "skipped": "skipped",
    "ignored": "ignored",
}


def _latest_run_id(conn: sqlite3.Connection, target: str) -> int | None:
    row = conn.execute(
        """SELECT id FROM build_runs
           WHERE target = ?
           ORDER BY started_at DESC, id DESC LIMIT 1""",
        (target,),
    ).fetchone()
    return int(row[0]) if row else None


def target_summary(conn: sqlite3.Connection, target: str) -> dict[str, Any]:
    """Return the summary.json shape for the latest run on ``target``."""
    run_id = _latest_run_id(conn, target)
    if run_id is None:
        return _empty_summary(target)
    summary = _run_summary_by_id(conn, run_id)
    return summary if summary is not None else _empty_summary(target)


def run_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    """Return the summary.json shape for a specific build_run, or None."""
    return _run_summary_by_id(conn, run_id)


def _run_summary_by_id(
    conn: sqlite3.Connection, run_id: int
) -> dict[str, Any] | None:
    run = conn.execute(
        """SELECT id, target, build_type, started_at, finished_at, total_expected
           FROM build_runs WHERE id = ?""",
        (run_id,),
    ).fetchone()
    if run is None:
        return None

    counts = conn.execute(
        """SELECT
             COUNT(*) AS total,
             COALESCE(SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END), 0) AS built,
             COALESCE(SUM(CASE WHEN result = 'failure' THEN 1 ELSE 0 END), 0) AS failed,
             COALESCE(SUM(CASE WHEN result = 'skipped' THEN 1 ELSE 0 END), 0) AS skipped,
             COALESCE(SUM(CASE WHEN result = 'ignored' THEN 1 ELSE 0 END), 0) AS ignored,
             COALESCE(SUM(CASE WHEN status = 'building' THEN 1 ELSE 0 END), 0) AS building,
             COALESCE(SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), 0) AS queued
           FROM build_results WHERE build_run_id = ?""",
        (run_id,),
    ).fetchone()

    total_recorded = int(counts["total"])
    total_expected = int(run["total_expected"] or total_recorded)
    remains = max(0, total_expected - total_recorded)

    elapsed = _elapsed_str(str(run["started_at"]), run["finished_at"])

    builders = _active_builders(conn, run_id)

    # kfiles counts chunks of *historical* rows the UI will fetch —
    # building/queued rows live in `builders`, not in NN_history.json,
    # so exclude them from the chunk math.
    historical = total_recorded - int(counts["building"]) - int(counts["queued"])
    kfiles = max(1, (historical + CHUNK_SIZE - 1) // CHUNK_SIZE) if historical else 0

    return {
        "profile": str(run["target"]),
        "kickoff": _format_kickoff(str(run["started_at"])),
        "kfiles": kfiles,
        "active": 1 if run["finished_at"] is None else 0,
        "stats": {
            # dsynth's "queued" is the whole queue. in_queue / in_progress
            # are the rows actually in those states.
            "queued": total_expected,
            "in_queue": int(counts["queued"]),
            "in_progress": int(counts["building"]),
            "built": int(counts["built"]),
            "failed": int(counts["failed"]),
            "ignored": int(counts["ignored"]),
            "skipped": int(counts["skipped"]),
            "remains": remains,
            "meta": 0,
            "elapsed": elapsed,
            "pkghour": 0,
            "impulse": 0,
            "swapinfo": "  -",
            "load": "  -",
        },
        "builders": builders,
    }


def target_history_chunk(
    conn: sqlite3.Connection,
    target: str,
    chunk_index: int,
) -> list[dict[str, Any]]:
    """Return one chunk of build entries for the latest run on ``target``."""
    run_id = _latest_run_id(conn, target)
    if run_id is None:
        return []
    return run_history_chunk(conn, run_id, chunk_index)


def run_history_chunk(
    conn: sqlite3.Connection,
    run_id: int,
    chunk_index: int,
) -> list[dict[str, Any]]:
    """Return one chunk of build entries for a specific build_run.

    Chunks are 1-indexed to match dsynth-progress (``01_history.json`` =
    chunk 1). Returns an empty list past the last chunk.
    """
    if chunk_index < 1:
        return []
    offset = (chunk_index - 1) * CHUNK_SIZE
    # 'building' and 'queued' rows are in-flight — they belong in
    # summary.builders, not in the historical record.
    rows = conn.execute(
        f"""SELECT br.origin, br.version, br.result, br.recorded_at, br.status,
                   {BUNDLE_FOR_RESULT_SQL} AS bundle_id
           FROM build_results br
           WHERE br.build_run_id = ?
             AND br.status NOT IN ('building', 'queued')
           ORDER BY br.recorded_at ASC, br.origin ASC
           LIMIT ? OFFSET ?""",
        (run_id, CHUNK_SIZE, offset),
    ).fetchall()

    entries: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        entry = {
            "entry": offset + i + 1,
            "elapsed": "",
            "ID": "00",
            "result": _RESULT_TO_DSYNTH.get(
                str(row["result"] or ""), str(row["result"] or "")
            ),
            "origin": str(row["origin"]),
            "info": str(row["version"] or ""),
            "duration": "",
            # Selected all along and then dropped on the floor, which left a
            # "Recorded" column with nothing to show.
            "recorded_at": str(row["recorded_at"] or "") or None,
        }
        # The bundle this port's failure produced, when it produced one.
        # It is what makes the row's logfile link resolvable: the log is a
        # blob on the bundle, not a file beside a static report. Only
        # failures upload evidence, so successes never carry this.
        bundle_id = row["bundle_id"]
        if bundle_id:
            entry["bundle_id"] = str(bundle_id)
        entries.append(entry)
    return entries


def _empty_summary(target: str) -> dict[str, Any]:
    return {
        "profile": target,
        "kickoff": "",
        "kfiles": 0,
        "active": 0,
        "stats": {
            "queued": 0,
            "in_queue": 0,
            "in_progress": 0,
            "built": 0,
            "failed": 0,
            "ignored": 0,
            "skipped": 0,
            "remains": 0,
            "meta": 0,
            "elapsed": "",
            "pkghour": 0,
            "impulse": 0,
            "swapinfo": "  -",
            "load": "  -",
        },
        "builders": [],
    }


def _active_builders(
    conn: sqlite3.Connection, run_id: int
) -> list[dict[str, Any]]:
    """One row per port currently in 'building' state.

    Tracker has no per-builder-slot model — every in-progress port maps
    to one virtual slot ID (zero-padded index). Matches dsynth-progress'
    table shape without claiming we have N physical builder slots.
    """
    rows = conn.execute(
        """SELECT origin, version
           FROM build_results
           WHERE build_run_id = ? AND status = 'building'
           ORDER BY origin ASC""",
        (run_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        out.append(
            {
                # ID / elapsed / phase / lines are dsynth shape and carry no
                # measurement -- see the module docstring. origin, version
                # and recorded_at are the row's own.
                "ID": _two_digit(i),
                "elapsed": " --:--:--",
                "phase": "build",
                "origin": str(row["origin"]),
                "lines": "",
                "version": str(row["version"] or "") or None,
            }
        )
    return out


def _two_digit(n: int) -> str:
    return f"{n:02d}" if n < 100 else str(n)


def _elapsed_str(started_at: str, finished_at: str | None) -> str:
    """HH:MM:SS between two ISO timestamps (best-effort).

    dsynth-progress' format is space-padded HH:MM:SS. If timestamps
    don't parse, returns empty.
    """
    from datetime import datetime

    try:
        start = datetime.fromisoformat(started_at)
    except (ValueError, TypeError):
        return ""
    if finished_at:
        try:
            end = datetime.fromisoformat(finished_at)
        except (ValueError, TypeError):
            return ""
    else:
        end = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
    delta = end - start
    secs = max(0, int(delta.total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_kickoff(started_at: str) -> str:
    """Best-effort match to dsynth's ' DD-Mon-YYYY HH:MM:SS UTC' format."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(started_at)
    except (ValueError, TypeError):
        return started_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(" %d-%b-%Y %H:%M:%S UTC")
