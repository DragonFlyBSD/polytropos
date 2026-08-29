"""init_db must survive a state.db that predates a column.

Found deploying onto a host whose evidence tree carried a state.db from
an earlier generation of the tool. The tracker crashed on startup::

    sqlite3.OperationalError: no such column: issue_key
      File ".../db/schema.py", line 519, in init_db
        conn.executescript(SCHEMA)

Two defects, and either one alone is enough to break startup:

1. ``MIGRATIONS`` carried an entry for ``jobs.owner_id`` but none for
   ``bundles.issue_key``.
2. ``init_db`` ran ``executescript(SCHEMA)`` *before* ``MIGRATIONS``.
   SCHEMA builds indexes, so ``idx_bundles_issue_key`` was asked to
   index a column three steps before the ALTER that adds it. Every
   table is ``CREATE TABLE IF NOT EXISTS``, so the outdated table was
   left alone and the index was the thing that blew up.

``jobs.owner_id`` has no index, which is the only reason it stayed
invisible. The ordering defect is general: it fires for whichever
migrated column gets indexed next.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from dportsv3.db import schema as schema_mod
from dportsv3.db.schema import MIGRATIONS, SCHEMA, init_db


_INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+"
    r"ON\s+(\w+)\s*\(([^)]*)\)",
    re.IGNORECASE,
)
_ALTER = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE
)


def _indexed_columns() -> list[tuple[str, str, str]]:
    """(index, table, column) for every column SCHEMA indexes."""
    out = []
    for index, table, cols in _INDEX.findall(SCHEMA):
        for col in cols.split(","):
            name = col.strip().split()[0] if col.strip() else ""
            if name:
                out.append((index, table, name))
    return out


def _migrated_columns() -> set[tuple[str, str]]:
    return {(t.lower(), c.lower()) for t, c in _ALTER.findall("\n".join(MIGRATIONS))}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


# --- the reported failure ---------------------------------------------------

def test_init_db_survives_a_bundles_table_without_issue_key():
    """The exact shape found on the host: an older `bundles`, grown by
    ALTER over time, that never received issue_key."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("DROP INDEX idx_bundles_issue_key")
    conn.execute("ALTER TABLE bundles DROP COLUMN issue_key")
    assert "issue_key" not in _columns(conn, "bundles")

    init_db(conn)

    assert "issue_key" in _columns(conn, "bundles")
    assert "idx_bundles_issue_key" in _indexes(conn, "bundles")


def test_bundles_issue_key_has_a_migration():
    assert ("bundles", "issue_key") in _migrated_columns()


def test_legacy_rows_are_kept_not_wiped():
    """The point of ADD COLUMN over a rebuild: real history survives."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("DROP INDEX idx_bundles_issue_key")
    conn.execute("ALTER TABLE bundles DROP COLUMN issue_key")
    conn.execute("INSERT INTO bundles(bundle_id, origin) VALUES ('b-1', 'devel/foo')")
    conn.commit()

    init_db(conn)

    row = conn.execute(
        "SELECT bundle_id, origin, issue_key FROM bundles"
    ).fetchone()
    assert row == ("b-1", "devel/foo", None)


# --- the general guard ------------------------------------------------------

@pytest.mark.parametrize("table,column", sorted(_migrated_columns()))
def test_a_migrated_column_is_restored_before_its_index_is_built(
    table, column
):
    """Simulate a DB predating each migrated column and re-init.

    This is the guard that generalizes. Whichever migrated column gets
    indexed next, the ordering has to hold for it too — and if it does
    not, this fails here rather than in production on somebody's
    existing state.db.

    Columns SCHEMA does not index pass trivially; they are parametrized
    anyway so the set stays honest as MIGRATIONS grows.
    """
    indexes_over = {
        index for index, t, c in _indexed_columns()
        if t.lower() == table and c.lower() == column
    }

    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for existing in _indexes(conn, table):
        if not existing.startswith("sqlite_"):
            conn.execute(f"DROP INDEX IF EXISTS {existing}")
    try:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    except sqlite3.OperationalError:
        pytest.skip(f"{table}.{column} is not droppable; cannot simulate")

    init_db(conn)

    assert column in _columns(conn, table), (
        f"{table}.{column} is in MIGRATIONS but is not there after "
        f"init_db on a DB that predates it"
    )
    for index in indexes_over:
        assert index in _indexes(conn, table), (
            f"{index} indexes {table}.{column}; SCHEMA must run after "
            f"the ALTER that adds it"
        )


# --- what must not regress --------------------------------------------------

def test_a_fresh_db_still_initialises():
    """Migrations now run first, so on a fresh DB every ALTER hits
    'no such table' before SCHEMA creates anything. That must stay
    tolerated rather than becoming a startup failure."""
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    assert "issue_key" in _columns(conn, "bundles")
    assert "owner_id" in _columns(conn, "jobs")


def test_init_db_is_idempotent():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    before = sorted(
        r[0] for r in conn.execute("SELECT name FROM sqlite_master ORDER BY name")
    )
    init_db(conn)
    after = sorted(
        r[0] for r in conn.execute("SELECT name FROM sqlite_master ORDER BY name")
    )
    assert before == after


def test_migrations_run_before_the_schema_script():
    """Ordering is the fix, so pin it: a SCHEMA index over a migrated
    column is only buildable if the ALTERs already ran."""
    import inspect

    src = inspect.getsource(schema_mod.init_db)
    assert src.index("for stmt in MIGRATIONS") < src.index("executescript(SCHEMA)")
