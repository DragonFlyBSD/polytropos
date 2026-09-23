"""Per-bundle spend for one port (poly-qqx9.9).

token_usage_for_port already answered "what has this port cost". What
nothing answered is which of the sibling attempts spent it, which is the
question the prior-attempts band poses and could not answer.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

pytest.importorskip("fastapi")

from dportsv3.db.schema import init_db
from dportsv3.tracker.agentic_queries import (
    token_usage_by_bundle,
    token_usage_for_port,
)

NOW = "2026-09-23T10:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "state.db"))
    c.row_factory = sqlite3.Row
    init_db(c)
    jobs = [
        ("j1", "b1", "devel/foo", "@2026Q3"),
        ("j2", "b2", "devel/foo", "@2026Q3"),
        ("j3", "b2", "devel/foo", "@2026Q3"),   # two jobs, one bundle
        ("j4", "b9", "devel/foo", "@2026Q2"),   # another target
        ("j5", "b8", "devel/bar", "@2026Q3"),   # another port
    ]
    for job_id, bundle_id, origin, target in jobs:
        c.execute(
            "INSERT INTO jobs (job_id, state, type, origin, flavor, "
            "bundle_dir, created_ts_utc, path, last_seen_at, target, "
            "bundle_id) VALUES (?,'done','patch',?,'','',?,'',?,?,?)",
            (job_id, origin, NOW, NOW, target, bundle_id))
    turns = [
        ("j1", 1000, 800, 200),
        ("j2", 2000, 500, 300),
        ("j3", 400, 0, 100),
        ("j4", 9999, 0, 9999),
        ("j5", 5555, 0, 5555),
    ]
    for job_id, prompt, cached, completion in turns:
        c.execute(
            "INSERT INTO activity_log (ts, job_id, stage, message, extra_json)"
            " VALUES (?,?, 'llm_turn', 'turn', ?)",
            (NOW, job_id, json.dumps({
                "prompt_tokens": prompt, "cached_tokens": cached,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            })))
    c.commit()
    return c


def test_spend_splits_by_bundle(conn):
    by_bundle = token_usage_by_bundle(conn, "devel/foo", "@2026Q3")
    assert set(by_bundle) == {"b1", "b2"}
    # billable is uncached prompt + completion, the same definition
    # token_usage_for_port uses, so a row and the total agree.
    assert by_bundle["b1"]["billable_tokens"] == (1000 - 800) + 200
    assert by_bundle["b1"]["llm_turns"] == 1


def test_two_jobs_on_one_bundle_add_up(conn):
    b2 = token_usage_by_bundle(conn, "devel/foo", "@2026Q3")["b2"]
    assert b2["jobs"] == 2
    assert b2["llm_turns"] == 2
    assert b2["billable_tokens"] == ((2000 - 500) + 300) + (400 + 100)


def test_the_rows_sum_to_the_port_total(conn):
    """The band's footer and its rows have to be the same quantity."""
    by_bundle = token_usage_by_bundle(conn, "devel/foo", "@2026Q3")
    port = token_usage_for_port(conn, "devel/foo", "@2026Q3")
    assert sum(b["billable_tokens"] for b in by_bundle.values()) == \
        port["billable_tokens"]
    assert sum(b["llm_turns"] for b in by_bundle.values()) == \
        port["llm_turns"]


def test_the_target_filter_excludes_other_branches(conn):
    assert "b9" not in token_usage_by_bundle(conn, "devel/foo", "@2026Q3")
    assert "b9" in token_usage_by_bundle(conn, "devel/foo")


def test_another_port_is_not_counted(conn):
    assert "b8" not in token_usage_by_bundle(conn, "devel/foo")


def test_a_port_with_no_turns_is_empty_not_zeroed(conn):
    assert token_usage_by_bundle(conn, "devel/nothing") == {}
