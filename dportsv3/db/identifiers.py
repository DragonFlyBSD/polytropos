"""What a bundle, run or job id may contain.

These ids are URL path segments. ``request.url_for`` refuses a value with
a path separator outright -- Starlette's ``str`` convertor asserts on it --
so a single stored id containing ``/`` raises while COMPOSING the page,
which means it does not break one row, it breaks every page that links to
that row. Measured on one such bundle: the Builds dashboard, the
occurrences list, and any issue, job or run page that mentions it. That is
most of the repair workspace, and it is not repairable from the UI,
because the pages that would let an operator find the row are among the
ones that fail.

Every writer that ships already sanitises -- the dsynth hooks through
``sanitize_component``, the runner through ``origin.replace("/", "_")``,
``issue_key`` through a hexdigest -- so this is the guard for everything
else that can reach the store: the artifact_store CLI takes ``--bundle-id``
verbatim, the ingest port has no authentication, and any of those four
minters could change.

The rule is a whitelist, because the failure it prevents is caused by a
character nobody thought about.
"""

from __future__ import annotations

import re

#: Every character the shipped minters can emit, and nothing else.
#: ``@`` is in because hook_pkg_failure appends a flavor after
#: sanitising (``<origin>@<flavor>-<ts>``); ``:`` and ``+`` because a
#: target or a version can carry them.
_ALLOWED = re.compile(r"\A[A-Za-z0-9._~@:+-]+\Z")

#: What the ids may not be, spelled out for an error message. Anything
#: not in _ALLOWED is refused; these are the ones worth naming because
#: they are what someone actually types.
_NAMED = {"/": "a path separator", "\\": "a backslash", "?": "a query mark",
          "#": "a fragment mark", "%": "a percent sign", " ": "a space"}


def is_safe_identifier(value: object) -> bool:
    """Whether ``value`` can be a URL path segment and a database key."""
    return isinstance(value, str) and bool(_ALLOWED.match(value))


def require_identifier(kind: str, value: object) -> str:
    """Return ``value``, or raise ValueError naming what is wrong with it.

    Raised at the door rather than checked at each ``url_for``: there are
    a dozen call sites and the next one to be written would not know to
    defend itself.
    """
    if is_safe_identifier(value):
        return str(value)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{kind} is required")
    bad = next((c for c in value if not _ALLOWED.match(c)), "")
    named = _NAMED.get(bad)
    detail = f"{named} ({bad!r})" if named else f"{bad!r}"
    raise ValueError(
        f"{kind} {value!r} contains {detail}. These ids are URL path "
        f"segments, so they are limited to letters, digits and ._~@:+-"
    )


#: Where an unsafe id would be rendered as a link. Checked at startup so
#: the condition is named once, at a moment someone is reading logs,
#: rather than discovered as a 500 on five unrelated pages.
_ID_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bundles", "bundle_id"),
    ("jobs", "job_id"),
    ("runs", "run_id"),
    ("issues", "issue_key"),
)


def stored_unsafe_identifiers(conn) -> list[tuple[str, str, str]]:
    """``(table, column, value)`` for every stored id that cannot be a URL.

    The guard above stops new ones. This finds any that predate it --
    written by an older build, or by something that reached the store
    directly -- because they are invisible until a page tries to link to
    one, and then the page that would let you find them is among the ones
    that fail.

    Bounded: a handful is a problem to report, ten thousand is a
    different problem and listing them helps nobody.
    """
    found: list[tuple[str, str, str]] = []
    for table, column in _ID_COLUMNS:
        try:
            rows = conn.execute(
                f"SELECT {column} FROM {table} "  # noqa: S608 — fixed names
                f"WHERE {column} LIKE '%/%' OR {column} LIKE '%?%' "
                f"OR {column} LIKE '%#%' OR {column} LIKE '% %' "
                f"LIMIT 20"
            ).fetchall()
        except Exception:  # noqa: BLE001 — a missing table is not a fault
            continue
        for row in rows:
            value = row[0]
            if not is_safe_identifier(value):
                found.append((table, column, str(value)))
    return found


#: Every column that holds one of these ids, so a rename can carry.
#: Derived by inspection, not by FK metadata: most of these are plain
#: indexed columns rather than declared foreign keys, which is why a
#: rename has to be told where to go.
_REFERENCES: dict[str, tuple[tuple[str, str], ...]] = {
    "bundle_id": (
        ("bundles", "bundle_id"), ("jobs", "bundle_id"),
        ("activity_log", "bundle_id"), ("artifact_refs", "bundle_id"),
        ("artifacts", "bundle_id"), ("bundle_review_requests", "bundle_id"),
        ("origin_skip_flags", "bundle_id"),
        ("user_context_requests", "bundle_id"),
        ("verify_requests", "bundle_id"),
    ),
    "run_id": (
        ("runs", "run_id"), ("bundles", "run_id"),
        ("user_context", "run_id"), ("user_context_history", "run_id"),
        ("user_context_requests", "run_id"),
    ),
    "job_id": (
        ("jobs", "job_id"), ("activity_log", "job_id"),
        ("job_events", "job_id"), ("runner_status", "job_id"),
        ("verify_requests", "job_id"),
    ),
    "issue_key": (("issues", "issue_key"), ("bundles", "issue_key")),
}


def safe_form(value: str) -> str:
    """The id the minters would have produced for this value.

    Same rule the dsynth hooks use -- ``tr '/:@' '___'`` then drop
    anything outside the set -- so a repaired id reads like every other
    one rather than like a repair.
    """
    swapped = value.replace("/", "_").replace("\\", "_")
    kept = [c for c in swapped if is_safe_identifier(c)]
    return "".join(kept) or "unnamed"


def repair_unsafe_identifiers(conn, *, dry_run: bool = True) -> list[dict]:
    """Rewrite every stored id that cannot be a URL, and its references.

    Explicit rather than automatic at startup. Renaming an id touches up
    to nine tables, and a rename that goes wrong is worse than the pages
    it fixes -- so this is a command someone runs, having read what it
    would do.

    Returns one entry per rename: the table it was found in, the old and
    new value, and how many rows each referencing column changed. With
    ``dry_run`` (the default) nothing is written and the counts are what
    WOULD change.
    """
    plan: list[dict] = []
    for table, column, value in stored_unsafe_identifiers(conn):
        if (table, column) not in [
            refs[0] for refs in _REFERENCES.values()
        ]:
            # Only the owning table seeds a rename; a dangling reference
            # is repaired by the rename of the row it points at.
            continue
        new = safe_form(value)
        # Collision is possible -- two bad ids can sanitise to one -- so
        # the repair says so rather than merging two occurrences.
        taken = conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ?",  # noqa: S608
            (new,),
        ).fetchone()
        entry = {"table": table, "column": column, "old": value,
                 "new": new, "collision": taken is not None, "updated": {}}
        if taken is None:
            for ref_table, ref_col in _REFERENCES[column]:
                try:
                    cur = conn.execute(
                        f"UPDATE {ref_table} SET {ref_col} = ? "  # noqa: S608
                        f"WHERE {ref_col} = ?", (new, value),
                    )
                except Exception:  # noqa: BLE001 — table may not exist
                    continue
                if cur.rowcount:
                    entry["updated"][f"{ref_table}.{ref_col}"] = cur.rowcount
        plan.append(entry)
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return plan
