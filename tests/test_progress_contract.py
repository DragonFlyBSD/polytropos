"""What summary.json and the history chunks actually measure.

Half of dsynth-progress' payload describes a build farm this tracker does
not model. Those fields are still emitted -- the lifted progress.js writes
them into the DOM -- but a view that renders them as facts is lying. These
tests pin both halves: the measured fields carry real values, and the
placeholders stay placeholders so nobody "fixes" one into something
plausible.
"""

from __future__ import annotations

import sqlite3

import pytest

from dportsv3.tracker.db import (
    create_build_run,
    enqueue_ports,
    init_db,
    record_results,
    update_port_status,
)
from dportsv3.tracker.progress_adapter import run_history_chunk, run_summary

TARGET = "@main"


@pytest.fixture
def run() -> tuple[sqlite3.Connection, int]:
    conn = init_db(":memory:")
    run_id = create_build_run(conn, TARGET, "release", "2026-09-06T06:00:00Z")
    record_results(conn, run_id, TARGET, [
        {"origin": "devel/alpha", "version": "1.0", "result": "success",
         "recorded_at": "2026-09-06T06:10:00Z"},
        {"origin": "devel/beta", "version": "2.0", "result": "failure",
         "recorded_at": "2026-09-06T06:20:00Z"},
    ])
    enqueue_ports(conn, run_id, [
        {"origin": "www/gamma", "version": "3.0"},
        {"origin": "x11/delta", "version": "4.0"},
        {"origin": "lang/epsilon", "version": "5.0"},
    ], total_expected=5)
    update_port_status(conn, run_id, "x11/delta", "building")
    conn.commit()
    yield conn, run_id
    conn.close()


# --- what is measured ----------------------------------------------------


def test_a_history_entry_carries_when_it_was_recorded(run) -> None:
    """The chunk query selected recorded_at and then dropped it, so a
    "Recorded" column had no data to fill it."""
    conn, run_id = run

    entries = {e["origin"]: e for e in run_history_chunk(conn, run_id, 1)}

    assert entries["devel/alpha"]["recorded_at"] == "2026-09-06T06:10:00Z"
    assert entries["devel/beta"]["recorded_at"] == "2026-09-06T06:20:00Z"


# --- a flavour, in dsynth's spelling and nowhere else (poly-223m) --------


@pytest.fixture
def flavored() -> tuple[sqlite3.Connection, int]:
    conn = init_db(":memory:")
    run_id = create_build_run(conn, TARGET, "release", "2026-09-06T06:00:00Z")
    record_results(conn, run_id, TARGET, [
        {"origin": "devel/glib20", "flavor": "bootstrap", "version": "2.80",
         "result": "success", "recorded_at": "2026-09-06T06:10:00Z"},
        {"origin": "devel/glib20", "flavor": "default", "version": "2.80",
         "result": "failure", "recorded_at": "2026-09-06T06:11:00Z"},
        {"origin": "editors/vim", "version": "9.2", "result": "success",
         "recorded_at": "2026-09-06T06:12:00Z"},
    ])
    # A separate port for the in-flight row: a 'building' result is
    # excluded from the history chunk by design, so reusing glib20 here
    # would take one of the two flavours back out of the payload the
    # test above is about.
    enqueue_ports(conn, run_id, [
        {"origin": "x11/delta", "flavor": "gtk3", "version": "4.0"},
    ], total_expected=4)
    update_port_status(conn, run_id, "x11/delta", "building", "gtk3")
    conn.commit()
    yield conn, run_id
    conn.close()


def test_two_flavors_of_one_port_are_two_entries(flavored) -> None:
    """The reason the flavour is in this payload at all: keyed by origin
    alone they overwrite each other and one of the two builds vanishes."""
    conn, run_id = flavored

    origins = [e["origin"] for e in run_history_chunk(conn, run_id, 1)]

    assert "devel/glib20@bootstrap" in origins
    assert "devel/glib20@default" in origins


def test_an_unflavored_port_keeps_a_bare_origin(flavored) -> None:
    """No trailing @, and no "@default" invented for a port that has no
    flavour -- the lifted UI turns this string into a port link."""
    conn, run_id = flavored

    origins = [e["origin"] for e in run_history_chunk(conn, run_id, 1)]

    assert "editors/vim" in origins
    assert not any(o.endswith("@") for o in origins)


def test_the_flavor_is_not_also_its_own_field(flavored) -> None:
    """It was, briefly, and nothing read it: the lifted progress.js
    renders and searches `origin`. A second copy in a payload whose extra
    fields are documented one by one is how that documentation stops
    being true -- and the exact-shape assertion in
    test_progress_logfile_link is what noticed (poly-223m)."""
    conn, run_id = flavored

    entries = run_history_chunk(conn, run_id, 1)
    builders = run_summary(conn, run_id)["builders"]

    assert entries, "fixture produced no entries"
    assert all("flavor" not in e for e in entries)
    assert builders, "fixture produced no builders"
    assert all("flavor" not in b for b in builders)


def test_a_flavored_builder_row_names_the_flavor_it_is_building(
    flavored,
) -> None:
    conn, run_id = flavored

    builders = run_summary(conn, run_id)["builders"]

    assert [b["origin"] for b in builders] == ["x11/delta@gtk3"]


def test_a_builder_row_carries_the_version_it_was_queued_with(run) -> None:
    conn, run_id = run

    builders = run_summary(conn, run_id)["builders"]

    assert len(builders) == 1
    assert builders[0]["origin"] == "x11/delta"
    assert builders[0]["version"] == "4.0"


def test_a_building_row_has_no_start_time_anywhere(run) -> None:
    """Why builders[].elapsed cannot be computed rather than merely is not:
    enqueue_ports writes recorded_at='' and update_port_status only moves
    the status, so nothing records when a port started building."""
    conn, run_id = run

    row = conn.execute(
        "SELECT recorded_at FROM build_results "
        "WHERE build_run_id = ? AND status = 'building'",
        (run_id,),
    ).fetchone()

    assert row[0] == ""
    assert "recorded_at" not in run_summary(conn, run_id)["builders"][0]


def test_in_queue_and_in_progress_are_the_rows_in_those_states(run) -> None:
    """stats.queued is dsynth's word for the whole queue. A view that read
    it as "how many are waiting" would be wrong by the size of the build."""
    conn, run_id = run

    stats = run_summary(conn, run_id)["stats"]

    assert stats["queued"] == 5            # dsynth: the whole queue
    assert stats["in_queue"] == 2          # actually sitting in 'queued'
    assert stats["in_progress"] == 1       # actually building
    assert stats["built"] == 1
    assert stats["failed"] == 1


def test_the_runs_own_elapsed_is_a_real_clock(run) -> None:
    conn, run_id = run

    assert run_summary(conn, run_id)["stats"]["elapsed"] not in ("", None)


# --- what is not measured ------------------------------------------------


def test_the_farm_telemetry_stays_absent(run) -> None:
    conn, run_id = run

    stats = run_summary(conn, run_id)["stats"]

    assert stats["load"] == "  -"
    assert stats["swapinfo"] == "  -"
    assert stats["pkghour"] == 0
    assert stats["impulse"] == 0
    assert stats["meta"] == 0


def test_a_builder_slot_is_an_index_not_a_slot(run) -> None:
    """There is no per-builder-slot model, so phase, elapsed and lines have
    nothing behind them and ID is a position in a list."""
    conn, run_id = run

    builder = run_summary(conn, run_id)["builders"][0]

    assert builder["ID"] == "00"
    assert builder["phase"] == "build"
    assert builder["elapsed"] == " --:--:--"
    assert builder["lines"] == ""


def test_the_queued_rows_are_a_count_and_not_a_list(run) -> None:
    """A run starts with every origin queued. Shipping them in summary.json
    would put the whole tree in one response; the Builds dashboard pages
    them instead."""
    conn, run_id = run

    summary = run_summary(conn, run_id)

    assert "queued_rows" not in summary
    assert summary["stats"]["in_queue"] == 2
    # builders is the in-flight list, and it is bounded by what is building.
    assert len(summary["builders"]) == summary["stats"]["in_progress"]


def test_an_empty_run_keeps_every_key(run) -> None:
    """A view reading stats.in_queue must not blow up on a target that has
    never built."""
    from dportsv3.tracker.progress_adapter import target_summary

    conn, _ = run

    stats = target_summary(conn, "@2026Q3")["stats"]

    assert stats["in_queue"] == 0
    assert stats["in_progress"] == 0
