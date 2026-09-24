"""Artifact relpath conventions, in one place for the writer and the reader.

The runner publishes these; the tracker reads them. They were drifting
apart by hand -- steps.py built the rescue path with an f-string while
tracker/worktree_source.py had its own ``rescued_relpath`` -- and a
convention spelled twice is a convention that eventually differs. This
module is in ``common`` because the agent must not import the tracker and
the tracker must not import the runner.

THE WORKING TREE IS VERSIONED BY ATTEMPT (poly-5tgc). One relpath per
attempt, re-published on every edit so the page advances while the agent
works; each publish is a complete snapshot, because the diff is cumulative
against the bundle's base branch rather than a delta. Keyed by JOB, not by
bundle: ``analysis/changes.diff`` belongs to the bundle, so on a retried
bundle one job's diff would overwrite its sibling's.
"""

from __future__ import annotations

import re

#: Every working-tree snapshot for a job lives under here.
WORKTREE_PREFIX = "analysis/worktree/"

_ATTEMPT_RE = re.compile(r"\.attempt(\d+)\.diff$")


def worktree_snapshot_relpath(job_id: str, attempt: int) -> str:
    """Where one attempt's working-tree snapshot is published."""
    return f"{WORKTREE_PREFIX}{job_id}.attempt{int(attempt)}.diff"


def worktree_snapshot_attempt(relpath: str, job_id: str) -> int | None:
    """The attempt number in one of this job's snapshot relpaths, or None.

    Checks the job id too: a bundle holds the snapshots of every job that
    ran on it, and a retried bundle's sibling job must not be read as this
    job's earlier attempt.
    """
    prefix = f"{WORKTREE_PREFIX}{job_id}"
    if not relpath.startswith(prefix):
        return None
    m = _ATTEMPT_RE.search(relpath[len(prefix):])
    return int(m.group(1)) if m else None


def rescued_relpath(job_id: str) -> str:
    """Where the rescue path files a job's diff when the harness raises.

    Per job, so a requeued attempt that gives up cleanly cannot write its
    own empty diff over work no attempt can reproduce (poly-be6o).
    """
    return f"analysis/rescued/{job_id}.diff"


def canonical_relpath() -> str:
    """The diff every patch job publishes at the end, keyed by BUNDLE.

    Delivery, verify-fix replay and the proposed_fix recipe all read this
    one, so it is overwritten by every later job on the bundle. That is
    right for the canonical artifact and wrong for anything per-job.
    """
    return "analysis/changes.diff"
