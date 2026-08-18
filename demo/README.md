# Local demo — build-confirmed resolution

Shows the C1/C2/A1–A4 loop: an issue becomes `resolved` because a confirm
**build** went green, not because a PR merged.

## What is real vs faked

**Real** (production code paths, unmodified):
- `process_build_requests` — the C2 level-triggered reconcile + single-flight
- `issues_needing_build` — the feed predicate
- `_record_confirm_verdict` — generation bookkeeping, stale-verdict guard,
  green/red transitions, A4 threshold, Green Head watermark
- `issue_actions.resolve_issue_build_confirmed` / `reopen_issue_build_failed`
- the tracker UI, reading a real `state.db` on the real schema

**Faked** — only one thing: **the build itself**. A confirm build shells into
a DragonFly dev-env chroot (`run_verify_fix` → `dev-env apply-and-build` →
dsynth), which cannot run on macOS/Linux. The driver hands the verdict logic a
green/red result directly instead of executing dsynth.

Nothing here touches a real port tree, a real chroot, or the network.

## Setup

```sh
cd scripts/generator
DEMO=/tmp/dports-demo
.venv/bin/python demo/seed_demo.py --db $DEMO/state.db
```

Seeds 8 issues, one per interesting state: ready-for-operator, confirm build
queued, confirm build in flight, provisional green 1/2, resolved with a Green
Head, reopened by a red confirm, muted, and regressed.

## Browse the UI

```sh
.venv/bin/python -m dportsv3 tracker serve --db $DEMO/state.db --port 8899
```

- <http://127.0.0.1:8899/agentic/issues> — the issue worklist
- <http://127.0.0.1:8899/agentic/issues/iss-queued> — one issue's detail
- <http://127.0.0.1:8899/agentic> — agentic dashboard

Caveat: the UI does **not** yet surface the confirm-build columns (generation,
in-flight marker, provisional green count, Green Head). That gap is tracked as
`DeltaPorts-chf`. Use the driver below to see those.

## Drive the loop

```sh
# what the reconcile loop currently sees
.venv/bin/python demo/drive_demo.py --db $DEMO/state.db status

# run the real C2 loop: enqueues confirm jobs, then proves single-flight
.venv/bin/python demo/drive_demo.py --db $DEMO/state.db reconcile

# happy path: two consecutive greens -> resolved + Green Head
.venv/bin/python demo/drive_demo.py --db $DEMO/state.db scenario-green

# failure path: red -> reopened, delivery pointer cleared, handoff for a human
.venv/bin/python demo/drive_demo.py --db $DEMO/state.db scenario-red

# or drive one verdict by hand
.venv/bin/python demo/drive_demo.py --db $DEMO/state.db verdict iss-queued green
```

Re-run `seed_demo.py` any time to reset (it recreates the DB).

## What to look for

| Column | Meaning |
|---|---|
| `req` | `requested_build_generation` — desired build state (bumped on accept) |
| `conf` | `last_confirmed_build_generation` — what has actually been built |
| `bld` | `building_generation` — single-flight marker; set while a build is in flight |
| `grn` | consecutive green count (A4; resolves at 2 by default) |
| `head` | Green Head watermark — the known-good boundary for regression detection |

`req > conf` and no `bld` ⇒ the reconcile loop will enqueue a confirm build.
