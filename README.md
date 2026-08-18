# Polytropos

> πολύτροπος — *"of many turns."* From **πολύ-** (many) + **τρόπος** (a turn, a way).
> The same root gives *μετατροπή*, conversion: literally "a turning-across."
> It is Homer's first word for Odysseus — the man of many turns, resourceful when
> the way is blocked.

Tooling for the DragonFly BSD ports overlay: it turns the FreeBSD Ports Collection
into DragonFly Ports, in several thousand different ways, and repairs the builds
that break along the way.

## What's here

| Path | What |
|---|---|
| `dportsv3/` | the Python package: DSL engine, compose pipeline, migration, build tracker, agentic repair loop, delivery |
| `dev-env/` | `dports_dev_env` — the DragonFly chroot build environment manager (a second distribution) |
| `hooks/dsynth/` | dsynth build hooks that report results into the tracker |
| `tools/` | `snippet-extractor`, `mass_convert.py` |
| `bin/` | the `dportsv3` bootstrap wrapper and the runner/artifact-store shims |
| `config/` | `*.sample` templates; local copies are gitignored |
| `docs/` | design docs, operator setup, and `agent-playbooks/` (read at runtime) |
| `tests/` | the suite (~2050 tests) |
| `demo/` | seed + driver for a local tracker demo, no chroot needed |

The ports data itself — `ports/`, `special/`, the port Makefiles — stays in the
**DeltaPorts** repository. This tool reads that tree as an input.

## Status: extraction in progress

This repo was split out of DeltaPorts, which still holds a working copy of
everything here. **Nothing has been deleted there**, so DeltaPorts continues to
work exactly as before while this is finished.

This repo does **not** run standalone yet. The code still discovers some of its
inputs by walking up out of its own directory, which worked only because it lived
inside the ports checkout. The suite is at **2051 passed, 11 failed** (from 53).

**Done — the two packages no longer import each other through `sys.path`.**
`dportsv3` now declares `dports-dev-env` as a real dependency, and that edge
points one way only. The one import going back the other way was the
`dev-env health` subcommand, a thin CLI shim over generator code; it moved to
where its implementation lives and is now `dportsv3 env-health NAME`. The
runtime-profile manifest both packages read moved inside `dports_dev_env/`, so
it ships in a wheel instead of being found by a guessed path.

**Remaining — 11 failures, all path discovery.** `config/`,
`hooks/dsynth/`, and `tools/snippet-extractor` are still resolved via
`Path(__file__).parents[N]`, which used to land in the DeltaPorts root and now
walks clean out of the repo. The most dangerous case is `playbooks.py`, which
locates `docs/agent-playbooks/` that way and returns `None` on failure — so the
agent would silently lose all of its pattern knowledge rather than error.

## Naming

The package directory is still `dportsv3`. The version-in-the-name is exactly what
this rename is fixing; renaming the package, the CLI entry point and the
`DPORTSV3_*` environment variables is a deliberate follow-up, because those env
vars are part of the operator-facing contract (dsynth hooks and service configs
set them).
