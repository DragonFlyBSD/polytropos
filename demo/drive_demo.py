#!/usr/bin/env python3
"""Drive the build-confirmed resolution loop against the demo DB.

Runs the REAL production code — `process_build_requests` (the C2
level-triggered reconcile) and `_record_confirm_verdict` (the A1/A2/A3/A4
verdict + transition logic). The ONLY thing faked is the build itself: the
actual confirm build shells into a DragonFly dev-env chroot via
`run_verify_fix` -> `dev-env apply-and-build` -> dsynth, which cannot run on
this host. So instead of executing dsynth we hand the verdict logic a
green/red result directly.

What is real here: the feed predicate, the single-flight marker, the
generation bookkeeping, the stale-verdict guard, the state transitions, the
green-count threshold, the Green Head watermark, the event log.

    python demo/drive_demo.py --db /tmp/demo/state.db status
    python demo/drive_demo.py --db /tmp/demo/state.db reconcile
    python demo/drive_demo.py --db /tmp/demo/state.db verdict iss-queued green
    python demo/drive_demo.py --db /tmp/demo/state.db scenario-green
    python demo/drive_demo.py --db /tmp/demo/state.db scenario-red
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dportsv3.agent import runner  # noqa: E402
from dportsv3.tracker.agentic_queries import issues_needing_build  # noqa: E402

COLS = ("issue_key", "origin", "state", "requested_build_generation",
        "last_confirmed_build_generation", "building_generation",
        "confirm_green_count", "green_head_run_id")


def attach(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    runner._state_db_conn = conn
    runner._state_db_lock = threading.Lock()
    return conn


def show(conn: sqlite3.Connection, title: str = "") -> None:
    if title:
        print(f"\n=== {title} ===")
    print(f"{'issue':17} {'origin':18} {'state':11} {'req':>3} {'conf':>4} "
          f"{'bld':>3} {'grn':>3} {'head':>4}")
    print("-" * 74)
    for r in conn.execute(
        f"SELECT {','.join(COLS)} FROM issues ORDER BY issue_key"
    ):
        print(f"{r['issue_key']:17} {r['origin']:18} {r['state']:11} "
              f"{r['requested_build_generation']:>3} "
              f"{r['last_confirmed_build_generation']:>4} "
              f"{str(r['building_generation'] or '-'):>3} "
              f"{r['confirm_green_count']:>3} "
              f"{str(r['green_head_run_id'] or '-'):>4}")


def cmd_status(conn, args) -> int:
    show(conn, "issues")
    feed = issues_needing_build(conn)
    print("\nC2 feed (issues needing a confirm build right now):")
    if not feed:
        print("  (none)")
    for i in feed:
        print(f"  {i['issue_key']:17} gen "
              f"{i['requested_build_generation']} > "
              f"{i['last_confirmed_build_generation']}  "
              f"[{i['origin']}]")
    return 0


def cmd_reconcile(conn, args) -> int:
    """Run the real C2 loop against a throwaway queue dir."""
    qroot = Path(tempfile.mkdtemp(prefix="demo-queue-"))
    (qroot / "pending").mkdir()
    print(f"queue: {qroot}")
    print("\n-- running process_build_requests (real C2 code) --")
    runner.process_build_requests(qroot)
    jobs = sorted(p.name for p in (qroot / "pending").glob("*confirm.job"))
    print(f"\nenqueued {len(jobs)} confirm job(s):")
    for j in jobs:
        print(f"  {j}")
        for line in (qroot / "pending" / j).read_text().splitlines():
            print(f"      {line}")
    print("\n-- second pass (single-flight: should enqueue nothing new) --")
    runner.process_build_requests(qroot)
    jobs2 = sorted(p.name for p in (qroot / "pending").glob("*confirm.job"))
    print(f"total jobs after 2nd pass: {len(jobs2)} "
          f"({'no duplicates' if len(jobs2) == len(jobs) else 'DUPLICATED!'})")
    show(conn, "after reconcile (building_generation now marks in-flight)")
    return 0


def cmd_verdict(conn, args) -> int:
    """Feed a green/red verdict through the real A1-A4 logic."""
    row = conn.execute(
        "SELECT requested_build_generation q, building_generation b, target "
        "FROM issues WHERE issue_key = ?", (args.issue,)
    ).fetchone()
    if row is None:
        print(f"no such issue: {args.issue}")
        return 1
    gen = row["b"] or row["q"]
    ok = args.verdict == "green"
    print(f"delivering {'GREEN' if ok else 'RED'} verdict for {args.issue} "
          f"at generation {gen}")
    out = runner._record_confirm_verdict(
        Path("/tmp"), args.issue, gen, ok=ok, requested_by="demo",
        target=row["target"] or "",
        verdict_detail="" if ok else "dsynth stage failed (dsynth_exit=1)",
    )
    print(f"  -> _record_confirm_verdict returned: {out!r}")
    show(conn, "after verdict")
    print("\nrecent events:")
    for e in conn.execute(
        "SELECT type, data_json FROM events ORDER BY id DESC LIMIT 4"
    ):
        print(f"  {e['type']:22} {e['data_json']}")
    return 0


def _scenario(conn, issue: str, verdicts: list[str]) -> None:
    row = conn.execute("SELECT target FROM issues WHERE issue_key=?",
                       (issue,)).fetchone()
    target = row["target"] if row else ""
    for n, v in enumerate(verdicts, start=1):
        r = conn.execute(
            "SELECT requested_build_generation q, building_generation b "
            "FROM issues WHERE issue_key=?", (issue,)).fetchone()
        gen = r["b"] or r["q"]
        print(f"\n--- build #{n}: {v.upper()} (generation {gen}) ---")
        out = runner._record_confirm_verdict(
            Path("/tmp"), issue, gen, ok=(v == "green"), requested_by="demo",
            target=target,
            verdict_detail="" if v == "green" else "dsynth failed (exit 1)")
        s = conn.execute(
            "SELECT state, confirm_green_count g, requested_build_generation q,"
            " last_confirmed_build_generation c, green_head_run_id h "
            "FROM issues WHERE issue_key=?", (issue,)).fetchone()
        print(f"  returned={out!r}  state={s['state']}  greens={s['g']}  "
              f"req={s['q']} conf={s['c']}  green_head={s['h'] or '-'}")
        if s["state"] == "resolving" and s["q"] > s["c"]:
            print("  -> still provisional: another INDEPENDENT build requested")
            conn.execute("UPDATE issues SET building_generation=? "
                         "WHERE issue_key=?", (s["q"], issue))
            conn.commit()


def cmd_scenario_green(conn, args) -> int:
    print("SCENARIO: accepted fix confirmed by two consecutive green builds")
    print("(A4 default threshold = 2, so the first green is provisional)")
    _scenario(conn, "iss-queued", ["green", "green"])
    show(conn, "final")
    print("\n=> iss-queued is now `resolved` because it BUILT, "
          "not because a PR merged.")
    return 0


def cmd_scenario_red(conn, args) -> int:
    print("SCENARIO: accepted fix fails its confirm build")
    _scenario(conn, "iss-building", ["red"])
    show(conn, "final")
    print("\n=> iss-building went back to `unresolved`, delivery pointer "
          "cleared,\n   and the runner would write analysis/manual_handoff.md "
          "for a human.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    v = sub.add_parser("verdict")
    v.add_argument("issue")
    v.add_argument("verdict", choices=["green", "red"])
    sub.add_parser("scenario-green")
    sub.add_parser("scenario-red")
    args = ap.parse_args()

    conn = attach(args.db)
    fn = {
        "status": cmd_status, "reconcile": cmd_reconcile,
        "verdict": cmd_verdict, "scenario-green": cmd_scenario_green,
        "scenario-red": cmd_scenario_red,
    }[args.cmd]
    try:
        return fn(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
