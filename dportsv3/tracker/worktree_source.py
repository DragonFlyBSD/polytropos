"""Where the job page gets the agent's working-tree diff.

Two sources, because a job has two lives (poly-qqx9.7):

RUNNING -- worker.emit_diff(env, origin, ".") reads the live overlay. Its
--intent-to-add bracket catches files the agent created but never
committed, so a new payload file shows as ADDED rather than missing.
Needs the env, which is on the job row since poly-qqx9.12.

FINISHED -- analysis/rescued/<job_id>.diff, written by the rescue path
when the job ended. The same bytes, at a path no later job overwrites,
and already per-job. Nothing new is recorded for this case; it reads what
is already there.

The live read is preferred when it is available and non-empty: the
rescued artifact is a snapshot of the end, and a running job has moved on
from it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def rescued_relpath(job_id: str) -> str:
    """Where the rescue path filed this job's diff."""
    return f"analysis/rescued/{job_id}.diff"


def from_artifacts(
    artifact_root: Path,
    ref: dict[str, Any] | None,
    relpath: str,
) -> str:
    """The rescued diff's bytes, or "" when the evidence tree is pruned."""
    from dportsv3.tracker import render  # noqa: PLC0415

    return render.artifact_raw_text(artifact_root, relpath, ref) or ""


def from_workspace(env: str, origin: str) -> str:
    """The live overlay diff for this port, or "" if it cannot be read.

    Never raises: the env can be gone, the runner can be on another host
    under poly-fij, and a page that cannot show a diff shows no band
    rather than an error where the work should be.
    """
    if not env or not origin:
        return ""
    try:
        from dportsv3.agent import worker  # noqa: PLC0415

        out = worker.emit_diff(env, origin, ".")
    except Exception:
        return ""
    if not isinstance(out, dict) or not out.get("ok"):
        return ""
    diff = str(out.get("diff") or "")
    # It has to BE a diff. An ok result whose body is anything else --
    # a wrapper that answered the wrong question, a relocated env
    # printing a path -- would otherwise count as "the live read
    # worked", and the rescued artifact behind it would never be
    # reached. The band would then be empty on a job that had changes
    # recorded, which is the one failure this fallback exists to
    # prevent.
    return diff if "diff --git " in diff else ""
