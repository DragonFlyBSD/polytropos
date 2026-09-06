"""Bundle reads + artifact refs for the tracker's agentic endpoints."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from dportsv3.agent.lifecycle import ACTIVE_WORK_STATE_VALUES
from dportsv3.tracker.agentic_queries._util import (
    like_contains,
    _row_dict,
    _maybe,
    _decode_extra_json,
)


def _bundle_where(
    target: str | None,
    origin: str | None,
    resolution: str | None,
    search: str | None,
) -> tuple[str, list[Any]]:
    """The WHERE ``list_bundles`` and ``count_bundles`` share."""
    clauses: list[str] = []
    params: list[Any] = []
    if target is not None:
        clauses.append("target = ?")
        params.append(target)
    if origin is not None:
        clauses.append("origin = ?")
        params.append(origin)
    if resolution is not None:
        # The empty string means "no resolution yet", which is a real
        # filter -- untriaged occurrences -- and NULL never equals anything.
        if resolution == "":
            clauses.append("resolution IS NULL")
        else:
            clauses.append("resolution = ?")
            params.append(resolution)
    if search:
        pattern = like_contains(search)
        clauses.append(
            r"(origin LIKE ? ESCAPE '\' OR bundle_id LIKE ? ESCAPE '\')"
        )
        params.extend([pattern, pattern])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def list_bundles(
    conn: sqlite3.Connection,
    target: str | None = None,
    origin: str | None = None,
    limit: int = 100,
    *,
    resolution: str | None = None,
    search: str | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Occurrences, newest first.

    ``origin`` is an exact match; ``search`` is a case-insensitive
    substring of the origin or the bundle id, so the box answers both what
    an operator types and what they paste.
    """
    where, params = _bundle_where(target, origin, resolution, search)
    sql = (
        f"SELECT * FROM bundles{where} "
        "ORDER BY ts_utc DESC, bundle_id DESC LIMIT ? OFFSET ?"
    )
    params.extend([max(1, int(limit)), max(0, int(offset))])
    return [_row_dict(row) for row in conn.execute(sql, params).fetchall()]


def count_bundles(
    conn: sqlite3.Connection,
    target: str | None = None,
    origin: str | None = None,
    *,
    resolution: str | None = None,
    search: str | None = None,
) -> int:
    """How many occurrences the same filters match."""
    where, params = _bundle_where(target, origin, resolution, search)
    row = conn.execute(f"SELECT COUNT(*) FROM bundles{where}", params).fetchone()
    return int(row[0]) if row is not None else 0


def get_bundle(conn: sqlite3.Connection, bundle_id: str) -> dict[str, Any] | None:
    # build_run_id is the farm build this failure came out of -- the
    # cockpit's "originating build" link, and the ordinal C3 compares
    # against an issue's known-good watermark. It lives on `runs`, not on
    # the bundle, so every reader that wants it has to join.
    row = conn.execute(
        "SELECT b.*, r.build_run_id AS build_run_id "
        "FROM bundles b LEFT JOIN runs r ON r.run_id = b.run_id "
        "WHERE b.bundle_id = ?",
        (bundle_id,),
    ).fetchone()
    if row is None:
        return None
    bundle = _row_dict(row)
    artifacts = conn.execute(
        """SELECT relpath, backend, sha256, fs_path, kind, size, created_at
           FROM artifact_refs
           WHERE bundle_id = ?
           ORDER BY relpath ASC""",
        (bundle_id,),
    ).fetchall()
    bundle["artifacts"] = [_row_dict(r) for r in artifacts]
    return bundle


def get_artifact_ref(
    conn: sqlite3.Connection, bundle_id: str, relpath: str
) -> dict[str, Any] | None:
    return _maybe(
        conn.execute(
            """SELECT backend, sha256, fs_path, kind, size, created_at
               FROM artifact_refs
               WHERE bundle_id = ? AND relpath = ?""",
            (bundle_id, relpath),
        ).fetchone()
    )


def list_port_bundles(
    conn: sqlite3.Connection,
    origin: str,
    target: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM bundles WHERE origin = ?"
    params: list[Any] = [origin]
    if target is not None:
        sql += " AND target = ?"
        params.append(target)
    sql += " ORDER BY ts_utc DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [_row_dict(row) for row in conn.execute(sql, params).fetchall()]


def bundles_for_run(
    conn: sqlite3.Connection, run_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM bundles WHERE run_id = ? ORDER BY ts_utc DESC LIMIT ?",
        (run_id, max(1, int(limit))),
    ).fetchall()
    return [_row_dict(row) for row in rows]
