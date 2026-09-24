"""Where the job page gets the agent's working-tree diff.

THE TRACKER READS ARTIFACTS. It does not run anything, and it must not:
the runner may be remote (poly-fij), so reaching into a build environment
is wrong by construction and not merely unavailable. It was also broken --
``dev-env path`` and ``dev-env exec`` both require_root, and the tracker
drops to an unprivileged account, so the live read failed silently on
every job (poly-paee).

TWO ARTIFACTS, in this order:

RESCUED -- analysis/rescued/<job_id>.diff, written by the rescue path when
the harness raises. Exact: per job, and no later job overwrites it. Rare,
because it only exists when an attempt blew up: 4 bundles on a live
builder.

CANONICAL -- analysis/changes.diff, which runner._write_changes_diff
publishes on every patch job. 289 bundles on the same host. It is per
BUNDLE, so on a retried bundle it may be a SIBLING job's diff rather than
this one's, and the band says so. Preferring exactness and rendering
nothing was the wrong trade: it cost the band 98% of the jobs it could
have shown.

Neither is live. A running job has published nothing yet, and the band
says that rather than hiding -- poly-5tgc is the follow-up that has the
runner publish per attempt.
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


def canonical_relpath() -> str:
    """The diff every patch job publishes, per bundle rather than per job."""
    return "analysis/changes.diff"
