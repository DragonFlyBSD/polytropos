"""The last lines of a running build, read by byte offset.

A build's output belongs to the tool row running it, not to a pane of its
own: a tail only exists while one tool runs, and when it finishes the
row collapses to its duration and rc (poly-qqx9.8).

READ BY OFFSET, NOT BY LINES. worker.dsynth_log reads the whole file and
keeps the last N lines, which is right for a model asking "why did this
fail" and wrong for a page asking every three seconds "what is new". A
build log grows to megabytes; re-reading it on every poll to show the
same forty lines is the cost poly-9hjm went looking for. So this seeks to
the caller's offset and returns what arrived since, and the caller sends
the offset back next time.

NOT CO-LOCATION. Today the runner and the tracker share a host and this
is a file read. Under poly-fij they do not, and the builder-side relay
(poly-fij.7) has to be able to answer the same question -- so the shape
here is a plain (path, offset) -> (text, next offset, eof) read with
nothing host-specific in the contract.

THE CAP IS STATED, NOT SILENT. A page left open through a 44-minute
build asks for a gap of megabytes. It gets the tail of that gap and is
told bytes were skipped, rather than being handed the lot or silently
shown the wrong end.
"""

from __future__ import annotations

import os
from typing import Any

#: One poll's worth. Matches worker's own stream cap, which poly-9hjm put
#: there after a single unbounded read cost 690k tokens.
MAX_CHUNK_BYTES = 32_768


def read_tail(
    env: str,
    origin: str,
    flavor: str = "",
    offset: int = 0,
    max_bytes: int = MAX_CHUNK_BYTES,
    max_lines: int = 0,
) -> dict[str, Any]:
    """Bytes written to this port's dsynth log since ``offset``.

    ``offset`` below zero means "start at the end minus max_bytes", which
    is what a page asks for on its first poll: the last screenful, not
    the whole build.

    Returns ``{ok, path, offset, text, eof, total_bytes, skipped}``.
    ``offset`` is what to send next time. ``ok`` is False with an
    ``error`` when no log exists yet -- dsynth writes one only once a
    build starts, so that is an ordinary state, not a fault.
    """
    path = log_path(env, origin, flavor)
    if path is None:
        return {
            "ok": False,
            "error": f"dsynth has written no log for {origin} yet",
            "path": "", "offset": 0, "text": "", "eof": True,
            "total_bytes": 0, "skipped": 0, "mtime": None, "lines": 0,
        }
    try:
        stat = os.stat(path)
        size, mtime = stat.st_size, stat.st_mtime
    except OSError as exc:
        return {
            "ok": False, "error": f"read failed: {exc}",
            "path": str(path), "offset": 0, "text": "", "eof": True,
            "total_bytes": 0, "skipped": 0, "mtime": None, "lines": 0,
        }

    cap = max(1, int(max_bytes))
    start = int(offset)
    skipped = 0
    if start < 0 or start > size:
        # First poll, or the log was replaced by a new build and is now
        # shorter than where we left off. Either way, start from the end.
        start = max(0, size - cap)
        skipped = start
    elif size - start > cap:
        # A page left open through a long build. Give it the newest
        # screenful and say how much was passed over.
        skipped = (size - cap) - start
        start = size - cap

    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(cap)
    except OSError as exc:
        return {
            "ok": False, "error": f"read failed: {exc}",
            "path": str(path), "offset": start, "text": "", "eof": True,
            "total_bytes": size, "skipped": 0, "mtime": mtime, "lines": 0,
        }

    text = chunk.decode("utf-8", errors="replace")
    if max_lines > 0:
        lines = text.splitlines()
        if len(lines) > max_lines:
            text = "\n".join(lines[-max_lines:])
    # The first line of a mid-file read is almost always a fragment of a
    # line the previous read already had. Drop it rather than show half a
    # word as though the build printed it.
    if skipped and "\n" in text:
        text = text.split("\n", 1)[1]
    return {
        "ok": True,
        "path": str(path),
        "offset": start + len(chunk),
        "text": text,
        "lines": text.count("\n") + 1 if text else 0,
        "eof": start + len(chunk) >= size,
        "total_bytes": size,
        "skipped": skipped,
        # When the log last grew. "last line N seconds ago" is the
        # liveness signal the page lacks: the live badge reports the
        # poll, this reports the JOB.
        "mtime": mtime,
    }


def log_path(env: str, origin: str, flavor: str = "") -> Any:
    """The log dsynth is writing for this port, or None.

    Delegates to the worker's own resolver: dsynth writes one log per
    flavor and a flavored port has no unflavored file at all, which is
    the trap that helper exists to avoid. Imported lazily so the tracker
    does not pull the agent's tool surface in at startup.
    """
    from dportsv3.agent import worker  # noqa: PLC0415

    candidates = worker._dsynth_log_candidates(env, origin)
    if not candidates:
        return None
    if flavor:
        stem = worker._dsynth_log_stem(origin)
        wanted = f"{stem}@{flavor.lstrip('@')}.log"
        for path in candidates:
            if path.name == wanted:
                return path
    # Newest first, which is what a running build is.
    return candidates[0]
