"""Delivery reads for the pipeline's delivery stage.

``review.py`` has the writers and the two single-row lookups the accept
path needs. What it has no shape for is a *list*: the delivery drawer
wants (issue, origin, provider, PR, status) as a set, and building that
from ``open_delivery_bundle_ids`` plus one
``latest_review_request_for_bundle`` per id is N+1.

Every read here is over the LATEST delivery row per bundle, the same rule
``open_delivery_bundle_ids`` uses: a bundle whose PR was closed out of band
must not read as open because an older row still says ``created``.

TWO AXES, NOT ONE STAGE
-----------------------
"Deliveries open upstream" and "issues awaiting a confirm build" are
different populations, and the obvious shortcut -- counting
``issues.state = 'resolving'`` and labelling it open PRs -- is wrong in
both directions. Since A2 an issue resolves when a build proves the fix,
never when a PR merges, so an issue can reach ``resolved`` with no PR ever
opened: poly-8e2 measured 17 of them, every one with a ``create_failed``
delivery row and an archive entry saying finished. :func:`delivery_counts`
returns both figures under names that cannot be mistaken for each other.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any

from dportsv3.tracker import issue_state
from dportsv3.tracker.agentic_queries._util import like_contains, _row_dict

#: Every status a ``bundle_review_requests`` row can hold. ``skipped`` is
#: deliberately absent: the orchestrator reports it as an outcome when
#: there is no provider or no diff, and writes no row at all.
DELIVERY_STATUSES: tuple[str, ...] = (
    "created", "updated", "merged", "closed", "create_failed",
)

#: Still open upstream, so still worth polling. Named once here and passed
#: to :func:`list_deliveries` as an ordinary status filter.
#:
#: ``review.find_open_review_request`` spells the same set the other way
#: round -- ``NOT IN ('closed', 'merged', 'create_failed')`` -- and must
#: keep doing so: it is an idempotency lookup that has to match the partial
#: unique index ``uq_brr_open_branch`` literally. The two agree only
#: because :data:`DELIVERY_STATUSES` is the whole vocabulary.
DELIVERY_OPEN_STATUSES: tuple[str, ...] = ("created", "updated")

#: A delivery that was attempted and never reached the provider. The fix
#: exists only in the local DeltaPorts; poly-8e2 owns making that visible
#: in the issue archive, which is a different question from counting it.
DELIVERY_FAILED_STATUS = "create_failed"

# "This is the newest delivery row for its bundle", as a predicate rather
# than a join against a MAX(id) aggregate.
#
# open_delivery_bundle_ids spells it as that aggregate, which is right
# there -- it wants every open bundle and has no LIMIT to stop at. Here it
# would be the wrong shape by two orders of magnitude: the aggregate has to
# be materialised over the whole table before the first row comes out, and
# ORDER BY id DESC then needs a temp b-tree on top. As a predicate the
# planner walks the table backwards on the rowid, checks one indexed
# subquery per row and stops at the LIMIT. First page of 100: 0.19 ms
# against 17.5 ms over 19,142 delivery rows; at offset 10,000, 8.6 against
# 27.6 ms. Only the unbounded histogram prefers the aggregate (4.6 against
# 5.6 ms), which is not worth a second spelling of the same rule.
_IS_LATEST = """NOT EXISTS (SELECT 1 FROM bundle_review_requests r2
                    WHERE r2.bundle_id = r.bundle_id AND r2.id > r.id)"""


def _delivery_where(
    statuses: Iterable[str] | None,
    provider: str | None,
    search: str | None,
) -> tuple[str, list[Any]]:
    """The WHERE ``list_deliveries`` and ``count_deliveries`` share, so a
    page and the total it is a page of cannot describe different sets."""
    clauses: list[str] = [_IS_LATEST]
    params: list[Any] = []
    status_list = list(statuses) if statuses is not None else None
    if status_list is not None:
        unknown = [s for s in status_list if s not in DELIVERY_STATUSES]
        if unknown:
            raise ValueError(f"Invalid delivery status: {unknown[0]}")
        if not status_list:
            # An explicit empty filter matches nothing. Without this it
            # would render as `IN ()`, which SQLite rejects outright.
            return " WHERE 0", []
        clauses.append(f"r.status IN ({','.join('?' * len(status_list))})")
        params.extend(status_list)
    if provider is not None:
        clauses.append("r.provider = ?")
        params.append(provider)
    if search:
        # The origin is what an operator types; the PR number and the
        # branch are what they paste out of a provider tab.
        pattern = like_contains(search)
        clauses.append(
            r"(b.origin LIKE ? ESCAPE '\' "
            r"OR r.provider_pr_id LIKE ? ESCAPE '\' "
            r"OR r.branch LIKE ? ESCAPE '\')"
        )
        params.extend([pattern, pattern, pattern])
    return " WHERE " + " AND ".join(clauses), params


_DELIVERY_SELECT = """
    SELECT r.id AS id, r.bundle_id AS bundle_id, r.provider AS provider,
           r.provider_pr_id AS provider_pr_id, r.url AS url,
           r.branch AS branch, r.title AS title, r.status AS status,
           r.created_at AS created_at, r.last_synced_at AS last_synced_at,
           r.error AS error, r.operator AS operator,
           b.origin AS origin, b.target AS target,
           b.issue_key AS issue_key, b.resolution AS bundle_resolution,
           i.state AS issue_state
"""

# LEFT JOIN both ways down: a delivery row outlives its bundle being
# pruned, and a bundle can carry no issue_key. Neither makes the delivery
# less real, and dropping it would make the drawer disagree with the count.
_DELIVERY_FROM = """
      FROM bundle_review_requests r
      LEFT JOIN bundles b ON b.bundle_id = r.bundle_id
      LEFT JOIN issues i ON i.issue_key = b.issue_key
"""


def list_deliveries(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None = None,
    provider: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Delivery rows with the issue and origin they belong to, newest first.

    One row per bundle -- its newest delivery -- carrying everything the
    drawer shows, so a page of deliveries is one query rather than one
    query plus a lookup per row.

    ``statuses`` is an allow-list out of :data:`DELIVERY_STATUSES`; pass
    :data:`DELIVERY_OPEN_STATUSES` for the ones still open upstream. An
    unknown status raises rather than silently matching nothing, because a
    filter that quietly returns an empty page reads exactly like a stage
    with no work in it.

    Ordered by ``id`` DESC, which is delivery order: the column is
    AUTOINCREMENT, so it needs no timestamp and no clock.
    """
    where, params = _delivery_where(statuses, provider, search)
    sql = (
        f"{_DELIVERY_SELECT}{_DELIVERY_FROM}{where} "
        "ORDER BY r.id DESC LIMIT ? OFFSET ?"
    )
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    return [_row_dict(row) for row in conn.execute(sql, params).fetchall()]


def count_deliveries(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None = None,
    provider: str | None = None,
    search: str | None = None,
) -> int:
    """How many deliveries the same filters match."""
    where, params = _delivery_where(statuses, provider, search)
    row = conn.execute(
        f"SELECT COUNT(*){_DELIVERY_FROM}{where}", params,
    ).fetchone()
    return int(row[0]) if row is not None else 0


def delivery_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    """The delivery stage's inventory -- both of its axes, named apart.

    ``by_status`` counts delivery rows (newest per bundle), zero-filled
    over :data:`DELIVERY_STATUSES`. ``open`` is how many are still open
    upstream and ``failed`` how many never reached the provider at all.

    ``awaiting_confirm_build`` is not a delivery figure and is returned
    alongside them so the page cannot accidentally print one as the other:
    it counts issues in ``resolving``, which since A2 means "a fix was
    accepted and a build must prove it", not "a PR is open". Neither
    population contains the other -- an issue can be resolved with a
    failed delivery (poly-8e2), and a merged PR can belong to an issue
    that reopened.
    """
    by_status = {status: 0 for status in DELIVERY_STATUSES}
    rows = conn.execute(
        "SELECT r.status AS status, COUNT(*) AS n "
        f"FROM bundle_review_requests r WHERE {_IS_LATEST} "
        "GROUP BY r.status"
    ).fetchall()
    for row in rows:
        # A status outside the vocabulary is counted rather than
        # dropped or raised on: update_review_request_status takes any
        # string, so a writer can add one, and an unnamed status on
        # the page is the signal to name it here. Losing it silently
        # would make the stage look emptier than it is.
        status = str(row["status"])
        by_status[status] = by_status.get(status, 0) + int(row["n"])
    awaiting = conn.execute(
        "SELECT COUNT(*) FROM issues WHERE state = ?",
        (issue_state.ISSUE_RESOLVING,),
    ).fetchone()
    return {
        "by_status": by_status,
        "total": sum(by_status.values()),
        "open": sum(by_status[s] for s in DELIVERY_OPEN_STATUSES),
        "failed": by_status[DELIVERY_FAILED_STATUS],
        "awaiting_confirm_build": int(awaiting[0] or 0),
    }
