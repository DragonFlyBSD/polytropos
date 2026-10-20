# DeltaPorts DSL v0 (Normative)

## Status

Normative language specification for `overlay.dops` in DeltaPorts v3.

This document is the single source of truth for DSL grammar and semantics.

Operator workflows and migration patterns are documented in
`docs/dportsv3-user-guide.md`; this file defines language semantics.

---

## Purpose

`overlay.dops` is the contributor-facing source format. The runtime compiles
DSL input into a normalized in-memory plan and executes that plan directly.

No persisted transition ops file is required.

---

## File and Scope Model

- File name: `overlay.dops`
- One origin per file; `port <category/name>` is required exactly once
- Directives are file-global; operations inherit the active target scope
- Multiple `target ...` directives are allowed in one file
- `type` is file-global (`port|mask|dport|lock`), not target-scoped
- Initial active scope is implicit `@any` before the first explicit `target`
- `target` accepts one selector or a comma-separated selector list in one token,
  for example `target @main,@2026Q1` (no spaces)

Supported targets:

- `@any`
- `@main`
- `@YYYYQ1`, `@YYYYQ2`, `@YYYYQ3`, `@YYYYQ4`

---

## Lexical Rules

- `#` starts a comment outside heredoc bodies
- Strings are double-quoted with escapes: `\\`, `\"`, `\n`, `\t`
- Bare identifiers are space-free tokens; quote values with spaces
- `\` at end of line is a continuation marker for command forms that allow it
- Heredoc form for recipes: `<<'TAG'` ... `TAG`
- Heredoc body is preserved exactly, including leading tabs
- `target` selector lists are lexed as one word token; write
  `target @main,@2026Q1` (no spaces after commas)

---

## Grammar (EBNF)

```ebnf
file              = { line } ;
line              = blank | comment | directive | operation ;

directive         = target_directive
                  | port_directive
                  | type_directive
                  | reason_directive
                  | maintainer_directive ;

target_directive  = "target" target_list ;
target_list       = target { "," target } ;
port_directive    = "port" origin ;
type_directive    = "type" port_type ;
reason_directive  = "reason" string ;
maintainer_directive = "maintainer" string ;

target            = "@any" | "@main" | "@" year "Q" quarter ;
year              = digit digit digit digit ;
quarter           = "1" | "2" | "3" | "4" ;
origin            = ident "/" ident ;
port_type         = "port" | "mask" | "dport" | "lock" ;

operation         = mk_op | file_op | text_op | patch_op ;

mk_op             = mk_var_op | mk_block_op | mk_target_op | mk_include_op ;

mk_var_op         = "mk" "set" var string [mk_file] [on_missing]
                  | "mk" "eval" var string [mk_file]
                  | "mk" "shell" var string [mk_file]
                  | "mk" "unset" var [mk_file] [on_missing]
                  | "mk" "add" var token [mk_file] [on_missing]
                  | "mk" "remove" var token [mk_file] [on_missing]
                  | "mk" "bump" var ["by" integer] [mk_file] ;

mk_block_op       = "mk" "disable-if" "condition" string [contains] [mk_file] [on_missing]
                  | "mk" "replace-if" "from" string "to" string [contains] [mk_file] [on_missing]
                  | "mk" "block" "set" "condition" string [contains] [mk_file] heredoc ;

mk_target_op      = "mk" "target" "set" ident [mk_file] heredoc
                  | "mk" "target" "append" ident [mk_file] heredoc
                  | "mk" "target" "remove" ident [mk_file] [on_missing]
                  | "mk" "target" "rename" ident "->" ident [mk_file] [on_missing] ;

mk_include_op     = "mk" "ensure-include" ident [mk_file] ;

file_op           = "file" "copy" path "->" path
                  | "file" "materialize" path "->" path
                  | "file" "remove" path [on_missing] ;

text_op           = "text" "line-remove" "file" path "exact" string [on_missing]
                  | "text" "line-insert-after" "file" path "anchor" string "line" string [on_missing]
                  | "text" "replace-once" "file" path "from" string "to" string [on_missing] ;

patch_op          = "patch" "apply" path ;

contains          = "contains" string ;
mk_file           = "file" path ;
on_missing        = "on-missing" ("error" | "warn" | "noop") ;
```

---

## Directive Semantics

- `target` sets active target selectors for all following operations until the
  next `target`
- `port` declares origin and is mandatory exactly once
- `type` defaults to `port` if omitted
- `reason` and `maintainer` are optional metadata fields

### Port root by type (normative)

`type` names the tree compose copies in as the port root before any
operation runs. The operations then apply to that copy the same way for
every type:

- `port`: the origin in the FreeBSD ports checkout, on the branch matching
  the build line
- `dport`: the overlay's `newport/`, the same tree on every build line
- `lock`: the origin in the lock root (`--lock-root`, the DPorts checkout)
- `mask`: none; the origin is removed and no operation runs

For a `dport`, `newport/` is the port's origin, read-only, as the FreeBSD
checkout is for a `port`. It holds what a FreeBSD port directory holds:
the Makefile and its includes, `distinfo`, `pkg-*` and `files/`, whose
`patch-*` files are the port's own, DragonFly-specific or not. It holds no
compat content, no `dragonfly/` and no `Makefile.DragonFly*`: that is
overlay content, and it goes beside `newport/` as it would beside a
FreeBSD port, in the `dragonfly/` payload folder, in `diffs/`, or as
operations. Compose warns on a dport whose `newport/` holds it
(`W_COMPOSE_DPORT_NEWPORT_COMPAT`). A change one build line needs goes in
that line's scope, as an operation or in its `dragonfly/@<line>/`
payload; a change to `newport/` reaches every build line and no scope can
narrow it.

### Target scope rules (normative)

- Active scope starts as `@any` before the first explicit `target`
- `target @a,@b,...` is a selector list; each following operation expands into
  one scoped operation per selector, in selector order
- `@any` MUST NOT be combined with explicit selectors in one `target` directive
  (for example, `target @any,@2026Q1` is invalid)
- A selector MUST NOT appear twice in one `target` directive (for
  example, `target @main,@main` is invalid)
- For an apply run targeting `T`, operation evaluation order is:
  1) all `@any` operations in source order
  2) all `T` operations in source order
  3) all other target-scoped operations, marked skipped with target-mismatch

### Payload folder (normative)

For a port with `overlay.dops` (types `port`, `dport` and `lock`), the files
under the overlay's `dragonfly/` directory are installed with no operation:

- `dragonfly/<path>` is installed on every build line, as `dragonfly/<path>`
- `dragonfly/@<line>/<path>` is installed on build line `<line>` only, as
  `dragonfly/<path>`
- a file an operation reads -- a `file materialize` `<src>` or a `patch apply`
  `<path>` -- on any build line is not installed by the folder: the
  operation installs it, where it says
- `dragonfly/@any/` is invalid: the flat folder is the one way to say every
  build line
- an apply run without an overlay directory of its own installs nothing

Each folder is installed at the head of its scope. For an apply run
targeting `T`, the flat files come before every `@any` operation, and
`dragonfly/@T/` after them and before every `T` operation. So on `T`,
`dragonfly/@T/<path>` replaces a flat file, or a file an `@any` operation
installed, at the same destination; and any operation can edit or remove
an installed file -- `file remove dragonfly/<path>` under `target T` drops a
shared patch on `T` only. Installed files are not operations: they take
no part in the dead-operation rule below.

### Dead operations (normative)

- An operation is dead when, on every build line it applies to, a later
  operation on that line replaces or removes the file it leaves its result
  in, and no operation in between reads that file.
- `file materialize` and `file copy` leave their result in `<dst>` and
  replace it whole; `file remove` removes `<path>`; `text` operations edit
  `file <path>` and `mk` operations edit `Makefile`, or their
  `file <path>`, in place. `file copy`
  reads `<src>`, `text` and `mk` operations read the file they edit, and
  `patch apply` counts as reading every file. Paths compare after
  normalization (`./x`, `a/../x` and `x` are one file).
- Build lines are open: an `@any` operation also runs on build lines that
  have no block of their own, so only a later `@any` operation can make it
  dead. An operation scoped to `T`, alone or in a selector list, is dead
  on `T` when a later operation whose selectors include `T` replaces or
  removes its file. A `T` operation that overrides an `@any` one, or a
  selector-list default on one of its lines, is valid.
- A dead operation is an error, `E_SEM_DEAD_OP`, reported at the dead
  operation.

---

## Operation Forms

### The file an mk op edits (normative)

Every `mk` operation edits the port's `Makefile`, or the file its
`file <path>` names. `file <path>` comes after the operation's arguments,
after `contains` and before `on-missing`, and before `<<'TAG'` on a heredoc
operation. The path is resolved like a `text` operation's: relative to the
port root, and an absolute path or one escaping the port root is
`E_APPLY_INVALID_PATH`.

It is for a file the port includes from its own directory, such as
`.include "${.CURDIR}/Makefile.common"`, when the variable or target is set
there. A file another port owns is edited by that port's overlay. Not for
`mk`: generated lists the framework includes (`Makefile.crates`,
`Makefile.cabal`, `Makefile.zig`), where an edit pins a version, and files
under `files/`, which belong to the software, not the port.

The forms and rules below are the same in any file, except that `mk add`
creates an absent variable with `<VAR>+= <token>` in a file other than
`Makefile`: the file that includes it may have assigned the variable
first, and `=` would discard that.

An operation that writes an assignment to `<VAR>` (`mk set`, `mk add`,
`mk eval`, `mk shell`, `mk bump`) fails with `E_APPLY_MK_OVERRIDDEN` when a
file included after the line it writes assigns `<VAR>` again with `=`, `:=`
or `!=`, because that assignment replaces the operation's value and the
operation would do nothing. The message names the file and line; put the
operation in that file with `file <path>`. Only top-level `.include` lines
and assignments count (one inside `.if` depends on its condition), and
only quoted includes that resolve inside the port root through
`${.CURDIR}`, `${.PARSEDIR}`, `${.CURDIR:H}`, `${.CURDIR:H:H}`,
`${PORTSDIR}` or a relative path are followed. `+=` and `?=` keep the
operation's value and pass.

### Makefile variable ops

```text
mk set <VAR> "<value>" [on-missing error|warn|noop]
mk eval <VAR> "<value>"
mk shell <VAR> "<value>"
mk unset <VAR> [on-missing error|warn|noop]
mk add <VAR> <token> [on-missing error|warn|noop]
mk remove <VAR> <token> [on-missing error|warn|noop]
mk bump PORTREVISION [by <N>]
```

`mk set` v1 behavior:

- if `<VAR>` exists exactly once, replace that assignment with `<VAR>= <value>`
- if `<VAR>` does not exist, create a new top-level `<VAR>= <value>` assignment
  before the first target or `.include`, whichever appears first
- if `<VAR>` exists more than once, fail with an ambiguous-match error

`mk bump` v1 behavior (`PORTREVISION` only; `<N>` is an integer from 1 to 99,
default 1; `on-missing` is not allowed). The composed value is the build
line's `PORTREVISION` as it stands when the op runs, plus `<N>`, so no number
is pinned: compose applies the op to a fresh upstream copy every time, and a
later upstream bump is kept. Checks, in this order:

- no `Makefile`: fail
- a slave port (`MASTERDIR=` at top level): refused, its `PORTREVISION` comes
  from or is overridden by its master; bump the master
- more than one assignment: fail with an ambiguous-match error
- one assignment inside a conditional: refused
- one assignment with an operator other than `=` or `?=`: refused
- one assignment whose value, after stripping a trailing `#` comment and
  whitespace, is not an integer: refused
- one assignment: rewritten as `PORTREVISION= <v+N>`, or
  `PORTREVISION?= <v+N>` when it used `?=`
- no assignment and any quoted `.include`: refused, the included file may set
  it
- no assignment: `PORTREVISION= <N>` is inserted where `mk set` would insert
  it

A refusal is `E_APPLY_BUMP_UNSUPPORTED` with the reason; nothing is skipped
silently.

Bumps on one build line add up in apply order (@any first, then the line's
own); a bump after a mk set adds to that value.

`mk eval` renders an immediate `<VAR>:= <value>` line appended before the last
`.include`. It is the faithful op for an immediate (`:=`) source assignment —
including any self-referential value (`${VAR:mod}`), where `mk set`'s recursive
`=` would be fatal. `on-missing` is not allowed (it always appends).

`mk shell` renders a shell `<VAR>!= <value>` line appended before the last
`.include` — the faithful op for a `!=` (command-substitution) assignment.
`mk set` (`=`) and `mk eval` (`:=`) do not run a shell; only `mk shell` does.
`on-missing` is not allowed (it always appends).

`mk remove` requires an existing assignment. `mk add` creates one,
`<VAR>= <token>`, when the variable is absent.

Operator mapping (source assignment → op): `=` → `mk set`; `:=` → `mk eval`;
`!=` → `mk shell`; `+=` → `mk add`. `?=` (conditional default) has no faithful
op — it must be escalated, never rendered as `mk set` (which would override an
upstream value the default was meant to defer to).

### Makefile conditional/block ops

```text
mk disable-if condition "<expr>" [contains "<anchor>"] [on-missing ...]
mk replace-if from "<expr>" to "<expr>" [contains "<anchor>"] [on-missing ...]

mk block set condition "<expr>" [contains "<anchor>"] <<'MK'
	<block line>
MK
```

`mk block set` v1 behavior (normative):

- matches and rewrites only `.if ... .endif` regions whose `.if` condition equals
  `<expr>` (not `.elif`-only matches)
- `contains` filters candidate regions by block text substring
- one match: replace full region body
- no match: insert new `.if ... .endif` block before the last `.include` line
  when present, else append at EOF
- multiple matches: fail with ambiguous-match error
- `on-missing` is not allowed on `mk block set`

### Makefile target/recipe ops

```text
mk target set <name> <<'MK'
	<recipe line>
MK

mk target append <name> <<'MK'
	<recipe line>
MK

mk target remove <name> [on-missing ...]
mk target rename <old> -> <new> [on-missing ...]
```

`mk target set` v1 behavior:

- if `<name>` exists exactly once, replace that target block
- if `<name>` does not exist, insert a new target block before the last
  `.include` line when present, else append at EOF
- if `<name>` exists more than once, fail with an ambiguous-match error

### Makefile include ops

```text
mk ensure-include <name>
```

`mk ensure-include` idempotently ensures `.include <<name>>` is present,
inserted before the last `.include` line (no-op if already present). Its use:
give a terminal-only port (one that only has `.include <bsd.port.mk>`) the
framework-var-defining `bsd.port.options.mk` *before* a `${DFLYVERSION}`/
`${OPSYS}` conditional, so the `.if` resolves instead of failing with
`Variable "DFLYVERSION" is undefined`. Emit it before the conditional;
placement of the conditional then follows from operation order.

### File/text ops

```text
file copy <src> -> <dst>
file materialize <src> -> <dst>
file remove <path> [on-missing ...]

text line-remove file <path> exact "<line>" [on-missing ...]
text line-insert-after file <path> anchor "<line>" line "<line>" [on-missing ...]
text replace-once file <path> from "<needle>" to "<replacement>" [on-missing ...]
```

### Patch fallback

```text
patch apply <path>
```

Path resolution semantics:

- `file copy`: `<src>` and `<dst>` are resolved relative to the port root
- `file materialize`: `<src>` is resolved relative to the `overlay.dops`
  directory (source root), `<dst>` is resolved relative to the port root
- `patch apply <path>` reads the patch file from the source root and applies
  it to the port root
- `file remove`, `text ... file <path>` and `mk ... file <path>` resolve
  paths relative to the port root
- absolute paths and paths escaping the active root are invalid
- `file materialize` does not support wildcard source patterns in v1

`patch apply` applies immediately to the port root tree; it does not register a
build-time patch asset lane.

---

## Determinism and Failure Policy

Defaults:

- ambiguous match: `error`
- parse failure in targeted input: `error`
- missing subject with no override: `error`

`on-missing` values:

- `error`: fail operation
- `warn`: emit warning and continue
- `noop`: skip silently (discouraged outside migration)

Determinism:

- operations apply in source order after target scoping is resolved
- multi-selector target directives preserve selector order during scoped-op
  expansion (for example `@2026Q1,@2026Q2` expands in that order)
- reapplying the same plan must be idempotent

---

## Compile Mapping to Normalized In-Memory Plan

- each DSL operation compiles to one normalized operation record
- operations before first explicit `target` compile with target `@any`
- `target` selector lists map to per-operation target expansion in selector
  order
- mapping of forms to operation kinds:
  - `mk set` -> `mk.var.set`
  - `mk eval` -> `mk.var.eval`
  - `mk shell` -> `mk.var.shell`
  - `mk unset` -> `mk.var.unset`
  - `mk add` -> `mk.var.token_add`
  - `mk remove` -> `mk.var.token_remove`
  - `mk bump` -> `mk.var.bump`
  - `mk disable-if` -> `mk.block.disable`
  - `mk replace-if` -> `mk.block.replace_condition`
  - `mk block set` -> `mk.block.set`
  - `mk ensure-include` -> `mk.include.ensure`
  - `mk target set|append|remove|rename` -> `mk.target.*`
  - `file copy|materialize|remove` -> `file.copy|file.materialize|file.remove`
  - `text ...` -> `text.*`
  - `patch apply` -> `patch.apply`
- directive mapping:
  - `type` -> `plan.type`
  - `reason` -> `plan.reason`
  - `maintainer` -> `plan.maintainer`
  - `port` -> `plan.port`

---

## Conformance Anchors (Implementation References)

Normative behavior in this document is implemented and regression-tested in:

- parser: `scripts/generator/dportsv3/engine/parser.py`
- semantic scope analysis: `scripts/generator/dportsv3/engine/semantic.py`
- plan compilation: `scripts/generator/dportsv3/engine/planner.py`
- apply target ordering/skipping: `scripts/generator/dportsv3/engine/apply.py`
- tests: `scripts/generator/tests/test_dportsv3_parser.py`
- tests: `scripts/generator/tests/test_dportsv3_semantic.py`
- tests: `scripts/generator/tests/test_dportsv3_planner.py`
- tests: `scripts/generator/tests/test_dportsv3_apply.py`

---

## Diagnostics Contract

`dportsv3 dsl parse|check|plan` emit structured diagnostics with:

- `severity` (`error|warning|info`)
- `code` (stable machine-readable identifier)
- `message`
- source location (`path`, optional `line` and `column`)

Reserved baseline codes:

- `E_PARSE_*`: lexical/syntax failures
- `E_SEM_*`: semantic validation failures
- `E_PLAN_*`: plan construction failures
- `W_*`: non-fatal warnings

Bootstrap compatibility code:

- `E_NOT_IMPLEMENTED`: temporary stub response during engine bring-up

---

## Conformance Examples

### Valid

```text
port security/dsniff
type port
reason "DragonFly-specific adjustments"

mk add USES ssl
mk set BROKEN_DragonFly "baseline applies to all targets"

target @2025Q2
mk remove USES linux on-missing warn
mk set BROKEN_DragonFly "fails with old SSL API"

mk target set dfly-patch <<'MK'
	${REINPLACE_CMD} -e 's/foo/bar/' ${WRKSRC}/file
MK

patch apply dragonfly/@2025Q2/patch-src_main.c
```

### Valid (multi-selector target scope)

```text
target @2026Q1,@2026Q2
port category/name
mk set VAR "shared-quarter-change"
```

### Valid (per-target override)

```text
port category/name
file materialize dragonfly/patch-a -> dragonfly/patch-a

target @main
file materialize dragonfly/@main/patch-a -> dragonfly/patch-a
```

The payload folder says the same with no operation, and is how DeltaPorts
writes it: `dragonfly/patch-a` is installed on every build line, and
`dragonfly/@main/patch-a` replaces it on `@main`. An operation is only needed
for what the folder cannot say, such as a destination outside `dragonfly/`.

### Invalid (missing `port`)

```text
target @main
mk set BROKEN "missing origin"
```

### Invalid (mixed `@any` with explicit target)

```text
target @any,@2026Q1
port category/name
mk add USES ssl
```

### Invalid (bad target)

```text
target @2025Q5
port category/name
```

### Invalid (selector list with spaces)

```text
target @main, @2026Q1
port category/name
```

### Invalid (repeated selector)

```text
target @main,@main
port category/name
```

### Invalid (dead operation)

```text
port category/name
file materialize dragonfly/patch-a -> dragonfly/patch-a
file materialize dragonfly/patch-b -> dragonfly/patch-a
```
