# The tracker UI: three views

The tracker serves three separate views. They are not three tabs onto one
dashboard — each answers a different question, and which one you want
depends on what you are doing.

| View | Route | Answers |
|---|---|---|
| **Builds** | `/` and `/builds` | What did the farm build, and what broke? |
| **Pipeline** | `/pipeline` | Where is work stuck between failing and fixed? |
| **Repairs** | `/agentic` | Which problems need a decision from me? |

`/` serves Builds, so an operator lands on the farm's own output.

Everything below is the short version. The UI explains its own model: the
**?** in the Repairs subnav opens a six-chapter guide that covers issues
versus occurrences versus jobs, how a failure is fingerprinted into an
issue, the worklist bands, the issue lifecycle, and every status an
occurrence can show with the actions it opens. It opens by itself the first
time you visit.

## Builds

One row per farm build run, newest first, with its success/failure/skipped
counts and how far through it is. Opening a run gives the per-port list,
live while the run is active.

This view is about *what dsynth did*. It has no opinion about whether
anything is being fixed.

## Pipeline

The machinery between a failure and a merged fix: triage, patch, verify,
delivery, confirm build. Work **branches** here — it is not one row
advancing through six linear states — so the view draws the flow, including
the retry loop back into triage.

Its secondary destinations describe the machinery rather than any one
problem:

- **Jobs** (`/agentic/jobs`) — every unit of runner work, with its state.
- **Deliveries** (`/pipeline/deliveries`) — accepted fixes on their way
  upstream, and the ones that fell out of the path.
- **Runner** (`/agentic/runner`) — the runner's own heartbeat, the active
  dev-env and environment health, and the operator pause.
- **Manual** (`/agentic/manual`) — escalations waiting for you to add
  context and hand them back to the agent.

## Repairs

The operator's queue, and the view you spend time in. A work queue on the
left, the selected occurrence's workspace on the right, both on screen at
once — picking something does not mean leaving the queue behind.

The stable unit is the **issue**: one fingerprinted problem. The
**occurrences** under it are individual build failures of that problem.
Something has already tried to fix each one before you see it, so the
question on screen is usually "is this fix good" rather than "what broke".

The queue filters by port, issue key or fingerprint, and its band chips
narrow it rather than jumping to it. Its secondary destinations:

- **Worklist** (`/agentic`) — the queue itself.
- **Issues** (`/agentic/issues`) — every fingerprinted problem, searchable,
  including the ones the queue's cap drops.
- **Occurrences** (`/agentic/bundles`) — every individual failure.

Two occurrences of one issue can be compared at
`/agentic/compare?a=<bundle>&b=<bundle>`, reachable from the occurrence
selector: it says which artifacts changed between two attempts and diffs
the ones that did.

## Repairing legacy identifiers

Identifiers written before the current guard can contain a path separator,
which no page can render as a link — one bad row takes down the Builds
dashboard and every page that mentions it. The server names them at startup
when it finds them. The remedy:

```sh
dportsv3 tracker repair-identifiers            # report only, writes nothing
dportsv3 tracker repair-identifiers --apply    # make the changes
```

Run the reporting form first. Rewriting an id means rewriting every
reference to it, and a rewrite that goes wrong is worse than the error it
prevents.

## Settings

Three settings tune what the UI recomputes. All have defaults that are
right for a single-host install, so no install needs to set them; see
`dportsv3 config show` for the current value and where it came from.

| Setting | Default | What it costs |
|---|---|---|
| `tracker.shell_facts_seconds` | 15 | How long the command bar's Queue / Needs you / Runner figures may be reused. They sit in the shell, so every page pays for them — 25.8 ms per render on 20,000 issues. Set 0 to recompute every time. |
| `tracker.preflight_refresh_seconds` | 300 | How often the Pipeline health strip re-runs the delivery preflight. The clone-state check shells out to `git status`, which is tens of milliseconds on a ports tree. The reading is stamped, so a longer interval makes the strip older, never wrong. |
| `tracker.runner_heartbeat_stale_seconds` | 60 | How long `runner_status.updated_at` may go unrefreshed before the UI calls the runner not running. The heartbeat thread touches it every 5 seconds. |

## What this page is not

It does not describe the repair loop itself — what triage does, what the
patch agent is allowed to edit, how a fix is verified. That is
`docs/agentic-operator-loop.md` and `docs/AGENTIC_BUILDS.md`, which describe
the loop rather than the screens.
