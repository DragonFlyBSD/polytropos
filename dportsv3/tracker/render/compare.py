"""Two occurrences of one issue, side by side.

The cockpit shows an issue's three attempts and says nothing about what is
DIFFERENT between them, which is close to the question it exists to answer
(poly-0e02.14).

Hashes first. ``artifact_refs.sha256`` is the blob backend's own key, so
"the agent produced the same patch again" is a string comparison that costs
no read at all, and only the rows that actually differ are worth opening.
Within one issue the error signature is equal by construction -- the issue
key hashes it -- so the digest says nothing here and the raw artifacts are
the whole story.
"""

from __future__ import annotations

import difflib
from typing import Any

#: Artifacts worth offering a diff for, cheapest-to-read first. Everything
#: else is listed with its status and left closed: a 40 MB build log is not
#: a comparison, it is a download.
_DIFFABLE_SUFFIXES = (".diff", ".patch", ".rej", ".md", ".txt", ".json",
                      ".jsonl")

#: Tried in order when nothing is asked for. changes.diff is the answer to
#: "did the proposed fix change?", which is the first thing an operator
#: wants; errors.txt is the answer to "did the error change?".
_PREFERRED = ("analysis/changes.diff", "analysis/proposed_fix.diff",
              "analysis/errors.txt", "analysis/triage.md",
              "analysis/patch.md")


def is_diffable(relpath: str) -> bool:
    """Whether two versions of this artifact can be shown as a text diff."""
    lowered = relpath.lower()
    if lowered.endswith(".gz"):
        lowered = lowered[:-3]
    return lowered.endswith(_DIFFABLE_SUFFIXES)


def compare_artifacts(
    a_artifacts: list[dict[str, Any]],
    b_artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per relpath either occurrence produced, union, sorted.

    ``status`` is one of:

    ``same``     both sides, identical sha256 -- the cheap "nothing changed"
    ``changed``  both sides, different sha256
    ``unknown``  both sides, but a sha is missing so it cannot be told
                 without reading. The ``fs`` backend does not hash.
    ``only_a`` / ``only_b``  produced by one attempt and not the other,
                 which is itself a finding: an attempt that never got far
                 enough to write a patch has no changes.diff.
    """
    by_a = {r["relpath"]: r for r in a_artifacts}
    by_b = {r["relpath"]: r for r in b_artifacts}
    rows: list[dict[str, Any]] = []
    for relpath in sorted(set(by_a) | set(by_b)):
        a = by_a.get(relpath)
        b = by_b.get(relpath)
        if a is not None and b is None:
            status = "only_a"
        elif a is None and b is not None:
            status = "only_b"
        else:
            a_sha = (a or {}).get("sha256")
            b_sha = (b or {}).get("sha256")
            if not a_sha or not b_sha:
                status = "unknown"
            else:
                status = "same" if a_sha == b_sha else "changed"
        rows.append({
            "relpath": relpath,
            "a": a,
            "b": b,
            "status": status,
            # Only offer to open what both sides have and what is text.
            "diffable": (
                a is not None and b is not None
                and status != "same" and is_diffable(relpath)
            ),
        })
    return rows


def default_relpath(rows: list[dict[str, Any]]) -> str | None:
    """Which comparison to open with: the first interesting one.

    A preferred artifact that differs, else any diffable row that differs,
    else nothing -- two identical attempts have no diff to show and saying
    so is the useful answer.
    """
    openable = {r["relpath"] for r in rows if r["diffable"]}
    for relpath in _PREFERRED:
        if relpath in openable:
            return relpath
    for row in rows:
        if row["diffable"]:
            return row["relpath"]
    return None


def unified(
    a_text: str, b_text: str, a_label: str, b_label: str, context: int = 3,
) -> str:
    """A unified diff of two artifact bodies, ready for ``render_diff``.

    Labelled with the bundle ids rather than with filenames: both sides are
    the same relpath, and which occurrence a line came from is the only
    thing the header can usefully say.
    """
    lines = difflib.unified_diff(
        a_text.splitlines(keepends=True),
        b_text.splitlines(keepends=True),
        fromfile=a_label,
        tofile=b_label,
        n=context,
    )
    out = "".join(lines)
    # splitlines(keepends=True) drops nothing, but a file with no trailing
    # newline yields a last line without one and the next hunk header would
    # run onto it.
    return out if out.endswith("\n") or not out else out + "\n"
