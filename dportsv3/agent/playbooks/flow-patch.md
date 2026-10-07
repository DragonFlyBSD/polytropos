---
triggers:
  flows: [patch]
tags: [overlay, dops, mk-var, scope, patch, genpatch, recovery]
priority: 55
---

# Patch-flow procedures — editing `overlay.dops` and port files

You edit `ports/<origin>/overlay.dops` **free-hand** in dops DSL: read
it with `grep`/`get_file`, write the new or changed lines with
`edit_file` (or `put_file` when creating the overlay from nothing), then
validate. There is no per-directive intent tool and no file-delete tool.
Your write primitives are `edit_file` (anchored replace — the default
for changing anything that already exists), `put_file` (whole-file
write — for creating a file, or replacing a short one outright) and
`install_patches`, plus the build loop. The grammar itself
(every `mk`/`file`/`text` op, heredoc blocks, conditional ops) is in
`dops_reference()` — call it at most once, for syntax this file and the
overlay do not show. This file covers the *flow* knowledge the grammar
reference doesn't: the write/validate loop, `mk`-directive traps,
scoping judgment, the static-patch workflow, and recovery from a bad
patch.

## Read the overlay through the engine, not raw

`get_file overlay.dops` returns the *literal* file — every `target @X`
section, every op regardless of scope. On a multi-target overlay you'd
have to apply the engine's scope filter in your head.

Use **`get_effective_overlay(origin)`** instead when you need to reason
about what compose will actually do. It runs the file through the engine
and returns:

- `target` — the env's compose target (the build line you're on).
- `effective_ops` — ops that **will** apply on this build, **in the
  order the engine executes them** (`apply_index` is the ordinal — see
  "Order is by scope, not by position" below; it is *not* file order),
  each tagged with `scope` and its engine `kind`
  (`mk.var.set`, `mk.var.token_add`, `patch.apply`, …). For `mk.var.*`
  the variable is in `name`, the value in `value`.
- `filtered_out` — ops scoped to *other* build lines, each with a
  `reason` for exclusion.

Raw `get_file overlay.dops` is still right for byte-exact inspection
(confirming a write landed in the section you intended); use the
effective view for "what applies here."

## Writing overlay.dops: the put_file → validate_dops loop

After every edit, **call `validate_dops(origin)` before you
`materialize_dports`.** A syntax error caught by validate costs nothing;
the same error caught by materialize wastes a whole build cycle.

- `validate_dops` runs the full engine check (lex → parse →
  document-level semantics) over the **entire** file. On failure it
  returns `ok=false` with a diagnostic carrying an `E_*` code plus
  `line:column`. Fix the offending line(s) and call it again until
  clean.
- **Change one logical thing at a time, then re-validate.** A single
  bad line is then rejected on its own with a precise diagnostic,
  instead of hiding inside a larger rewrite.
- Distinguish the two failure shapes:
  - **Your edit is invalid** (bad keyword, malformed heredoc, a
    duplicate document directive like a second `port`/`type`/`reason`/
    `maintainer` line) → fix the line and re-emit.
  - **The overlay was already broken before you touched it** → the file
    needs a human-authored fix. Retrying your edit won't help —
    **escalate** (`Rebuild Status: gave-up` with that diagnostic).
- For a `put_file` on any file you haven't `get_file`'d this session,
  pass `expected_sha256` from a prior read so the write is race-safe
  against stale content.

### Heredoc blocks — exact form

`mk target` recipes and `mk block` regions use heredocs. The engine is
strict:

```dops
mk target set post-patch <<'MK1'
${REINPLACE_CMD} -e 's,/usr/local,${PREFIX},' ${WRKSRC}/Makefile
MK1
```

- The opener must be **quoted**: `<<'MK1'`. A bare `<<MK1` is rejected
  with `E_PARSE_INVALID_HEREDOC_START: expected <<'TAG'`.
- The terminator line must equal the tag **exactly** — `MK1`, never
  `  MK1` or `MK1 `. Any leading/trailing whitespace and the block
  never closes (`overlay.dops is corrupt`), which no edit can recover
  from on a guess.
- Match the body's existing shell-recipe indentation (tabs) so the
  rendered overlay stays parseable as a Makefile target. To extend a
  recipe, rewrite the body with the extra line appended; there is no
  separate "append to body" op.

## `mk` directive semantics and traps

The grammar for `mk set/add/remove/unset` is in `dops_reference()`.
These are the non-obvious traps that cause silent wrong-builds:

### unset vs set-to-empty

To make an upstream-set variable *not present*, write `mk unset VAR` —
it deletes the whole assignment line, including whatever upstream
FreeBSD had. Do **not** "set it to empty": `FOO=` still counts as
**defined**, so framework code doing `.if defined(FOO)` stays true and
may still read an empty path and fail. `unset` is the clean atomic
answer (e.g. an upstream `LICENSE_FILE=${PORTSDIR}/COPYRIGHT` pointing
at a file absent from our tree — `mk unset LICENSE_FILE`, and the
license check falls back to the BSD2CLAUSE template default).

### add appends a token; it does NOT override a keyed value

`mk add VAR "token"` mirrors make's `+=`: create-or-append, idempotent
(re-adding a present token is a no-op). It is **not** a way to change
the value of an existing key inside a key-valued list like `PLIST_SUB`,
`SUB_LIST`, `MAKE_ENV`. If upstream has `PLIST_SUB= OSMAJOR=<x>` and you
`mk add PLIST_SUB "OSMAJOR=<y>"`, **both** land in the flattened value.
The framework builds a repeated `sed -e s!%%OSMAJOR%%!…!g` list, and
sed processes `-e` flags **first-match-wins**: the upstream (broken)
value wins and your "override" is dead code. There is no `mk` op to
rewrite an existing keyed value — use a patch or `text replace-once`
against the generated file.

### remove needs the variable to exist

`mk remove VAR "token"` takes a token *out* of a list; the assignment
must already exist (else `assignment not found`) and the token must be
present (else `token not found`). To make a whole variable go away you
want `unset`, not `remove`.

### ambiguity and accumulation

- A `set`/`unset`/`remove` that matches **more than one** upstream
  assignment of the same variable refuses with `E_APPLY_AMBIGUOUS_MATCH`
  — the engine won't guess which to rewrite, and a `target` block does
  not change that. Rewrite it with `text replace-once`, quoting enough of
  the neighbouring lines (joined with `\n`) that the `from` text occurs
  once in the file, or hand-resolve. (`add` does not refuse on
  multi-assignment.)
- Re-emitting `mk set VAR` for the same key **accumulates** lines in the
  overlay. The composed Makefile is still correct (ops play last-wins —
  within one scope that is declaration order; across scopes see "Order is
  by scope, not by position"), but the file carries every copy. To
  drop a superseded `mk` line, edit `overlay.dops` and delete that exact
  line (whole-token key match — `USE` won't match `USES`).

### grep the framework before guessing a variable

`Mk/bsd.port.mk` and friends are the source of truth for what a Makefile
variable means and what value-shape it requires. A wrong guess yields an
opaque error (`.for` arity mismatch, "Wrong number of words", a
missing-file error from a path you didn't construct). Before writing an
`mk` op for a variable whose value-shape you're inferring, grep first —
a few hundred tokens against `Mk/` beats a multi-thousand-token failed
build cycle:

```
grep("^[[:space:]]*<VAR>[[:space:]]*[+:?]?=", "/work/freebsd-ports/Mk")
grep("\\$\\{<VAR>\\}|\\$\\(<VAR>\\)", "/work/freebsd-ports/Mk")
grep("\\.for .* in .*<VAR>", "/work/freebsd-ports/Mk")
```

The first finds the definition/default; the second finds consumers; the
third finds `.for` iteration — the tell that the value is a
whitespace-separated list with fixed per-entry arity. Also worth
grepping: `Mk/Uses/*.mk` (per-`USES` behavior), `Mk/Features/*.mk`,
`Mk/Scripts/*.sh`.

## Scoping — where your change goes

The active scope is set by a `target` directive; ops inherit the most
recently named scope. `target @any` applies on **every** DragonFly build
line, later ones included. A `target @main` (or `@2026Q3`, etc.) section
applies only on that build line. Your line is the env's compose target,
`target` in `get_effective_overlay` (and from `env_verify`).

Your env builds one line, so nothing here can show a change is right for
the others. A fix missing from another line leaves it as it was; a wrong
shared fix breaks a line that was building. So:

| Situation | Where it goes |
|---|---|
| The port had no overlay ops when the job started (none, or only its bootstrap header) | its ops in `target @any` |
| It had one, and you add an op or a patch | a `target <your line>` block; `install_patches` puts a patch in `dragonfly/@<your line>/` |
| The op to change is in your line's block | change it there |
| The op you need is in another line's block | share it: `target <its line>,<your line>` on the line above it, `target <its line>` on the line below — but not a patch under `dragonfly/@<its line>/`, which is that line's own: write yours with `install_patches` |
| An `@any` patch no longer applies on your line | re-cut it; `install_patches` writes your line's own copy, never the shared file |
| An `@any` op composes but is wrong for your line | override it in your line's block |
| An `@any` op fails to compose on your line | your own change did that: undo it. One that already failed when the job started never reaches you; it goes to an operator |

**Do not edit or delete an `@any` op** on a port that had an overlay
before the job: every line reads it, and yours is the only one built.
Overriding works because `@any` ops run first (next section): `mk set` in
your block replaces the value, `mk remove` takes back a token `@any`
added, `file remove dragonfly/<name>` drops a shared patch on your line
only. A compose error is different: no later op can undo an op that
failed.

When you do write to `@any`, prefer relative ops (`mk add`, `mk remove`)
to an absolute `mk set`: a relative op is right against every line's
upstream, an absolute value only against the one you read it from.

Leave `on-missing` at its default, `error`: a missing anchor or file is
how another line learns an op no longer fits it.

## Order is by scope, not by position

For a build on target `T` the engine executes **every `@any` op first, in
file order, then every `T` op, in file order.** File position does not
decide what runs last — scope does. `mk` and `text` ops are last-wins, so
this changes results (`mk set` inserts when the variable is absent, so
the example works either way):

```
target @main
mk set FOO "from-main"
target @any
mk set FOO "from-any"      # reads last in the file
```

On `@main`: `@any` runs first, then `@main` → **`FOO= from-main`**.
On `@2026Q3`: only the `@any` op runs → **`FOO= from-any`**.

One edit, two different composed Makefiles, and **you can only build one
of them** — an env has exactly one target. So an op that must beat an
`@any` op has to live in that target's own block; putting it *below* the
`@any` op is not enough and does the opposite of what reading the file
suggests.

Two consequences worth holding onto:

- `get_effective_overlay`'s `effective_ops` is already in this order, and
  `apply_index` is the position. Trust that ordinal over the file.
- It also decides which op wins when two `file materialize` ops name the
  same destination — the scoped one wins on its own target and loses on
  every other.

## Creating a static source patch (`dragonfly/*`)

`dragonfly/*` patches target **upstream source** inside the distfile
(`Makefile.am`, `src/foo.c`, …) — files that don't exist at compose
time, only after `do-extract` at build time. **Never `patch apply` a
`dragonfly/*` patch**; stage it with `file materialize dragonfly/X ->
dragonfly/X` so `bsd.port.mk`'s `do-patch` applies it at build time.
(`diffs/*.diff` framework patches are the opposite domain — see the
quickref's "Two kinds of patches".)

For a non-trivial source change, edit the source and let the engine
produce the diff rather than hand-writing one:

1. `make_extract(origin)` — populates WRKSRC; use the returned `wrksrc`
   path for all reads from here on. This is `do-extract` only: the
   distfile is unpacked **pristine**, no patches applied.
2. `make_patch(origin)` — **run this when the file you're about to edit
   is also touched by a FreeBSD `files/patch-*` (or an existing
   `dragonfly/*` patch).** It runs `do-patch`, which applies `files/*`
   then `dragonfly/*` in order, leaving WRKSRC in the real build-time
   state. `dragonfly/*` patches apply **after** `files/*` at build time,
   so your new patch's context must reflect the post-`files/*` source —
   not pristine upstream. Skip this step only when the target file is
   untouched by any framework patch (pristine == build state). If a
   broken `dragonfly/*` patch you're regenerating was already dropped
   from `overlay.dops` (the deferred-patch channel removes a rejecting
   patch to get compose green), `do-patch` won't choke on it. On
   failure, the rejecting patch is named in the tool's `stdout_tail`.
3. `dupe(<wrksrc>/path/to/file.c)` — snapshots a `.orig` and exposes the
   file for editing. **Dupe AFTER make_patch** so the baseline is the
   post-`do-patch` state — that baseline is what genpatch diffs against.
4. `edit_file <wrksrc>/path/to/file.c <old_string> <new_string>` — change
   it. Anchored replace: you pass only the region you are changing, so
   the rest of the file is never at risk. **Do not use `put_file` here.**
   A whole-file write means re-emitting every byte from a windowed read,
   and on a multi-kilobyte source that truncates — genpatch then diffs a
   corrupted baseline and produces an empty or garbage patch. Edit the
   extracted source here; the patch reaches `ports/<origin>/` through
   `genpatch` and `install_patches`.
5. `genpatch(<same path>)` — runs `diff -u` between `.orig` and current,
   depositing a WRKSRC-relative `patch-*` file. (It picks up WRKSRC from
   the prior `make_extract` automatically.) Because the `.orig` baseline
   is post-`do-patch`, the hunk context matches what `do-patch` sees at
   build time and the patch applies cleanly.
6. `install_patches(origin)` — copies the generated patch into the
   port's payload. A re-cut replaces the file its existing
   `file materialize` line reads, and that line stays as it is — unless
   other build lines read that file too: then it writes your line's own
   copy under `dragonfly/@<your line>/`. A new patch goes there as well,
   or to flat `dragonfly/<name>` with an `@any` op on a port that had no
   overlay when the job started. Either way `scope_note` gives the op to
   add. **Read `installed` for the path and `scope_note`, when present,
   for what to do next.**

**`dupe` is only one step of this flow.** It exists solely to support
patch generation — it is not an investigation tool, not a "before"
snapshot for reading, and not a way to edit an existing
`dragonfly/patch-*`. A `dupe` with no follow-up `genpatch`/
`install_patches` in the same attempt is wasted work and a sign you
reached for the wrong tool. For a small change you can also write the
unified diff by hand from prior `get_file` reads and stage it directly.
A file other build lines read too is refused, and the refusal says where
your line's copy goes.

## Recovering from a broken patch — never text-edit the diff

**First decide which of three situations you are in. They present
almost identically and two of them have opposite fixes.**

- **Stale / drifted** — the patch is well-formed and still encodes the
  change DragonFly wants; upstream simply edited the lines it targets.
  The tell is `<N> out of <M> hunks failed--saving rejects to <file>.rej`
  after a version bump, with the rejects naming real hunks.
  → **Not this section.** Refresh it and *keep* the `file materialize`
  line — see `error-prefer-dops-over-static-patches.md`, "Re-cut a
  drifted source patch". Deleting a drifted patch throws away a fix
  that is still wanted.
- **Superseded** — FreeBSD has added a `files/patch-*` that already
  achieves what ours did, and since `files/*` applies first ours now
  rejects against source that is already fixed. → **Not this section
  either**, and it is the one case where deleting the `dragonfly/` file
  and its `file materialize` line is correct. It has to be established
  rather than assumed — same file, even same line, is not evidence. See
  `error-prefer-dops-over-static-patches.md`, "has FreeBSD made our
  patch unnecessary?", which carries a worked counter-example. If that
  op is under `target @any` on a port that had an overlay before the
  job, drop the patch on your line only, with `file remove
  dragonfly/<name>` in your line's block: another line's upstream may
  not have FreeBSD's fix yet.
- **Malformed** — the patch file itself is garbage: a hunk header that
  disagrees with its own body, a truncated diff, `E_APPLY_PATCH_FAILED`
  with "malformed patch", `E_APPLY_MISSING_SUBJECT`. Nothing in the
  file is worth keeping. → This section.

For a malformed patch the only correct recovery is **regenerate, then
overwrite in place** — in that order:

1. Regenerate a correct patch via the dupe→genpatch flow above, or stage
   a corrected hand-written diff.
2. Write it over the broken one, at the path its own `file materialize`
   op reads — which is **not** necessarily `dragonfly/<name>`: on a port
   that scopes this patch the source is `dragonfly/@<target>/<name>`.
   `install_patches` resolves that from the overlay and reports it in
   `installed`. When other build lines read that file too, it writes
   your line's own copy instead and `scope_note` gives the one op to add.
   `put_file` and `edit_file` refuse that file the same way.

**`overlay.dops` needs no edit at all.** Its `file materialize` line
already names that source, so overwriting the file there is the whole
swap: one write, and no moment where the port is staged without a patch.
Only if the replacement must take a *different* filename do you touch
`overlay.dops`, and then only after the new file exists.

**Never leave `overlay.dops` with the install line removed and no
replacement written.** In that window the port composes green with the
DragonFly fix silently absent — a build that passes and a platform
regression that nothing reports. If your attempt dies there (budget
exhausted, a tool error, an interrupted run), that broken-but-green
state is what you leave behind. Regenerating first means every
intermediate state carries either the old patch or the new one.

If compose genuinely must be made green *before* a replacement exists,
that is the deferred-patch channel's job (see the `make_patch` step
above), not a bare line removal — it records that a patch was dropped.

**Do not** text-edit the diff to "fix" line numbers. Editing a diff
shifts the hunk *body* but not the hunk *header*, producing a patch that
lies about its own bytes. The classic failure shape: a malformed patch
is "fixed" with successive in-place text edits, and every subsequent
`materialize_dports` then dies with `E_APPLY_MISSING_SUBJECT` against a
file that was never staged. Patch files are output artifacts — treat
them as regenerate-only.

## Removing directives and files

There is no delete tool. To stop applying something, **edit
`overlay.dops` and remove the relevant line or block**, then
re-validate. That is for ops in your line's block, or in an overlay
this job created; an `@any` op on a port that had an overlay before the
job is overridden in your block instead (see Scoping):

- A `patch apply` / `file materialize` / `file copy` line → delete the
  line; compose stops applying it. (In dops mode the compat auto-copy is
  suppressed, so an unreferenced `dragonfly/*` source file left on disk
  is inert — it only reaches the build via an explicit `file materialize`
  line.)
- A whole `mk target <name> <<TAG … TAG` heredoc block → delete the
  opening line through the closing tag, inclusive.
- A single `mk set/add/remove/unset` line → delete that exact line.

When you remove a directive, do it as one focused `put_file` rewrite and
re-validate; don't try to counter a wrong op with a second op when
deleting the line is cleaner. You have no file-delete primitive and don't
need one: on a successful fix the runner reconciles orphaned `dragonfly/` /
`diffs/` source artifacts — it deletes any file no longer referenced by the
overlay (a removed `file materialize` / `patch apply` line) before capturing
the diff, so the removal is part of the delivered fix.

## Bumping PORTREVISION

When the port already builds at this upstream version and you
changed how it builds (added a patch, edited flags), bump the
revision with mk bump PORTREVISION, in the same target block as the
change it accounts for. It
adds one to whatever that build line's upstream Makefile says, on
every compose, so it never pins a number. Do not write mk set
PORTREVISION: an absolute number reverts every later upstream bump.
If the overlay already writes PORTREVISION, leave that op alone and
say so in your report: replacing it can lower a revision already
shipped, which an operator decides. A port's first fix is not a
rebuild: do not bump it.
