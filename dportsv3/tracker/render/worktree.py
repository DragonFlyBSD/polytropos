"""What the agent changed, as a file list the operator can read.

attempt_loop reads the carried overlay diff at the top of every retry and
hands it to the MODEL. The model could see what it had changed; the
operator could not -- the job page printed a bundle directory as text and
stopped (poly-qqx9.7).

TWO THINGS A PLAIN ``git diff --stat`` WOULD NOT SAY:

The overlay under ``ports/<origin>/`` is NOT reset between attempts, so
the tree is cumulative. attempt_loop's own measurement across one
four-attempt job: 0b, 1694b, 2116b, 2204b. Without per-attempt
attribution an operator reads attempt 1's edits as attempt 3's. The
attribution is a query, not a schema change: the attempt a file first
appeared in is the first attempt whose write-tool row names that path,
and poly-qqx9.13 put those arguments on the row.

And the diff itself comes from emit_diff, whose --intent-to-add bracket
catches untracked files -- so a payload file the agent created shows as
ADDED rather than missing, which is the difference between "it wrote
nothing" and "it wrote a new file".

The diff is rendered by render.render_diff, the one the artifact reader
uses. There is no second diff renderer here.
"""

from __future__ import annotations

import re
from typing import Any

_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ ")


def parse_diff_files(raw: str) -> list[dict[str, Any]]:
    """One entry per file in a unified diff: status, path, +/- counts.

    Status is A, M or D, taken from git's own file-mode lines rather than
    guessed from the hunks: a file whose every line changed is still M,
    and one created empty is still A.
    """
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_hunk = False
    for line in (raw or "").splitlines():
        m = _FILE_RE.match(line)
        if m:
            cur = {"path": m.group(2), "status": "M", "added": 0,
                   "removed": 0, "lines": [line]}
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        # Kept so each file's pane renders only its own diff, through
        # render.render_diff -- there is no second diff renderer here.
        cur["lines"].append(line)
        if line.startswith("new file mode"):
            cur["status"] = "A"
        elif line.startswith("deleted file mode"):
            cur["status"] = "D"
        elif _HUNK_RE.match(line):
            in_hunk = True
        elif in_hunk:
            # Inside a hunk a content line carries exactly one +/-/space
            # marker. No special case for "+++": the file headers sit
            # before the first @@, and these ports' payloads ARE patches,
            # so a line reading "++++ CMakeLists.txt" is an added line
            # whose content happens to start with +++.
            if line.startswith("+"):
                cur["added"] += 1
            elif line.startswith("-"):
                cur["removed"] += 1
    for entry in files:
        entry["raw"] = "\n".join(entry.pop("lines"))
    return files


def _names(path: str, arg: str) -> bool:
    """Does this tool argument name this file?

    A write tool is called with whatever relative path the model chose --
    ``files/patch-Makefile``, ``./files/patch-Makefile``, sometimes just
    the basename -- while the diff carries the full in-tree path. Match
    on the tail, which is the part both agree on.
    """
    a = (arg or "").strip().lstrip("./")
    if not a:
        return False
    if path.endswith(a):
        return True
    return path.rsplit("/", 1)[-1] == a.rsplit("/", 1)[-1]


def attribute_files(
    files: list[dict[str, Any]],
    write_calls: list[dict[str, Any]],
) -> None:
    """Stamp each file with the attempt that first wrote it.

    ``write_calls`` is ``[{attempt, args}]`` for the job's write-tool
    rows, oldest first. The FIRST attempt that names a path owns it: the
    tree is cumulative, so the last one to touch a file is nearly always
    the current attempt and says nothing.
    """
    for entry in files:
        entry["attempt"] = None
        for call in write_calls:
            args = call.get("args")
            if not isinstance(args, dict):
                continue
            if any(isinstance(v, str) and _names(entry["path"], v)
                   for v in args.values()):
                entry["attempt"] = call.get("attempt")
                break


def working_tree(
    raw: str,
    write_calls: list[dict[str, Any]] | None = None,
    live_path: str | None = None,
) -> dict[str, Any]:
    """The changed files, attributed, with the totals the band shows.

    ``live_path`` marks the file a write tool is working on right now.
    Returns ``{files, added, removed, n_files, empty}``; ``empty`` is
    True when the agent has changed nothing yet, which is a real state on
    a job that is still reading.
    """
    files = parse_diff_files(raw)
    attribute_files(files, write_calls or [])
    for entry in files:
        entry["live"] = bool(live_path and _names(entry["path"], live_path))
    return {
        "files": files,
        "n_files": len(files),
        "added": sum(f["added"] for f in files),
        "removed": sum(f["removed"] for f in files),
        "empty": not files,
    }
