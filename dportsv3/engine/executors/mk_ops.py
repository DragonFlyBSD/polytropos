"""Makefile operation executors."""

from __future__ import annotations

import posixpath
from pathlib import Path

from dportsv3.engine.apply_common import (
    _failed_row,
    _load_makefile,
    _missing_row,
    _on_missing,
    _replace_line_range,
    _resolve_path,
    _success_row,
    _target_block_span,
)
from dportsv3.engine.fsops import FileTransaction
from dportsv3.engine.makefile_cst import (
    AssignmentNode,
    DirectiveElifNode,
    DirectiveEndifNode,
    DirectiveIfNode,
    IncludeNode,
    TargetNode,
)
from dportsv3.engine.makefile_rewrite import (
    find_condition,
    set_var,
    token_add,
    token_remove,
    target_append,
    target_remove,
    target_rename,
    target_set,
    unset_var,
)
from dportsv3.engine.models import ApplyContext, ApplyOpResult, PlanOp


def _mk_file(op: PlanOp) -> str | None:
    """The file an op names with ``file <path>``; None is the Makefile."""
    value = op.payload.get("file")
    return value if isinstance(value, str) else None


def _no_file(op: PlanOp) -> str:
    return f"{_mk_file(op) or 'Makefile'} does not exist"


def _in_included_file(op: PlanOp) -> bool:
    """Whether the op edits a file other than the port's Makefile."""
    name = _mk_file(op)
    return name is not None and posixpath.normpath(name) != "Makefile"


#: Operators that replace whatever was assigned before them. ``+=`` adds to
#: it and ``?=`` keeps it, so neither undoes an op's line.
_REPLACING_OPERATORS = ("=", ":=", "!=")


def _included_path(
    port_root: Path, including_dir: Path, include: str
) -> Path | None:
    """The file a quoted ``.include`` names, when it is inside the port root.

    ``${.CURDIR}`` is the port root, ``${.PARSEDIR}`` the including file's
    directory, ``${.CURDIR:H}``, ``${.CURDIR:H:H}`` and ``${PORTSDIR}`` its
    parents in the composed tree, and a bare relative path is taken from
    the including file's directory, where bmake looks first. Anything else
    -- a framework ``<...>`` include, another variable, a file outside the
    port root -- is None: not followed.
    """
    if len(include) < 2 or not (include[0] == include[-1] == '"'):
        return None
    spec = include[1:-1].strip()
    for var, value in (
        ("${.CURDIR:H:H}", port_root.parent.parent),
        ("${.CURDIR:H}", port_root.parent),
        ("${.CURDIR}", port_root),
        ("${.PARSEDIR}", including_dir),
        ("${PORTSDIR}", port_root.parent.parent),
    ):
        spec = spec.replace(var, str(value))
    if "$" in spec:
        return None
    resolved = (including_dir / spec).resolve()
    try:
        resolved.relative_to(port_root.resolve())
    except ValueError:
        return None
    return resolved


def _reassigned_by_include(
    txn: FileTransaction,
    port_root: Path,
    path: Path,
    document,
    name: str,
    after_line: int,
    seen: set[Path],
) -> tuple[Path, int, str] | None:
    """Where a file included after ``after_line`` of ``path`` replaces
    ``name``: (that file, its line, the operator), or None.

    Only top-level includes and assignments count: one inside ``.if``
    replaces the value only when the condition holds, which apply cannot
    tell. Each included file is read whole, with its own includes, since
    all of it comes after the line.
    """
    depth = 0
    for node in document.nodes:
        if isinstance(node, DirectiveIfNode):
            depth += 1
        elif isinstance(node, DirectiveEndifNode):
            depth = max(0, depth - 1)
        elif depth or node.span.line_start <= after_line:
            continue
        elif isinstance(node, AssignmentNode):
            if (
                path not in seen
                and node.name == name
                and node.operator in _REPLACING_OPERATORS
            ):
                return path, node.span.line_start, node.operator
        elif isinstance(node, IncludeNode):
            included = _included_path(port_root, path.parent, node.include)
            if included is None or included in seen or included == path:
                continue
            try:
                loaded = _load_makefile(txn, included)
            except ValueError:
                loaded = None
            if loaded is None:
                continue
            hit = _reassigned_by_include(
                txn, port_root, included, loaded[1], name, 0, seen | {path}
            )
            if hit is not None:
                return hit
    return None


def _overridden_row(
    op: PlanOp,
    context: ApplyContext,
    txn: FileTransaction,
    path: Path,
    document,
    name: str,
    after_line: int,
) -> ApplyOpResult | None:
    """A failed row when a file included after the line this op writes
    assigns ``name`` again, so the op's value never reaches the build.

    Apply would otherwise report success for an op that does nothing:
    multimedia/avidemux's ``mk add USES "alias"`` became ``USES= alias``
    above ``.include "${.CURDIR}/Makefile.common"``, whose ``USES=``
    replaced it (poly-bz7n.2). The cure is to edit the variable where it
    is set, with ``file <that file>``.
    """
    hit = _reassigned_by_include(
        txn, context.port_root.resolve(), path, document, name, after_line,
        {path},
    )
    if hit is None:
        return None
    where, line, operator = hit
    rel = where.relative_to(context.port_root.resolve()).as_posix()
    return _failed_row(
        op,
        code="E_APPLY_MK_OVERRIDDEN",
        message=(
            f"{name}{operator} in {rel}:{line} replaces what this op writes: "
            f"{rel} is included after it, so the op's value never reaches "
            f"the build. Edit {name} where it is set: add 'file {rel}' to "
            "the op"
        ),
        source_path=path,
    )


def _written_after(text: str, document, node_index: int | None,
                   insert_before: int | None) -> int:
    """The last original line before the one an op writes: the node it
    rewrites, or the line it is inserted above, or the end of the file."""
    if node_index is not None:
        return document.nodes[node_index].span.line_end
    if insert_before is None:
        return len(text.splitlines())
    return insert_before - 1


def _mk_var_set_insert_line(document) -> int | None:
    for node in document.nodes:
        if isinstance(node, (TargetNode, IncludeNode)):
            return node.span.line_start
    return None


def _insert_before_last_include_line(document) -> int | None:
    insert_before: int | None = None
    for node in document.nodes:
        if isinstance(node, IncludeNode):
            insert_before = node.span.line_start
    return insert_before


#: Framework includes that compute ``PORT_OPTIONS`` from the ``OPTIONS_*``
#: inputs. ``bsd.port.pre.mk`` pulls in ``bsd.port.options.mk``, so either
#: one closes the window.
_OPTIONS_COMPUTING_INCLUDES = ("bsd.port.options.mk", "bsd.port.pre.mk")


def _first_options_include_line(document) -> int | None:
    """Line of the first *unconditional* include that computes options.

    None means there is nothing to insert before: either the port is
    *terminal-only* — its only framework include is the trailing
    ``bsd.port.mk`` — or every options include it has sits inside a
    conditional.

    Conditional ones are skipped deliberately. Inserting immediately
    before an include nested in an ``.if`` would put the assignment
    inside that block, so it would apply only when the condition held —
    a different bug from the one being fixed, and just as quiet.
    Measured across the composed 2026Q3 tree: 2,523 of 2,539 ports with
    an options include have it at top level, 16 do not, and none of
    those 16 carries an ``mk eval OPTIONS*`` overlay. So this costs
    nothing today and keeps the change from ever making a port worse
    than it already is — those 16 stay as poly-cpd found them.
    """
    depth = 0
    for node in document.nodes:
        if isinstance(node, DirectiveIfNode):
            depth += 1
        elif isinstance(node, DirectiveEndifNode):
            depth = max(0, depth - 1)
        elif (
            depth == 0
            and isinstance(node, IncludeNode)
            and any(n in node.include for n in _OPTIONS_COMPUTING_INCLUDES)
        ):
            return node.span.line_start
    return None


def _eval_insert_line(document, name: str) -> int | None:
    """Where a ``mk eval`` assignment to ``name`` has to land (poly-cpd).

    Appending at the end of the body is right for a terminal-only port:
    the line still precedes ``.include <bsd.port.mk>``, so options are
    computed afterwards and the assignment counts.

    It is wrong for a *sandwiched* port, one that includes
    ``bsd.port.options.mk`` or ``bsd.port.pre.mk`` partway down.
    ``PORT_OPTIONS`` is derived at the point of that include, so an
    ``OPTIONS_*`` assignment appended after it cannot affect a value
    already computed — and it fails silently, which is how ~79 overlays
    came to carry option filtering that does nothing.

    Confirmed in the dsynth builder on devel/libunwind, where the
    overlay's ``mk eval OPTIONS_DEFINE`` landed after the include and
    ``Mk/bsd.sanity.mk`` reported the option's own helpers as
    "incorrectly set after bsd.port.options.mk and are ineffective".

    Only ``OPTIONS_*`` names move. Those are the documented inputs to
    that include, so for them the current placement is provably wrong.
    Every other ``mk eval`` keeps today's position, because an
    assignment can legitimately *want* to read what the framework
    computed — moving those would break the ones that work today.
    """
    if name.startswith("OPTIONS_"):
        options_line = _first_options_include_line(document)
        if options_line is not None:
            return options_line
    return _insert_before_last_include_line(document)


def _logical_tokens(value: str) -> list[str]:
    """Split an assignment value into tokens, treating ``\\`` line
    continuations as joins rather than literal tokens.

    A CST ``AssignmentNode`` value for a *continued* assignment carries the
    physical lines joined by ``\\n`` with each non-final line still ending in
    ``\\``. A naive ``value.split()`` turns those backslashes (and any blank
    continuation lines) into bogus tokens, which then corrupt the value when
    the assignment is rewritten flat. Strip the trailing ``\\`` per physical
    line before tokenizing.
    """
    tokens: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.endswith("\\"):
            stripped = stripped[:-1]
        tokens.extend(stripped.split())
    return tokens


def exec_mk_var_set(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    name = op.payload.get("name")
    value = op.payload.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.var.set requires name and value",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = set_var(document, name, value)
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple assignments found for {name}",
            source_path=path,
        )

    node_index = intent.node_indices[0] if intent.node_indices else None
    overridden = _overridden_row(
        op, context, txn, path, document, name,
        _written_after(
            text, document, node_index, _mk_var_set_insert_line(document)
        ),
    )
    if overridden is not None:
        return overridden
    updated = _write_assignment(text, document, node_index, f"{name}= {value}")
    txn.stage_write(path, updated)
    return _success_row(op, "mk-var-set")


def _write_assignment(
    text: str, document, node_index: int | None, line: str
) -> str:
    """Write one assignment ``line`` into the Makefile ``text``.

    It replaces node ``node_index``'s line range; with no node it goes
    before the first target or ``.include``, else at the end of the file.
    mk set and mk bump both write through here, so a bump of an absent
    PORTREVISION lands exactly where mk set would put it.
    """
    if node_index is not None:
        node = document.nodes[node_index]
        return _replace_line_range(
            text,
            start=node.span.line_start,
            end=node.span.line_end,
            new_lines=[line],
        )
    insert_before = _mk_var_set_insert_line(document)
    if insert_before is None:
        line_count = len(text.splitlines(keepends=False))
        return _replace_line_range(
            text,
            start=line_count + 1,
            end=line_count,
            new_lines=[line],
        )
    return _replace_line_range(
        text,
        start=insert_before,
        end=insert_before - 1,
        new_lines=[line],
    )


def _bump_unsupported(op: PlanOp, message: str, path) -> ApplyOpResult:
    return _failed_row(
        op, code="E_APPLY_BUMP_UNSUPPORTED", message=message, source_path=path
    )


def exec_mk_var_bump(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    """``mk bump PORTREVISION [by N]``: the line's value as it stands, plus N.

    Relative, so it never pins a number: compose applies the op to a
    fresh upstream copy every time, so the bump computes from upstream's
    value on every run and cannot walk upward. Every Makefile shape a
    lexical bump would get wrong is refused with the reason, never
    skipped: a slave port, more than one assignment, one inside a
    conditional, an operator other than = or ?=, a value that is not an
    integer, and no assignment where a quoted .include may set it.
    """
    name = op.payload.get("name")
    by = op.payload.get("by")
    if not isinstance(name, str) or not isinstance(by, int):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.var.bump requires name and by",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op,
            policy="error",
            message=_no_file(op),
            source_path=path,
        )

    text, document = loaded
    depth = 0
    slave = False
    found: list[tuple[int, AssignmentNode, bool]] = []
    first_quoted_include: str | None = None
    for index, node in enumerate(document.nodes):
        if isinstance(node, DirectiveIfNode):
            depth += 1
        elif isinstance(node, DirectiveEndifNode):
            depth -= 1
        elif isinstance(node, AssignmentNode):
            if node.name == "MASTERDIR" and depth == 0:
                slave = True
            if node.name == name:
                found.append((index, node, depth > 0))
        elif isinstance(node, IncludeNode):
            if first_quoted_include is None and node.include.startswith('"'):
                first_quoted_include = node.include

    if slave:
        return _bump_unsupported(
            op,
            f"slave port: {name} comes from or is overridden by its master; "
            "bump the master",
            path,
        )
    if len(found) > 1:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple assignments found for {name}",
            source_path=path,
        )
    if found:
        index, assignment, conditional = found[0]
        if conditional:
            return _bump_unsupported(
                op, f"{name} is assigned inside a conditional", path
            )
        if assignment.operator not in ("=", "?="):
            return _bump_unsupported(
                op,
                f"{name} uses {assignment.operator}; only = and ?= can be "
                "bumped",
                path,
            )
        current = assignment.value.split("#", 1)[0].strip()
        if not (current.isascii() and current.isdigit()):
            return _bump_unsupported(
                op, f"{name} is '{current}', not an integer", path
            )
        overridden = _overridden_row(
            op, context, txn, path, document, name,
            assignment.span.line_end,
        )
        if overridden is not None:
            return overridden
        updated = _write_assignment(
            text,
            document,
            index,
            f"{name}{assignment.operator} {int(current) + by}",
        )
    elif first_quoted_include is not None:
        return _bump_unsupported(
            op,
            f"{name} may be set by an included file ({first_quoted_include})",
            path,
        )
    else:
        updated = _write_assignment(text, document, None, f"{name}= {by}")

    txn.stage_write(path, updated)
    return _success_row(op, "mk-var-bump")


def exec_mk_var_eval(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    # Insert a verbatim immediate (`:=`) assignment into the port Makefile
    # body, reproducing a trailing Makefile.DragonFly fragment — at the end
    # for a terminal-only port, and before the options-computing include for
    # an OPTIONS_* name in a sandwiched one (see _eval_insert_line). This
    # is the faithful representation of a self-referential assignment
    # (`VAR:= ${VAR:S/a/b/}`, `${VAR:N...}`, prepend, etc.): immediate
    # expansion sees the fully-accumulated upstream value, and unlike `mk set`
    # it never rewrites the upstream line (which would make `=` recursive).
    # The op TYPE declares the intent — the executor does not inspect value.
    name = op.payload.get("name")
    value = op.payload.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.var.eval requires name and value",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op,
            policy=_on_missing(op),
            message=_no_file(op),
            source_path=path,
        )

    text, document = loaded
    replacement = f"{name}:= {value}"
    insert_before = _eval_insert_line(document, name)
    overridden = _overridden_row(
        op, context, txn, path, document, name,
        _written_after(text, document, None, insert_before),
    )
    if overridden is not None:
        return overridden
    if insert_before is None:
        line_count = len(text.splitlines(keepends=False))
        updated = _replace_line_range(
            text, start=line_count + 1, end=line_count, new_lines=[replacement]
        )
    else:
        updated = _replace_line_range(
            text, start=insert_before, end=insert_before - 1, new_lines=[replacement]
        )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-var-eval")


def exec_mk_var_shell(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    # Append a verbatim shell (`!=`) assignment to the port Makefile body,
    # reproducing a `VAR!= command` line. `!=` runs the command at parse time
    # and assigns its output — distinct from both `mk set` (`=`, recursive)
    # and `mk eval` (`:=`, immediate make-expansion, no shell). The op TYPE
    # declares the intent; the executor renders the value verbatim and never
    # rewrites the upstream line. Placement is before the last include, the
    # faithful spot for a Makefile.DragonFly-injected assignment. This no
    # longer mirrors `mk eval`, which moves an OPTIONS_* name ahead of the
    # options include (poly-cpd); a `!=` assigning an OPTIONS_* variable
    # would want the same treatment, but no overlay does that today and an
    # untested path is worse than a documented gap.
    name = op.payload.get("name")
    value = op.payload.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.var.shell requires name and value",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op,
            policy=_on_missing(op),
            message=_no_file(op),
            source_path=path,
        )

    text, document = loaded
    replacement = f"{name}!= {value}"
    insert_before = _insert_before_last_include_line(document)
    overridden = _overridden_row(
        op, context, txn, path, document, name,
        _written_after(text, document, None, insert_before),
    )
    if overridden is not None:
        return overridden
    if insert_before is None:
        line_count = len(text.splitlines(keepends=False))
        updated = _replace_line_range(
            text, start=line_count + 1, end=line_count, new_lines=[replacement]
        )
    else:
        updated = _replace_line_range(
            text, start=insert_before, end=insert_before - 1, new_lines=[replacement]
        )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-var-shell")


def exec_mk_ensure_include(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    # Idempotently ensure the port Makefile carries `.include <NAME>`, inserted
    # before the terminal include. Used to give a terminal-only port the
    # framework-var-defining `bsd.port.options.mk` so a following
    # `${DFLYVERSION}`/`${OPSYS}` conditional resolves. Dumb by design — it
    # matches the exact include name; the author only emits it where needed
    # (pre-sandwiched ports already define the vars and don't get it).
    name = op.payload.get("name")
    if not isinstance(name, str) or not name:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.include.ensure requires an include name",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op,
            policy=_on_missing(op),
            message=_no_file(op),
            source_path=path,
        )

    text, document = loaded
    for node in document.nodes:
        if isinstance(node, IncludeNode) and name in node.include:
            return _success_row(op, "mk-include-present")

    directive = f".include <{name}>"
    insert_before = _insert_before_last_include_line(document)
    if insert_before is None:
        updated = text
        if updated and not updated.endswith(("\n", "\r\n")):
            updated += "\n"
        updated += directive + "\n"
    else:
        updated = _replace_line_range(
            text, start=insert_before, end=insert_before - 1, new_lines=[directive]
        )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-include-ensured")


def exec_mk_var_unset(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    name = op.payload.get("name")
    if not isinstance(name, str):
        return _failed_row(
            op, code="E_APPLY_INVALID_OPERATION", message="mk.var.unset requires name"
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = unset_var(document, name)
    if not intent.node_indices:
        return _missing_row(
            op, policy=policy, message=f"assignment not found: {name}", source_path=path
        )
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple assignments found for {name}",
            source_path=path,
        )

    node = document.nodes[intent.node_indices[0]]
    updated = _replace_line_range(
        text, start=node.span.line_start, end=node.span.line_end, new_lines=[]
    )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-var-unset")


def exec_mk_var_token_add(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    name = op.payload.get("name")
    value = op.payload.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.var.token_add requires name and value",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = token_add(document, name, value)

    if not intent.node_indices:
        # No existing assignment. Mirror make's `+=` semantics:
        # `VAR+= token` on an undefined VAR is equivalent to
        # `VAR= token` (creates the assignment with the token as
        # its sole value). A stricter refusal would force every
        # translator (LLM convert agent, deterministic
        # migration/convert.py) to track upstream-variable presence
        # before deciding between `mk add` and `mk set` — a
        # distinction make itself doesn't require, and one that
        # neither translator can answer reliably without an extra
        # read. Matches the existing exec_mk_var_set "no prior
        # assignment" code path so set/add stay symmetric.
        #
        # Not in an included file (``file <path>``): the file that
        # includes it may have assigned VAR before the include, and `=`
        # there would throw that away. `+=` is what add means anywhere.
        operator = "+=" if _in_included_file(op) else "="
        replacement = f"{name}{operator} {value}"
        insert_before = _mk_var_set_insert_line(document)
        overridden = _overridden_row(
            op, context, txn, path, document, name,
            _written_after(text, document, None, insert_before),
        )
        if overridden is not None:
            return overridden
        if insert_before is None:
            line_count = len(text.splitlines(keepends=False))
            updated = _replace_line_range(
                text,
                start=line_count + 1,
                end=line_count,
                new_lines=[replacement],
            )
        else:
            updated = _replace_line_range(
                text,
                start=insert_before,
                end=insert_before - 1,
                new_lines=[replacement],
            )
        txn.stage_write(path, updated)
        return _success_row(op, "mk-token-created")

    # In-place token append is only safe when there's exactly one
    # match AND the assignment is not continued (single physical line).
    # Rewriting a continued assignment as one flat line would either
    # (a) embed the original `\` line-continuations as literal tokens,
    # corrupting the value, or (b) require us to reconstruct the
    # continuation cosmetics, which we don't try to do.
    if len(intent.node_indices) == 1:
        node = document.nodes[intent.node_indices[0]]
        if not node.continued:
            tokens = [tok for tok in node.value.split() if tok]
            if value in tokens:
                return _success_row(op, "mk-token-exists")
            overridden = _overridden_row(
                op, context, txn, path, document, name, node.span.line_end,
            )
            if overridden is not None:
                return overridden
            tokens.append(value)
            # Preserve the original operator (`=`, `+=`, `?=`, `:=`,
            # `!=`). Flattening to `=` would silently change semantics
            # when the original assignment is inside an `.if` block or
            # relies on `?=` deferred-default behavior.
            replacement = f"{node.name}{node.operator} {' '.join(tokens)}"
            updated = _replace_line_range(
                text,
                start=node.span.line_start,
                end=node.span.line_end,
                new_lines=[replacement],
            )
            txn.stage_write(path, updated)
            return _success_row(op, "mk-token-added")

    # Fall through: continued single assignment, or multiple
    # assignments. Don't try to rewrite an existing line — instead
    # leave all matches intact and append a new `+=` line, which
    # `make` flattens into the accumulated value at evaluation time.
    for idx in intent.node_indices:
        existing = document.nodes[idx]
        if value in existing.value.split():
            return _success_row(op, "mk-token-exists")

    replacement = f"{name}+= {value}"
    insert_before = _mk_var_set_insert_line(document)
    overridden = _overridden_row(
        op, context, txn, path, document, name,
        _written_after(text, document, None, insert_before),
    )
    if overridden is not None:
        return overridden
    if insert_before is None:
        line_count = len(text.splitlines(keepends=False))
        updated = _replace_line_range(
            text,
            start=line_count + 1,
            end=line_count,
            new_lines=[replacement],
        )
    else:
        updated = _replace_line_range(
            text,
            start=insert_before,
            end=insert_before - 1,
            new_lines=[replacement],
        )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-token-added")


def exec_mk_var_token_remove(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    name = op.payload.get("name")
    value = op.payload.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.var.token_remove requires name and value",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = token_remove(document, name, value)
    if not intent.node_indices:
        return _missing_row(
            op, policy=policy, message=f"assignment not found: {name}", source_path=path
        )
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple assignments found for {name}",
            source_path=path,
        )

    node = document.nodes[intent.node_indices[0]]
    # Continuation-aware: a continued assignment's value embeds `\` markers
    # that a plain .split() would turn into literal tokens (corrupting the
    # rewrite). Preserve the original operator rather than forcing `=`.
    tokens = _logical_tokens(node.value)
    if value not in tokens:
        return _missing_row(
            op, policy=policy, message=f"token not found: {value}", source_path=path
        )
    tokens = [tok for tok in tokens if tok != value]
    replacement = f"{node.name}{node.operator} {' '.join(tokens)}" if tokens else ""
    new_lines = [replacement] if replacement else []
    updated = _replace_line_range(
        text,
        start=node.span.line_start,
        end=node.span.line_end,
        new_lines=new_lines,
    )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-token-removed")


def exec_mk_target_set(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    name = op.payload.get("name")
    recipe = op.payload.get("recipe")
    if not isinstance(name, str) or not isinstance(recipe, list):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.target.set requires name and recipe",
        )
    if not all(isinstance(line, str) for line in recipe):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.target.set recipe must be list[str]",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _failed_row(
            op,
            code="E_APPLY_MISSING_SUBJECT",
            message=_no_file(op),
            source_path=path,
        )

    text, document = loaded
    intent = target_set(document, name, recipe)
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple targets found: {name}",
            source_path=path,
        )

    lines = [f"{name}:", *recipe]
    if intent.node_indices:
        start, end = _target_block_span(document, intent.node_indices[0])
        updated = _replace_line_range(text, start=start, end=end, new_lines=lines)
    else:
        insert_before_line = _insert_before_last_include_line(document)
        if insert_before_line is None:
            updated = text
            if updated and not updated.endswith(("\n", "\r\n")):
                updated += "\n"
            updated += "\n".join(lines) + "\n"
        else:
            updated = _replace_line_range(
                text,
                start=insert_before_line,
                end=insert_before_line - 1,
                new_lines=[*lines, ""],
            )

    txn.stage_write(path, updated)
    return _success_row(op, "mk-target-set")


def exec_mk_target_append(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    name = op.payload.get("name")
    recipe = op.payload.get("recipe")
    if not isinstance(name, str) or not isinstance(recipe, list):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.target.append requires name and recipe",
        )
    if not all(isinstance(line, str) for line in recipe):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.target.append recipe must be list[str]",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _failed_row(
            op,
            code="E_APPLY_MISSING_SUBJECT",
            message=_no_file(op),
            source_path=path,
        )

    text, document = loaded
    intent = target_append(document, name, recipe)
    if not intent.node_indices:
        return _failed_row(
            op,
            code="E_APPLY_MISSING_SUBJECT",
            message=f"target not found: {name}",
            source_path=path,
        )
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple targets found: {name}",
            source_path=path,
        )

    start, end = _target_block_span(document, intent.node_indices[0])
    block_lines = text.splitlines(keepends=False)[start - 1 : end]
    block_lines.extend(recipe)
    updated = _replace_line_range(text, start=start, end=end, new_lines=block_lines)
    txn.stage_write(path, updated)
    return _success_row(op, "mk-target-appended")


def exec_mk_target_remove(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    name = op.payload.get("name")
    if not isinstance(name, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.target.remove requires name",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = target_remove(document, name)
    if not intent.node_indices:
        return _missing_row(
            op, policy=policy, message=f"target not found: {name}", source_path=path
        )
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple targets found: {name}",
            source_path=path,
        )

    start, end = _target_block_span(document, intent.node_indices[0])
    updated = _replace_line_range(text, start=start, end=end, new_lines=[])
    txn.stage_write(path, updated)
    return _success_row(op, "mk-target-removed")


def exec_mk_target_rename(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    old = op.payload.get("old")
    new = op.payload.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.target.rename requires old and new",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = target_rename(document, old, new)
    if not intent.node_indices:
        return _missing_row(
            op, policy=policy, message=f"target not found: {old}", source_path=path
        )
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple targets found: {old}",
            source_path=path,
        )

    node = document.nodes[intent.node_indices[0]]
    replacement = f"{new}:"
    updated = _replace_line_range(
        text,
        start=node.span.line_start,
        end=node.span.line_start,
        new_lines=[replacement],
    )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-target-renamed")


def exec_mk_block_disable(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    condition = op.payload.get("condition")
    contains = op.payload.get("contains")
    if not isinstance(condition, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.block.disable requires condition",
        )
    if contains is not None and not isinstance(contains, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.block.disable contains must be string",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = find_condition(document, expr=condition, contains=contains)
    if not intent.node_indices:
        return _missing_row(
            op,
            policy=policy,
            message=f"condition not found: {condition}",
            source_path=path,
        )
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple conditions found: {condition}",
            source_path=path,
        )

    node = document.nodes[intent.node_indices[0]]
    if not isinstance(node, (DirectiveIfNode, DirectiveElifNode)):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="condition node is not .if/.elif",
            source_path=path,
        )

    original_line = text.splitlines(keepends=False)[node.span.line_start - 1]
    replacement = f"# {original_line}" if original_line else f"# {condition}"
    updated = _replace_line_range(
        text,
        start=node.span.line_start,
        end=node.span.line_end,
        new_lines=[replacement],
    )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-block-disabled")


def exec_mk_block_replace_condition(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    from_condition = op.payload.get("from")
    to_condition = op.payload.get("to")
    contains = op.payload.get("contains")
    if not isinstance(from_condition, str) or not isinstance(to_condition, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.block.replace_condition requires from and to",
        )
    if contains is not None and not isinstance(contains, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.block.replace_condition contains must be string",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    policy = _on_missing(op)
    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _missing_row(
            op, policy=policy, message=_no_file(op), source_path=path
        )

    text, document = loaded
    intent = find_condition(document, expr=from_condition, contains=contains)
    if not intent.node_indices:
        return _missing_row(
            op,
            policy=policy,
            message=f"condition not found: {from_condition}",
            source_path=path,
        )
    if intent.ambiguous:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple conditions found: {from_condition}",
            source_path=path,
        )

    node = document.nodes[intent.node_indices[0]]
    if isinstance(node, DirectiveIfNode):
        replacement = f".if {to_condition}"
    elif isinstance(node, DirectiveElifNode):
        replacement = f".elif {to_condition}"
    else:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="condition node is not .if/.elif",
            source_path=path,
        )

    updated = _replace_line_range(
        text,
        start=node.span.line_start,
        end=node.span.line_end,
        new_lines=[replacement],
    )
    txn.stage_write(path, updated)
    return _success_row(op, "mk-condition-replaced")


def exec_mk_block_set(
    op: PlanOp, context: ApplyContext, txn: FileTransaction
) -> ApplyOpResult:
    condition = op.payload.get("condition")
    recipe = op.payload.get("recipe")
    contains = op.payload.get("contains")
    if not isinstance(condition, str) or not isinstance(recipe, list):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.block.set requires condition and recipe",
        )
    if contains is not None and not isinstance(contains, str):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.block.set contains must be string",
        )
    if not all(isinstance(line, str) for line in recipe):
        return _failed_row(
            op,
            code="E_APPLY_INVALID_OPERATION",
            message="mk.block.set recipe must be list[str]",
        )

    try:
        path = _resolve_path(context.port_root, _mk_file(op), default="Makefile")
    except ValueError as exc:
        return _failed_row(
            op,
            code="E_APPLY_INVALID_PATH",
            message=str(exc),
            source_path=context.port_root,
        )

    loaded = _load_makefile(txn, path)
    if loaded is None:
        return _failed_row(
            op,
            code="E_APPLY_MISSING_SUBJECT",
            message=_no_file(op),
            source_path=path,
        )

    text, document = loaded
    lines = text.splitlines(keepends=False)

    matches: list[tuple[int, int]] = []
    for region_start, region_end in document.directive_regions:
        start_node = document.nodes[region_start]
        if not isinstance(start_node, DirectiveIfNode):
            continue
        if start_node.condition != condition:
            continue
        start_line = start_node.span.line_start
        end_line = document.nodes[region_end].span.line_end
        block_text = "\n".join(lines[start_line - 1 : end_line])
        if contains is not None and contains not in block_text:
            continue
        matches.append((start_line, end_line))

    if len(matches) > 1:
        return _failed_row(
            op,
            code="E_APPLY_AMBIGUOUS_MATCH",
            message=f"multiple .if blocks found: {condition}",
            source_path=path,
        )

    block_lines = [f".if {condition}", *recipe, ".endif"]
    if matches:
        start_line, end_line = matches[0]
        updated = _replace_line_range(
            text,
            start=start_line,
            end=end_line,
            new_lines=block_lines,
        )
        txn.stage_write(path, updated)
        return _success_row(op, "mk-block-replaced")

    insert_before_line = _insert_before_last_include_line(document)

    if insert_before_line is None:
        updated = text
        if updated and not updated.endswith(("\n", "\r\n")):
            updated += "\n"
        updated += "\n".join(block_lines) + "\n"
    else:
        updated = _replace_line_range(
            text,
            start=insert_before_line,
            end=insert_before_line - 1,
            new_lines=[*block_lines, ""],
        )

    txn.stage_write(path, updated)
    return _success_row(op, "mk-block-inserted")
