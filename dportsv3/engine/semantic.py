"""Semantic analyzer for DeltaPorts v3 DSL."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from dportsv3.common.validation import is_scoped_target, normalize_on_missing
from dportsv3.engine.ast import (
    AstDocument,
    FileOpNode,
    MaintainerDirective,
    MkOpNode,
    OperationNode,
    PatchOpNode,
    PortDirective,
    ReasonDirective,
    TargetDirective,
    TextOpNode,
    TypeDirective,
)
from dportsv3.engine.models import Diagnostic, SourceSpan


@dataclass(frozen=True)
class ScopedOperation:
    """Operation with resolved target scope."""

    target: str
    operation: OperationNode


@dataclass
class SemanticResult:
    """Semantic analysis result."""

    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    document: AstDocument | None = None
    scoped_ops: list[ScopedOperation] = field(default_factory=list)


def _diag(
    code: str,
    message: str,
    span: SourceSpan,
    source_path: Path | None,
) -> Diagnostic:
    return Diagnostic(
        severity="error",
        code=code,
        message=message,
        source_path=str(source_path) if source_path is not None else None,
        line=span.line_start,
        column=span.column_start,
    )


def _file_effect(
    op: OperationNode,
) -> tuple[str | None, bool, frozenset[str] | None]:
    """What ``op`` does to the port tree, for the dead-op rule.

    Returns (the file the op leaves its result in, whether it replaces
    or removes that file whole, the files it reads first). The reads are
    ``None`` for ``patch apply``: its subjects are inside the patch, so it
    counts as reading every file. Paths go through posixpath.normpath,
    because apply_common._resolve_path resolves ``./x`` and ``a/../x``
    to ``x``.

    This is the effects table poly-7pwa.8 lifts into a shared engine
    helper; extend it here rather than re-encoding it there.
    """
    if isinstance(op, FileOpNode):
        target = op.path if op.action == "remove" else op.dst
        reads = {op.src} if op.action == "copy" and op.src else set()
        return (
            posixpath.normpath(target) if target else None,
            True,
            frozenset(posixpath.normpath(p) for p in reads),
        )
    if isinstance(op, TextOpNode):
        path = posixpath.normpath(op.file_path) if op.file_path else None
        return path, False, frozenset({path} if path else ())
    if isinstance(op, MkOpNode):
        return "Makefile", False, frozenset({"Makefile"})
    return None, False, None


_FAMILY = {FileOpNode: "file", TextOpNode: "text", MkOpNode: "mk"}


def _lines(span: SourceSpan) -> str:
    if span.line_end > span.line_start:
        return f"lines {span.line_start}-{span.line_end}"
    return f"line {span.line_start}"


def _validate_dead_ops(
    op_scopes: list[tuple[OperationNode, tuple[str, ...]]],
    source_path: Path | None,
) -> list[Diagnostic]:
    """E_SEM_DEAD_OP for an op no build line ever sees (poly-7pwa.3).

    ``op_scopes`` is every operation in file order with its distinct
    selectors. On a build for T the engine runs every @any op, then every
    T op, each in file order (docs/dsl-v0.md, "Target scope rules";
    models.order_ops_for_target), so a T op can only be replaced by a
    later op whose selectors include T, and an @any op -- which also runs
    on lines that have no block of their own -- only by a later @any op.
    A change to that order is a change to this rule.
    """
    effects = [_file_effect(op) for op, _ in op_scopes]
    diagnostics: list[Diagnostic] = []
    for i, (op, lines) in enumerate(op_scopes):
        path = effects[i][0]
        if path is None:
            continue
        killers: dict[str, int] = {}
        for line in lines:
            for j in range(i + 1, len(op_scopes)):
                if line not in op_scopes[j][1]:
                    continue
                written, replaces, reads = effects[j]
                if reads is None or path in reads:
                    break
                if replaces and written == path:
                    killers[line] = j
                    break
        if len(killers) != len(lines):
            continue
        by_killer: dict[int, list[str]] = {}
        for line in lines:
            by_killer.setdefault(killers[line], []).append(
                "every build line" if line == "@any" else line
            )
        clauses = []
        for j, where in sorted(by_killer.items()):
            killer = op_scopes[j][0]
            verb = "removes" if killer.action == "remove" else "writes"
            clauses.append(
                f"line {killer.span.line_start} {verb} {path} after it "
                f"on {' and '.join(where)}"
            )
        later = ("that later op was" if len(by_killer) == 1
                 else "one of those later ops was")
        here = _lines(op.span)
        diagnostics.append(_diag(
            "E_SEM_DEAD_OP",
            f"{here} ({_FAMILY[type(op)]} {op.action}) never takes "
            f"effect: {'; '.join(clauses)}, so no build line ever sees "
            f"its result. If {later} meant for another build line, move "
            f"it into that line's target block. Otherwise delete {here}, "
            f"or, to keep it instead, move it to the end of the file "
            f"under its own 'target {','.join(lines)}' line.",
            op.span, source_path,
        ))
    return diagnostics


def _validate_on_missing(
    value: str | None,
    allowed: bool,
    span: SourceSpan,
    source_path: Path | None,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if value is None:
        return diagnostics

    if not allowed:
        diagnostics.append(
            _diag(
                "E_SEM_INVALID_OPERATION_STATE",
                "on-missing is not allowed for this operation",
                span,
                source_path,
            )
        )
        return diagnostics

    if normalize_on_missing(value) is None:
        diagnostics.append(
            _diag(
                "E_SEM_INVALID_OPERATION_STATE",
                "on-missing must be one of: error|warn|noop",
                span,
                source_path,
            )
        )

    return diagnostics


def _has_glob_pattern(value: str) -> bool:
    return any(ch in value for ch in ("*", "?", "[", "]"))


def _validate_operation(
    op: OperationNode, source_path: Path | None
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    if isinstance(op, MkOpNode):
        if op.action in {"set", "eval", "shell"}:
            if op.var is None or op.value is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        f"mk {op.action} requires var and value",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "unset":
            if op.var is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk unset requires var",
                        op.span,
                        source_path,
                    )
                )
        elif op.action in {"add", "remove"}:
            if op.var is None or op.token is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        f"mk {op.action} requires var and token",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "disable-if":
            if op.condition is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk disable-if requires condition",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "replace-if":
            if op.condition_from is None or op.condition_to is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk replace-if requires from and to conditions",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "block-set":
            if op.condition is None or op.heredoc_tag is None or op.recipe is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk block-set requires condition, heredoc tag, and recipe",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "bump":
            if op.var != "PORTREVISION":
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk bump supports PORTREVISION only",
                        op.span,
                        source_path,
                    )
                )
            elif (
                op.value is None
                or not (op.value.isascii() and op.value.isdigit())
                or not 1 <= int(op.value) <= 99
            ):
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk bump by takes an integer from 1 to 99",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "ensure-include":
            if op.include is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk ensure-include requires an include name",
                        op.span,
                        source_path,
                    )
                )
        elif op.action in {"target-set", "target-append"}:
            if op.name is None or op.heredoc_tag is None or op.recipe is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        f"mk {op.action} requires target name, heredoc tag, and recipe",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "target-remove":
            if op.name is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk target-remove requires target name",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "target-rename":
            if op.old_name is None or op.new_name is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "mk target-rename requires old and new target names",
                        op.span,
                        source_path,
                    )
                )
        else:
            diagnostics.append(
                _diag(
                    "E_SEM_INVALID_OPERATION_STATE",
                    f"unknown mk action: {op.action}",
                    op.span,
                    source_path,
                )
            )

        on_missing_allowed = op.action not in {
            "bump",
            "eval",
            "shell",
            "block-set",
            "target-set",
            "target-append",
            "ensure-include",
        }
        diagnostics.extend(
            _validate_on_missing(
                op.on_missing, on_missing_allowed, op.span, source_path
            )
        )
        return diagnostics

    if isinstance(op, FileOpNode):
        if op.action == "copy":
            if op.src is None or op.dst is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "file copy requires src and dst",
                        op.span,
                        source_path,
                    )
                )
            diagnostics.extend(
                _validate_on_missing(op.on_missing, False, op.span, source_path)
            )
        elif op.action == "materialize":
            if op.src is None or op.dst is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "file materialize requires src and dst",
                        op.span,
                        source_path,
                    )
                )
            elif _has_glob_pattern(op.src):
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "file materialize src does not support wildcards in v1",
                        op.span,
                        source_path,
                    )
                )
            diagnostics.extend(
                _validate_on_missing(op.on_missing, False, op.span, source_path)
            )
        elif op.action == "remove":
            if op.path is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "file remove requires path",
                        op.span,
                        source_path,
                    )
                )
            diagnostics.extend(
                _validate_on_missing(op.on_missing, True, op.span, source_path)
            )
        else:
            diagnostics.append(
                _diag(
                    "E_SEM_INVALID_OPERATION_STATE",
                    f"unknown file action: {op.action}",
                    op.span,
                    source_path,
                )
            )
        return diagnostics

    if isinstance(op, TextOpNode):
        if not op.file_path:
            diagnostics.append(
                _diag(
                    "E_SEM_INVALID_OPERATION_STATE",
                    "text operation requires file path",
                    op.span,
                    source_path,
                )
            )

        if op.action == "line-remove":
            if op.exact is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "text line-remove requires exact string",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "line-insert-after":
            if op.anchor is None or op.line is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "text line-insert-after requires anchor and line",
                        op.span,
                        source_path,
                    )
                )
        elif op.action == "replace-once":
            if op.from_text is None or op.to_text is None:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_OPERATION_STATE",
                        "text replace-once requires from and to strings",
                        op.span,
                        source_path,
                    )
                )
        else:
            diagnostics.append(
                _diag(
                    "E_SEM_INVALID_OPERATION_STATE",
                    f"unknown text action: {op.action}",
                    op.span,
                    source_path,
                )
            )

        diagnostics.extend(
            _validate_on_missing(op.on_missing, True, op.span, source_path)
        )
        return diagnostics

    if isinstance(op, PatchOpNode):
        if not op.path:
            diagnostics.append(
                _diag(
                    "E_SEM_INVALID_OPERATION_STATE",
                    "patch apply requires path",
                    op.span,
                    source_path,
                )
            )

    return diagnostics


def analyze_document(
    document: AstDocument,
    source_path: Path | None = None,
) -> SemanticResult:
    """Run semantic validation and target scope resolution."""
    diagnostics: list[Diagnostic] = []
    scoped_ops: list[ScopedOperation] = []
    op_scopes: list[tuple[OperationNode, tuple[str, ...]]] = []

    port_count = 0
    type_count = 0
    reason_count = 0
    maintainer_count = 0

    current_targets: tuple[str, ...] = ("@any",)

    for statement in document.statements:
        if isinstance(statement, TargetDirective):
            targets = statement.targets or (statement.target,)
            if "@any" in targets and len(targets) > 1:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_TARGET_SCOPE",
                        "target directive cannot combine @any with explicit selectors",
                        statement.span,
                        source_path,
                    )
                )
            for target in targets:
                if is_scoped_target(target):
                    continue
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_TARGET_SCOPE",
                        "target directive must be @any, @main, or @YYYYQ[1-4]",
                        statement.span,
                        source_path,
                    )
                )
            repeated = [t for i, t in enumerate(targets) if t in targets[:i]]
            if repeated:
                diagnostics.append(
                    _diag(
                        "E_SEM_INVALID_TARGET_SCOPE",
                        f"target directive repeats a selector: {repeated[0]}",
                        statement.span,
                        source_path,
                    )
                )
            current_targets = tuple(targets)
            continue

        if isinstance(statement, PortDirective):
            port_count += 1
            if port_count > 1:
                diagnostics.append(
                    _diag(
                        "E_SEM_DUPLICATE_PORT",
                        "port directive appears more than once",
                        statement.span,
                        source_path,
                    )
                )
            continue

        if isinstance(statement, TypeDirective):
            type_count += 1
            if type_count > 1:
                diagnostics.append(
                    _diag(
                        "E_SEM_DUPLICATE_TYPE",
                        "type directive appears more than once",
                        statement.span,
                        source_path,
                    )
                )
            continue

        if isinstance(statement, ReasonDirective):
            reason_count += 1
            if reason_count > 1:
                diagnostics.append(
                    _diag(
                        "E_SEM_DUPLICATE_REASON",
                        "reason directive appears more than once",
                        statement.span,
                        source_path,
                    )
                )
            continue

        if isinstance(statement, MaintainerDirective):
            maintainer_count += 1
            if maintainer_count > 1:
                diagnostics.append(
                    _diag(
                        "E_SEM_DUPLICATE_MAINTAINER",
                        "maintainer directive appears more than once",
                        statement.span,
                        source_path,
                    )
                )
            continue

        operation = statement
        for target in current_targets:
            scoped_ops.append(ScopedOperation(target=target, operation=operation))
        op_scopes.append(
            (operation, tuple(dict.fromkeys(current_targets)))
        )

        diagnostics.extend(_validate_operation(operation, source_path))

    if port_count == 0:
        diagnostics.append(
            _diag(
                "E_SEM_MISSING_PORT",
                "exactly one port directive is required",
                document.span,
                source_path,
            )
        )

    diagnostics.extend(_validate_dead_ops(op_scopes, source_path))

    return SemanticResult(
        ok=not diagnostics,
        diagnostics=diagnostics,
        document=document,
        scoped_ops=scoped_ops,
    )
