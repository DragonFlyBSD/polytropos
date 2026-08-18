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
| `tools/` | `mass_convert.py`, offline conversion tooling |
| `bin/` | the `dportsv3` bootstrap wrapper and the runner/artifact-store shims |
| `config/` | operator-edited config; empty by default, templates ship in the package |
| `docs/` | design docs and operator setup |
| `tests/` | the suite (~2085 tests) |
| `demo/` | seed + driver for a local tracker demo, no chroot needed |

The ports data itself — `ports/`, `special/`, the port Makefiles — stays in the
**DeltaPorts** repository. This tool reads that tree as an input.

## Status: extraction in progress

This repo was split out of DeltaPorts, which still holds a working copy of
everything here. **Nothing has been deleted there**, so DeltaPorts continues to
work exactly as before while this is finished.

**The tool runs standalone.** The suite is green — **2085 passed** — and a
plain wheel install, with no checkout anywhere above it, finds every input it
needs. Two things got it there:

**The two packages no longer import each other through `sys.path`.**
`dportsv3` declares `dports-dev-env` as a real dependency, and that edge points
one way only. The one import going the other way was the `dev-env health`
subcommand, a thin CLI shim over generator code; it moved to where its
implementation lives and is now `dportsv3 env-health NAME`.

**Nothing walks up out of the package any more.** Every input was found by
counting parent directories to a surrounding repository, which resolved only
while the tool sat inside the DeltaPorts checkout at one exact depth. Inputs
are now split by who owns them: tool-owned data (agent playbooks, the dops
quick reference, config templates, the dsynth hooks, the snippet extractor)
ships *inside* the package, and site-owned inputs (the live config directory,
the ports tree) are named explicitly. `dportsv3/paths.py` is the single
resolver, and it raises rather than shrugs — the worst of the old cases
returned `None` when it could not find the playbooks, so the agent would run
with its whole pattern library missing and never say so.

What remains before the cut-over is provisioning: the chroot mounts a
DeltaPorts checkout at `/work/DeltaPorts`, and it now needs both repositories.
`/work/DeltaPorts` itself must not move — it is the agent's only legal edit
surface, baked into both the LLM prompts and the worker's guardrails.

## Naming

The package directory is still `dportsv3`. The version-in-the-name is exactly what
this rename is fixing; renaming the package, the CLI entry point and the
`DPORTSV3_*` environment variables is a deliberate follow-up, because those env
vars are part of the operator-facing contract (dsynth hooks and service configs
set them).
