"""The paged, filterable read of one build run's origin results.

get_build_results returns every row a run recorded — 13,440 for the run this
was measured against — with no filter, no page and no link to the evidence a
failure produced. get_build_results_page is the read the Builds table needs
instead, and these tests pin the three things that make it correct: the
filter vocabulary, the wildcard escaping, and the fact that it does not sort
the whole run to return fifty rows.
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

import pytest

from dportsv3.tracker.db import (
    BUILD_RESULT_STATES,
    create_build_run,
    enqueue_ports,
    get_build_results_page,
    init_db,
    record_results,
    update_port_status,
)

TARGET = "@main"
_ORIGINS = [
    ("devel/alpha", "success"),
    ("devel/beta", "failure"),
    ("graphics/gamma", "skipped"),
    ("lang/delta", "ignored"),
    ("net/epsilon", "failure"),
    ("www/zeta_one", "success"),
    ("www/100%pure", "success"),
]


class Build(NamedTuple):
    """One run and the connection it lives in. sqlite3.Connection takes no
    attributes, so the run id travels beside it rather than on it."""

    conn: sqlite3.Connection
    id: int


@pytest.fixture
def build() -> Build:
    connection = init_db(":memory:")
    run_id = create_build_run(connection, TARGET, "release", "2026-09-01T08:00:00Z")
    record_results(
        connection,
        run_id,
        TARGET,
        [
            {
                "origin": origin,
                "version": "1.0",
                "result": result,
                "recorded_at": "2026-09-01T08:10:00Z",
            }
            for origin, result in _ORIGINS
        ],
    )
    # Two in-flight rows: enqueue writes result='' status='queued', and the
    # runner promotes one of them to 'building'.
    enqueue_ports(
        connection,
        run_id,
        [
            {"origin": "sysutils/queued", "version": "2.0"},
            {"origin": "sysutils/inflight", "version": "2.0"},
        ],
    )
    update_port_status(connection, run_id, "sysutils/inflight", "building")
    yield Build(connection, run_id)
    connection.close()


# --- paging --------------------------------------------------------------


def test_total_counts_the_match_not_the_page(build: Build) -> None:
    page = get_build_results_page(build.conn, build.id, limit=3)

    assert page["total"] == 9
    assert len(page["results"]) == 3
    assert page["limit"] == 3
    assert page["offset"] == 0


def test_pages_partition_the_run_in_origin_order(build: Build) -> None:
    seen: list[str] = []
    for offset in (0, 4, 8):
        page = get_build_results_page(build.conn, build.id, limit=4, offset=offset)
        seen.extend(str(row["origin"]) for row in page["results"])

    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 9


def test_an_offset_past_the_end_is_an_empty_page_not_an_error(
    build: Build,
) -> None:
    page = get_build_results_page(build.conn, build.id, offset=500)

    assert page["results"] == []
    assert page["total"] == 9


def test_limit_is_clamped_to_a_page_a_browser_can_render(
    build: Build,
) -> None:
    assert get_build_results_page(build.conn, build.id, limit=10_000)["limit"] == 500
    assert get_build_results_page(build.conn, build.id, limit=0)["limit"] == 1


# --- the state filter ----------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("success", ["devel/alpha", "www/100%pure", "www/zeta_one"]),
        ("failure", ["devel/beta", "net/epsilon"]),
        ("skipped", ["graphics/gamma"]),
        ("ignored", ["lang/delta"]),
        ("queued", ["sysutils/queued"]),
        ("building", ["sysutils/inflight"]),
    ],
)
def test_every_state_selects_its_own_rows(
    build: Build, state: str, expected: list[str]
) -> None:
    page = get_build_results_page(build.conn, build.id, state=state)

    assert [str(row["origin"]) for row in page["results"]] == expected
    assert page["total"] == len(expected)


def test_the_state_vocabulary_is_result_and_status_together(
    build: Build,
) -> None:
    """One axis, not two: an in-flight row has no result, a finished row's
    status is a copy of its result."""
    covered = {
        state: get_build_results_page(build.conn, build.id, state=state)["total"]
        for state in BUILD_RESULT_STATES
    }

    assert sum(covered.values()) == get_build_results_page(build.conn, build.id)["total"]


def test_a_queued_row_is_not_reported_as_a_result(build: Build) -> None:
    queued = get_build_results_page(build.conn, build.id, state="queued")["results"][0]

    assert queued["result"] == ""
    for state in ("success", "failure", "skipped", "ignored"):
        origins = [
            row["origin"]
            for row in get_build_results_page(build.conn, build.id, state=state)["results"]
        ]
        assert "sysutils/queued" not in origins


def test_an_unknown_state_is_rejected_rather_than_returning_nothing(
    build: Build,
) -> None:
    with pytest.raises(ValueError, match="Invalid build result state: recorded"):
        get_build_results_page(build.conn, build.id, state="recorded")


def test_an_unknown_run_is_a_missing_run_not_an_empty_one(
    build: Build,
) -> None:
    with pytest.raises(ValueError, match="Unknown build run: 4242"):
        get_build_results_page(build.conn, 4242)


# --- search --------------------------------------------------------------


def test_search_matches_a_substring_of_the_origin(build: Build) -> None:
    page = get_build_results_page(build.conn, build.id, search="devel/")

    assert [str(row["origin"]) for row in page["results"]] == [
        "devel/alpha",
        "devel/beta",
    ]


def test_search_ignores_case(build: Build) -> None:
    assert get_build_results_page(build.conn, build.id, search="GAMMA")["total"] == 1


def test_search_combines_with_the_state_filter(build: Build) -> None:
    page = get_build_results_page(build.conn, build.id, state="failure", search="devel")

    assert [str(row["origin"]) for row in page["results"]] == ["devel/beta"]


def test_an_underscore_is_an_underscore_not_a_wildcard(
    build: Build,
) -> None:
    """Unescaped, LIKE '%_%' matches every origin in the run."""
    page = get_build_results_page(build.conn, build.id, search="_")

    assert [str(row["origin"]) for row in page["results"]] == ["www/zeta_one"]


def test_a_percent_is_a_percent_not_a_wildcard(build: Build) -> None:
    page = get_build_results_page(build.conn, build.id, search="%")

    assert [str(row["origin"]) for row in page["results"]] == ["www/100%pure"]


def test_an_empty_search_is_no_search(build: Build) -> None:
    assert get_build_results_page(build.conn, build.id, search="")["total"] == 9


# --- the evidence link ---------------------------------------------------


def _add_bundle(
    conn: sqlite3.Connection,
    bundle_id: str,
    origin: str,
    *,
    build_run_id: int | None,
    ts: str,
    run_key: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO runs(run_id, target, build_run_id) VALUES (?, ?, ?)",
        (run_key, TARGET, build_run_id),
    )
    conn.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, target, ts_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        (bundle_id, run_key, origin, TARGET, ts),
    )
    conn.commit()


def test_a_failure_carries_the_bundle_it_produced(build: Build) -> None:
    _add_bundle(
        build.conn, "b-1", "devel/beta",
        build_run_id=build.id, ts="2026-09-01T08:11:00Z", run_key="r-1",
    )

    page = get_build_results_page(build.conn, build.id, state="failure")

    by_origin = {str(row["origin"]): row["bundle_id"] for row in page["results"]}
    assert by_origin["devel/beta"] == "b-1"
    assert by_origin["net/epsilon"] is None


def test_a_success_carries_no_bundle(build: Build) -> None:
    page = get_build_results_page(build.conn, build.id, state="success")

    assert {row["bundle_id"] for row in page["results"]} == {None}


def test_the_newest_occurrence_in_this_run_is_the_one_linked(
    build: Build,
) -> None:
    _add_bundle(
        build.conn, "b-old", "devel/beta",
        build_run_id=build.id, ts="2026-09-01T08:11:00Z", run_key="r-1",
    )
    _add_bundle(
        build.conn, "b-new", "devel/beta",
        build_run_id=build.id, ts="2026-09-01T09:30:00Z", run_key="r-2",
    )

    page = get_build_results_page(build.conn, build.id, search="devel/beta")

    assert page["results"][0]["bundle_id"] == "b-new"


def test_a_bundle_from_another_build_run_is_not_attributed_to_this_one(
    build: Build,
) -> None:
    """The cross-link is per run: the same origin fails in many builds, and
    the Builds table must show the evidence from the build it is showing."""
    other = create_build_run(
        build.conn, "@2026Q3", "release", "2026-09-02T08:00:00Z"
    )
    _add_bundle(
        build.conn, "b-elsewhere", "devel/beta",
        build_run_id=other, ts="2026-09-02T08:11:00Z", run_key="r-other",
    )
    _add_bundle(
        build.conn, "b-orphan", "net/epsilon",
        build_run_id=None, ts="2026-09-01T08:11:00Z", run_key="r-orphan",
    )

    page = get_build_results_page(build.conn, build.id, state="failure")

    assert {row["bundle_id"] for row in page["results"]} == {None}


# --- the plan the paging depends on --------------------------------------


def _plan(build: Build, **kwargs) -> list[str]:
    """The query plan of the page SELECT, as the call itself ran it.

    sqlite3.Connection.execute is read-only, so the statement is captured
    from the trace callback (which hands back the SQL with its parameters
    already bound) and re-explained, rather than re-typed here where it
    could drift from the query it claims to describe.
    """
    conn = build.conn
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        get_build_results_page(conn, build.id, **kwargs)
    finally:
        conn.set_trace_callback(None)

    page_sql = [sql for sql in statements if "ORDER BY br.origin" in sql]
    assert len(page_sql) == 1, statements
    return [str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + page_sql[0])]


def test_a_page_does_not_sort_the_whole_run(build: Build) -> None:
    """build_results is keyed (build_run_id, origin), so ORDER BY origin
    rides the primary key. Ordering by anything else — get_build_results'
    CASE over status — costs a temp b-tree over every row the run recorded
    before LIMIT can apply: measured 5.94 ms against 0.26 ms at offset 13000
    on a 13,440-row run.

    One temp b-tree is expected and belongs to the correlated subselect
    picking the newest bundle; the outer ORDER BY must add none.
    """
    plan = _plan(build, limit=50)

    assert any("sqlite_autoindex_build_results_1" in line for line in plan), plan
    assert sum("TEMP B-TREE" in line for line in plan) == 1, plan


def test_the_bundle_lookup_reaches_runs_by_key_not_by_scan(
    build: Build,
) -> None:
    """poly-0e02.3 said to index runs(build_run_id) first. Measured, the
    subselect drives from bundles on origin and reaches runs by its primary
    key, so the index would never be used."""
    plan = _plan(build, limit=50)

    assert any("SEARCH r USING INDEX sqlite_autoindex_runs_1" in x for x in plan), plan
    assert not any("SCAN r" in line for line in plan), plan
