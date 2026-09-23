"""Operator notes: something the operator knows that the agent does not.

Queued by the job page, delivered by the loop on its next turn, and kept
forever afterwards -- a note is part of why the job did what it did next,
so a later reader needs it in place (poly-qqx9.11).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from dportsv3.tracker.agentic_queries._util import _row_dict

#: A sentence or two. The cap is not about storage -- it is that this
#: lands in the model's context on the next turn, and a wall of text
#: there costs the job tokens it was budgeted without.
MAX_NOTE_CHARS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_operator_note(
    conn: sqlite3.Connection,
    job_id: str,
    text: str,
    author: str | None = None,
) -> dict[str, Any]:
    """Record a note for the running job. Returns the stored row."""
    body = (text or "").strip()[:MAX_NOTE_CHARS]
    if not body:
        raise ValueError("an operator note cannot be empty")
    cur = conn.execute(
        "INSERT INTO operator_notes (job_id, text, author, created_at) "
        "VALUES (?, ?, ?, ?)",
        (job_id, body, author, _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM operator_notes WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return _row_dict(row)


def operator_notes_for_job(
    conn: sqlite3.Connection, job_id: str,
) -> list[dict[str, Any]]:
    """Every note for this job, oldest first, delivered or not."""
    rows = conn.execute(
        "SELECT * FROM operator_notes WHERE job_id = ? ORDER BY id ASC",
        (job_id,),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def take_pending_operator_notes(
    conn: sqlite3.Connection,
    job_id: str,
    attempt: int | None = None,
    turn: int | None = None,
) -> list[dict[str, Any]]:
    """Claim this job's undelivered notes, stamping where they landed.

    Claim, not read: the loop is about to put these in front of the model
    and must not do it twice. The UPDATE is the claim, and it runs before
    the rows are returned, so a crash between the two loses the note
    rather than repeating it -- the safer direction for something that
    costs tokens every time it is delivered.
    """
    pending = conn.execute(
        "SELECT * FROM operator_notes "
        "WHERE job_id = ? AND delivered_at IS NULL ORDER BY id ASC",
        (job_id,),
    ).fetchall()
    if not pending:
        return []
    ids = [int(row["id"]) for row in pending]
    conn.execute(
        "UPDATE operator_notes SET delivered_at = ?, delivered_attempt = ?, "
        "delivered_turn = ? WHERE id IN (%s)" % ",".join("?" * len(ids)),
        (_now(), attempt, turn, *ids),
    )
    conn.commit()
    stamped = _now()
    claimed = []
    for row in pending:
        item = _row_dict(row)
        # The caller is handed what is now on the row, not the snapshot
        # it read a moment ago: these are delivered from here on.
        item.update({"delivered_at": stamped, "delivered_attempt": attempt,
                     "delivered_turn": turn})
        claimed.append(item)
    return claimed


def pending_note_count(conn: sqlite3.Connection, job_id: str) -> int:
    """How many notes this job has waiting."""
    row = conn.execute(
        "SELECT COUNT(*) FROM operator_notes "
        "WHERE job_id = ? AND delivered_at IS NULL",
        (job_id,),
    ).fetchone()
    return int(row[0]) if row else 0
