"""Worker tool functions for the agent harness, on top of dev-env primitives.

Every function takes the dev-env name as its first argument. Filesystem
operations work host-side on the env's writable overlay
(``env_dir/writable/...``); commands that must run inside the chroot
shell out to ``dportsv3 dev-env exec``.

The agent sees chroot-absolute paths like ``/work/DeltaPorts/ports/...``;
those translate to ``<env_dir/writable>/work/DeltaPorts/ports/...`` on
the host. Paths outside ``/work/`` are rejected — the agent has no
business writing to ``/etc`` or anywhere else in the chroot.

No git operations. The dev-env's writable overlay is the workspace;
dirty edits stay dirty. ``emit_diff`` runs ``git diff`` for audit but
never commits.

Design note: we drive dev-env via its public CLI (``python -m dportsv3
dev-env ...``) rather than importing ``dports_dev_env`` directly. The
CLI is the contract that's stable across dev-env refactors; the
``EnvironmentStore`` internals are not. Subprocess overhead is bounded
(``env_paths`` is cached per-process; ``env_verify`` runs once per
attempt; chroot-bound ops are unavoidably subprocess anyway).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# How to invoke `dportsv3`. This specifically needs the *shell wrapper*
# (`bin/dportsv3`), which is the only entry point that routes the `dev-env`
# subcommand — it dispatches to a separate venv. Neither `python -m dportsv3`
# nor the `dportsv3` console script knows about `dev-env` at all.
#
# Resolution order:
#   1. $DPORTS_DEV_ENV_CMD (whitespace-split). Names the dev-env entry point
#      outright — what a packaged install sets if it needs to override PATH.
#   2. $DPORTSV3_CMD + "dev-env". The checkout wrapper exports this pointing
#      at itself, and it routes `dev-env` by exec'ing dports-dev-env. Honoured
#      ahead of PATH so a checkout uses ITS OWN dev-env venv rather than some
#      other copy that happens to be installed.
#   3. `dports-dev-env` on PATH — the console script of the dev-env
#      distribution, which is what a port installs into ${PREFIX}/bin.
# Otherwise, raise at first use.
#
# There used to be a step between them: a sibling lookup four parents up from
# this file. It only resolved while the tool lived inside the DeltaPorts
# checkout, and it silently outranked PATH.
#
# What is deliberately NOT in that list is `dportsv3` on PATH. In an active
# venv shutil.which("dportsv3") finds the *console script*, which has no
# `dev-env` subcommand, so a `dev-env exec` through it fails with an argparse
# error rather than anything self-explanatory. Resolving dports-dev-env
# directly means the runtime never needs the checkout's shell wrapper at all,
# which is what lets the three services run from a packaged install.
def _resolve_dev_env_cmd() -> list[str]:
    explicit = os.environ.get("DPORTS_DEV_ENV_CMD")
    if explicit:
        return explicit.split()
    wrapper = os.environ.get("DPORTSV3_CMD")
    if wrapper:
        return [*wrapper.split(), "dev-env"]
    found = shutil.which("dports-dev-env")
    if found:
        return [found]
    raise RuntimeError(
        "could not locate the dev-env entry point. Put `dports-dev-env` on "
        "PATH, or set $DPORTS_DEV_ENV_CMD to it, or set $DPORTSV3_CMD to a "
        "checkout's bin/dportsv3. Note that the `dportsv3` console script is "
        "not a substitute: it does not implement the `dev-env` subcommand "
        "this needs, and would fail later with an argparse error."
    )


_DEV_ENV_CMD: list[str] | None = None


def _dev_env_cmd() -> list[str]:
    """argv prefix that routes a ``dev-env`` subcommand."""
    global _DEV_ENV_CMD
    if _DEV_ENV_CMD is None:
        _DEV_ENV_CMD = _resolve_dev_env_cmd()
    return _DEV_ENV_CMD


# -----------------------------------------------------------------------------
# Path resolution + dev-env state
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvPaths:
    """Host-side paths for a dev-env."""
    env_dir: Path
    writable: Path

    @property
    def deltaports(self) -> Path:
        return self.writable / "work" / "DeltaPorts"


def _run_dev_env(*args: str) -> subprocess.CompletedProcess:
    """Run a ``dev-env`` subcommand. ``args`` excludes the word itself."""
    return subprocess.run(
        [*_dev_env_cmd(), *args],
        capture_output=True, text=True, check=False,
    )


# Cap per-stream output the LLM sees. Build errors live at the tail of
# the log; we preserve the LAST bytes when truncating, not the first.
_MAX_STREAM_BYTES = 32_768


def _tail(s: str, max_bytes: int = _MAX_STREAM_BYTES) -> tuple[str, bool]:
    if len(s) <= max_bytes:
        return s, False
    return "…[truncated]…\n" + s[-max_bytes:], True


def _exec_result(rc: int, stdout: str, stderr: str, **extra) -> dict:
    """Build a uniform tool-result dict from a subprocess outcome.

    LLM-facing tools return this shape so the harness/LLM can inspect
    the failure rather than recovering from an opaque exception.
    """
    out, out_trunc = _tail(stdout)
    err, err_trunc = _tail(stderr)
    return {
        "ok": rc == 0,
        "rc": rc,
        "stdout_tail": out,
        "stderr_tail": err,
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
        **extra,
    }


@lru_cache(maxsize=32)
def env_paths(env: str) -> EnvPaths:
    """Resolve host-side paths for ``env``. Cached per-process.

    Each ``dportsv3 dev-env path`` invocation costs a few hundred ms;
    caching means repeated tool calls during one patch attempt pay it
    only once. The env_dir for a given name is immutable for the
    lifetime of that env, so caching is safe.
    """
    p1 = _run_dev_env("path", env)
    if p1.returncode != 0:
        raise RuntimeError(f"dev-env path failed: {(p1.stderr or p1.stdout).strip()}")
    p2 = _run_dev_env("path", env, "--writable")
    if p2.returncode != 0:
        raise RuntimeError(f"dev-env path --writable failed: {(p2.stderr or p2.stdout).strip()}")
    return EnvPaths(env_dir=Path(p1.stdout.strip()), writable=Path(p2.stdout.strip()))


def env_verify(env: str) -> dict:
    """Return the env's state dict; raise if not usable.

    Wraps ``dportsv3 dev-env status NAME``. Fails only when the env is
    missing or in a non-``ready`` state (``creating``, ``destroying``,
    ``failed``). A ``root_mounted: false`` env is still usable: host-side
    tool ops operate on the writable overlay directly, and
    ``dportsv3 dev-env exec`` auto-mounts on demand for chroot ops.
    """
    p = _run_dev_env("status", env)
    if p.returncode != 0:
        raise RuntimeError(f"dev-env status failed: {(p.stderr or p.stdout).strip()}")
    info = json.loads(p.stdout.strip())
    if info.get("status") != "ready":
        raise RuntimeError(f"env {env} not ready: status={info.get('status')}")
    return info


def _resolve_chroot_path(paths: EnvPaths, chroot_path: str) -> Path:
    """Translate an in-chroot absolute path to its host-side location.

    Only paths under ``/work/`` are allowed; ``..`` traversal escaping
    the writable overlay is rejected via ``Path.relative_to`` after
    realpath resolution.
    """
    if not (chroot_path == "/work" or chroot_path.startswith("/work/")):
        raise ValueError(f"path must be under /work (got {chroot_path!r})")
    rel = chroot_path.lstrip("/")  # "work/..."
    resolved = (paths.writable / rel).resolve()
    writable_root = paths.writable.resolve()
    try:
        resolved.relative_to(writable_root)
    except ValueError as exc:
        raise ValueError(f"path escapes writable overlay: {chroot_path!r}") from exc
    return resolved


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -----------------------------------------------------------------------------
# Path guardrails — observed failure modes the PATCH_SYSTEM prompt warns
# against but weaker models violate anyway. Enforce at the tool boundary
# so the model sees a clear error result and can adjust on the next turn.
# -----------------------------------------------------------------------------


def _reject_makefile_dragonfly_authoring(chroot_path: str) -> dict | None:
    """Refuse any ``put_file`` that creates/edits ``Makefile.DragonFly*``
    under ``ports/`` — the Step 48 dops-only authoring lock.

    ``Makefile.DragonFly`` is the one compat artifact with no role in dops
    authoring: its variable/target/conditional edits are expressed as ``mk``
    directives in ``overlay.dops``. Refusing new ones **unconditionally**
    (not just when an ``overlay.dops`` already exists, as the pre-cutover
    guard did) freezes the un-migrated compat residue so it cannot grow —
    nothing new enters compat. It also still prevents the half-migrated
    state (``Makefile.DragonFly`` + ``overlay.dops`` together) that
    ``assess_dops`` rejects.

    ``diffs/``, ``dragonfly/`` and ``newport/`` are intentionally NOT
    blocked — they are dops sources too (``patch apply diffs/X``,
    ``file materialize dragonfly/X``, type=dport ``newport/``).

    Returns None (allowed) when the path is not a ``Makefile.DragonFly``
    in a port subtree.
    """
    if not chroot_path.startswith("/work/DeltaPorts/ports/"):
        return None
    basename = chroot_path.rpartition("/")[2]
    if not basename.startswith("Makefile.DragonFly"):
        return None
    return {
        "ok": False,
        "error": (
            f"put_file rejected: {chroot_path!r} — DeltaPorts authoring is "
            f"dops-only (Step 48 cutover). New Makefile.DragonFly files are "
            f"not allowed; express the variable/target/conditional edit as "
            f"`mk` directives in overlay.dops instead."
        ),
        "path": chroot_path,
        "blocked_by": "compat_makefile_authoring_lock",
    }


def _reject_orphan_dops_write(chroot_path: str) -> dict | None:
    """Refuse ``put_file`` writes of ``*.dops`` files outside the
    port subtree.

    Background: the convert agent in gperf-20260525-173301Z hit a
    related guard, then "creatively" worked around it by writing
    ``overlay.dops`` to ``/work/overlay.dops`` (writable root).
    The write succeeded — there was no canonical-path check — and
    the file was orphaned: ``validate_dops`` reads from
    ``/work/DeltaPorts/ports/<origin>/overlay.dops``, so it saw
    "not found" and the agent burned its budget thrashing.

    Bright-line rule: ``*.dops`` files only live at
    ``/work/DeltaPorts/ports/<origin>/<name>.dops``. Anywhere else
    is an orphan write and should fail loudly with the right
    destination in the error message so the agent self-corrects.
    """
    if not chroot_path.endswith(".dops"):
        return None
    # Canonical: ports/<origin>/<name>.dops under DeltaPorts.
    if chroot_path.startswith("/work/DeltaPorts/ports/"):
        return None
    return {
        "ok": False,
        "error": (
            f"put_file rejected: {chroot_path!r} is a *.dops file "
            f"outside the port subtree. The canonical location is "
            f"/work/DeltaPorts/ports/<origin>/<name>.dops — writing "
            f"anywhere else produces an orphan file that "
            f"validate_dops and the composer cannot see."
        ),
        "path": chroot_path,
        "blocked_by": "orphan_dops_write",
    }


def _reject_dports_write(chroot_path: str) -> dict | None:
    """Refuse ``put_file`` writes to regenerated trees.

    Two trees are read-only outputs the agent must not write to:

    - ``/work/DPorts/`` — the **lock root**: last-known-good DPorts
      checkout. Not regenerated by materialize_dports (that's a common
      misconception). It's pinned to whatever DeltaPorts STATUS says
      was the last successful build. Edits here are out-of-band and
      ignored by every other tool.
    - ``/work/artifacts/compose/<target>/`` — the **compose root**:
      what materialize_dports outputs. Regenerated wholesale; any
      edits are wiped on the next materialize.

    Either way, the answer is the same: edit
    ``/work/DeltaPorts/ports/<origin>/`` (patches, Makefile.DragonFly,
    overlay.dops, dragonfly/*), then call materialize_dports.
    """
    is_lock_root = (
        chroot_path == "/work/DPorts"
        or chroot_path.startswith("/work/DPorts/")
    )
    is_compose_root = (
        chroot_path == "/work/artifacts/compose"
        or chroot_path.startswith("/work/artifacts/compose/")
    )
    if not (is_lock_root or is_compose_root):
        return None
    which = "lock root (/work/DPorts/)" if is_lock_root else (
        "compose root (/work/artifacts/compose/<target>/)"
    )
    return {
        "ok": False,
        "error": (
            f"put_file rejected: {chroot_path!r} is under the {which}. "
            "Lock root = last-known-good DPorts checkout (not regenerated "
            "by materialize). Compose root = materialize_dports output "
            "(wiped on every materialize). Either way, your edit will "
            "not survive. Edit /work/DeltaPorts/ports/<origin>/ instead "
            "(patches, Makefile.DragonFly, overlay.dops, dragonfly/* "
            "files), then call materialize_dports(<origin>) to apply."
        ),
        "path": chroot_path,
        "kind": "regenerated_tree_write_refused",
    }


# Paths inside the dsynth build root that are pure scaffolding /
# per-slot machinery, never the right place to look for a port's
# source. Refusing list_dir/grep here saves the agent from spending
# turns enumerating dsynth internals.
_DSYNTH_SCAFFOLDING_PREFIXES = (
    "/work/dsynth/build/Template",
)


def _reject_dsynth_scaffolding(chroot_path: str, op: str) -> dict | None:
    """Refuse list_dir/grep into dsynth's per-slot Template scaffolding.

    These paths exist (they're dsynth's read-only build "image" each
    slot mounts on top of) but they have nothing to do with any
    particular port. Models trying to find ``Makefile.in.rej`` often
    grep them blindly and burn turns on 500+ noise matches.
    """
    norm = chroot_path.rstrip("/")
    for prefix in _DSYNTH_SCAFFOLDING_PREFIXES:
        if norm == prefix or norm.startswith(prefix + "/"):
            return {
                "ok": False,
                "error": (
                    f"{op} refused: {chroot_path!r} is dsynth's per-slot "
                    "Template scaffolding (read-only build image), not a "
                    "port build dir. For a port's current build state try "
                    "/work/obj/<origin>/ (stale build artifacts) or "
                    "/work/dsynth/build/<slot>/construction/ during a "
                    "live build. For source overlays use "
                    "/work/DeltaPorts/ports/<origin>/."
                ),
                "path": chroot_path,
                "kind": "scaffolding_refused",
            }
    return None


# -----------------------------------------------------------------------------
# Tool functions (host-side, simple set — chroot-bound ones land in step 2c)
# -----------------------------------------------------------------------------


def list_dir(env: str, path: str, *, max_entries: int = 200) -> dict:
    """List the contents of a directory in the env's writable overlay.

    Returns up to ``max_entries`` entries, each with ``name``, ``kind``
    (file/dir/symlink/other), and ``size`` (for files). Useful when
    you don't know what's inside a directory or you need to find a
    config.log / patch / dragonfly-overlay file without a recursive
    grep.
    """
    refused = _reject_dsynth_scaffolding(path, op="list_dir")
    if refused is not None:
        return refused
    paths = env_paths(env)
    host = _resolve_chroot_path(paths, path)
    if not host.exists():
        return {"ok": False, "error": f"no such path: {path}", "kind": "missing", "path": path}
    if not host.is_dir():
        return {"ok": False, "error": f"not a directory: {path}", "kind": "not_a_directory", "path": path}
    entries: list[dict] = []
    truncated = False
    for i, child in enumerate(sorted(host.iterdir())):
        if i >= max_entries:
            truncated = True
            break
        if child.is_symlink():
            kind = "symlink"
            size = 0
        elif child.is_dir():
            kind = "dir"
            size = 0
        elif child.is_file():
            kind = "file"
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
        else:
            kind = "other"
            size = 0
        entries.append({"name": child.name, "kind": kind, "size": size})
    return {
        "ok": True,
        "path": path,
        "entries": entries,
        "truncated": truncated,
        "total_returned": len(entries),
    }


_GET_FILE_DEFAULT_LIMIT_LINES = 200


def get_file(
    env: str,
    path: str,
    *,
    offset_lines: int = 0,
    limit_lines: int = _GET_FILE_DEFAULT_LIMIT_LINES,
    start_line: int = 0,
    end_line: int = 0,
) -> dict:
    """Read ``path`` from the env's writable overlay, **line-windowed**.

    Returns up to ``limit_lines`` lines starting at zero-indexed
    ``offset_lines``. Default is the first 200 lines.

    Why line-windowed: whole-file reads pile up in the conversation
    history. A 200KB ``Makefile.in`` returned to the LLM becomes
    200KB of prompt on every subsequent turn — quadratic cost. A
    smoke run on devel/libuv burned 1.5M tokens almost entirely on
    re-sent ``Makefile.in`` content. With windowed reads + grep-first
    discipline, the same investigation costs an order of magnitude less.

    The fields you get back:

    - ``content``: the slice of the file as text (UTF-8) or base64 for
      binary; line-windowing applies only to text content
    - ``total_lines``: total lines in the source file
    - ``first_line`` / ``last_line``: 1-indexed inclusive range covered
    - ``truncated``: True iff there are more lines past ``last_line``
    - ``sha256``: hash over the **full** file's bytes (stable across
      windows so ``put_file(expected_sha256=...)`` works regardless)
    - ``hint`` *(when truncated)*: a line telling you how to resume

    For binary content (NULs / non-UTF-8), the read is still
    whole-file (no line concept), but capped at 32KB to avoid the
    same problem. Use ``grep`` instead for searching binary-ish files.
    """
    paths = env_paths(env)
    host = _resolve_chroot_path(paths, path)
    if not host.exists():
        return {
            "ok": False,
            "error": f"no such file: {path}",
            "kind": "missing",
            "path": path,
        }
    if host.is_dir():
        return {
            "ok": False,
            "error": f"path is a directory; use list_dir or grep instead: {path}",
            "kind": "is_directory",
            "path": path,
        }
    if not host.is_file():
        return {
            "ok": False,
            "error": f"not a regular file: {path}",
            "kind": "not_a_regular_file",
            "path": path,
        }
    data = host.read_bytes()
    full_sha = _sha256(data)
    full_size = len(data)

    text: str | None = None
    if b"\x00" not in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None

    if text is not None:
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        # Clamp inputs defensively. limit_lines<=0 means "use default";
        # offset_lines<0 means "from start".
        if limit_lines <= 0:
            limit_lines = _GET_FILE_DEFAULT_LIMIT_LINES
        # A 1-indexed inclusive range wins when given. "lines 201-212"
        # is how the model states it and how every other harness's tool
        # takes it; offset+count is kept for existing callers.
        if start_line > 0:
            start = min(max(start_line - 1, 0), total_lines)
            end = (min(end_line, total_lines) if end_line > 0
                   else min(start + limit_lines, total_lines))
            end = max(end, start)
        else:
            if offset_lines < 0:
                offset_lines = 0
            start = min(offset_lines, total_lines)
            end = min(start + limit_lines, total_lines)
        # cat -n: number, tab, content — the convention Claude Code, Zed
        # and Roo Code all use. Numbers are the FILE's, so a window
        # starting at line 166 begins at 166 rather than 1. Without this
        # the model rebuilds the mapping in its own reasoning: one
        # measured turn spent 53,124 chars transcribing a file to attach
        # numbers before it could write a patch (poly-pg4).
        window = "".join(
            f"{n:6d}\t{line}"
            for n, line in enumerate(lines[start:end], start=start + 1)
        )
        truncated = end < total_lines
        # When the window is empty (offset past EOF, or empty file),
        # report first_line=0 to signal "no content returned" rather
        # than a confusing 1-past-last value. Otherwise it's the
        # 1-indexed first line covered.
        if end > start:
            first_line = start + 1
        else:
            first_line = 0
        # Already shown this exact window, and the file has not changed
        # since? Then the bytes are still verbatim in the conversation,
        # and returning them again buys nothing while re-billing them as
        # fresh input. Point at them instead.
        #
        # The sha is what makes this safe: after a put_file the hash
        # differs and the new content is returned normally. Keyed on the
        # window too, so asking for a different slice always reads.
        key = (env, path, start, limit_lines)
        if _READ_CACHE.get(key) == full_sha:
            return {
                "path": path,
                "encoding": "text",
                "unchanged": True,
                "sha256": full_sha,
                "size": full_size,
                "total_lines": total_lines,
                "first_line": first_line,
                "last_line": end,
                "truncated": truncated,
                "note": (
                    f"Lines {first_line}-{end} of {path} are unchanged "
                    f"since you read them earlier in this attempt "
                    f"(sha256 {full_sha[:12]}…), so the content is not "
                    f"repeated — scroll back to it. Pass different "
                    f"offset_lines/limit_lines to read another part, or "
                    f"grep to search."
                ),
            }
        _READ_CACHE[key] = full_sha
        result = {
            "path": path,
            "encoding": "text",
            "content": window,
            # Explicit, so nothing downstream has to infer it from the
            # shape of the text — and so put_file's guard has a name to
            # point at when it refuses numbered content.
            "content_is_line_numbered": True,
            "sha256": full_sha,
            "size": full_size,
            "total_lines": total_lines,
            "first_line": first_line,
            "last_line": end,
            "truncated": truncated,
        }
        if truncated:
            remaining = total_lines - end
            result["hint"] = (
                f"{remaining} more line(s) past line {end}. To continue, "
                f"call get_file({path!r}, start_line={end + 1}). To narrow "
                f"down without reading the whole file, prefer grep "
                f"(returns only matching lines + context)."
            )
        return result

    # Binary path: cap at 32KB and report what was elided. The agent
    # almost never needs raw binary content anyway; this is a backstop.
    _MAX_BINARY_BYTES = 32_768
    if len(data) > _MAX_BINARY_BYTES:
        capped = data[:_MAX_BINARY_BYTES]
        return {
            "path": path,
            "encoding": "base64",
            "content": base64.b64encode(capped).decode("ascii"),
            "sha256": full_sha,
            "size": full_size,
            "truncated": True,
            "hint": (
                f"binary file capped at {_MAX_BINARY_BYTES} bytes of "
                f"{full_size}. Use grep or extract specific tools rather "
                f"than reading the raw bytes."
            ),
        }
    return {
        "path": path,
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
        "sha256": full_sha,
        "size": full_size,
        "truncated": False,
    }


def _reject_line_numbered_content(path: str, content: str) -> dict | None:
    """Refuse a write that still carries get_file's line numbers.

    get_file returns ``cat -n`` output — six-wide right-aligned number,
    tab, content — because the model otherwise rebuilds that mapping by
    hand at enormous cost. The hazard it introduces is that the model
    can echo the numbered text straight back into put_file and write a
    file where every line is prefixed with a number, silently corrupting
    it. Cheaper to refuse here than to debug a mangled patch later.

    Deliberately strict about what counts: three or more consecutive
    lines matching the exact emitted shape. Real files do contain
    number-then-tab lines — TSV data, some tables — but not the
    right-aligned six-column form repeated down the file.
    """
    lines = content.splitlines()
    if len(lines) < 3:
        return None
    numbered = re.compile(r"^\s{0,5}\d+\t")
    run = best = 0
    for line in lines:
        run = run + 1 if numbered.match(line) else 0
        best = max(best, run)
    if best < 3:
        return None
    return {
        "ok": False,
        "error": (
            f"refusing to write {path}: the content still has get_file's "
            f"line numbers on it ({best} consecutive numbered lines). "
            f"Those are display only — strip the number and tab from "
            f"each line before writing, or the file gets them baked in."
        ),
        "kind": "line_numbered_content",
        "path": path,
    }


def put_file(
    env: str,
    path: str,
    content: str,
    *,
    encoding: str = "text",
    expected_sha256: str | None = None,
) -> dict:
    """Write ``content`` to ``path`` in the env's writable overlay.

    ``encoding`` is ``"text"`` (UTF-8) or ``"base64"``. If
    ``expected_sha256`` is given, the existing file's sha256 must match
    or the write fails (optimistic lock against the LLM editing stale
    content). File mode is preserved for existing files.

    Writes under ``/work/DPorts/`` are refused — that tree is wiped on
    the next ``materialize_dports``. The error message points the
    agent at the correct ``/work/DeltaPorts/ports/<origin>/`` path.
    """
    refused = _reject_dports_write(path)
    if refused is not None:
        return refused
    refused = _reject_makefile_dragonfly_authoring(path)
    if refused is not None:
        return refused
    refused = _reject_orphan_dops_write(path)
    if refused is not None:
        return refused
    if encoding == "text":
        refused = _reject_line_numbered_content(path, content)
        if refused is not None:
            return refused
    paths = env_paths(env)
    host = _resolve_chroot_path(paths, path)

    if encoding == "base64":
        data = base64.b64decode(content.encode("ascii"))
    elif encoding == "text":
        data = content.encode("utf-8")
    else:
        raise ValueError(f"unknown encoding: {encoding}")

    if expected_sha256 is not None:
        if not host.is_file():
            raise RuntimeError(
                f"put_file: expected_sha256 given but file does not exist: {path}"
            )
        current = _sha256(host.read_bytes())
        if current != expected_sha256:
            raise RuntimeError(
                f"put_file: sha256 mismatch on {path} "
                f"(expected {expected_sha256[:12]}…, got {current[:12]}…)"
            )

    mode = host.stat().st_mode & 0o777 if host.is_file() else None
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_bytes(data)
    if mode is not None:
        host.chmod(mode)
    return {"path": path, "sha256": _sha256(data), "size": len(data)}


def _git_diff_with_untracked(env: str, rel: str) -> subprocess.CompletedProcess:
    """Run ``git diff`` for ``rel`` against HEAD, including untracked files.

    Plain ``git diff`` is silent on untracked files; agents that create
    new files (e.g. a fresh ``overlay.dops`` on a compat-mode port)
    would otherwise produce an empty diff despite a real edit.
    ``add --intent-to-add`` registers a placeholder entry so the new
    file shows up as an addition in the diff; ``reset`` after returns
    the index to its prior state so we leave no staged residue.

    Runs IN-CHROOT. It used to run on the host against
    ``<writable>/work/DeltaPorts``, which worked while that was an ordinary
    checkout. Since B1 it can be a linked worktree, and a worktree's ``.git``
    file holds an *absolute* ``gitdir:`` pointer written with the chroot's
    ``/work/...`` prefix — so host-side git in one fails outright with "not a
    git repository". Host-side access to the tree is now file I/O only.
    """
    return _git_in_ports_tree(env, f"diff -- {shlex.quote(rel)}", rel)


def _git_diff_against_base(
    env: str, base_branch: str, rel: str,
) -> subprocess.CompletedProcess:
    """Step 30 slice 2: capture the diff between the env's base
    branch and the current working tree for ``rel``.

    Combines committed deltas on the current bundle branch
    (convert's overlay.dops creation, files-removed deletions,
    etc.) with the agent's uncommitted working-tree edits (patch
    agent's overlay.dops edits). ``--intent-to-add``
    handles freshly-created files; the trailing reset returns the
    index to its prior state.

    Use case: ``analysis/delivery.diff`` — the artifact the
    Accept-delivery path sends to the configured provider. Unlike
    ``changes.diff`` (HEAD-relative, audit focus), this is
    "everything since upstream" and is therefore the correct
    shape for an upstream PR.
    """
    return _git_in_ports_tree(
        env, f"diff {shlex.quote(base_branch)} -- {shlex.quote(rel)}", rel,
    )


def _git_in_ports_tree(env: str, git_args: str, rel: str) -> subprocess.CompletedProcess:
    """Run one ``git <git_args>`` in the env's ports tree, bracketed by the
    intent-to-add/reset dance, and return the middle command's result.

    One place builds these so both diff helpers share the bracket and the
    chroot prefix. ``rel`` is added before and reset after, so a
    freshly-created file shows up as an addition without leaving staged
    residue.
    """
    q_rel = shlex.quote(rel)
    script = (
        f"cd {shlex.quote(PORTS_DIR)} && "
        f"git add --intent-to-add -- {q_rel} >/dev/null 2>&1; "
        f"git {git_args}; rc=$?; "
        f"git reset -- {q_rel} >/dev/null 2>&1; "
        f"exit $rc"
    )
    return _exec(env, "/bin/sh", "-c", script, cwd=PORTS_DIR)


def emit_diff(env: str, origin: str, relpath: str) -> dict:
    """Return the working-tree diff for ``ports/<origin>/<relpath>``.

    Pure read — never commits. Captures both modified-tracked and
    freshly-created files (the ``--intent-to-add`` dance in
    ``_git_diff_with_untracked``). ``diff`` is empty only when the path
    truly hasn't changed vs HEAD. ``ok`` is False only on git invocation
    errors (rc >= 128), not on "no changes" (rc=0).
    """
    paths = env_paths(env)
    rel = f"ports/{origin}/{relpath}"
    p = _git_diff_with_untracked(env, rel)
    diff_text, diff_trunc = _tail(p.stdout, max_bytes=_MAX_STREAM_BYTES * 2)
    return _exec_result(
        p.returncode, "", p.stderr,
        origin=origin,
        relpath=relpath,
        diff=diff_text,
        diff_truncated=diff_trunc,
    )


def grep(
    env: str,
    pattern: str,
    path: str,
    *,
    include: str | None = None,
    max_bytes: int = 8192,
    context: int = 3,
) -> dict:
    """Recursive grep over the env's writable overlay, with context lines.

    Uses POSIX ``grep -rn`` (always present on dfly) — not ripgrep,
    which isn't packaged for DragonFly. ``ok=True`` whenever grep
    ran without error, even when there were zero matches (rc=1 from
    grep is "no matches", not a failure). ``ok=False`` only when
    grep itself crashed (rc>=2) or the path is invalid.

    ``context`` (default 3) is the number of surrounding lines
    (``grep -C``) returned per match, so the agent rarely needs to
    fall back to ``get_file`` after grep. Set to 0 to suppress
    context entirely. The token cost difference between "matches +
    a few context lines" and "the whole 200KB Makefile.in" is the
    difference between affordable and not.
    """
    refused = _reject_dsynth_scaffolding(path, op="grep")
    if refused is not None:
        refused["pattern"] = pattern
        refused["matches"] = ""
        refused["match_count"] = 0
        return refused
    paths = env_paths(env)
    host = _resolve_chroot_path(paths, path)
    if not host.exists():
        return {
            "ok": False,
            "error": f"no such path: {path}",
            "pattern": pattern,
            "matches": "",
            "match_count": 0,
        }
    # -E extended regex (closest to rg's default), -r recursive, -n line numbers,
    # -I skip binary files, --include= glob (gnu/bsd grep both support it),
    # -C <n> context lines around each match (both GNU and BSD grep support it).
    cmd = ["grep", "-rnIE"]
    if context and context > 0:
        cmd.append(f"-C{int(context)}")
    if include:
        cmd.append(f"--include={include}")
    cmd.extend(["--", pattern, str(host)])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"grep invocation failed: {exc}",
            "pattern": pattern,
            "matches": "",
            "match_count": 0,
        }
    # grep exit codes: 0 = matches, 1 = no matches, ≥2 = error
    if p.returncode >= 2:
        return {
            "ok": False,
            "error": (p.stderr.strip() or f"grep exited with rc={p.returncode}"),
            "pattern": pattern,
            "matches": p.stdout,
            "match_count": 0,
        }
    output = p.stdout
    match_count = output.count("\n") if output else 0
    truncated = False
    if len(output) > max_bytes:
        output = output[:max_bytes]
        truncated = True
    return {
        "ok": True,
        "root": str(host),
        "pattern": pattern,
        "matches": output,
        "match_count": match_count,
        "truncated": truncated,
        "max_bytes": max_bytes,
    }


# -----------------------------------------------------------------------------
# Chroot-bound tool functions — all shell out via `dportsv3 dev-env exec`
# -----------------------------------------------------------------------------


def _exec(
    env: str,
    *argv: str,
    cwd: str = "/work/DeltaPorts",
    input_text: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` inside the dev-env chroot, return CompletedProcess.

    ``dev-env exec`` auto-mounts the env root on demand. Stdout/stderr
    are captured; callers decide how to surface them to the LLM.

    ``input_text`` is fed to the subprocess's stdin (useful for tools
    that prompt — e.g. dsynth's "rebuild local repository? [Y/n]"). If
    None, stdin is /dev/null (so an unexpected prompt fails fast rather
    than hanging).
    """
    return subprocess.run(
        [*_dev_env_cmd(), "exec", "--quiet", "--cwd", cwd, env, "--", *argv],
        capture_output=True,
        text=True,
        check=False,
        input=input_text if input_text is not None else "",
        timeout=timeout,
    )


# --------------------------------------------------------------------
# Compose-freshness tracking (refuses dsynth_build against a stale
# compose tree — observed false positive on devel/gperf 2026-05-26
# where dsynth claimed rebuild_ok=true against a pre-corruption
# compose because materialize_dports had failed 3× mid-attempt).
#
# State is per-(env, origin) and lives in-process: dsynth_build is
# always called inside an agent attempt that's also calling
# materialize_dports, so the in-memory dict is sufficient. The hash
# is recomputed from disk at each check, so external operator
# changes are still caught (the next dsynth_build will see a
# mismatch and refuse).
# --------------------------------------------------------------------

# (env, origin) → port-subtree content hash at last successful materialize
_MATERIALIZE_STATE: dict[tuple[str, str], str] = {}

# (env, path, offset, limit) → sha256 of the file when that exact window
# was last returned to the model, THIS ATTEMPT.
#
# Scoped to an attempt on purpose. The saving here is "you already have
# these bytes in your context"; a retry starts from a fresh message list,
# so carrying this across attempts would withhold content the model does
# not actually have. `reset_attempt_caches` is what enforces that, and
# attempt_loop calls it before every attempt.
_READ_CACHE: dict[tuple[str, str, int, int], str] = {}


def reset_attempt_caches() -> None:
    """Forget what the model has been shown; call at each attempt start."""
    _READ_CACHE.clear()


def _port_subtree_hash(env: str, origin: str) -> str:
    """Hash of ``ports/<origin>/`` contents in the env's writable layer.

    Used to detect "the substrate has changed since the last
    successful materialize_dports". Computed host-side using Python's
    hashlib over the writable overlay — same physical filesystem the
    chroot writes to, no need to enter the chroot. Reads files in
    sorted relpath order so the hash is stable across calls.

    Returns empty string on any error (no env paths, port subtree
    doesn't exist, OS-level read failure) — caller treats that as
    "no valid baseline" and refuses dsynth_build, which is the
    conservative answer.

    Prior shell-pipeline implementation used ``sort -z`` and
    ``xargs -0`` — both GNU-only flags. On DragonFly the BSD ``sort``
    rejects ``-z`` and the pipeline silently produced no hash,
    so every dsynth_build refused with "no successful
    materialize_dports" (observed on archivers/liblz4 2026-05-26).
    """
    try:
        paths = env_paths(env)
    except Exception:
        return ""
    port_dir = paths.deltaports / "ports" / origin
    if not port_dir.is_dir():
        return ""
    h = hashlib.sha256()
    try:
        for path in sorted(port_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(port_dir).as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            try:
                h.update(path.read_bytes())
            except OSError:
                return ""
            h.update(b"\0")
    except OSError:
        return ""
    return h.hexdigest()


def materialize_dports(env: str, origin: str) -> dict:
    """Regenerate the DPorts tree for ``origin`` using ``reapply`` inside the env.

    Returns a result dict (``ok``, ``rc``, ``stdout_tail``,
    ``stderr_tail``, etc.). The LLM inspects ``ok`` and the tails
    to decide what to do — no exceptions bubble up for build failures.

    Env-level failures (missing py311 deps, broken venv) used to be
    inferred from stderr here and tagged onto the result; that path
    moved to ``dportsv3.agent.health`` as a direct probe. The runner
    gates on health before claiming jobs; tool errors that look
    health-related trigger a cache-invalidating re-probe.

    On success, records the port subtree's content hash so
    :func:`dsynth_build` can detect a stale compose tree later in
    the same attempt.
    """
    p = _exec(env, "reapply", origin)
    result = _exec_result(p.returncode, p.stdout, p.stderr, origin=origin)
    if result.get("ok"):
        h = _port_subtree_hash(env, origin)
        if h:
            _MATERIALIZE_STATE[(env, origin)] = h
    else:
        # Failed materialize invalidates the baseline — substrate
        # may be in an intermediate state. Drop the entry; next
        # dsynth_build sees "no baseline" and refuses.
        _MATERIALIZE_STATE.pop((env, origin), None)
    return result


def materialize_dports_with_report(env: str, origin: str) -> dict:
    """Step 37: like :func:`materialize_dports`, but invokes reapply
    with ``--json`` so the caller gets the structured compose report
    instead of the text formatter's stdout.

    Returns a dict shaped like ``materialize_dports`` (``ok``, ``rc``,
    ``stdout_tail``, ``stderr_tail``) PLUS:

    - ``report``: parsed compose result dict, or ``None`` when the
      JSON didn't parse (compose's stdout may carry preamble lines
      before/around the JSON document; we try to recover but degrade
      gracefully).

    Used by the convert defer loop to identify which framework patch
    failed via ``report['ports'][i]['dops_failed_op_results'][j]
    ['diagnostics'][0]['source_path']`` directly — no text scraping,
    no dependency on the human formatter's bracket suffix being
    deployed.
    """
    p = _exec(env, "reapply", "--json", origin)
    result = _exec_result(p.returncode, p.stdout, p.stderr, origin=origin)
    try:
        report = json.loads((p.stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        report = None
    result["report"] = report if isinstance(report, dict) else None
    return result


WRKDIRPREFIX = "/work/obj"

# Why the agent's `make` calls set NO_DEPENDS.
#
# A dsynth builder installs the port's dependencies from the repo it
# built itself (`pkg install /packages/All/<pkgfile>`), so inside a
# builder every version matches the tree. The dev-env chroot has no such
# repo: its /usr/local comes from the base, provisioned once from the
# official mirror, which tracks DragonFly releases rather than a ports
# branch. The base is null-mounted read-only and shared across envs, so
# nothing can install a tree-matched package into it either.
#
# `*-depends` resolve with DEPENDS_TARGET=install, which walks into each
# unsatisfied dependency and builds it. Against a mismatched /usr/local
# that recursion dies on a version skew several ports deep, long before
# do-extract runs — a wall the agent cannot see and cannot fix by
# editing the port's patches.
#
# We don't need those dependencies. `make extract` unpacks the distfile
# and `make patch` applies files/patch-* then dragonfly/*; both use tools
# from the base. bsd.port.mk guards every depends type on this one
# variable:
#
#     .for deptype in PKG EXTRACT PATCH FETCH BUILD LIB RUN TEST
#     ${deptype:tl}-depends:
#     .  if defined(${deptype}_DEPENDS) && !defined(NO_DEPENDS)
#
# Its only other uses are in `fetch-required`/`fetch-required-list`,
# which we never invoke, and it leaves WRKSRC, PATCHDIR, PATCH_LIST,
# EXTRACT_ONLY and DISTFILES untouched.
#
# The cost: a port whose extract genuinely needs an unarchiver package
# now fails in do-extract rather than while installing it. Base bsdtar
# covers gz/bz2/xz/zst, so this is rare — and `make_extract` names
# EXTRACT_DEPENDS in the failure so it reads as a missing tool rather
# than a corrupt distfile.
NO_DEPENDS = "NO_DEPENDS=yes"

# Why the agent's `make` calls set PACKAGE_BUILDING.
#
# bsd.default-versions.mk derives a handful of *_DEFAULT knobs from what
# is installed rather than from what the tree declares. PERL5_DEFAULT is
# the one that bites:
#
#     .  if !exists(${LOCALBASE}/bin/perl) || (!defined(_PORTS_ENV_CHECK) \
#         && defined(PACKAGE_BUILDING))
#     PERL5_DEFAULT?=     5.42                    # what the tree declares
#     .  elif !defined(PERL5_DEFAULT)
#     _PERL5_FROM_BIN!=   ${LOCALBASE}/bin/perl -e 'printf "%vd\n", $$^V;'
#     PERL5_DEFAULT:=     ${_PERL5_FROM_BIN:R}    # what happens to be installed
#
# The base's perl is whatever the official mirror had; the tree wants a
# newer one. So PERL5_DEFAULT resolves to the installed version, perl5.mk
# rejects it as unsupported, and IGNORE is set. An IGNOREd port has no
# real targets at all — bsd.port.mk replaces extract, patch and eleven
# others with `echo the reason; exit 1` — so the agent cannot even unpack
# a distfile to re-cut a patch, and NO_DEPENDS above never gets reached.
#
# dsynth sets this in every builder's make.conf (dsynth's build.c:
# addbuildenv("PACKAGE_BUILDING", "yes", BENV_MAKECONF)), which is why
# the same tree builds there and IGNOREs here. Setting it makes the
# agent read the tree the way the builder it is repairing reads it,
# rather than diverging from it a second way.
#
# Not a bypass: with it set the framework itself stops objecting. The
# port-facing effects run the same direction — NO_PACKAGE and
# MANUAL_PACKAGE_BUILD ports become IGNOREd, exactly as they are on the
# farm, so the agent declines them instead of pretending. WRKSRC,
# PATCHDIR, PATCH_LIST, EXTRACT_ONLY, DISTFILES and DISTNAME are
# unchanged (verified against the tree on hardware).
PACKAGE_BUILDING = "PACKAGE_BUILDING=yes"


# Per-(env, origin) WRKSRC cache, populated by `make_extract()` and
# read by `genpatch()` (to invoke the script from the right cwd with a
# WRKSRC-relative arg).
#
# Stays at module scope because the worker is otherwise stateless;
# entries live for the runner process's lifetime. A worker restart
# between extract and a downstream call empties the cache — those
# downstream calls then fall back to legacy behavior. The patch
# agent's per-attempt opening procedure already calls make_extract
# first, so the cache is repopulated naturally on the next attempt.
_WRKSRC_CACHE: dict[tuple[str, str], str] = {}


# Step 38a: per-env compose-target cache. The runner populates this
# at attempt start (process_patch_job / process_convert_job) from
# job["target"] so `get_effective_overlay` can scope-filter the
# overlay against the env's build target. Empty value (None or "")
# is the @any default.
_TARGET_CACHE: dict[str, str | None] = {}


def set_env_target(env: str, target: str | None) -> None:
    """Record the env's compose target for downstream target-scoped
    reads (``get_effective_overlay``).

    Called by the runner at attempt start.
    """
    _TARGET_CACHE[env] = target


def peek_env_target(env: str) -> str | None:
    """Non-destructive read of the cached compose target for `env`.

    Returns None on cache miss (no target set → fall back to @any).
    """
    return _TARGET_CACHE.get(env)


def get_effective_overlay(env: str, origin: str) -> dict:
    """Return the scope-filtered, ordered ops effective for the env's
    build target — Step 38f's agent-visible read surface.

    Today the agent reads ``overlay.dops`` via ``get_file`` and has to
    mentally apply scope filtering: walk top-to-bottom tracking the
    active ``target @X`` directive, keep ops whose scope is ``@any``
    or matches the env's target, drop the rest. With multi-target
    overlays enabled by 38d this gets error-prone fast. This function
    externalizes the work into a tool — feed it origin, get back
    structured ops the engine would actually apply.

    Returns a dict with::

        {
            "ok": True,
            "target": "<env target>",
            "effective_ops": [<PlanOp dict>, ...],
            "filtered_out": [<PlanOp dict + reason>, ...],
            "overlay_path": "ports/<origin>/overlay.dops",
        }

    Or on failure::

        {"ok": False, "error": "<actionable message>"}

    Failure modes:

    - Env target not in the cache (runner didn't call
      ``set_env_target``) → refuse with a calling-context-bug
      message. Same surfacing as ``_append_overlay``'s @current path.
    - Overlay parses but fails semantic checks (operator hand-edit,
      invalid scope, etc.) → refuse with the engine's first diagnostic.
    - Overlay parses but emits no scoped ops → return empty lists.

    On a port with no ``overlay.dops`` (a fresh port that's never had
    agent edits), ``ok=True`` with empty lists — not an error.
    """
    from dportsv3.engine.api import build_plan  # noqa: PLC0415

    target = peek_env_target(env)
    if not target:
        return {
            "ok": False,
            "error": (
                f"env {env!r} has no compose target cached "
                f"(t.target is unset). This indicates a calling-context "
                f"bug: the cache should be populated at job start by "
                f"worker.set_env_target. Retrying will not help — "
                f"escalate."
            ),
        }

    paths = env_paths(env)
    workspace = paths.deltaports
    if not workspace.is_dir():
        return {
            "ok": False,
            "error": (
                f"env workspace does not exist: {workspace}. "
                f"Verify the env is set up before reading overlay state."
            ),
        }

    overlay_path = workspace / "ports" / origin / "overlay.dops"
    overlay_relpath = str(overlay_path.relative_to(workspace))

    if not overlay_path.is_file():
        return {
            "ok": True,
            "target": target,
            "effective_ops": [],
            "filtered_out": [],
            "overlay_path": overlay_relpath,
        }

    try:
        text = overlay_path.read_text()
    except OSError as exc:
        return {
            "ok": False,
            "error": f"read failed for {overlay_relpath}: {exc}",
        }

    plan_result = build_plan(text, source_path=overlay_path)
    if not plan_result.ok or plan_result.plan is None:
        # Surface the first engine diagnostic verbatim.
        first = (
            plan_result.diagnostics[0] if plan_result.diagnostics else None
        )
        msg = (
            f"engine refused {overlay_relpath}: {first.code}: {first.message}"
            if first
            else f"engine refused {overlay_relpath} (no diagnostic returned)"
        )
        return {"ok": False, "error": msg}

    effective: list[dict] = []
    filtered: list[dict] = []
    for op in plan_result.plan.ops:
        op_dict = op.to_dict()
        # Rename per-op `target` → `scope` for the agent-facing
        # response. PlanOp.target IS the scope the op is bound to,
        # but the top-level `target` field in this dict already means
        # "the env's build target" — two different meanings need
        # two different names. Throughout Step 38 we've used `scope`
        # for the per-op layer; align the response shape.
        op_dict["scope"] = op_dict.pop("target")
        if op.target in ("@any", target):
            effective.append(op_dict)
        else:
            filtered.append({
                **op_dict,
                "reason": (
                    f"scope {op.target} does not match env target {target}"
                ),
            })

    return {
        "ok": True,
        "target": target,
        "effective_ops": effective,
        "filtered_out": filtered,
        "overlay_path": overlay_relpath,
    }


def probe_overlay_facts(env: str, origin: str):
    """Collect raw overlay facts from inside the dev-env chroot."""
    from dportsv3.agent.overlay_state import (  # noqa: PLC0415
        OverlayFacts,
        makefile_dragonfly_text_auto_safe,
    )

    cmd = r'''
set -u
PORT="$DELTAPORTS_ROOT/ports/$1"
[ -d "$PORT" ] || { echo MISSING=1; exit 0; }
echo MISSING=0
[ -e "$PORT/overlay.dops" ] && echo DOPS=1 || echo DOPS=0
for f in "$PORT"/Makefile.DragonFly "$PORT"/Makefile.DragonFly.@any; do
    [ -e "$f" ] && echo MKDFLY=${f##*/}
done
for f in "$PORT"/Makefile.DragonFly.*; do
    [ -e "$f" ] || continue
    case "${f##*/}" in Makefile.DragonFly.@any) continue ;; esac
    echo TARGET_MKDFLY=${f##*/}
done
if [ -d "$PORT/dragonfly" ]; then
    find "$PORT/dragonfly" -type f | sed "s|^$PORT/||; s|^|DRAGONFLY_FILE=|"
fi
if [ -d "$PORT/diffs" ]; then
    find "$PORT/diffs" -type f \( -name '*.diff' -o -name '*.patch' \) \
        | sed "s|^$PORT/||; s|^|DIFF_FILE=|"
fi
[ -d "$PORT/newport" ] && echo NEWPORT=1 || echo NEWPORT=0
'''
    p = _exec(env, "/bin/sh", "-c", cmd, "_", origin)
    if p.returncode != 0:
        raise RuntimeError(
            f"overlay fact probe failed for {origin!r} in env "
            f"{env!r} (rc={p.returncode}): "
            f"{(p.stderr or '').strip()[:300]}"
        )

    flags: dict[str, list[str] | bool] = {
        "MKDFLY": [],
        "TARGET_MKDFLY": [],
        "DRAGONFLY_FILE": [],
        "DIFF_FILE": [],
    }
    for line in (p.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in {"MKDFLY", "TARGET_MKDFLY", "DRAGONFLY_FILE", "DIFF_FILE"}:
            flags.setdefault(key, [])
            assert isinstance(flags[key], list)
            flags[key].append(value)
        else:
            flags[key] = value == "1"

    if flags.get("MISSING"):
        return OverlayFacts(origin=origin, port_exists=False)

    makefiles = tuple(sorted(flags.get("MKDFLY") or ()))
    targeted = tuple(sorted(flags.get("TARGET_MKDFLY") or ()))
    auto_safe = False
    reasons: list[str] = []
    if len(makefiles) == 1 and makefiles[0] == "Makefile.DragonFly":
        cat_cmd = 'cat "$DELTAPORTS_ROOT/ports/$1/Makefile.DragonFly"'
        cat = _exec(env, "/bin/sh", "-c", cat_cmd, "_", origin)
        if cat.returncode == 0:
            auto_safe, reason = makefile_dragonfly_text_auto_safe(cat.stdout)
        else:
            auto_safe, reason = False, "makefile_read_error"
        reasons.append(reason)
    elif makefiles or targeted:
        reasons.append("targeted_or_multiple_makefile_dragonfly")

    return OverlayFacts(
        origin=origin,
        port_exists=True,
        overlay_dops=bool(flags.get("DOPS")),
        makefile_dragonfly=makefiles,
        targeted_makefile_dragonfly=targeted,
        dragonfly_files=tuple(sorted(flags.get("DRAGONFLY_FILE") or ())),
        diff_files=tuple(sorted(flags.get("DIFF_FILE") or ())),
        newport=bool(flags.get("NEWPORT")),
        auto_safe_makefile=auto_safe,
        makefile_reasons=tuple(reasons),
    )


def assess_dops(env: str, origin: str):
    """Return the shared overlay assessment for a port in the dev-env."""
    from dportsv3.agent.overlay_state import assess_overlay  # noqa: PLC0415
    return assess_overlay(probe_overlay_facts(env, origin))


def classify_dops(env: str, origin: str) -> str:
    """Classify a port's dops state by inspecting the dev-env's
    writable overlay (the substrate the convert agent writes into).

    Returns one of: ``converted`` / ``auto_safe_pending`` /
    ``needs_judgment`` / ``not_in_scope``.

    Probes facts inside the chroot, then feeds the shared
    overlay-state assessment used by host-side tooling. The probe
    touches:

    - ``$DELTAPORTS_ROOT/ports/<origin>/overlay.dops``
    - ``$DELTAPORTS_ROOT/ports/<origin>/Makefile.DragonFly[.*]``
    - ``$DELTAPORTS_ROOT/ports/<origin>/dragonfly/`` (with content)
    - ``$DELTAPORTS_ROOT/ports/<origin>/diffs/`` (with .diff/.patch)
    - ``$DELTAPORTS_ROOT/ports/<origin>/newport/``

    The classification rules match :func:`dportsv3.agent.dops.classify`,
    but file checks happen against the env's view, not the host clone.
    """
    return assess_dops(env, origin).state


import shlex  # noqa: E402 — needed by assert_port_clean / reset_port


def assert_port_clean(env: str, origin: str) -> dict:
    """Step 25g: assert the env's ``ports/<origin>/`` subtree is
    at git HEAD with no uncommitted or untracked changes.

    Returns ``{ok: bool, dirty_paths: list[str]}``. dirty_paths is
    the parsed output of ``git status --porcelain`` for the
    subtree — empty when ok=True.

    Used by:
    - verify-fix's pre-replay check (apply-and-build refuses to
      replay against a dirty port subtree; operator must
      ``git stash`` or ``dportsv3 dev-env reset-port`` first)
    - the patch flow's pre-job invariant (future 25d slice)
    - operator inspection via the CLI

    Runs ``git status`` inside the chroot for substrate parity
    with every other tool.
    """
    rel = f"ports/{origin}"
    p = _exec(
        env, "/bin/sh", "-c",
        f"cd /work/DeltaPorts && git status --porcelain "
        f"--untracked-files=all -- {shlex.quote(rel)}",
        cwd="/work/DeltaPorts",
    )
    if p.returncode != 0:
        return _exec_result(
            p.returncode, p.stdout or "", p.stderr or "",
            error=f"git status failed for {rel}",
        )
    raw = (p.stdout or "").rstrip("\n")
    if not raw:
        return {"ok": True, "dirty_paths": [], "untracked_only": True}
    # Each porcelain line: "XY path" (X=index, Y=worktree, a space,
    # then the path). "??" is untracked. Keep the status code so
    # callers can tell a freshly bootstrapped (untracked) overlay
    # baseline apart from leftover tracked edits from a prior run.
    # For renames git emits "old -> new" — we keep the new side.
    dirty: list[str] = []
    untracked_only = True
    for line in raw.splitlines():
        if not line:
            continue
        status = line[:2]
        if status != "??":
            untracked_only = False
        path = line[3:].strip() if len(line) > 3 else line[2:].strip()
        if "->" in path:
            path = path.split("->", 1)[1].strip()
        dirty.append(path)
    return {
        "ok": False,
        "dirty_paths": dirty,
        "untracked_only": untracked_only,
    }


def reset_port(env: str, origin: str) -> dict:
    """Wipe the per-origin WRKDIR and re-materialize the compose tree, so the
    next job starts from a pristine WRKDIR and a baseline-derived composed
    tree.

    Both of those live outside the job's worktree — ``/work/obj`` and
    ``/work/artifacts/compose`` — and are shared across jobs, so destroying
    the worktree does not clean them and this still has work to do.

    What it no longer does is reset the source checkout. That stage existed to
    stop one job's escaped writes reaching the next one through the shared
    checkout; since B1 each job works in its own worktree, which is destroyed
    with the job.

    Stages, in order:

    1. Best-effort ``make clean`` against the compose root to
       remove the WRKDIR under ``$WRKDIRPREFIX/<origin>/<version>/``
       along with any ``.orig`` files / agent edits the prior job
       left in WRKSRC. Runs first because the agent's edits to the
       substrate haven't been undone yet — so the in-tree Makefile
       (or any .DragonFly fragments) still reflects the patched
       state ``make clean`` was authored against.

    2. ``reapply <origin>`` — regenerate
       ``/work/artifacts/compose/<target>/<origin>/``.

       READ THIS BEFORE RELYING ON IT. This does NOT compose from a
       reset substrate, despite what it used to claim. Stage 1's source
       reset was removed when B1 gave each job its own worktree, and
       that worktree is destroyed later, in ``runner.py``, after this
       runs. So the substrate here still holds the agent's edits and
       this composes them.

       It succeeds — it composed *something* — so ``reapply_ok`` is
       True and nothing warns, while the shared compose tree is left
       contradicting the repository. Measured on devel/glib20: a later
       build reported "packages built: 3, failed: 0" against a tree
       missing two of the port's five patches (poly-15l).

       Correctness therefore does not rest on this. Each consumer
       composes the origin for itself before it works — see the
       compose-at-start in ``steps.py``. This stage remains only so an
       operator poking at the env between jobs finds something
       plausible rather than a half-written tree.

       Best-effort: a reapply failure here means baseline HEAD
       itself doesn't compose cleanly — which was already the
       state when we started, so it isn't a regression caused by
       reset. Surfaces as ``reapply_ok``/``reapply_error`` for
       diagnosis but does not flip ``ok``.

    Cache hygiene: ``_clean_port_workdir`` drops ``_WRKSRC_CACHE``
    and ``_MATERIALIZE_STATE`` entries for ``(env, origin)`` so any
    code path that holds a cached path/hash sees the cache miss and
    re-derives.

    Both stages are best-effort: their failures surface as
    ``workdir_clean_*`` / ``reapply_*`` keys in the result but do not flip
    ``ok``. Nothing here is load-bearing any more — the stage that was is
    gone, and correctness now rests on the worktree being thrown away.
    """
    # 1. Best-effort WRKDIR cleanup — runs against the still-patched
    # substrate (the in-tree Makefile/.DragonFly is what ``make clean``
    # was authored against). ok stays True even on failure.
    workdir = _clean_port_workdir(env, origin)

    # The substrate reset that used to sit here is gone. It ran
    # `git checkout HEAD -- . && git clean -fd` over the whole tree because
    # writes that escaped ports/<origin> — a slave port patched via its
    # master's PATCHDIR, a failed attempt's leftovers — would otherwise
    # persist in the shared checkout and poison the next job. There is no
    # shared checkout now: the job's tree is a worktree that is destroyed with
    # the job, so those writes cannot outlive it.
    #
    # The other two stages stay. They clean the WRKDIR under /work/obj and the
    # composed tree under /work/artifacts/compose, both of which live outside
    # the worktree and are shared across jobs.

    # 2. Re-materialize the compose tree from the baseline substrate.
    # Best-effort: a failure here reflects baseline state we didn't
    # cause and shouldn't mask as a reset failure.
    rp = _exec(env, "reapply", origin)
    reapply_ok = (rp.returncode == 0)

    result = {
        "ok": True,
        "origin": origin,
        "workdir_clean_ok": bool(workdir.get("ok")),
        "reapply_ok": reapply_ok,
    }
    if not workdir.get("ok"):
        # Surface diagnosis without flipping the overall result.
        # Prefer the actual stderr tail since the static ``error``
        # label is just "make clean failed for <origin>" and the
        # stderr usually carries the make/target-specific reason.
        result["workdir_clean_error"] = (
            workdir.get("stderr_tail") or workdir.get("error", "")
        )[:300]
    if not reapply_ok:
        result["reapply_error"] = (
            (rp.stderr or "") or (rp.stdout or "")
        )[-300:]
    return result


def is_slave_port(env: str, origin: str) -> bool:
    """True if ``origin`` is a slave port (its Makefile sets ``MASTERDIR``).

    Detected from the composed port Makefile rather than ``make -V
    MASTERDIR`` to avoid invoking the ports framework: a slave's own
    Makefile carries an explicit ``MASTERDIR=`` assignment (the standard
    slave convention), and non-slaves never do. A bare ``MASTERDIR``
    *mention* (e.g. ``.include "${MASTERDIR}/Makefile"``) is not enough —
    we key on the assignment at line start.

    Fail-open: any probe error (no cached target, missing Makefile,
    grep error) returns False, so a hiccup never wrongly escalates a
    normal port to MANUAL.
    """
    target = peek_env_target(env) or ""
    if not target:
        return False
    mk = f"/work/artifacts/compose/{target}/{origin}/Makefile"
    p = _exec(
        env, "/bin/sh", "-c",
        f"grep -qE '^MASTERDIR[[:space:]]*[?:]?=' {shlex.quote(mk)}",
        cwd="/work/DeltaPorts",
    )
    return p.returncode == 0


def _clean_port_workdir(env: str, origin: str) -> dict:
    """Best-effort ``make clean`` for ``<origin>``'s WRKDIR + cache
    invalidation. Used by :func:`reset_port` as the first stage —
    runs against the still-patched substrate so the in-tree Makefile
    (which the existing WRKDIR was authored against) is still
    present.

    Runs ``make clean`` against ``$DPORTS_COMPOSE_ROOT/<origin>``
    with ``WRKDIRPREFIX=/work/obj`` so the per-origin WRKDIR
    under ``/work/obj/<origin>/<version>/`` is removed along
    with everything the agent's ``dupe``/``genpatch``/``put_file``
    edits dropped into WRKSRC.

    ``NOCLEANDEPENDS`` — not ``NO_DEPENDS``, which does not reach this
    target — keeps that to *this* port. bsd.port.mk's ``clean`` pulls in
    ``limited-clean-depends``, which walks the dependency graph running
    ``make clean`` in each port it finds. We only ever asked for one
    WRKDIR, and the walk goes looking for dependencies the dev-env's
    package universe cannot satisfy anyway.

    Drops the in-process ``_WRKSRC_CACHE`` and ``_MATERIALIZE_STATE``
    entries for ``(env, origin)`` regardless of the make rc —
    once we've asked for the WRKDIR to go away, any cached path
    or content hash for it is stale by definition.
    """
    _WRKSRC_CACHE.pop((env, origin), None)
    _MATERIALIZE_STATE.pop((env, origin), None)
    cmd = (
        'set -e; '
        f'cd "$DPORTS_COMPOSE_ROOT/{origin}"; '
        f'make PORTSDIR="$DPORTS_COMPOSE_ROOT" '
        f'     WRKDIRPREFIX="{WRKDIRPREFIX}" '
        f'     NOCLEANDEPENDS=yes '
        f'     BATCH=yes clean'
    )
    p = _exec(env, "/bin/sh", "-c", cmd)
    if p.returncode != 0:
        return _exec_result(
            p.returncode, p.stdout, p.stderr,
            error=f"make clean failed for {origin}",
        )
    return {
        "ok": True,
        "origin": origin,
        "stdout_tail": (p.stdout or "")[-512:],
    }


# --------------------------------------------------------------------
# Per-bundle branches.
#
# Each bundle gets its own branch ``bundle/<bundle_id>``, so the env's base
# branch stays at upstream and no convert commits accumulate across bundles.
# A bundle's convert → retriage → patch chain reuses the branch; verify runs
# on a throwaway ``bundle/<id>-verify`` recut from base each time.
#
# The branch is checked out by ``create_job_worktree``, in a worktree of its
# own that is destroyed at job end. Nothing switches branches on a shared
# checkout any more, so there is no lifecycle to sequence and nothing to
# restore afterwards.
# --------------------------------------------------------------------

# Per-env cache of the resolved base branch (``master``/``main``).
# Reading it costs a subprocess per call and an env's base does not change
# during its lifetime, so caching is safe — but only successful reads go in.
# Caching a value derived from a failed read is what made the previous
# implementation return a wrong base for the whole process lifetime.
_BUNDLE_BASE_BRANCH_CACHE: dict[str, str] = {}


def _resolve_bundle_base_branch(env: str) -> str | None:
    """The branch new ``bundle/<id>`` branches are cut from, or None.

    Reads the branch actually checked out in ``PORTS_MAIN_DIR``. That is the
    right authority: dev-env clones that checkout with
    ``git clone --single-branch --branch <deltaports_branch>``, so its HEAD
    *is* the branch the env was built on, and nothing moves it afterwards —
    ``sync``'s ``reset --hard`` moves the branch pointer without detaching,
    and ``update`` fast-forwards.

    This used to read ``git symbolic-ref refs/remotes/origin/HEAD``, on the
    stated assumption that a mirror clone usually sets it. ``--single-branch``
    never sets it, so that read failed on every env and the ``|| echo master``
    fallback supplied the answer every time — correct only where the branch
    happened to be ``master``.

    Addresses ``PORTS_MAIN_DIR``, not ``PORTS_DIR``: the latter is the
    agent-facing symlink, which points into a job's worktree while one runs.

    Returns None when the branch cannot be determined — a failed command, no
    output, or a detached HEAD. Callers must refuse to build rather than
    guess; cutting a bundle branch from the wrong base is silent and
    poisons everything branched from it afterwards.
    """
    cached = _BUNDLE_BASE_BRANCH_CACHE.get(env)
    if cached:
        return cached
    p = _exec(
        env, "/bin/sh", "-c",
        f"git -C {shlex.quote(PORTS_MAIN_DIR)} rev-parse --abbrev-ref HEAD",
        cwd="/work",
    )
    base = (p.stdout or "").strip()
    # "HEAD" is what rev-parse prints for a detached checkout — a branch name
    # is required, since the worktree is created with `-b <new> <base>`.
    if p.returncode != 0 or not base or base == "HEAD":
        return None
    _BUNDLE_BASE_BRANCH_CACHE[env] = base
    return base


def _branch_name_for(bundle_id: str) -> str:
    """Branch naming convention: ``bundle/<bundle_id>``. Stripping
    the trailing ``.job`` extension if a job filename was passed by
    accident — bundle IDs themselves don't carry it but caller
    plumbing sometimes does."""
    name = bundle_id.strip()
    if name.endswith(".job"):
        name = name[:-len(".job")]
    return f"bundle/{name}"


# In-chroot layout. Deliberately literals rather than an import of
# ``dports_dev_env.layout``: this module drives dev-env through its CLI, not
# its internals (see the module docstring), and ``/work/DeltaPorts`` is already
# spelled out throughout. ``test_worker_layout_agrees_with_dev_env`` pins these
# against dev-env's constants so the two cannot drift apart silently.
PORTS_DIR = "/work/DeltaPorts"
PORTS_MAIN_DIR = "/work/ports-main"


def _job_worktree_name(bundle_id: str, kind: str) -> str:
    """Directory name for a job's worktree: ``job-<kind>-<bundle_id>``.

    Lives directly under ``/work/`` beside the main checkout, so the symlink
    that ``PORTS_DIR`` is can point at it with a *relative* target. That
    matters: the link is followed both from inside the chroot and from the
    host's writable layer, and only a relative target resolves under both.
    """
    ident = bundle_id.strip().removesuffix(".job")
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in ident)
    return f"job-{kind}-{safe}"


def _point_ports_link(env: str, target: str) -> subprocess.CompletedProcess:
    """Point the env's ports-tree name at ``target`` (a sibling directory
    name under ``/work``).

    ``rm`` then ``ln`` rather than ``ln -sfn``/``ln -sfh``: the two spellings
    of "don't follow the existing link" differ between GNU and BSD, and this
    runs on DragonFly.
    """
    return _exec(
        env, "/bin/sh", "-c",
        f"rm -f {shlex.quote(PORTS_DIR)} && "
        f"ln -s {shlex.quote(target)} {shlex.quote(PORTS_DIR)}",
        cwd="/work",
    )


def create_job_worktree(env: str, bundle_id: str, kind: str = "patch") -> dict:
    """Give this job its own checkout of the ports tree.

    Replaces "check a branch out on the one shared tree": each job gets a
    linked worktree off the main checkout's object store, and ``PORTS_DIR`` is
    repointed at it. Every attempt in the job shares this one worktree, exactly
    as attempts share the tree today.

    ``kind="patch"`` reuses the bundle's branch, creating it off base the first
    time, so a bundle's convert->patch chain keeps its commits. ``kind="verify"``
    force-recuts from base: the verify gate replays the complete changes.diff on
    a clean base and must not inherit the agent's commits.

    Returns the standard result dict plus ``branch``, ``base``, ``worktree``
    (chroot path) and ``created``.
    """
    if not bundle_id:
        return {"ok": False, "error": "bundle_id is required"}
    if kind not in ("patch", "verify"):
        return {"ok": False, "error": f"unknown worktree kind: {kind}"}

    branch = (
        _branch_name_for(bundle_id) if kind == "patch"
        else _verify_branch_name_for(bundle_id)
    )
    base = _resolve_bundle_base_branch(env)
    if base is None:
        return {
            "ok": False,
            "error": (
                f"could not determine the base branch from "
                f"{PORTS_MAIN_DIR} in env {env!r} (detached HEAD, or the "
                f"checkout is unreadable). Refusing to guess: a bundle "
                f"branch cut from the wrong base is silent."
            ),
            "branch": branch,
        }
    name = _job_worktree_name(bundle_id, kind)
    worktree = f"/work/{name}"
    q_wt, q_branch, q_base = shlex.quote(worktree), shlex.quote(branch), shlex.quote(base)
    git = f"git -C {shlex.quote(PORTS_MAIN_DIR)}"

    # Clear any leftover from a job that died before teardown; a stale worktree
    # would otherwise make `worktree add` fail on an occupied path.
    _exec(env, "/bin/sh", "-c",
          f"{git} worktree remove --force {q_wt} >/dev/null 2>&1; "
          f"rm -rf {q_wt}; {git} worktree prune",
          cwd="/work")

    if kind == "verify":
        add = f"{git} worktree add --force -B {q_branch} {q_wt} {q_base}"
    else:
        # -B would discard the bundle branch's existing commits.
        add = (
            f"if {git} rev-parse --verify --quiet refs/heads/{q_branch} >/dev/null; then "
            f"{git} worktree add --force {q_wt} {q_branch}; else "
            f"{git} worktree add --force -b {q_branch} {q_wt} {q_base}; fi"
        )
    add_p = _exec(env, "/bin/sh", "-c", add, cwd="/work")
    if add_p.returncode != 0:
        return _exec_result(
            add_p.returncode, add_p.stdout, add_p.stderr,
            error=f"worktree add for {branch} failed",
            branch=branch, base=base, worktree=worktree,
        )

    link_p = _point_ports_link(env, name)
    if link_p.returncode != 0:
        return _exec_result(
            link_p.returncode, link_p.stdout, link_p.stderr,
            error=f"could not point {PORTS_DIR} at {name}",
            branch=branch, base=base, worktree=worktree,
        )
    return {
        "ok": True, "branch": branch, "base": base,
        "worktree": worktree, "created": True,
    }


def sweep_job_worktrees(env: str, keep: tuple[str, ...] = ()) -> dict:
    """Remove every per-job worktree in the env except those named in ``keep``.

    Crash recovery (B3). A job that dies between ``create_job_worktree`` and
    ``destroy_job_worktree`` leaves its tree behind; nothing else reclaims it,
    because the next job for a *different* bundle gets a different directory.

    Works on directory NAMES, not bundle ids. ``_job_worktree_name`` sanitizes
    the id (any character outside ``[A-Za-z0-9._-]`` becomes ``_``), so the
    mapping is lossy and a swept name cannot be turned back into the bundle it
    came from. ``destroy_job_worktree`` is therefore the wrong tool here.

    The ports link is restored to the main checkout first: it may be pointing
    at one of the directories about to be removed.

    Safe to call when nothing is stale — it is a no-op that still prunes the
    worktree admin entries of directories that vanished.
    """
    link_p = _point_ports_link(env, PORTS_MAIN_DIR.rpartition("/")[2])
    if link_p.returncode != 0:
        return _exec_result(
            link_p.returncode, link_p.stdout, link_p.stderr,
            error=f"could not restore {PORTS_DIR} before sweeping",
            removed=[], kept=list(keep),
        )

    git = f"git -C {shlex.quote(PORTS_MAIN_DIR)}"
    keep_list = " ".join(shlex.quote(k) for k in keep)
    # The `case` re-checks the job- prefix the glob already implies: this
    # loop runs rm -rf, and a guard that cannot fire is cheaper than the
    # one time the glob is edited into something broader.
    script = f"""
set -u
cd /work || exit 1
{git} worktree prune
for d in job-*; do
    [ -d "$d" ] || continue
    case "$d" in job-*) ;; *) continue ;; esac
    for k in {keep_list or "''"}; do
        [ "$d" = "$k" ] && continue 2
    done
    {git} worktree remove --force "/work/$d" >/dev/null 2>&1
    rm -rf "/work/$d"
    printf 'removed %s\n' "$d"
done
{git} worktree prune
"""
    p = _exec(env, "/bin/sh", "-c", script, cwd="/work")
    removed = [
        line.split(" ", 1)[1]
        for line in (p.stdout or "").splitlines()
        if line.startswith("removed ")
    ]
    return _exec_result(
        p.returncode, p.stdout, p.stderr,
        removed=removed, kept=list(keep),
    )


def destroy_job_worktree(
    env: str, bundle_id: str, kind: str = "patch", *, drop_branch: bool = False,
) -> dict:
    """Throw the job's worktree away.

    The point of B1: there is no tree to scrub afterwards, because the tree
    the job dirtied does not outlive it. ``PORTS_DIR`` goes back to the main
    checkout first, so nothing is left addressing a directory being removed.

    ``drop_branch`` also deletes the branch — right for the throwaway verify
    branch, wrong for a bundle branch whose commits a later job still wants.
    """
    if not bundle_id:
        return {"ok": False, "error": "bundle_id is required"}
    if kind not in ("patch", "verify"):
        return {"ok": False, "error": f"unknown worktree kind: {kind}"}

    branch = (
        _branch_name_for(bundle_id) if kind == "patch"
        else _verify_branch_name_for(bundle_id)
    )
    name = _job_worktree_name(bundle_id, kind)
    worktree = f"/work/{name}"
    q_wt, q_branch = shlex.quote(worktree), shlex.quote(branch)
    git = f"git -C {shlex.quote(PORTS_MAIN_DIR)}"

    link_p = _point_ports_link(env, PORTS_MAIN_DIR.rpartition("/")[2])
    if link_p.returncode != 0:
        return _exec_result(
            link_p.returncode, link_p.stdout, link_p.stderr,
            error=f"could not restore {PORTS_DIR}",
            branch=branch, worktree=worktree,
        )

    cmd = f"{git} worktree remove --force {q_wt}; rm -rf {q_wt}; {git} worktree prune"
    if drop_branch:
        cmd += f"; {git} branch -D {q_branch} >/dev/null 2>&1 || true"
    rm_p = _exec(env, "/bin/sh", "-c", cmd, cwd="/work")
    return _exec_result(
        rm_p.returncode, rm_p.stdout, rm_p.stderr,
        branch=branch, worktree=worktree, dropped_branch=drop_branch,
    )


def _verify_branch_name_for(bundle_id: str) -> str:
    """Throwaway branch name verify-fix runs on: ``bundle/<id>-verify``.

    Distinct from the patch/convert ``bundle/<id>`` branch so verify
    can't collide with (or inherit commits from) the agent's working
    branch. Verify recreates this from base every run — changes.diff
    is the complete canonical artifact, so no prior commits are
    needed."""
    return _branch_name_for(bundle_id) + "-verify"


def commit_port_changes(
    env: str, origin: str, message: str, paths: str | None = None,
) -> dict:
    """Commit working-tree changes under ``ports/<origin>/`` (or a
    narrower ``paths`` subpath) to the env's git, so the next job's
    preflight sees a clean HEAD.

    Used for the triage-bootstrap → patch handoff: triage's ``put_file``
    landing of ``overlay.dops`` produces an untracked file in the env's
    writable layer; without this commit the patch job hits
    ``patch_preflight_dirty`` and dies. Pass ``paths`` (e.g.
    ``ports/<origin>/overlay.dops``) to commit ONLY that path — the
    caller is responsible for having verified nothing else under the
    port should be swept in. Defaults to the whole ``ports/<origin>/``
    subtree.

    No-op when the target is already clean (no diff, no untracked). The
    commit author is set to a dportsv3-managed identity so
    operator-authored commits stay distinguishable.

    Returns the standard worker result dict. ``committed: bool``
    distinguishes "committed N files" from "nothing to commit".
    """
    rel = paths or f"ports/{origin}"
    # `git add -A` picks up tracked-modified, deleted, AND untracked
    # files. `git diff --cached --quiet` returns non-zero when the
    # index has staged changes — that's our signal to commit.
    # `git -c user.name=... -c user.email=... commit` keeps the
    # config local to this invocation so we don't mutate the env's
    # git config.
    safe_msg = message.replace("'", "'\\''")
    cmd = (
        f"cd /work/DeltaPorts && "
        f"git add -A -- {shlex.quote(rel)} && "
        f"if git diff --cached --quiet -- {shlex.quote(rel)}; then "
        f"  echo 'nothing-to-commit'; "
        f"else "
        f"  git -c user.name=dportsv3-runner "
        f"      -c user.email=runner@dportsv3 "
        f"      commit -m '{safe_msg}' -- {shlex.quote(rel)}; "
        f"fi"
    )
    p = _exec(env, "/bin/sh", "-c", cmd, cwd="/work/DeltaPorts")
    out = (p.stdout or "")
    err = (p.stderr or "")
    if p.returncode != 0:
        return _exec_result(
            p.returncode, out, err,
            error=f"commit_port_changes failed for {rel}",
        )
    committed = "nothing-to-commit" not in out
    return {
        "ok": True,
        "origin": origin,
        "committed": committed,
        "paths_changed": [rel] if committed else [],
        "stdout_tail": out[-1024:],
    }


def validate_dops(env: str, origin: str) -> dict:
    """Run ``dportsv3 dsl check`` against the port's overlay.dops.

    Used by the convert agent to validate its rewrite *before*
    emitting the Conversion Proof. Cheap — parse + semantic check
    via dportsv3.engine.api, no compose, no filesystem materialize.

    Returns ``ok=True`` only when the dsl check exits 0 (no
    diagnostics). On failure, the diagnostics (stderr) carry the
    line/column/code so the agent can fix and retry.

    The full compose-side check still runs in
    ``_verify_conversion`` after the agent finishes — this is the
    cheap inner-loop validation, not a replacement.

    Invokes the wrapper via the ``POLYTROPOS_ROOT`` env var that
    dev-env's ``build_env_dict`` sets inside the chroot — it isn't on
    the chroot's PATH. The overlay it checks is addressed via
    ``DELTAPORTS_ROOT``: two different trees since the repository
    split, and the wrapper is not in the ports one.
    """
    cmd = (
        '"$POLYTROPOS_ROOT/bin/dportsv3" dsl check '
        '"$DELTAPORTS_ROOT/ports/$1/overlay.dops"'
    )
    p = _exec(env, "/bin/sh", "-c", cmd, "_", origin)
    return _exec_result(
        p.returncode, p.stdout, p.stderr,
        origin=origin,
        dops_path=f"$DELTAPORTS_ROOT/ports/{origin}/overlay.dops",
    )


_DOPS_QUICKREF_PATH = Path(__file__).resolve().parent / "dops_quickref.md"


def dops_reference(env: str) -> dict:
    """Return the on-demand dops quick-reference.

    Co-located with the agent module but NOT inside the playbooks
    directory, so the playbook selector doesn't ship it in every payload. Call once
    per patch attempt at most, only after confirming there's no
    ``overlay.dops`` yet and you intend to write one.

    The full normative grammar lives at ``docs/dsl-v0.md`` — this
    file is the minimal subset patch agents need most often. The
    ``env`` argument is unused (tool signature parity); the content
    is identical across envs.
    """
    try:
        text = _DOPS_QUICKREF_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "error": f"failed to read dops quick-reference: {exc}",
            "path": str(_DOPS_QUICKREF_PATH),
        }
    return {
        "ok": True,
        "content": text,
        "encoding": "text",
        "size": len(text),
        "source": "dops_quickref.md (co-located; full grammar: docs/dsl-v0.md)",
    }


def _extracted_wrksrc(env: str, origin: str) -> tuple[str, str]:
    """``(WRKDIR, WRKSRC)`` if the distfile is really unpacked, else ``("", "")``.

    Asked only after a non-zero ``make extract``, to tell "nothing was
    unpacked" apart from "unpacked, and then a later step failed". Both
    paths come from ``make -V`` rather than being derived from each other:
    WRKSRC is not always a direct child of WRKDIR.

    Emptiness is checked on disk, not inferred. A port whose do-extract
    fails outright still leaves WRKDIR behind, so the existence of the
    directory proves nothing; its contents do.
    """
    cmd = (
        f'cd "$DPORTS_COMPOSE_ROOT/{origin}"; '
        f'set -- $(make PORTSDIR="$DPORTS_COMPOSE_ROOT" '
        f'              WRKDIRPREFIX="{WRKDIRPREFIX}" '
        f'              {PACKAGE_BUILDING} '
        f'              BATCH=yes -V WRKDIR -V WRKSRC 2>/dev/null); '
        f'[ $# -eq 2 ] || exit 1; '
        f'[ -d "$2" ] || exit 1; '
        f'[ -n "$(ls -A "$2" 2>/dev/null)" ] || exit 1; '
        f'printf "%s\n%s\n" "$1" "$2"'
    )
    r = _exec(env, "/bin/sh", "-c", cmd)
    if r.returncode != 0:
        return "", ""
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return (lines[0], lines[1]) if len(lines) == 2 else ("", "")


def make_extract(env: str, origin: str) -> dict:
    """Run ``make extract`` against the **compose root** —
    ``$DPORTS_COMPOSE_ROOT`` (= ``/work/artifacts/compose/<target>``).

    Critically not the lock root at ``/work/DPorts/``. The lock root is
    the last-known-good DPorts checkout (what DeltaPorts STATUS tracks
    as ``Last success``); ``materialize_dports`` does NOT update it.
    Building against it would silently use stale Makefile + distinfo
    and produce a WRKSRC pointing at the wrong version. The compose
    root is what dsynth builds and what extract must target.

    Returns ``wrkdir`` + ``wrksrc`` from ``make -V``. ``WRKDIRPREFIX``
    is redirected to ``/work/obj`` (the writable overlay) because
    bsd.port.mk's default (``/usr/obj/dports``) is read-only, and
    ``NO_DEPENDS`` and ``PACKAGE_BUILDING`` are both set because the
    dev-env's package universe does not match the tree. They clear
    different walls — ``PACKAGE_BUILDING`` stops the framework IGNOREing
    the port over an installed version, ``NO_DEPENDS`` stops the targets
    that survive from recursing into a read-only /usr/local. See the
    constants for the full reasoning.

    ``$DPORTS_COMPOSE_ROOT`` is set in the dev-env's chroot environment
    (see ``build_env_dict`` in tools/dev-env helpers); we expand it in
    the child shell rather than threading the target through every
    caller.
    """
    extract_cmd = (
        'set -e; '
        f'cd "$DPORTS_COMPOSE_ROOT/{origin}"; '
        f'make PORTSDIR="$DPORTS_COMPOSE_ROOT" '
        f'     WRKDIRPREFIX="{WRKDIRPREFIX}" '
        f'     {NO_DEPENDS} '
        f'     {PACKAGE_BUILDING} '
        f'     BATCH=yes extract'
    )
    p = _exec(env, "/bin/sh", "-c", extract_cmd)
    if p.returncode != 0:
        # Distinguish "this port is unbuildable in this environment"
        # from "extract went wrong". bsd.port.mk sets IGNORE when a
        # precondition fails (an unsupported ${X}_DEFAULT, a missing
        # dependency it refuses to guess at) and then stops. No amount
        # of work on the port's patches changes that, and an agent that
        # keeps going will improvise around a wall it cannot see.
        # `make -V IGNORE` is the framework's own answer, so ask it
        # rather than pattern-matching the error text.
        # PACKAGE_BUILDING has to be set here too. It changes how
        # bsd.default-versions.mk resolves the *_DEFAULT knobs, and IGNORE
        # is derived from those — so a probe without it answers a question
        # about a different environment than the one that just failed, and
        # reports a wall the real command never hit.
        ignore_cmd = (
            f'cd "$DPORTS_COMPOSE_ROOT/{origin}"; '
            f'make PORTSDIR="$DPORTS_COMPOSE_ROOT" '
            f'     WRKDIRPREFIX="{WRKDIRPREFIX}" '
            f'     {PACKAGE_BUILDING} '
            f'     BATCH=yes -V IGNORE'
        )
        ig = _exec(env, "/bin/sh", "-c", ignore_cmd)
        reason = ig.stdout.strip() if ig.returncode == 0 else ""
        if reason:
            return _exec_result(
                p.returncode, p.stdout, p.stderr,
                origin=origin, wrkdir="", wrksrc="",
                blocking=True,
                ignore_reason=reason,
                summary=(
                    f"{origin} is IGNOREd by the ports framework in this "
                    f"environment: {reason}. This is an environment "
                    f"problem, not a defect in the port's patches — the "
                    f"source cannot be extracted, so the patch cannot be "
                    f"regenerated. Stop and report it."
                ),
            )
        # Not IGNOREd, so the extract itself broke. We skip
        # extract-depends (see NO_DEPENDS above), which for the handful
        # of ports that need a real unarchiver means do-extract runs
        # without it and fails looking like a corrupt distfile. Name the
        # dependency we skipped so it reads as what it is.
        deps_cmd = (
            f'cd "$DPORTS_COMPOSE_ROOT/{origin}"; '
            f'make PORTSDIR="$DPORTS_COMPOSE_ROOT" '
            f'     WRKDIRPREFIX="{WRKDIRPREFIX}" '
            f'     {PACKAGE_BUILDING} '
            f'     BATCH=yes -V EXTRACT_DEPENDS'
        )
        ed = _exec(env, "/bin/sh", "-c", deps_cmd)
        declared = ed.stdout.strip() if ed.returncode == 0 else ""
        # The source may be on disk anyway. `post-extract` runs after
        # do-extract has unpacked the distfile, and a port whose
        # post-extract copies files out of an EXTRACT_DEPENDS package
        # fails there with a complete, usable WRKSRC already written.
        # glib20 is the measured case: fetch, checksum and extract all
        # succeed, then post-extract dies copying pkgconfig out of
        # gobject-introspection@bootstrap. The agent only needs the
        # source — it re-cuts patches, it does not build — so reporting
        # a failure here sends it away from work it could do.
        salvaged_dir, salvaged = _extracted_wrksrc(env, origin)
        if salvaged:
            _WRKSRC_CACHE[(env, origin)] = salvaged
            return _exec_result(
                0, p.stdout, p.stderr,
                origin=origin, wrkdir=salvaged_dir, wrksrc=salvaged,
                extract_depends=declared,
                post_extract_failed=True,
                summary=(
                    f"`make extract` unpacked {origin} and then failed in a "
                    f"later step; WRKSRC is present and usable at {salvaged}. "
                    + (
                        f"The port declares EXTRACT_DEPENDS={declared}, which "
                        f"the dev-env cannot install, and that is the likely "
                        f"cause. "
                        if declared else ""
                    )
                    + "Proceed with dupe/genpatch against WRKSRC; do not "
                      "treat this as a distfile problem."
                ),
            )
        if declared:
            return _exec_result(
                p.returncode, p.stdout, p.stderr,
                origin=origin, wrkdir="", wrksrc="",
                extract_depends=declared,
                summary=(
                    f"`make extract` FAILED for {origin}, which declares "
                    f"EXTRACT_DEPENDS={declared}. Those are not installed "
                    f"— the dev-env cannot install tree-matched packages "
                    f"— so if the failure is a missing extract tool it is "
                    f"an environment problem, not a defect in the port's "
                    f"patches. Check stdout_tail/stderr_tail before "
                    f"treating this as a distfile or checksum problem."
                ),
            )
        return _exec_result(p.returncode, p.stdout, p.stderr,
                            origin=origin, wrkdir="", wrksrc="")
    # Same PACKAGE_BUILDING as the extract above. The measured values are
    # identical either way, but a variable query has to describe the same
    # environment as the command it describes — otherwise the agent can
    # unpack to one path and go looking in another.
    query_cmd = (
        'set -e; '
        f'cd "$DPORTS_COMPOSE_ROOT/{origin}"; '
        f'make PORTSDIR="$DPORTS_COMPOSE_ROOT" '
        f'     WRKDIRPREFIX="{WRKDIRPREFIX}" '
        f'     {PACKAGE_BUILDING} '
        f'     BATCH=yes -V WRKDIR -V WRKSRC'
    )
    q = _exec(env, "/bin/sh", "-c", query_cmd)
    if q.returncode != 0:
        return _exec_result(q.returncode, q.stdout, q.stderr,
                            origin=origin, wrkdir="", wrksrc="",
                            extract_step="query-wrkdir")
    lines = [line.strip() for line in q.stdout.splitlines() if line.strip()]
    wrkdir = lines[0] if len(lines) > 0 else ""
    wrksrc = lines[1] if len(lines) > 1 else ""
    # Cache WRKSRC so genpatch() can invoke the script from the
    # right cwd with a WRKSRC-relative arg (producing clean
    # patch-<rel> filenames).
    if wrksrc:
        _WRKSRC_CACHE[(env, origin)] = wrksrc
    summary = (
        f"Extracted {origin} from the compose root. "
        f"wrksrc={wrksrc} — use this exact path. "
        f"Do NOT look under /work/DPorts/{origin}/ (that's the lock "
        f"root, last-known-good versions, NOT the just-extracted source)."
        if wrksrc else
        f"Extracted {origin}, but wrkdir/wrksrc query returned empty."
    )
    return _exec_result(0, p.stdout, p.stderr,
                        origin=origin, wrkdir=wrkdir, wrksrc=wrksrc,
                        summary=summary)


def make_patch(env: str, origin: str) -> dict:
    """Run ``make patch`` against the **compose root** — the
    ``do-patch`` phase that ``make_extract`` deliberately does NOT run.

    ``make_extract`` only unpacks the distfile; WRKSRC is pristine
    upstream afterward. ``do-patch`` is what applies ``files/patch-*``
    (the FreeBSD framework patches) and then the port's
    ``dragonfly/*`` patches, in that order, leaving WRKSRC in the
    actual state dsynth builds from.

    Call this AFTER ``materialize_dports(origin)`` + ``make_extract(
    origin)`` and BEFORE ``dupe``/``genpatch`` when you are authoring a
    new ``dragonfly/`` patch that must sit on top of ``files/``
    modifications: ``dupe`` snapshots the file's post-``do-patch``
    state, so ``genpatch``'s baseline matches what ``do-patch`` will
    see at build time. Without it, ``dupe`` would snapshot pristine
    upstream and the generated hunk's context would not match the
    build-time tree, rejecting at ``dsynth_build``.

    Same target/env model as ``make_extract``: compose root,
    ``WRKDIRPREFIX=/work/obj``, ``NO_DEPENDS``, ``PACKAGE_BUILDING``.
    On failure the per-patch reject
    (``Hunk #N ... FAILED``) is in stdout/stderr — surfaced in the
    tails so the caller can see WHICH patch rejected without a
    separate log read. ``do-patch`` writes a ``.patch_done`` cookie
    and is a no-op on re-run; run it once per extract.
    """
    patch_cmd = (
        'set -e; '
        f'cd "$DPORTS_COMPOSE_ROOT/{origin}"; '
        f'make PORTSDIR="$DPORTS_COMPOSE_ROOT" '
        f'     WRKDIRPREFIX="{WRKDIRPREFIX}" '
        f'     {NO_DEPENDS} '
        f'     {PACKAGE_BUILDING} '
        f'     BATCH=yes patch'
    )
    p = _exec(env, "/bin/sh", "-c", patch_cmd)
    if p.returncode != 0:
        return _exec_result(
            p.returncode, p.stdout, p.stderr, origin=origin,
            summary=(
                f"`make patch` FAILED for {origin}. A framework "
                f"(files/patch-*) or dragonfly/* patch rejected — see "
                f"stdout_tail/stderr_tail for the `Hunk #N ... FAILED` "
                f"line identifying which patch. WRKSRC is now "
                f"half-patched; do NOT dupe/genpatch against it."
            ),
        )
    return _exec_result(
        0, p.stdout, p.stderr, origin=origin,
        summary=(
            f"Patched {origin}: applied files/patch-* then dragonfly/* "
            f"into WRKSRC. WRKSRC now matches the build-time tree — "
            f"safe to dupe/edit/genpatch a new dragonfly patch on top."
        ),
    )


def dupe(env: str, path: str) -> dict:
    """Run ``dupe PATH`` inside the chroot (clones source file with .orig backup).

    Returns a result dict; LLM inspects ``ok``.
    """
    p = _exec(env, "dupe", path)
    return _exec_result(p.returncode, p.stdout, p.stderr, path=path)


def genpatch(env: str, path: str) -> dict:
    """Run ``genpatch PATH`` inside the chroot.

    The wrapped script (`ports-mgmt/genpatch`) has two requirements
    we now satisfy explicitly:

    1. It checks `realpath /usr/obj/dports` to compute its strip-prefix
       unless `$WORKTREE` is set. Our extract uses `WRKDIRPREFIX=
       /work/obj` because the DragonFly default is read-only, so the
       fallback fails — we always set `WORKTREE=/work/obj`.
    2. It encodes its `$1` arg verbatim into the patch filename
       (`s|/|_|g` etc.). To get the canonical `dragonfly/patch-<rel>`
       naming, the caller must `cd <WRKSRC>` and pass a WRKSRC-relative
       arg. The wrapper does this when `_WRKSRC_CACHE` has a wrksrc
       entry whose prefix `path` starts with.

    Two staging conventions, depending on cache state:

    - Cache hit: patch lands in WRKSRC with a clean WRKSRC-relative
      encoded name.
    - install_patches flow (cache miss): patch lands in
      `/work/genpatch-out/` with a full-path-encoded filename.
      `install_patches` copies from there into the port's
      `dragonfly/` subtree by basename, so the encoded filename is
      irrelevant — only the location matters.

    Returns ``patch_path`` / ``patch_basename`` on cache-hit so
    callers can locate the file without re-deriving the encoding.
    """
    import shlex  # noqa: PLC0415

    # Find the (env, origin) whose cached WRKSRC contains `path`.
    # Multiple ports can be cached for one env; we match on prefix.
    matched_wrksrc: str | None = None
    matched_origin: str | None = None
    for (cached_env, cached_origin), cached_wrksrc in _WRKSRC_CACHE.items():
        if cached_env != env:
            continue
        ws = cached_wrksrc.rstrip("/")
        if path == ws or path.startswith(ws + "/"):
            matched_wrksrc = ws
            matched_origin = cached_origin
            break

    patch_basename: str | None = None
    patch_path: str | None = None
    paths = env_paths(env)
    genpatch_out = paths.writable / "work" / "genpatch-out"

    if matched_wrksrc is not None:
        # Cache hit — resolve the file's path relative to WRKSRC.
        relpath = (
            "" if path == matched_wrksrc
            else path[len(matched_wrksrc) + 1:]
        )
        if not relpath:
            # Caller passed the WRKSRC root itself, not a file under
            # it. genpatch can't operate on a directory.
            return _exec_result(
                1, "", f"path {path!r} is the WRKSRC root, not a file",
                error="genpatch expects a file path under WRKSRC",
                path=path,
            )
        patch_basename = (
            "patch-" + relpath.replace("_", "__").replace("/", "_")
        )
        patch_path = f"{matched_wrksrc}/{patch_basename}"
        cmd = (
            f'cd {shlex.quote(matched_wrksrc)} && '
            f'WORKTREE={shlex.quote(WRKDIRPREFIX)} '
            f'genpatch {shlex.quote(relpath)}'
        )
    else:
        # Cache miss — legacy install_patches staging. `mkdir -p` is
        # cheap and guards against a wiped /work/genpatch-out.
        cmd = (
            'mkdir -p /work/genpatch-out && '
            'cd /work/genpatch-out && '
            f'WORKTREE={shlex.quote(WRKDIRPREFIX)} '
            f'genpatch {shlex.quote(path)}'
        )

    p = _exec(env, "/bin/sh", "-c", cmd)

    # List both staging dirs for diagnostic completeness — operators
    # debugging a cache-miss case may be looking at the legacy dir
    # while we expected the wrksrc one (or vice versa).
    patches: list[str] = []
    if genpatch_out.is_dir():
        for f in sorted(genpatch_out.iterdir()):
            if f.is_file() and f.name.startswith("patch-"):
                patches.append(f.name)

    return _exec_result(
        p.returncode, p.stdout, p.stderr,
        path=path,
        wrksrc=matched_wrksrc,
        origin=matched_origin,
        patch_path=patch_path,
        patch_basename=patch_basename,
        output_dir=str(genpatch_out),
        patches=patches,
    )


def install_patches(env: str, origin: str, patches: list[str] | None = None) -> dict:
    """Copy patches from ``/work/genpatch-out/`` into DeltaPorts overlay.

    Destination is
    ``<env_dir/writable>/work/DeltaPorts/ports/<origin>/dragonfly/``.
    Host-side file copy; no chroot exec needed since both source and
    destination are in the writable overlay. If ``patches`` is None,
    every ``patch-*`` file in ``genpatch-out/`` is installed.
    """
    paths = env_paths(env)
    src = paths.writable / "work" / "genpatch-out"
    dst = paths.deltaports / "ports" / origin / "dragonfly"
    if not src.is_dir():
        raise FileNotFoundError(f"genpatch output dir does not exist: {src}")
    if patches is None:
        candidates = [f for f in sorted(src.iterdir()) if f.is_file() and f.name.startswith("patch-")]
    else:
        candidates = [src / name for name in patches]
        missing = [str(p) for p in candidates if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"missing patches: {missing}")
    dst.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for f in candidates:
        target = dst / f.name
        shutil.copy2(f, target)
        installed.append(str(target.relative_to(paths.deltaports)))
    return {"origin": origin, "destination": str(dst), "installed": installed}


DSYNTH_LOGS_DIR = "/work/dsynth/logs"


def _dsynth_log_stem(origin: str) -> str:
    """dsynth's log filename stem: '/' becomes '___' in the origin."""
    return origin.replace("/", "___")


def _dsynth_log_candidates(env: str, origin: str) -> list[Path]:
    """Every per-port log dsynth wrote for ``origin``, newest first.

    dsynth writes one log PER FLAVOR, suffixed ``@<flavor>``, so a
    flavored port has no ``<origin>.log`` at all — devel/glib20 produces
    devel___glib20@bootstrap.log and devel___glib20@default.log and
    nothing else. Building the unflavored name and stopping there is why
    this used to report "no log" for a port whose logs were sitting in
    the directory the agent then listed by hand.
    """
    stem = _dsynth_log_stem(origin)
    try:
        logs = env_paths(env).writable / "work" / "dsynth" / "logs"
        found = [p for p in (logs / f"{stem}.log",) if p.is_file()]
        found += sorted(logs.glob(f"{stem}@*.log"))
        return sorted(set(found), key=lambda p: p.stat().st_mtime,
                      reverse=True)
    except (OSError, RuntimeError):
        # Resolving the env can fail outright. Callers use this to
        # decorate a result, never to decide anything, so degrade to
        # "no logs known" rather than turning a hint into an exception.
        return []


def _dsynth_log_path(origin: str) -> str:
    """The unflavored in-chroot log path, for messages only.

    Only a hint: a flavored port has no such file. Callers that need a
    real path use ``_dsynth_log_candidates``.
    """
    return f"{DSYNTH_LOGS_DIR}/{_dsynth_log_stem(origin)}.log"


def dsynth_build(env: str, origin: str) -> dict:
    """Run ``dsynth build <origin>`` inside the chroot.

    Invokes dsynth directly (not via the ``dbuild`` helper) with:
    - ``-S`` to disable the ncurses TUI (otherwise stdout is curses
      escape codes the LLM can't parse)
    - ``-y`` to assume-yes on all prompts (no stdin gymnastics)

    The result dict carries ``log_hint``: the in-chroot path to the
    per-port build log dsynth actually wrote, and ``log_flavors`` when
    the port has more than one. On failure, the agent should call
    ``dsynth_log(origin)`` to read it — stdout/stderr_tail here only
    capture the wrapper output, not the actual build error.

    The ``dbuild`` helper at
    ``scripts/tools/dev-env/dports_dev_env/helpers.py:62-90`` is
    intentionally not used: it doesn't pass -S/-y because humans use
    it interactively.
    """
    # Stale-compose guard: refuse if substrate state at last
    # successful materialize_dports doesn't match the substrate
    # state now. Without this, dsynth builds against a stale
    # compose tree from an earlier materialize and falsely reports
    # rebuild_ok=true even when the agent's intervening edits
    # haven't been re-composed (seen on devel_gperf-20260526-064013Z
    # — 3 mid-attempt materialize failures but dsynth still passed
    # against the pre-corruption tree).
    baseline = _MATERIALIZE_STATE.get((env, origin))
    if baseline is None:
        return {
            "ok": False,
            "rebuild_ok": False,
            "origin": origin,
            "error": (
                f"dsynth_build refused: no successful "
                f"materialize_dports for {origin!r} in this attempt. "
                f"Call materialize_dports first; the compose tree "
                f"on disk may be from a stale state."
            ),
            "blocked_by": "stale_compose",
        }
    current = _port_subtree_hash(env, origin)
    if current and current != baseline:
        return {
            "ok": False,
            "rebuild_ok": False,
            "origin": origin,
            "error": (
                f"dsynth_build refused: ports/{origin}/ has changed "
                f"since the last successful materialize_dports "
                f"(baseline sha {baseline[:12]}…, now {current[:12]}…). "
                f"Re-run materialize_dports to refresh the compose "
                f"tree before building."
            ),
            "blocked_by": "stale_compose",
        }

    # Read DPORTS_DSYNTH_PROFILE inside the chroot (set by dev-env's
    # build_env_dict at helpers.py:113) and invoke dsynth directly.
    # Using sh -c so we can reference the env var on the chroot side.
    #
    # ``/work/.dports-agent-hooks-disabled`` short-circuits
    # dsynth-hooks/hook_common.sh so an agent-driven build never
    # produces a new failure bundle. There is one env per target —
    # the same env the operator uses for production dsynth runs — so
    # without this guard every failed attempt the agent makes would
    # fire the hooks, upload a new bundle, and the runner would
    # enqueue another triage job for an origin the agent is already
    # actively patching. That's an unbounded loop in the worst case
    # and pure waste in the best.
    #
    # A flag file (not an env var) because dsynth strips arbitrary
    # env vars before invoking hooks. The trap on EXIT cleans up even
    # if dsynth exits non-zero. ``/work`` is the dev-env's writable
    # overlay so we can write/remove it freely from any in-chroot
    # process.
    cmd = (
        'flag=/work/.dports-agent-hooks-disabled; '
        'trap "rm -f \\"$flag\\"" EXIT; '
        ': > "$flag"; '
        'dsynth -S -y -p "$DPORTS_DSYNTH_PROFILE" build "$1"'
    )
    p = _exec(env, "/bin/sh", "-c", cmd, "_", origin)
    # Name the log dsynth actually wrote. The unflavored path is a
    # guess that is wrong for every flavored port, and handing the
    # model a path that does not exist costs it turns discovering so.
    written = _dsynth_log_candidates(env, origin)
    log_hint = (f"{DSYNTH_LOGS_DIR}/{written[0].name}" if written
                else _dsynth_log_path(origin))
    return _exec_result(
        p.returncode, p.stdout, p.stderr,
        origin=origin,
        rebuild_ok=p.returncode == 0,
        log_hint=log_hint,
        log_flavors=[_dsynth_log_flavor(x, origin) for x in written],
    )


def _dsynth_log_flavor(path: Path, origin: str) -> str:
    """The flavor a log filename encodes, or "" for an unflavored one."""
    stem = _dsynth_log_stem(origin)
    name = path.name[:-len(".log")] if path.name.endswith(".log") else path.name
    return name[len(stem) + 1:] if name.startswith(f"{stem}@") else ""


def dsynth_log(env: str, origin: str, tail_lines: int = 200,
               flavor: str = "") -> dict:
    """Read the tail of dsynth's per-port build log.

    Call this when ``dsynth_build`` returned ``rebuild_ok=false``;
    the actual error message lives here, not in dsynth_build's
    stdout (which is just wrapper output).

    dsynth writes one log per flavor. Pass ``flavor`` to pick one;
    without it the most recently written is used and the rest are
    named in ``available_flavors`` so a second call can be exact.
    """
    candidates = _dsynth_log_candidates(env, origin)
    if flavor:
        stem = _dsynth_log_stem(origin)
        wanted = f"{stem}@{flavor.lstrip('@')}.log"
        picked = [p for p in candidates if p.name == wanted]
        if not picked:
            return {
                "ok": False,
                "error": (
                    f"no log for flavor {flavor!r}; dsynth wrote "
                    f"{[p.name for p in candidates]}"
                ),
                "origin": origin,
                "log_path": "",
                "available_flavors": [_dsynth_log_flavor(p, origin)
                                      for p in candidates],
                "tail": "",
            }
        candidates = picked
    if not candidates:
        logs_dir = env_paths(env).writable / "work" / "dsynth" / "logs"
        return {
            "ok": False,
            "error": (
                f"dsynth wrote no log for {origin} under {DSYNTH_LOGS_DIR}. "
                f"It writes one only after a build starts, so run "
                f"dsynth_build first."
            ),
            "origin": origin,
            "log_path": "",
            "available_flavors": [],
            "tail": "",
        }
    log_path = candidates[0]
    try:
        text = log_path.read_text(errors="replace")
    except OSError as exc:
        return {
            "ok": False,
            "error": f"read failed: {exc}",
            "origin": origin,
            "log_path": str(log_path),
            "tail": "",
        }
    lines = text.splitlines()
    if tail_lines > 0 and len(lines) > tail_lines:
        truncated = True
        kept = lines[-tail_lines:]
    else:
        truncated = False
        kept = lines
    return {
        "ok": True,
        "origin": origin,
        "log_path": f"{DSYNTH_LOGS_DIR}/{log_path.name}",
        "flavor": _dsynth_log_flavor(log_path, origin),
        "available_flavors": [_dsynth_log_flavor(p, origin)
                              for p in candidates],
        "tail": "\n".join(kept),
        "truncated": truncated,
        "total_lines": len(lines),
    }
