---
name: deploy
description: Deploy this checkout's current master to a polytropos builder host — pull, deploy install, refresh the dsynth hooks, restart the tracker and runner, verify. Asks which target, then hands the work to the deploy-builder agent and relays its table.
---

# Deploy to a builder

Delegate the whole run to the `deploy-builder` agent. It is `model: sonnet`
and holds root on the build host; the reason it exists is that this task is
mechanical and should not cost an Opus context, and that its instructions
should stop being re-derived from memory every time.

## 1. Ask for the target

The runbook is tool-owned and committed. The target is site-owned and is
**never** written into this file or the agent's — no hostnames, no IPs, no
usernames, no home paths in anything committed here.

Ask with `AskUserQuestion`, in one call:

- **Target** — the ssh destination. If a target has already been used in this
  conversation, offer it as the first option, labelled with the destination
  and marked `(Recommended)`. The tool's automatic "Other" option is how the
  user types one that is not listed.
- **Checkout** — the absolute path to the polytropos checkout on that host.
  Offer any path already seen this conversation; otherwise rely on "Other".

Ask once. Do not ask again for values the user has already given you in this
conversation — re-asking is the tax this skill exists to remove.

## 2. Work out what the deploy should prove

Before delegating, look at what is actually shipping:

```sh
git log --oneline origin/master..master   # unpushed — the user must push first
git log --oneline -3
```

If anything is unpushed, say so and stop: `deploy install` installs from the
checkout on the *builder*, which pulls from the remote. Deploying without
pushing silently installs the old code.

Derive two things from the commits that are shipping:

- the **expected commit** — the short SHA that should land
- **import assertions** — one-line Python expressions, evaluated against the
  installed venv, that are true only if this change is live. Prefer
  `hasattr`, `inspect.signature(...).parameters`, and substring checks on
  `inspect.getsource(...)`. Generic "it imports" is not an assertion.

## 3. Delegate

Call the `Agent` tool with `subagent_type: "deploy-builder"`, passing the ssh
destination, the checkout path, the expected commit, and the import
assertions. Pass nothing else — the agent discovers the environments itself.

## 4. Relay

The agent's report is not shown to the user. Relay its table verbatim, plus
the PID transitions. Do not soften a halt into a summary: if it stopped, lead
with the phase it stopped at and what it did not do.

## The one trap that outlives the deploy

If a deploy ever stops the runner while `dsynth` is live, the `build_runs`
row stays open, and its partial unique index makes every later build
invisible to the tracker and fire no hooks. The agent halts rather than
letting this happen. If it reports that halt, the fix is
`dportsv3 tracker finish-build --run <id> --server <tracker> --finished-at <ts>`
and it is the user's call, not the agent's.
