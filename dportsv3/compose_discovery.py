"""Overlay discovery and context resolution for compose pipeline."""

from __future__ import annotations

from pathlib import Path

from dportsv3.common.io import read_toml_file, write_toml_file
from dportsv3.common.validation import (
    compose_target_branch,
    is_compose_target,
    is_scoped_target,
)
from dportsv3.compose_models import ComposePortContext
from dportsv3.policy import EXCLUDED_TOP_LEVEL


def normalize_target(target: str) -> str | None:
    """Normalize compose target to branch token (without '@')."""
    return compose_target_branch(target)


def list_port_origins(base: Path) -> set[str]:
    """List category/origin rows that look like ports directories."""
    origins: set[str] = set()
    if not base.exists() or not base.is_dir():
        return origins
    for category in sorted(path for path in base.iterdir() if path.is_dir()):
        if category.name.startswith(".") or category.name in EXCLUDED_TOP_LEVEL:
            continue
        for port in sorted(path for path in category.iterdir() if path.is_dir()):
            if (port / "Makefile").exists():
                origins.add(f"{category.name}/{port.name}")
    return origins


def _removed_in_targets(payload: object) -> list[str]:
    """The str entries of payload["removed_in"], or [] for any other shape.

    The one parse of removed_in: everything that reads or rewrites the
    list takes it from here, so no two of them can disagree about what
    the manifest says.
    """
    if not isinstance(payload, dict):
        return []
    value = payload.get("removed_in")
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def read_overlay_removed_in(port_path: Path) -> list[str]:
    """Return the removed_in target list from overlay.toml, or [] if absent."""
    manifest = port_path / "overlay.toml"
    if not manifest.exists():
        return []
    payload, error = read_toml_file(manifest)
    if error is not None or payload is None:
        return []
    return _removed_in_targets(payload)


def write_overlay_removed_in(port_path: Path, target: str) -> tuple[bool, str | None]:
    """Add one target to overlay.toml removed_in and report whether file changed."""
    manifest = port_path / "overlay.toml"
    if manifest.exists():
        payload, error = read_toml_file(manifest)
        if error is not None:
            return False, error
        if payload is None:
            payload = {}
    else:
        payload = {}

    current = _removed_in_targets(payload)
    if target in current:
        return False, None

    payload["removed_in"] = sorted({*current, target})
    error = write_toml_file(manifest, payload)
    if error is not None:
        return False, error
    return True, None


def compat_diff_files_script_parity(port_dir: Path) -> tuple[list[Path], list[str]]:
    """Resolve compat fallback patch files matching script behavior."""
    diffs_dir = port_dir / "diffs"
    if not diffs_dir.exists() or not diffs_dir.is_dir():
        return [], []

    diff_files = sorted(path for path in diffs_dir.rglob("*.diff") if path.is_file())
    ignored_patch_files = sorted(
        path for path in diffs_dir.rglob("*.patch") if path.is_file()
    )

    notes: list[str] = []
    if ignored_patch_files:
        notes.append(
            "ignored non-script patch files under diffs/ (*.patch): "
            f"{len(ignored_patch_files)}"
        )
    if diff_files:
        notes.append(f"script-parity diffs selected: {len(diff_files)}")
    remove_file = diffs_dir / "REMOVE"
    if remove_file.exists() and remove_file.is_file():
        notes.append("script-parity REMOVE list detected")

    return diff_files, notes


def compat_dragonfly_files_script_parity(
    port_dir: Path,
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Resolve compat dragonfly payload files matching script behavior."""
    dragonfly_dir = port_dir / "dragonfly"
    if not dragonfly_dir.exists() or not dragonfly_dir.is_dir():
        return [], []

    rows = [
        (src, src.relative_to(dragonfly_dir))
        for src in sorted(path for path in dragonfly_dir.rglob("*") if path.is_file())
    ]
    notes = [f"script-parity dragonfly payload selected: {len(rows)}"] if rows else []
    return rows, notes


def resolve_layered_compat_makefile(
    port_dir: Path,
    target: str,
) -> tuple[Path | None, list[str]]:
    """Resolve compat Makefile layering with explicit script precedence."""
    explicit = port_dir / f"Makefile.DragonFly.{target}"
    baseline = port_dir / "Makefile.DragonFly.@any"
    legacy = port_dir / "Makefile.DragonFly"
    notes: list[str] = []

    if legacy.exists() and legacy.is_file():
        if explicit.exists() and explicit.is_file():
            notes.append(
                "script-parity makefile precedence: using Makefile.DragonFly over target-scoped file"
            )
        elif baseline.exists() and baseline.is_file():
            notes.append(
                "script-parity makefile precedence: using Makefile.DragonFly over @any file"
            )
        return legacy, notes

    if explicit.exists() and explicit.is_file():
        if baseline.exists() and baseline.is_file():
            notes.append("makefile override: Makefile.DragonFly.@any -> target-scoped")
        return explicit, notes

    if baseline.exists() and baseline.is_file():
        return baseline, notes

    return None, notes


def discover_overlay_contexts(
    delta_root: Path, target: str
) -> list[ComposePortContext]:
    """Discover compose contexts across ports overlays."""
    ports_root = delta_root / "ports"
    rows: list[ComposePortContext] = []
    if not ports_root.exists() or not ports_root.is_dir():
        return rows

    for category in sorted(path for path in ports_root.iterdir() if path.is_dir()):
        for port in sorted(path for path in category.iterdir() if path.is_dir()):
            dops_path = port / "overlay.dops"
            overlay_manifest = port / "overlay.toml"
            status_file = port / "STATUS"
            diffs_dir = port / "diffs"
            has_any_diff = diffs_dir.exists() and any(
                path.is_file() and path.suffix in {".diff", ".patch"}
                for path in diffs_dir.rglob("*")
            )
            dragonfly_dir = port / "dragonfly"
            has_any_dragonfly = dragonfly_dir.exists() and any(
                path.is_file() for path in dragonfly_dir.rglob("*")
            )
            newport = port / "newport"
            makefile_dragonfly = port / "Makefile.DragonFly"
            makefile_dragonfly_target = port / f"Makefile.DragonFly.{target}"
            makefile_dragonfly_any = port / "Makefile.DragonFly.@any"
            has_overlay = (
                dops_path.exists()
                or has_any_diff
                or has_any_dragonfly
                or newport.exists()
                or makefile_dragonfly.exists()
                or makefile_dragonfly_target.exists()
                or makefile_dragonfly_any.exists()
                or overlay_manifest.exists()
                or status_file.exists()
            )
            if not has_overlay:
                continue

            mode = "dops" if dops_path.exists() else "compat"
            mode_reason = (
                "overlay.dops-present" if dops_path.exists() else "overlay.dops-missing"
            )

            patches, patch_notes = compat_diff_files_script_parity(port)
            payload_files, payload_notes = compat_dragonfly_files_script_parity(port)
            compat_makefile, makefile_notes = resolve_layered_compat_makefile(
                port, target
            )
            compat_override_notes = makefile_notes
            compat_legacy_notes = patch_notes + payload_notes
            if compat_makefile == makefile_dragonfly:
                compat_legacy_notes.append(
                    "script-parity makefile source: Makefile.DragonFly"
                )

            rows.append(
                ComposePortContext(
                    origin=f"{category.name}/{port.name}",
                    path=port,
                    dops_path=dops_path if dops_path.exists() else None,
                    mode=mode,
                    mode_reason=mode_reason,
                    compat_makefile=compat_makefile,
                    compat_override_notes=compat_override_notes,
                    compat_legacy_notes=compat_legacy_notes,
                    fallback_patches=patches,
                    implicit_payload_files=payload_files,
                )
            )

    rows.sort(key=lambda row: row.origin)
    return rows


def validate_target_scoped_payloads(port_ctx: ComposePortContext) -> list[str]:
    """Validate target-scoped payload lane directories."""
    errors: list[str] = []
    for lane in ["diffs", "dragonfly"]:
        lane_dir = port_ctx.path / lane
        if not lane_dir.exists() or not lane_dir.is_dir():
            continue

        for entry in sorted(lane_dir.iterdir()):
            if entry.name.startswith("@") and not is_scoped_target(entry.name):
                errors.append(f"invalid target directory in {lane}: {entry.name}")
    return errors

def compat_scoped_payload_warnings(
    port_ctx: ComposePortContext, target: str = ""
) -> list[str]:
    """Scoped payload in a COMPAT-mode port, which no build line reads right.

    Both lanes are target-blind here, and they fail in OPPOSITE directions:

    - ``dragonfly/``: the payload is copied path-preserving, so
      ``dragonfly/@main/patch-x`` lands at that path in the output. do-patch
      globs ``dragonfly/patch-*`` NON-recursively, so the patch silently
      stops applying -- and the other build line's copy arrives beside it,
      equally inert.
    - ``diffs/``: nothing is copied at all. Every ``*.diff`` found
      recursively is APPLIED to the one output tree, so a two-target port
      has both build lines' framework patches applied together. Overlapping
      hunks give a loud E_COMPOSE_COMPAT_FAILED; non-overlapping ones give a
      silently cross-contaminated tree. The bead's first framing was right
      about this lane and its own audit correction retracted it after
      looking only at ``dragonfly``.

    WHY THIS WARNS AND DOES NOT FIX. Not because the intent is unclear --
    docs/implementation-plan-v3.md specifies per-target layering for both
    lanes. Because these two resolvers are named ``*_script_parity`` and
    that is their contract: the legacy generator still runs and is
    target-blind in exactly these two ways (``merge.sh`` does
    ``cp -pr ${DP}/dragonfly ${WORKAREA}/``, then ``find ${DP}/diffs -name
    '*.diff'`` and applies each). The compat path's output is the
    byte-parity oracle the remaining compat ports are migrated against
    (19 ``Makefile.DragonFly``, 28 ``diffs/`` and 23 ``dragonfly/`` among
    compat ports, measured 2026-10-04), with "compose-output-with-compat
    byte-equals" as the stated definition of done. Diverging from the
    oracle inside the one code path whose contract is parity with it is
    worse than the hazard, and no test edit buys that back.

    The suite pins the present behaviour for a real target directory too,
    not merely for ``@any``: a compat port composing ``@main`` is expected
    to produce ``dragonfly/@main/pkg-descr`` at that path.

    ``@any`` is not flagged because nothing in the compat path interprets a
    payload-lane ``@any`` at all -- there is no behaviour to second-guess,
    and no port in the tree uses one.
    """
    if port_ctx.mode != "compat":
        return []
    warnings: list[str] = []
    for lane in ["diffs", "dragonfly"]:
        lane_dir = port_ctx.path / lane
        if not lane_dir.exists() or not lane_dir.is_dir():
            continue
        scoped = sorted(
            entry.name
            for entry in lane_dir.iterdir()
            # is_compose_target is exactly "a real build line" -- scoped
            # minus @any. An invalid @name is already an error from
            # validate_target_scoped_payloads; repeating it here in
            # different words would be noise.
            if entry.is_dir() and is_compose_target(entry.name)
        )
        if not scoped:
            continue
        others = [s for s in scoped if s != target]
        if lane == "dragonfly":
            effect = (
                "the payload is copied path-preserving, and do-patch globs "
                "dragonfly/patch-* non-recursively, so none of it applies"
            )
            if others:
                effect += (
                    f"; {','.join(others)} belongs to another build line and "
                    f"is copied into this tree regardless"
                )
            remedy = (
                f"a `file materialize {lane}/@<target>/X -> {lane}/X` op "
                f"names the flattening explicitly"
            )
        else:
            effect = (
                "every *.diff found recursively is APPLIED to this one "
                "output tree"
            )
            if others:
                effect += (
                    f", so {','.join(others)}'s framework patches are applied "
                    f"here too"
                )
            remedy = f"a `patch apply {lane}/@<target>/X` op selects one"
        warnings.append(
            f"{lane}/{{{','.join(scoped)}}} is target-scoped but this port "
            f"composes in compat mode, which is target-blind for parity with "
            f"the legacy generator: {effect}. Convert the port to "
            f"overlay.dops, where {remedy}."
        )
    return warnings
