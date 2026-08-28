---
triggers:
  classifications: []      # wildcard — cross-cutting guidance, attach to every triage/patch payload
  flows: [triage, patch]
tags: [dops, static-patch, version-bump]
priority: 50
---

# Known Issue: Static patch fails after upstream version bump

## Pattern

- Triage classification = `patch-error`.
- Failure message:
  - `<N> out of <M> hunks failed--saving rejects to <file>.rej`
  - `===> FAILED Applying dragonfly patch-<file>`
  - `===> FAILED to apply cleanly dragonfly patch(es)  patch-<file>`
- The failing patch lives at `ports/<origin>/dragonfly/patch-*`.
- The DeltaPorts STATUS file shows a `Last success:` version that is
  older than the upstream `DISTVERSION` in the materialized
  `Makefile` (i.e. FreeBSD upstream has moved forward while the
  DeltaPorts overlay's patches haven't been updated).

## Cause

DeltaPorts overlays FreeBSD ports. When FreeBSD bumps a port's
`DISTVERSION`, `materialize_dports` picks up the new upstream Makefile
and distinfo automatically — but the DragonFly-specific patches under
`ports/<origin>/dragonfly/` do not update themselves. The patch fails when
upstream changes the lines it targets. Two distinct shapes:

- **Generated-file decay** — the patch targets a file autotools regenerates
  (`Makefile.in`, `configure`). The context shifts on *every* bump; these
  decay constantly.
- **Source drift** — the patch targets a hand-written source file (`.c`/`.h`)
  and upstream happened to edit the same line this release. A one-time drift,
  not constant decay.

The durable fix differs by shape — that's what Step 1 decides. Do not assume
"static patch failed ⇒ convert to a dops substitution"; that's right for
generated files and wrong (fragile) for hand-written source.

## Fix

### Step 0 — does overlay.dops already exist?

```
get_file /work/DeltaPorts/ports/<origin>/overlay.dops
```

- **Yes** → the port is dops-managed. Add new operations to the
  existing file; follow the existing style.
- **No** → the port is still on static patches.

If you're about to write an `overlay.dops` and you don't have the
syntax memorized, call `dops_reference()` **once** to get the
condensed quick-reference.

### Step 1 — what does the failing patch target? (decide this FIRST)

This single question determines the approach. Look at the file the patch
modifies — **not** at "static patch failed, so convert to dops". Picking the op
before classifying the target is the #1 mistake here (it produces a fragile
`REINPLACE` for a source patch that just needed re-cutting).

- **Hand-written source** — `.c`, `.h`, a hand-maintained `Makefile`, or the
  autotools *sources* `Makefile.am` / `configure.ac`. The static patch is the
  **correct, durable form**; it failed only because upstream edited the same
  lines (a one-time drift, not per-bump decay). → **Re-cut the patch** (next
  section), KEEP the `file materialize`. **Do NOT** convert it to a `REINPLACE`
  / `text replace-once`: a whole-line substitution **silently no-ops** the next
  time that line changes, dropping the DragonFly fix with no build error. A
  re-cut patch instead **fails loudly** on the next drift. (This is exactly the
  `lib/readline/terminal.c` case — re-cut it, do not REINPLACE it.)
- **Generated file** — `configure`, `Makefile.in`, libtool m4 output: anything
  autotools regenerates at build time. Patching the generated output is brittle
  (it decays on *every* bump). → Convert to a dops op (Option A) or patch the
  autotools *source* (Option B).

### Re-cut a drifted source patch (the hand-written-source branch)

1. `make_extract`; use `wrksrc` from its response (don't guess from
   `DISTVERSION` — the obj tree has stale leftovers).
2. `grep` the new upstream file for the same logical change site.
3. Edit the file in `WRKSRC`, `genpatch`, write the refreshed patch back under
   `dragonfly/`, and **keep** the `file materialize` line. Done — the patch
   applies cleanly now and surfaces loudly the next time it drifts.

**`overlay.dops` is not edited at all in this flow.** The refreshed patch
overwrites the old one at the same path, so the existing `file materialize`
line already points at it. If you find yourself removing that line, stop —
you are about to delete the fix rather than repair it. A drifted patch is
not a broken patch: it is still the change DragonFly needs, aimed at lines
that moved.

That failure has happened. The port then builds **green** with the
DragonFly fix gone, and nothing reports it — the worst shape a change can
take here, because it looks like a pass. Guidance for genuinely malformed
patches (`flow-patch.md`, "Recovering from a broken patch") does touch
`overlay.dops`; that procedure is for a different situation and does not
apply to drift.

### Option A (GENERATED-file target only) — convert to dops

The change is a small textual substitution on a *generated* file. Express it as
a dops `text` / `mk replace-if` op against the generated output, run at
`pre-configure` / `post-patch`:

```dops
# Instead of dragonfly/patch-Makefile.in (regenerated every upstream bump),
# express the same logical change as a build-time substitution:
text replace-once file ${WRKSRC}/Makefile.in \
    from "freebsd*)" \
    to   "freebsd*|dragonfly*)"
```

Pair with **removing the patch from the overlay** — delete its
`file materialize dragonfly/patch-<file> -> …` line. You have no file-delete
tool and don't need one (and don't `git rm` — no git in the loop): the runner
reconciles the now-orphaned `dragonfly/patch-<file>`, deleting it as part of
the captured fix.

### Option B (GENERATED-file target) — patch the autotools *source*

Patch `Makefile.am` (generates `Makefile.in`) or `configure.ac` (generates
`configure`) instead of the output — the source changes less often, so the
patch survives bumps. Then rely on `USES= autoreconf` or add a `pre-configure`
step (`mk target set pre-configure <<'MK' … MK`).

### Option C (last resort) — regenerate the static patch as-is

Only when none of the above fits. Re-cut against the new context (as in the
re-cut section), and note in the commit message that it's a regenerate-only fix
likely to break on the next bump.

## When NOT to convert

Keep the static patch when:

- The change spans many lines or multiple hunks with complex context
  dependencies that don't reduce to a small set of substitutions.
- The patch adds or removes whole functions / blocks rather than
  tweaking lines.
- The target file is hand-maintained upstream (not generated). These
  patches don't decay on every release because the source is stable.

## Examples

- `devel/libuv`: `dragonfly/patch-Makefile.in` is a candidate — the
  hunks adjust autotools `FREEBSD_TRUE` and `am__append_*` blocks
  that automake regenerates on each upstream bump. Converting the
  per-line conditionals to `text replace-once` ops against the
  generated `Makefile.in` (or to patches against `Makefile.am`)
  would remove this whole class of recurring breakage.
- A future scan should flag every `ports/*/*/dragonfly/patch-Makefile.in`
  and `ports/*/*/dragonfly/patch-configure` as a candidate for dops
  conversion.
