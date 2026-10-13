"""The ``dragonfly/`` folder is the install (poly-u9n4).

For a DSL port, a file under the overlay's ``dragonfly/`` reaches the
composed port with no op:

- ``dragonfly/<path>`` on every build line, as ``dragonfly/<path>``;
- ``dragonfly/@<line>/<path>`` on that line only, as ``dragonfly/<path>``;
- except a file an explicit op reads -- a ``file materialize`` source or
  a ``patch apply`` path -- on ANY build line. That op installs it, where
  it says, and the folder does not: a file only one block's op reads stays
  off every other line, as it did before the rule.

EACH FOLDER AT THE HEAD OF ITS SCOPE. Apply order is by scope
(models.order_ops_for_target) and the folders follow it: the flat files
are staged before the first ``@any`` op, the line's own folder after the
last ``@any`` op and before the line's first. So any op can still edit or
remove an installed file, and a line's own copy replaces a flat file or
an ``@any`` op's file of the same name with no op of its own.

One rule, read by everything that has to agree about it: apply stages
these files, and the agent's tools report and guard them from here.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from dportsv3.common.validation import is_compose_target
from dportsv3.engine.models import FolderInstall, PlanOp

#: The lane the folder rule installs from.
PAYLOAD_LANE = "dragonfly"

#: The op kinds that read a file from the overlay dir, and the key.
_READERS = {"file.materialize": "src", "patch.apply": "path"}


def folder_scope(rel: str) -> str | None:
    """The scope the folder rule gives the overlay-relative path ``rel``.

    ``@any`` for a file under ``dragonfly/`` whose first component below it
    does not start with ``@``; the line for ``dragonfly/@<line>/<path>``;
    None for anything else -- outside ``dragonfly/``, or under an ``@``
    directory that is not a build line. ``dragonfly/@any/`` is one of
    those: the flat folder is the one way to say every line, and compose
    rejects the other. Whether an op reads the file is not asked here.
    """
    parts = PurePosixPath(posixpath.normpath(rel)).parts
    if len(parts) < 2 or parts[0] != PAYLOAD_LANE or ".." in parts:
        return None
    if not parts[1].startswith("@"):
        return "@any"
    if len(parts) > 2 and is_compose_target(parts[1]):
        return parts[1]
    return None


def folder_destination(rel: str) -> str:
    """Where the folder rule installs ``rel``: its own folder's prefix dropped."""
    parts = PurePosixPath(posixpath.normpath(rel)).parts
    if len(parts) > 2 and parts[1].startswith("@"):
        return "/".join((PAYLOAD_LANE, *parts[2:]))
    return "/".join(parts)


def explicit_reads(ops: Iterable[PlanOp]) -> set[str]:
    """The overlay-relative files an op reads, on any build line, normalized."""
    reads: set[str] = set()
    for op in ops:
        key = _READERS.get(op.kind)
        value = op.payload.get(key) if key else None
        if isinstance(value, str):
            reads.add(posixpath.normpath(value))
    return reads


def payload_files(overlay_dir: Path) -> list[str]:
    """Every file under the overlay's ``dragonfly/``, overlay-relative, sorted."""
    lane = overlay_dir / PAYLOAD_LANE
    if not lane.is_dir():
        return []
    return sorted(
        path.relative_to(overlay_dir).as_posix()
        for path in lane.rglob("*")
        if path.is_file()
    )


def folder_installs(
    overlay_dir: Path,
    ops: Iterable[PlanOp],
    target: str,
    *,
    files: Iterable[str] | None = None,
) -> list[FolderInstall]:
    """What build line ``target`` installs from ``dragonfly/`` with no op.

    In staging order: the flat files, then ``target``'s own folder, each
    sorted by path. ``files`` (overlay-relative) stands in for a listing of
    ``overlay_dir``, for a caller asking about a tree it has not written.
    """
    listed = payload_files(overlay_dir) if files is None else files
    skip = explicit_reads(ops)
    flat: list[FolderInstall] = []
    own: list[FolderInstall] = []
    for rel in sorted({posixpath.normpath(f) for f in listed}):
        if rel in skip:
            continue
        scope = folder_scope(rel)
        if scope == "@any":
            flat.append(FolderInstall(rel, rel, "@any"))
        elif scope is not None and scope == target:
            own.append(FolderInstall(rel, folder_destination(rel), scope))
    return flat + own


def with_folder_installs(
    ordered_ops: list[PlanOp], installs: list[FolderInstall]
) -> list[PlanOp | FolderInstall]:
    """``ordered_ops`` with each folder's installs at the head of its scope.

    ``ordered_ops`` is models.order_ops_for_target's list, every ``@any``
    op first: the flat files go before them, the line's own after them.
    """
    head = sum(1 for op in ordered_ops if op.target == "@any")
    return [
        *(i for i in installs if i.target == "@any"),
        *ordered_ops[:head],
        *(i for i in installs if i.target != "@any"),
        *ordered_ops[head:],
    ]
