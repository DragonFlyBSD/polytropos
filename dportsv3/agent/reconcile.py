"""Reconcile a port's on-disk `diffs/` artifacts against its overlay.dops.

When a patch edit removes a `patch apply` / `file materialize` line, the
`diffs/` file it read is left on disk, unreferenced. A `diffs/` file only
reaches the build through such an op, so it is inert, but it lingers as
dead substrate; the runner removes it here, before the delivery diff is
captured, so the removal is part of the fix.

NOT `dragonfly/` (poly-u9n4.4). There the folder is the install: a file no
op reads is installed on every line its folder names, so "unreferenced"
means "installed", and every patch the agent wrote with no op would be
deleted before the diff was captured. The agent drops a dragonfly/ file
itself, with `delete_file`.
"""

from __future__ import annotations

from pathlib import Path

from dportsv3.engine.api import build_plan

# Directories whose files only reach the build via an explicit overlay op.
_RECONCILED_DIRS = ("diffs",)


def _referenced_artifacts(port_dir: Path) -> set[str] | None:
    """Port-relative paths the overlay references as source artifacts
    (`file materialize`/`file copy` src, `patch apply` path). Returns ``None``
    — meaning *do not reconcile* — when there is no overlay or it does not
    plan cleanly (never delete based on an overlay we can't read)."""
    overlay = port_dir / "overlay.dops"
    if not overlay.is_file():
        return None
    planned = build_plan(overlay.read_text(), overlay)
    if not planned.ok or planned.plan is None:
        return None
    refs: set[str] = set()
    for op in planned.plan.to_dict()["ops"]:
        kind = op.get("kind")
        if kind in ("file.materialize", "file.copy"):
            src = op.get("src")
            if isinstance(src, str):
                refs.add(src)
        elif kind == "patch.apply":
            path = op.get("path")
            if isinstance(path, str):
                refs.add(path)
    return refs


def reconcile_orphaned_artifacts(port_dir: Path) -> list[str]:
    """Delete `diffs/` files the port's overlay no longer references, plus
    any directory left empty. Returns the port-relative
    paths removed (sorted). No-op (returns ``[]``) when the overlay is absent
    or unparseable — fail safe, never delete against an unknown reference set.
    """
    refs = _referenced_artifacts(port_dir)
    if refs is None:
        return []

    removed: list[str] = []
    for sub in _RECONCILED_DIRS:
        directory = port_dir / sub
        if not directory.is_dir():
            continue
        for f in sorted(p for p in directory.rglob("*") if p.is_file()):
            rel = f.relative_to(port_dir).as_posix()
            if rel not in refs:
                f.unlink()
                removed.append(rel)
        if not any(directory.rglob("*")):
            try:
                directory.rmdir()
            except OSError:
                pass
    return removed
