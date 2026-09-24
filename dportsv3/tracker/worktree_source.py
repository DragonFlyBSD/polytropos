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

from dportsv3.common import artifacts


# The writer's conventions, not a second copy of them: the runner publishes
# these paths and this module reads them, and a convention spelled twice is
# one that eventually differs.
rescued_relpath = artifacts.rescued_relpath
canonical_relpath = artifacts.canonical_relpath
worktree_snapshot_relpath = artifacts.worktree_snapshot_relpath
worktree_snapshot_attempt = artifacts.worktree_snapshot_attempt


def from_artifacts(
    artifact_root: Path,
    ref: dict[str, Any] | None,
    relpath: str,
) -> str:
    """The rescued diff's bytes, or "" when the evidence tree is pruned."""
    from dportsv3.tracker import render  # noqa: PLC0415

    return render.artifact_raw_text(artifact_root, relpath, ref) or ""


# ---------------------------------------------------------------------------
# Assembly. Lives here rather than in a route module because BOTH the job
# page and the live fragment render it, and a second copy is how the two
# would start disagreeing about which version is current.
# ---------------------------------------------------------------------------


def versions_for(conn, job, job_id, artifact_root, want_attempt):
    """The published snapshots, and which one this request is showing.

    Returns ``(versions, selected)``. ``versions`` is one entry per
    published attempt with its raw diff already read, oldest first;
    ``selected`` is the entry to render -- the requested attempt if it
    published, else the newest.

    THE NEWEST IS THE DEFAULT, and on a live job it keeps being the newest
    as attempts arrive; an explicit ?attempt=N pins one instead, which the
    3s poll must then leave alone (poly-5tgc UI 2).
    """
    from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
        worktree_versions,
    )

    if not job.get("bundle_id"):
        return [], None
    versions = []
    for ref in worktree_versions(conn, job["bundle_id"], job_id):
        relpath = str(ref.get("relpath") or "")
        versions.append({
            "attempt": ref["attempt"],
            "relpath": relpath,
            "size": int(ref.get("size") or 0),
            "created_at": ref.get("created_at"),
            # Read now: attribution needs every snapshot, not just the
            # selected one, and at ~2.6 KB each (measured over 290) N reads
            # for N attempts is cheaper than a second pass.
            "raw": from_artifacts(artifact_root, ref, relpath),
        })
    if not versions:
        return [], None
    selected = None
    if want_attempt is not None:
        selected = next(
            (v for v in versions if v["attempt"] == want_attempt), None)
    return versions, selected or versions[-1]


def working_tree_for(conn, job, job_id, cards, artifact_root,
                      want_attempt=None):
    """The agent's changed files, from the artifacts the runner published.

    READS, NEVER RUNS. The live overlay read this used to prefer went
    through ``dev-env exec``, which requires root while the tracker is
    unprivileged -- so it failed silently on every job -- and it would be
    wrong anyway once the runner is remote (poly-paee).

    THREE SOURCES, best first:

    * ``snapshot`` -- one per attempt, published by the runner as the agent
      edits (poly-5tgc). Per job, versioned, and the only one that exists
      while the job is still running.
    * ``rescued`` -- the per-job diff the rescue path files when the harness
      raises. Exact, and rare.
    * ``bundle`` -- ``analysis/changes.diff``, which is keyed by BUNDLE, so
      on a retried bundle it may be a SIBLING job's diff. Labelled as such.

    Returns ``{"empty": True, "pending": True}`` for a patch job with none
    of them -- normally one whose runner predates the snapshot publishing --
    because an absent band reads as "changed nothing".
    """
    from dportsv3.tracker import render  # noqa: PLC0415
    from dportsv3.tracker.agentic_queries import (  # noqa: PLC0415
        WRITE_TOOLS,
        get_artifact_ref,
        write_tool_calls,
    )

    versions, selected = versions_for(
        conn, job, job_id, artifact_root, want_attempt)

    raw = ""
    source = None
    if selected is not None:
        raw, source = selected["raw"], "snapshot"
    elif job.get("bundle_id"):
        for relpath, label in (
            (rescued_relpath(job_id), "rescued"),
            (canonical_relpath(), "bundle"),
        ):
            ref = get_artifact_ref(conn, job["bundle_id"], relpath)
            raw = from_artifacts(artifact_root, ref, relpath)
            if raw:
                source = label
                break

    # An empty SELECTED snapshot is a real answer -- "this attempt changed
    # nothing" -- and must not fall through to the pending state, which
    # would claim nothing was published at all.
    if not raw and selected is None:
        if job.get("type") == "patch":
            return {"empty": True, "pending": True, "files": [],
                    "n_files": 0, "added": 0, "removed": 0, "versions": []}
        return None

    # The file a write tool is working on right now, so the list can mark
    # it. Only the newest turn can hold a running call.
    live_path = None
    newest = next((c for c in cards if c.get("kind") == "turn"), None)
    for tool in (newest or {}).get("tools") or []:
        if tool.get("running") and tool.get("name") in WRITE_TOOLS:
            args = tool.get("args") or {}
            live_path = next(
                (v for v in args.values() if isinstance(v, str)), None)
            break

    tree = render.working_tree(
        raw, write_tool_calls(conn, job_id), live_path=live_path)

    # Attribution from the snapshots themselves where they exist: exact,
    # and it survives the activity cap that evicts the write-tool rows the
    # fallback reads (poly-a162).
    if versions:
        across = render.attribute_across_snapshots(
            [(v["attempt"], v["raw"]) for v in versions])
        for entry in tree["files"]:
            found = across.get(entry["path"])
            if found:
                entry["attempt"] = found["first"]
                entry["touched_here"] = (
                    found["last_changed"] == selected["attempt"])

    for entry in tree["files"]:
        entry["html"] = render.render_diff(entry.pop("raw"))
    tree["source"] = source
    tree["versions"] = [
        {"attempt": v["attempt"], "size": v["size"],
         "created_at": v["created_at"],
         "empty": not v["raw"].strip(),
         "selected": selected is not None and v["attempt"] == selected["attempt"]}
        for v in versions
    ]
    tree["selected_attempt"] = selected["attempt"] if selected else None
    tree["as_of"] = selected["created_at"] if selected else None
    tree["is_latest"] = bool(
        selected and versions and selected["attempt"] == versions[-1]["attempt"])
    # A publish that could not look, or never landed, leaves a hole. Shown
    # as a gap rather than renumbering the versions around it.
    if versions:
        published = {v["attempt"] for v in versions}
        tree["gaps"] = [
            n for n in range(1, max(published) + 1) if n not in published]
    else:
        tree["gaps"] = []
    return tree
