"""Bring up a new build line as a copy of an existing one (poly-7pwa.25).

A quarterly branch of freebsd-ports starts as a copy of main, so its
DragonFly overlay has to start as a copy of @main's. ``@any`` ops already
reach every line. What does not is everything under a ``target @main``
block -- and since poly-7pwa.27 that is where every fix to a port with
ops goes, mk and text ops as well as patches.

So each port with a block naming the old line gets ONE ``target <new>``
block appended, holding the body of every such block in file order, and
the payload those ops read under ``<lane>/@<T>/`` is copied to
``<lane>/@<new>/``. The new line's blocks and folder are its own: a later
fix on either line changes that line only, which is what install_patches
and the job brief already assume.

Adding the new line to each list (``target @main,@2026Q4``) is the smaller
diff and the wrong one. Every @main op and patch would be shared with the
new line, and install_patches could no longer re-cut a @main patch: its
own copy would be the shared file itself.

Nothing is written unless the new overlay plans, the new line plans
exactly the old line's ops, and every other line plans what it did before.
"""

from __future__ import annotations

import posixpath
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

#: A payload path under a line's own folder: lane, line, rest.
_SCOPED = re.compile(r"^(dragonfly|diffs)/@(?:main|\d{4}Q[1-4])/(.+)$")
#: The op kinds that read payload from the overlay dir, and the key.
_PAYLOAD_KEY = {"file.materialize": "src", "patch.apply": "path"}


@dataclass(frozen=True)
class PortBranch:
    """One port's part of a branch: the new overlay, or why it is skipped."""

    origin: str
    detail: str
    text: str | None = None
    #: (source, copy), relative to the port dir.
    copies: tuple[tuple[str, str], ...] = ()

    @property
    def skipped(self) -> bool:
        return self.text is None


def _new_path(path: str, new_line: str) -> str | None:
    # Normalized as compose resolves it: ./dragonfly/@main/x is @main's too.
    m = _SCOPED.match(posixpath.normpath(path))
    return f"{m.group(1)}/{new_line}/{m.group(2)}" if m else None


def _plan(text: str):
    from dportsv3.engine.api import build_plan  # noqa: PLC0415

    planned = build_plan(text, None)
    if not planned.ok or planned.plan is None:
        code = next((d.code for d in planned.diagnostics), "?")
        return None, code
    return list(planned.plan.ops), ""


def _line_ops(ops: list, line: str, new_line: str | None = None) -> list:
    """What ``line`` runs, by payload; with ``new_line``, as copied there."""
    from dportsv3.agent.scope_check import payload_identity  # noqa: PLC0415

    out = []
    for op in ops:
        if op.target != line:
            continue
        key = _PAYLOAD_KEY.get(op.kind)
        moved = new_line and key and _new_path(str(op.payload.get(key)), new_line)
        if moved:
            op = replace(op, payload={**op.payload, key: moved})
        out.append(payload_identity(op))
    return out


def branch_overlay(text: str, from_line: str, new_line: str
                   ) -> tuple[str, tuple[tuple[str, str], ...], int] | str | None:
    """Append ``new_line``'s copy of ``from_line``'s blocks to ``text``.

    Returns (new text, payload copies, ops copied), a skip reason, or None
    when no block names ``from_line``.
    """
    from dportsv3.engine.api import parse_dsl  # noqa: PLC0415
    from dportsv3.engine.ast import TargetDirective  # noqa: PLC0415

    before, code = _plan(text)
    if before is None:
        return f"the overlay does not plan ({code})"
    lines = text.split("\n")  # as the lexer counts lines
    heads = [s for s in parse_dsl(text).ast.statements
             if isinstance(s, TargetDirective)]
    if any(new_line in (h.targets or (h.target,)) for h in heads):
        return f"it already has a block naming {new_line}"

    body: list[str] = []
    copies: dict[str, str] = {}
    for i, head in enumerate(heads):
        if from_line not in (head.targets or (head.target,)):
            continue
        # 1-based line numbers: after the header, up to the next one.
        first = head.span.line_end + 1
        last = heads[i + 1].span.line_start - 1 if i + 1 < len(heads) else len(lines)
        block = {n: lines[n - 1] for n in range(first, last + 1)}
        # The comments above the next header are that block's.
        while block and (not block[last].strip()
                         or block[last].lstrip().startswith("#")):
            del block[last]
            last -= 1
        while block and not block[first].strip():
            del block[first]
            first += 1
        if not block:
            continue
        for op in before:
            key = _PAYLOAD_KEY.get(op.kind)
            if op.target != from_line or not key or op.span is None:
                continue
            old = str(op.payload.get(key))
            new = _new_path(old, new_line)
            if (new in (None, posixpath.normpath(old))
                    or not first <= op.span.line_start <= last):
                continue
            if copies.get(new, old) != old:
                return f"{copies[new]} and {old} would both be copied to {new}"
            copies[new] = old
            at = next((n for n in range(op.span.line_start, op.span.line_end + 1)
                       if old in block.get(n, "")), None)
            if at is not None:
                block[at] = block[at].replace(old, new, 1)
        body += ([""] if body else []) + list(block.values())
    if not body:
        return None

    out = (text if text.endswith("\n") else text + "\n") + (
        f"\n# {new_line} starts as a copy of {from_line}'s own ops "
        f"(dportsv3 migrate branch-line).\ntarget {new_line}\n\n"
        + "\n".join(body) + "\n")
    after, code = _plan(out)
    if after is None:
        return f"the copied overlay does not plan ({code})"
    named = {op.target for op in before + after} - {new_line}
    if any(_line_ops(before, t) != _line_ops(after, t) for t in named):
        return "the copy would change another line's ops"
    copied = _line_ops(after, new_line)
    if copied != _line_ops(after, from_line, new_line):
        return f"{new_line} would not plan {from_line}'s ops"
    return out, tuple((old, new) for new, old in copies.items()), len(copied)


def plan_branch(delta_root: Path, from_line: str, new_line: str
                ) -> list[PortBranch]:
    """The branch of every DSL port with a block naming ``from_line``."""
    rows: list[PortBranch] = []
    ports = Path(delta_root) / "ports"
    for overlay in sorted(ports.glob("*/*/overlay.dops")):
        raw = overlay.read_bytes()
        if from_line.encode() not in raw:
            continue
        origin = str(overlay.parent.relative_to(ports))
        if b"\r" in raw:  # read_text/write_text would rewrite every line
            rows.append(PortBranch(origin, "it has CR line endings"))
            continue
        text = raw.decode()
        result = branch_overlay(text, from_line, new_line)
        if result is None:
            continue
        if isinstance(result, str):
            rows.append(PortBranch(origin, result))
            continue
        new_text, copies, n = result
        problem = next((
            f"{src} is missing" if not (overlay.parent / src).is_file()
            else f"{dst} exists with other content"
            for src, dst in copies
            if not (overlay.parent / src).is_file()
            or ((overlay.parent / dst).exists()
                and (overlay.parent / dst).read_bytes()
                != (overlay.parent / src).read_bytes())
        ), None)
        if problem:
            rows.append(PortBranch(origin, problem))
            continue
        rows.append(PortBranch(
            origin,
            f"{n} ops into target {new_line}"
            + (f", {len(copies)} payload file{'s' if len(copies) > 1 else ''}"
               if copies else ""),
            new_text, copies,
        ))
    return rows


def apply_branch(delta_root: Path, rows: list[PortBranch]) -> None:
    """Write each planned branch: payload copies first, then the overlay."""
    ports = Path(delta_root) / "ports"
    for row in rows:
        if row.skipped:
            continue
        port = ports / row.origin
        for src, dst in row.copies:
            (port / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(port / src, port / dst)
        (port / "overlay.dops").write_text(row.text)
