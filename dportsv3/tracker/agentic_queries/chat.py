"""Fix-review chat turns, stored against the occurrence they are about.

The conversation used to live only in the operator's localStorage. That
restores a reload on the same machine and nothing else: two operators
looking at the same failing port could not see each other's questions, and
the reasoning that produced a decision was not attached to the bundle that
was decided (poly-pf4a).

Occurrence-scoped, not issue-scoped, because that is what the chat is
about: ``fix_chat.build_chat_messages`` seeds it with one bundle's frozen
artifacts and that bundle's session dump, so a turn read beside a different
attempt would be answering about evidence that attempt never produced.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from dportsv3.tracker.agentic_queries._util import _row_dict


def list_chat_turns(
    conn: sqlite3.Connection, bundle_id: str,
) -> list[dict[str, Any]]:
    """Every turn for this occurrence, oldest first.

    ``artifacts_included`` comes back as a list; it is stored as JSON
    because it is a per-turn record of what the model was shown, not
    something anything queries across.
    """
    rows = conn.execute(
        """SELECT id, bundle_id, role, content, created_at,
                  session_relpath, artifacts_included
           FROM bundle_chat_turns
           WHERE bundle_id = ?
           ORDER BY id ASC""",
        (bundle_id,),
    ).fetchall()
    turns = []
    for row in rows:
        turn = _row_dict(row)
        raw = turn.get("artifacts_included")
        try:
            turn["artifacts_included"] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            # A turn is worth showing even if this one field is unreadable.
            turn["artifacts_included"] = []
        turns.append(turn)
    return turns


def append_chat_turn(
    conn: sqlite3.Connection,
    bundle_id: str,
    role: str,
    content: str,
    *,
    session_relpath: str | None = None,
    artifacts_included: list[str] | None = None,
) -> int:
    """Record one turn. Returns its row id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO bundle_chat_turns
               (bundle_id, role, content, created_at, session_relpath,
                artifacts_included)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (bundle_id, role, content, now, session_relpath,
         json.dumps(artifacts_included) if artifacts_included else None),
    )
    return int(cur.lastrowid or 0)


def clear_chat_turns(conn: sqlite3.Connection, bundle_id: str) -> int:
    """Drop this occurrence's conversation. Returns how many turns went.

    Deliberately a delete and not a soft-hide: the operator asking for it
    is asking for it to be gone, and a conversation nobody wants kept is
    not evidence.
    """
    cur = conn.execute(
        "DELETE FROM bundle_chat_turns WHERE bundle_id = ?", (bundle_id,),
    )
    return int(cur.rowcount or 0)
