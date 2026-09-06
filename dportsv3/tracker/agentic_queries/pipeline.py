"""Inventory counts for the pipeline overview.

The overview asks "is the system healthy and moving", so every number on
it has to be a real count of a named population -- not ``len()`` of a
capped page, which under-reports silently and gets more wrong the busier
the system is.

Three of the counts are plain aggregates (``issue_inventory``). The other
two look like aggregates and are not: ``regressed`` and the worklist band
depths are *projections* -- ``issue_state`` decides them in Python, from
rows, and no SQL predicate here is allowed to decide them a second time.
Both are still exact and uncapped; what makes that affordable is the shape
of the SQL, not a cap:

* regression can only happen to a ``resolved`` issue, so SQL narrows to
  those and Python streams the rows through the real predicate;
* a band depends on four columns and nothing else, so SQL groups by them
  and Python runs the projection once per distinct combination.

Measured on 20,000 issues / 51,359 occurrences / 17,122 jobs:
``issue_inventory`` 1.3 ms, ``regressed_issue_count`` 17.7 ms over 6,000
resolved issues, ``worklist_band_counts`` 23 ms over 12,800 open ones
(the same answer the full materialise gives, at 394 ms).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dportsv3.tracker import issue_state

#: The band a ``None`` bucket belongs to. ``issue_state.issue_bucket``
#: returns None for an open issue whose latest occurrence the runner is
#: still working: nothing for the operator to do *yet*. The worklist drops
#: those rows because it lists what needs you; an inventory must not,
#: because they are the system moving on its own -- which is exactly what
#: this page exists to show.
RUNNER_BAND = "runner"


def issue_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    """How many issues exist, by stored state, and how many are systemic.

    ``by_state`` is zero-filled over ``ISSUE_STORED_STATES`` so a state
    nothing is in reads as 0 rather than going missing -- the difference
    between "no issue is muted" and "the muted count is broken".

    STORED states only. ``regressed`` is derived from a ``resolved`` row
    and cannot appear in a GROUP BY; :func:`regressed_issue_count` is how
    many of the ``resolved`` figure are actually red again.

    Two statements rather than one: adding ``SUM(times_seen >= N)`` to the
    histogram forces a table scan and costs 3.4 ms, where the bare
    ``GROUP BY state`` runs off ``idx_issues_state`` in 0.4 ms and the
    systemic count off the table in 0.9 ms.
    """
    by_state = {state: 0 for state in sorted(issue_state.ISSUE_STORED_STATES)}
    histogram = "SELECT state, COUNT(*) FROM issues GROUP BY state"
    for row in conn.execute(histogram):
        by_state[str(row[0])] = int(row[1])
    systemic = conn.execute(
        "SELECT COUNT(*) FROM issues WHERE times_seen >= ?",
        (issue_state.SYSTEMIC_THRESHOLD,),
    ).fetchone()[0]
    return {
        "total": sum(by_state.values()),
        "by_state": by_state,
        "systemic": int(systemic or 0),
        "systemic_threshold": issue_state.SYSTEMIC_THRESHOLD,
    }


# Occurrences of resolved issues, with the two fields the boundary rule
# compares: the occurrence's own timestamp and the build ordinal of the run
# it came from. LEFT JOIN, because an occurrence whose run predates the
# build link still counts -- the rule falls back to timestamps for it.
_REGRESSED_SQL = """
    SELECT i.issue_key       AS issue_key,
           i.green_head_run_id AS green_head_run_id,
           i.resolved_at     AS resolved_at,
           b.ts_utc          AS ts_utc,
           r.build_run_id    AS build_run_id
      FROM issues i
      JOIN bundles b ON b.issue_key = i.issue_key
      LEFT JOIN runs r ON r.run_id = b.run_id
     WHERE i.state = ?
"""


def regressed_issue_count(conn: sqlite3.Connection) -> int:
    """How many resolved issues have come back -- exact, over all of them.

    ``regressed`` is derived on read (C3) precisely so the badge cannot
    disagree with what the builds did, and this count must not become the
    fourth definition of it: the boundary rule stays in
    ``issue_state.occurrence_past_boundary`` and rows are streamed through
    it. The SQL here only narrows the population, and the narrowing is a
    fact about the rule rather than a copy of it -- ``derived_regression``
    returns None for anything that is not ``resolved``, so no other state
    can contribute.

    Transcribing the rule into the WHERE clause instead runs in 7.8 ms
    against 17.7. That is not the trade: three implementations of
    "regressed" have already disagreed with each other (poly-l9y), and a
    fourth living in SQL would drift the same way for 10 ms.
    """
    crossed: set[str] = set()
    for row in conn.execute(_REGRESSED_SQL, (issue_state.ISSUE_RESOLVED,)):
        key = str(row["issue_key"])
        if key in crossed:
            continue
        if issue_state.occurrence_past_boundary(
            {"green_head_run_id": row["green_head_run_id"],
             "resolved_at": row["resolved_at"]},
            {"build_run_id": row["build_run_id"], "ts_utc": row["ts_utc"]},
        ):
            crossed.add(key)
    return len(crossed)


#: The occurrence columns ``fix_state.fix_status`` reads, and nothing else.
#: :func:`worklist_band_counts` groups by exactly these, so a projection
#: that starts reading a fifth column silently makes the counts wrong --
#: ``tests/test_pipeline_counts.py`` asserts the grouped answer still
#: equals the full materialise, which is what catches that.
BUCKET_INPUT_COLUMNS: tuple[str, ...] = (
    "resolution", "verification_status", "job_state",
)

# One row per distinct (issue state, resolution, verification_status,
# job_state) combination among open issues, with how many issues are in it.
#
# Only the LATEST occurrence is joined, because that is the one
# `issue_state.actionable_occurrence` picks -- same order as
# `_OCCURRENCE_ORDER`, ts_utc then bundle_id, so ties break identically.
# The rest of an issue's occurrences cannot change its band: for an open or
# resolving issue `effective_state` returns the stored state without
# looking at them at all.
#
# LEFT JOIN so an open issue with no occurrence yet still appears; it
# buckets as `decide` ("open, needs a look"), and dropping it would lose a
# real piece of operator work.
_BANDS_SQL = """
    SELECT i.state       AS issue_state,
           b.resolution  AS resolution,
           b.verification_status AS verification_status,
           (SELECT j.state FROM jobs j WHERE j.bundle_id = b.bundle_id
             ORDER BY j.created_ts_utc DESC, j.job_id DESC LIMIT 1)
               AS job_state,
           b.bundle_id IS NOT NULL AS has_occurrence,
           COUNT(*)      AS n
      FROM issues i
      LEFT JOIN bundles b ON b.bundle_id = (
            SELECT b2.bundle_id FROM bundles b2
             WHERE b2.issue_key = i.issue_key
             ORDER BY b2.ts_utc DESC, b2.bundle_id DESC LIMIT 1)
     WHERE i.state IN (?, ?)
     GROUP BY issue_state, resolution, verification_status, job_state,
              has_occurrence
"""


def worklist_band_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How deep each operator band is, across every open issue.

    Keyed by ``issue_state.ISSUE_WORKLIST_SECTIONS`` (minus the two
    archives, which are stored states and counted by
    :func:`issue_inventory`), plus :data:`RUNNER_BAND`. Every key is
    present, zero-filled.

    The band is ``issue_state.issue_bucket``'s answer and nothing else --
    this does not re-derive it, it just stops asking the same question
    12,800 times. ``fix_status`` reads three occurrence fields and
    ``issue_bucket`` adds the issue's state, so all the issues sharing
    those four values share a band: 64 distinct combinations here, so the
    projection runs 64 times and the counts multiply out. 23 ms against
    394 ms for materialising every issue and its occurrences, same answer.

    Indexing was the wrong lever for this: adding
    ``bundles(issue_key, ts_utc, bundle_id)`` and
    ``jobs(bundle_id, created_ts_utc, job_id)`` took the row-at-a-time
    version from 115 ms to 108 ms, because 67 ms of it was Python.
    """
    counts: dict[str, int] = {
        key: 0 for key, _label, _cls in issue_state.ISSUE_WORKLIST_SECTIONS
        if key not in ("done", "muted")
    }
    counts[RUNNER_BAND] = 0
    open_states = (issue_state.ISSUE_UNRESOLVED, issue_state.ISSUE_RESOLVING)
    for row in conn.execute(_BANDS_SQL, open_states):
        occurrence = [{
            column: row[column] for column in BUCKET_INPUT_COLUMNS
        }] if row["has_occurrence"] else []
        bucket = issue_state.issue_bucket(
            {"state": row["issue_state"]}, occurrence,
        )
        counts[bucket or RUNNER_BAND] += int(row["n"])
    return counts
