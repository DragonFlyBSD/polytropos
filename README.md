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

This repo does **not** run standalone yet. The code still discovers its inputs by
walking up out of its own directory, which worked only because it lived inside the
ports checkout. Running the suite from here gives **2006 passed, 53 failed**, from
exactly two causes:

1. **37** — `ModuleNotFoundError: dports_dev_env`. The generator and the dev-env
   import each other by mutating `sys.path`, assuming they are siblings under one
   `scripts/` directory. Neither declares the other as a dependency.
2. **16** — missing files: `config/`, `hooks/dsynth/`, `tools/snippet-extractor`
   are resolved via `Path(__file__).parents[N]`, which used to land in the
   DeltaPorts root.

Both are tracked, and fixing them is the remaining work. The most dangerous case
is `playbooks.py`, which locates `docs/agent-playbooks/` by walking ancestors and
returns `None` on failure — so the agent would silently lose all of its pattern
knowledge rather than error.

## Naming

The package directory is still `dportsv3`. The version-in-the-name is exactly what
this rename is fixing; renaming the package, the CLI entry point and the
`DPORTSV3_*` environment variables is a deliberate follow-up, because those env
vars are part of the operator-facing contract (dsynth hooks and service configs
set them).
