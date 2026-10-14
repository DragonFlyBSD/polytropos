# Step 47 — `diffs/` absorption — working plan (TEMPORARY, delete when done)

Working tracker for implementing Step 47. Going in order, phase by phase.
Reference spec: `docs/agentic-architecture-backlog.md` Step 47.
Per-phase focus: trim completed phases to keep this doc current, not cumulative.

## STATUS 2026-10-14: Phase 6 DONE (poly-u9n4.2); Phases 3–5 still on hold
Moved here on 2026-10-14 from the DeltaPorts working tree, where it sat
untracked since the toolchain split. Step 48 is done (backlog). Phase 6
landed as poly-u9n4.2 (`9f335a5`); see its section below. Nothing else has
moved since the June pause recorded next.

## STATUS: PAUSED pending Step 48 (2026-06-09)
Strategic shift: the compat surface is ~4,940 ports (3,893 Makefile.DragonFly,
1,784 dragonfly/, 252 diffs/). Decision: do a standalone MASS compat→dops
conversion as its OWN step (Step 48), reusing the existing convert code, so the
runtime `convert` phase can be retired and we operate dops-only. AFTER Step 48,
Step 47 is REFRAMED away from compat→dops toward the STEADY-STATE dops plist/file
edit interface (mk add PLIST_FILES for adds = order-free; text.line_* for
removes; rewrite error-plist-mismatch.md which currently points at dead compat
artifacts). Phases 0–2 already shipped (gate, REMOVE, Makefile.diff) stand.
Phase 3+ (plist/distinfo/dragonfly) are ON HOLD — likely subsumed by Step 48.

Key steady-state finding: the anchor/order agony was a MIGRATION-only artifact
(byte-parity forces positional reproduction). Steady-state adds use
`mk add PLIST_FILES` (framework places it, order-free) → no anchor problem.

## Locked decisions
- Lane by **relationship-to-upstream**: additive → whole-file materialize;
  modificative → typed delta op.
- `pkg-plist` → `plist { + − }` set-delta (`final = upstream ∪ adds − removes`, re-sorted).
- `distinfo` → `distinfo { + ~ − }` set-delta, **hashes declared inline** (`sha256=`/`size=`).
- `dragonfly/` additive lane: **convention-driven auto-materialize**; explicit
  `file materialize` still honored and **wins** (convention skips claimed files).
- Irreducible modificative text patch → explicit `patch apply`, body inlined, file deleted.
- **Definition of done per port:** compose-output-with-compat byte-equals
  compose-output-with-dops on every supported target (parity gate). Delete `diffs/` only after green.

Tags: **[mig]** translator · **[eng]** engine (high care) · **[comp]** compose

## Open sub-decisions (settle before the phase that needs them)
- [ ] **4.2** inline `patch apply`: embed body in dops vs keep a file (engine variant of `_exec_patch_apply`, which today reads a path).
- [x] **Phase 6 ordering**: run independently, ahead of the spine (poly-u9n4, 2026-10).

---

## Phase 0 — parity-gate harness (gates everything) — IMPLEMENTED (review→tests→commit pending)
- [x] 0.1 Per-port oracle: `fsutils.diff_tree` (content-aware tree diff) +
      `migration/parity.py` `check_parity` (compose origin from baseline_root &
      candidate_root into temp dirs, byte-compare the `<origin>` subtree) + CLI.
      Verified for real: identical roots → OK; deleting a port's diffs/REMOVE in
      the candidate → FAIL pinpointing the now-unstripped file. Minimal
      single-port candidate root composes fine (cheap per-port gating).
      Refinement found while demoing: gate composes BOTH sides with oracle
      `off` — the bmake oracle only runs in dops-mode, so a compat baseline
      never validates while a dops candidate would; that asymmetry would
      falsely fail every absorption. Gate checks output parity, not Makefile
      validation. End-to-end demo (audio/artyfx-lv2 REMOVE→file remove):
      correct absorption → OK; wrong filename → FAIL (only_right the file).

## Phase 1 — `REMOVE` → `file remove` — DONE (6 of 27 flipped; rest gated by later phases)
- [x] 1.1 [mig] `migration/absorb_remove.py`: `absorb_remove` (bootstrap overlay
      header from STATUS type + `file remove … on-missing noop` ops; deletes
      REMOVE + empty diffs/; skips unsafe entries to match compat ignore).
- [x] 1.2 `absorb_remove_gated` (single-port candidate → parity gate per target →
      mutate real tree only on green). Flipped the 6 REMOVE-only ports:
      audio/artyfx-lv2, math/mate-calc, security/md5deep, net/honeyd, devel/nasm,
      devel/rubygem-thrift. Tests: test_absorb_remove.py, test_fsutils_diff_tree.py.
- KEY CONSTRAINT (found): an overlay.dops flips the port to dops-mode, which
  SUPPRESSES the whole compat diffs/ path — so a port can't be partially
  absorbed. Only REMOVE-only ports flip in Phase 1; the other 21 (carry
  Makefile.DragonFly / other diffs / dragonfly) wait until those types are
  absorbed too. The gate enforces this (partial absorption → parity FAIL).

## Phase 2 — `Makefile.diff` → hybrid (mk vars + text.replace_once) — IN PROGRESS
RECON FINDINGS (183 files / 275 hunks):
- Hunk mix: 33% var-assignment, 37% target-recipe, 15% conditional, 13% mixed.
  Only the var subset maps cleanly to `mk` ops. STRATEGY = HYBRID (user pick):
  clean var hunks → `mk` op (only where it reproduces bytes — CST token-add
  preserves formatting); everything else → `text.replace_once` (exact old→new).
- Byte-parity tension: `mk` re-renders; only safe where bytes match. Local
  self-check (apply ops to upstream Makefile vs patch(upstream,diff)) decides
  per port; gate is final arbiter.
- DRIFT: 50 of 183 ports' diffs reject against current upstream → baseline is
  E_COMPOSE_COMPAT_FAILED (degraded partial state, no clean parity target).
  DECISION: Phase 2 absorbs the 133 CLEAN-baseline ports only. The 50 drift
  ports DEFERRED to Phase 2b (reconcile rejected hunks vs upstream — fix, not
  encode; LLM/manual). Do NOT silently drop drifted DragonFly intent.
KEY FINDINGS:
- Engine CAN express every change type (verified byte-exact): var→mk add/set,
  conditional→mk replace-if, recipe→mk target set <<TAG. CST round-trips lossless.
  Expressibility is NOT the constraint.
- BYTE-EXACT parity is the WRONG gate for Makefiles: mk ops re-render
  (separator tab→space) and relocate inserted vars — both functionally
  meaningless to make. Measured: byte-exact=0/26, ws-normalized=14/26,
  ws+order-independent=18/26. => adopt CONTENT-EXACT (whitespace-normalized,
  order-preserving) parity for the Makefile FILE only; byte-exact elsewhere.
  Placement (relocation) stays a difference (order-preserving) — it's riskier
  to ignore (immediate := vars, .if ordering) → those escalate.
- text.replace_once is single-line-only (no heredoc) — not usable for multi-line.

ARCHITECTURE (settled): translator does the deterministic subset; what it
can't handle it ESCALATES with structured context (unhandled hunks + Makefile +
intended result) to the PATCH AGENT — same pattern as convert's deferred
patches. The patch agent (LLM, like me) writes the ops freehand; its functional
/build success criterion sidesteps placement. NO CST-differ, NO new engine.
- [x] 2.0 [gate] content-exact (per-file normalizer) in diff_tree/check_parity;
      makefile_whitespace_normalizer (ws-normalize Makefile, byte-exact else).
- [x] 2.1 [mig] absorb_makefile.py: hunk parser + in-place var→mk emitter
      (add/remove/set) + local content-exact self-check. Correct-by-construction:
      anything not reproducing content-exact escalates.
- [x] 2.2 [mig] Escalation emits DeferredPatch (existing patch-agent deferred
      shape) — routes the tail into the convert→patch handoff. Never mutates.
- [x] 2.3 Gated content-exact flip → 6 ports flipped (powerarchitect,
      gauche-readline, pkgconf, fileobj, fortunate, ipdecap). Commits b11c6b3, 399f5e3.
- SURVEY: 6 auto-flip; ~162 escalate (140 non-det hunk, 15 ops-no-reproduce,
  7 compose-parity, 2 drift); 13 upstream-missing.
- GAPS (host-side, later): enqueue the 162 escalated DeferredPatches to the
  patch agent (needs runner/dsynth). 162 stay on compat meanwhile (no regression).
- NOTE: gate composes mark stale overlays as a by-design byproduct → use
  explicit git staging on flips (mark_stale read-only flag was reverted).
- [ ] 2b (LATER) patch-agent run (host-side) for escalated ports; drift pass (50).

## Phase 3 — `plist` set-delta (~75) — first engine op — NOT STARTED
- [ ] 3.1 [eng] Lexer `{ + − }` block grammar, strictly additive.
      **Golden lexer tests over existing corpus proving identical tokenization.**
- [ ] 3.2 [eng] AST node + parser.
- [ ] 3.3 [eng] `semantic.py` validation/diagnostics.
- [ ] 3.4 [eng] `planner._map_operation` → `plist` kind.
- [ ] 3.5 [eng] Executor: read upstream pkg-plist in port_root, apply
      ∪adds −removes, re-sort, write via `FileTransaction`; register in `apply.py`.
- [ ] 3.6 [mig] Translator `pkg-plist.diff` +/- lines → block entries.
- [ ] 3.7 Gate; `@`-ordering cases → `text.line_*` fallback; flip; delete.

## Phase 4 — text residue (~12 `*.in.diff`) — NOT STARTED
- [ ] 4.1 [mig] Per-file provenance classify (upstream- vs DragonFly-owned).
- [ ] 4.2 [eng?] DragonFly-owned → materialize; upstream-owned → inline `patch apply`
      (resolve the open sub-decision first).
- [ ] 4.3 Gate; flip; delete.

## Phase 5 — `distinfo` set-delta (~11) — second engine op (deps Phase 2) — NOT STARTED
- [ ] 5.1 [eng] Extend Phase-3 grammar: `~` verb + inline `sha256=`/`size=`.
- [ ] 5.2 [eng] AST/parser/semantic/planner/executor (apply + ~ − vs upstream distinfo).
- [ ] 5.3 [mig] Translator `distinfo.diff` → block.
- [ ] 5.4 Gate; flip; delete.

## Phase 6 — convention-driven `dragonfly/` lane (independent) — DONE except 6.3
- [x] 6.1 Auto-install `dragonfly/`, explicit-op skip (explicit wins) — in the
      engine's `apply_plan` rather than compose, so compose, `dsl apply` and the
      agent's tools agree (`dportsv3/engine/payload.py`, poly-u9n4.2). Added
      beyond this plan: `dragonfly/@<line>/` installs on that line only, each
      folder at the head of its scope (`docs/dsl-v0.md`, "Payload folder").
- [x] 6.2 Parity gate: rule off vs on, every DSL port with a `dragonfly/`
      folder, `@main` and `@2026Q3`, byte-identical once the seven files no op
      read were deleted (DeltaPorts `01122ff7d42`, poly-u9n4.1).
- [ ] 6.3 [comp] Retire compat `diffs/` path per migrated port.
- [x] Follow-up (poly-u9n4.3): the 4,483 `file materialize` ops the folder
      made redundant were deleted in one DeltaPorts commit; whole-tree compose
      of `@main` and `@2026Q3` byte-identical before and after.
