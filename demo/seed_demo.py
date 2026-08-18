#!/usr/bin/env python3
"""Seed a demo state.db showing the build-confirmed resolution loop.

Creates one issue per interesting state so the tracker UI and the CLI driver
have something real to render. Everything is synthetic — no dsynth, no chroot,
no network.

    python demo/seed_demo.py --db /tmp/demo/state.db

Then serve it:

    dportsv3 tracker serve --db /tmp/demo/state.db --port 8080
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dportsv3.db.schema import init_db  # noqa: E402

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def ago(**kw) -> str:
    return iso(NOW - timedelta(**kw))


TARGET = "@main"


def _issue(conn, key, origin, state, *, times_seen=1, first, last,
           delivery_bundle_id=None, resolved_at=None, regressed_at=None,
           muted_at=None, muted_by=None, reopened_at=None, reopened_by=None,
           requested=0, confirmed=0, building=None, green_head=None,
           greens=0, latest_bundle=None):
    conn.execute(
        "INSERT INTO issues(issue_key, target, origin, fingerprint, state, "
        "times_seen, first_seen_at, last_seen_at, latest_bundle_id, "
        "delivery_bundle_id, resolved_at, regressed_at, muted_at, muted_by, "
        "reopened_at, reopened_by, requested_build_generation, "
        "last_confirmed_build_generation, building_generation, "
        "green_head_run_id, confirm_green_count, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, TARGET, origin, f"fp{key}", state, times_seen, first, last,
         latest_bundle, delivery_bundle_id, resolved_at, regressed_at,
         muted_at, muted_by, reopened_at, reopened_by, requested, confirmed,
         building, green_head, greens, last),
    )


def _bundle(conn, bundle_id, origin, issue_key, *, ts, result="failure",
            resolution=None, verification_status=None, accepted_at=None):
    conn.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, flavor, ts_utc, "
        "result, path, last_seen_at, target, issue_key, error_signature, "
        "resolution, verification_status, accepted_at, accepted_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bundle_id, "demo-run", origin, "", ts, result, f"/bundles/{bundle_id}",
         ts, TARGET, issue_key, f"sig-{issue_key}", resolution,
         verification_status, accepted_at, "operator" if accepted_at else None),
    )


def seed(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # A little farm-build history so Green Head has real ordinals to point at.
    for i, (bt, when) in enumerate(
        [("test", ago(days=3)), ("release", ago(days=2)),
         ("test", ago(days=1)), ("test", ago(hours=4))], start=1
    ):
        conn.execute(
            "INSERT INTO build_runs(id, target, build_type, started_at, "
            "finished_at, total_expected) VALUES(?,?,?,?,?,?)",
            (i, TARGET, bt, when, when, 120),
        )

    # ---------------------------------------------------------------
    # 1. UNRESOLVED — agent produced a fix, verified, waiting on operator
    # ---------------------------------------------------------------
    _issue(conn, "iss-ready", "devel/libfoo", "unresolved",
           times_seen=2, first=ago(days=4), last=ago(hours=6),
           latest_bundle="bnd-ready")
    _bundle(conn, "bnd-ready", "devel/libfoo", "iss-ready", ts=ago(hours=6),
            resolution="agent_fixed", verification_status="verified")

    # ---------------------------------------------------------------
    # 2. RESOLVING — accepted, confirm build REQUESTED but not started
    #    (requested 1 > confirmed 0, nothing in flight → C2 will pick it up)
    # ---------------------------------------------------------------
    _issue(conn, "iss-queued", "graphics/libbar", "resolving",
           times_seen=1, first=ago(days=2), last=ago(hours=8),
           delivery_bundle_id="bnd-queued", latest_bundle="bnd-queued",
           requested=1, confirmed=0)
    _bundle(conn, "bnd-queued", "graphics/libbar", "iss-queued",
            ts=ago(hours=8), resolution="accepted",
            verification_status="verified", accepted_at=ago(hours=2))

    # ---------------------------------------------------------------
    # 3. RESOLVING — confirm build IN FLIGHT (single-flight marker set)
    # ---------------------------------------------------------------
    _issue(conn, "iss-building", "net/libbaz", "resolving",
           times_seen=3, first=ago(days=6), last=ago(hours=12),
           delivery_bundle_id="bnd-building", latest_bundle="bnd-building",
           requested=1, confirmed=0, building=1)
    _bundle(conn, "bnd-building", "net/libbaz", "iss-building",
            ts=ago(hours=12), resolution="accepted",
            verification_status="verified", accepted_at=ago(hours=1))

    # ---------------------------------------------------------------
    # 4. RESOLVING — PROVISIONAL green 1/2 (A4): first green landed, a
    #    second independent build was requested (requested 2 > confirmed 1)
    # ---------------------------------------------------------------
    _issue(conn, "iss-provisional", "lang/libqux", "resolving",
           times_seen=1, first=ago(days=1), last=ago(hours=20),
           delivery_bundle_id="bnd-provisional",
           latest_bundle="bnd-provisional",
           requested=2, confirmed=1, greens=1)
    _bundle(conn, "bnd-provisional", "lang/libqux", "iss-provisional",
            ts=ago(hours=20), resolution="accepted",
            verification_status="verified", accepted_at=ago(minutes=40))

    # ---------------------------------------------------------------
    # 5. RESOLVED — two greens confirmed it, Green Head watermark recorded
    # ---------------------------------------------------------------
    _issue(conn, "iss-resolved", "devel/libdone", "resolved",
           times_seen=2, first=ago(days=8), last=ago(days=1),
           delivery_bundle_id="bnd-resolved", latest_bundle="bnd-resolved",
           resolved_at=ago(minutes=30), requested=2, confirmed=2,
           green_head=4, greens=0)
    _bundle(conn, "bnd-resolved", "devel/libdone", "iss-resolved",
            ts=ago(days=1), resolution="accepted",
            verification_status="verified", accepted_at=ago(hours=3))

    # ---------------------------------------------------------------
    # 6. REOPENED BY A RED CONFIRM (A3) — the accepted fix did not hold
    # ---------------------------------------------------------------
    _issue(conn, "iss-redconfirm", "x11/libbroken", "unresolved",
           times_seen=4, first=ago(days=10), last=ago(days=2),
           latest_bundle="bnd-redconfirm",
           reopened_at=ago(minutes=15), reopened_by="runner-reconcile",
           requested=1, confirmed=1)
    _bundle(conn, "bnd-redconfirm", "x11/libbroken", "iss-redconfirm",
            ts=ago(days=2), resolution="accepted",
            verification_status="verified", accepted_at=ago(hours=5))

    # ---------------------------------------------------------------
    # 7. MUTED — operator silenced it (confirm work should skip it)
    # ---------------------------------------------------------------
    _issue(conn, "iss-muted", "misc/libnoise", "muted",
           times_seen=7, first=ago(days=20), last=ago(days=1),
           latest_bundle="bnd-muted", muted_at=ago(days=1),
           muted_by="operator")
    _bundle(conn, "bnd-muted", "misc/libnoise", "iss-muted", ts=ago(days=1),
            resolution="agent_gave_up")

    # ---------------------------------------------------------------
    # 8. REGRESSED — was resolved, came back
    # ---------------------------------------------------------------
    _issue(conn, "iss-regressed", "audio/libback", "regressed",
           times_seen=5, first=ago(days=30), last=ago(hours=3),
           latest_bundle="bnd-regressed", resolved_at=ago(days=5),
           regressed_at=ago(hours=3))
    _bundle(conn, "bnd-regressed", "audio/libback", "iss-regressed",
            ts=ago(hours=3), resolution=None)

    conn.commit()

    n = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    b = conn.execute("SELECT COUNT(*) FROM bundles").fetchone()[0]
    conn.close()
    print(f"seeded {db_path}: {n} issues, {b} occurrences, 4 build runs")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True)
    args = ap.parse_args()
    seed(args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
