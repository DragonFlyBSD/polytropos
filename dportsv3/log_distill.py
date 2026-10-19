"""Distill a dsynth build log into the ``logs/errors.txt`` a bundle carries.

errors.txt is what triage reads first, and the issue fingerprint is taken
from it (:mod:`dportsv3.fingerprint`). The store builds it when the failure
hook upserts the bundle, from the full log the hook uploaded just before
(poly-m8m). It used to be a grep pipeline inside the hook, with two faults:
its patterns were compiler vocabulary only, so a ports failure (patch,
depends, fetch) arrived with no candidates; and it cut the file to its first
200000 bytes, so a tail of long compiler command lines lost its end.
x11-servers/xorg-server's run-depends failure sat ten lines from the end of
a 1 MB log and never reached triage in thirteen occurrences.

dsynth brackets every phase: ``-- Phase: <name>`` opens it and
``SUCCEEDED``/``FAILED hh:mm:ss`` closes it. The last phase in the log is
the one that failed, and its output is the evidence. The layout puts it
first because the readers only see the edges: triage and the patch agent
get the first and last 16 KB (``runner.context_file_cap``), the fix chat
16 KB, the manual handoff the last lines::

    == Summary ==
    == Failed phase: <name> ==      that phase's output, cut from its start
    == First error candidates ==    pattern hits in that phase, "N:line"
    == Error blocks ==              the same hits with +/-2 lines
    == Tail (last 200 lines) ==     the log's end, cut from its start

A log with no phase markers gets no failed-phase section and is searched
whole, which is the shape this replaced. Stdlib only, so it runs wherever
the logs are, a backfill included.
"""

from __future__ import annotations

import gzip
import re
from collections import deque
from collections.abc import Iterable
from pathlib import Path

#: One list for candidates and blocks, as in the hook this replaced: the two
#: sections answered different questions when they were kept separately.
#: The generic markers every failed phase carries -- ``*** Error code``,
#: ``Stop.``, ``make: stopped making`` and dsynth's ``FAILED hh:mm:ss`` --
#: are left out on purpose. The first candidate is the fingerprint, and a
#: line every failure has would fold all of a port's failures into one
#: issue. The failed-phase section shows them anyway.
#:
#: Separate patterns, not one alternation: re cannot skip ahead to a
#: literal across alternatives, so the joined form tried every branch at
#: every position and took 5.4 s on a 147 MB log of compiler command lines
#: that this list searches in under one.
_PATTERNS = tuple(re.compile(p) for p in (
    r"fatal error:",
    # The clang/gcc diagnostic shape. Bare "error:" would pull in prose.
    r"^\S+:\d+:\d+: error:",
    r"undefined reference",
    r"ld: error:",
    r"collect2: error",
    r"CMake Error",
    r"configure: error",
    r"meson\.build:.*ERROR",
    r"^ninja: build stopped",
    r"error: failed to run custom build command for",
    r"ERROR: ",
    r"No such file or directory",
    r"^===>\s+Stopped\s+in\s+",
    # The ports framework's own failure lines.
    r"FAILED to apply",
    # Literal first, so the search can skip ahead: "1 out of 2 hunks
    # failed" (BSD patch) and "hunk FAILED" (GNU patch).
    r"out of \d+ hunks? (?i:failed)",
    r"^===>\s+FAILED",
    # Anchored on "- not found": the same log prints one "- found" line
    # for every dependency that is there (24 in xorg-server's).
    r"^===>\s.* depends on [\w ]+: .* - not found$",
))


def _matches(text: str) -> bool:
    return any(p.search(text) for p in _PATTERNS)


_PHASE = re.compile(r"^-- Phase: (\S+)")
#: dsynth draws a rule of dashes right above each phase marker. Requiring
#: it keeps a build's own "-- Phase: ..." output (cmake prints "-- " lines
#: by the hundred) from restarting the scope halfway through the build.
_RULE = re.compile(r"^-{20,}$")

#: Longer lines are cut. Compiler command lines run to 3 KB, and a byte
#: budget without a line cap holds two or three of them.
LINE_CAP = 1000
_LINE_CUT = " [... line cut]"

PHASE_BUDGET = 8 * 1024
CANDIDATES_MAX = 60
CANDIDATES_BUDGET = 6 * 1024
BLOCKS_BUDGET = 48 * 1024
TAIL_LINES = 200
#: The whole file. The tail gets what the other sections leave.
TOTAL_BUDGET = 200_000


def _cap(line: str) -> str:
    return line if len(line) <= LINE_CAP else line[:LINE_CAP] + _LINE_CUT


class _Scope:
    """What is kept for one phase, or for the whole of a log without phases.

    A new phase marker starts a new scope, so at the end of the log the
    scope holds the failed phase only and a hit from an earlier phase
    cannot become the fingerprint.
    """

    def __init__(self, name: str | None, first: int) -> None:
        self.name = name
        self.first = first
        self.last = first - 1
        self.lines: deque[str] = deque()
        self.size = 0
        self.cut = 0
        self.candidates: list[str] = []
        self.candidates_size = 0
        self.candidates_full = False
        self.blocks: list[str] = []
        self.blocks_size = 0
        self.blocks_full = False
        self.before: deque[tuple[int, str]] = deque(maxlen=2)
        self.after = 0
        self.last_printed = 0

    def keep(self, n: int, line: str) -> None:
        """Keep the phase's newest lines inside PHASE_BUDGET."""
        self.lines.append(line)
        self.size += len(line) + 1
        while self.size > PHASE_BUDGET and len(self.lines) > 1:
            self.size -= len(self.lines.popleft()) + 1
            self.cut += 1
        self.last = n

    def hit(self, n: int, line: str) -> None:
        entry = f"{n}:{line}"
        if not self.candidates_full and len(self.candidates) < CANDIDATES_MAX:
            if self.candidates_size + len(entry) + 1 > CANDIDATES_BUDGET:
                self.candidates_full = True
            else:
                self.candidates.append(entry)
                self.candidates_size += len(entry) + 1
        # grep -C 2: "N:" for a hit, "N-" for context, "--" between groups.
        out = []
        start = self.before[0][0] if self.before else n
        if self.blocks and start > self.last_printed + 1:
            out.append("--")
        out.extend(f"{bn}-{bl}" for bn, bl in self.before)
        out.append(entry)
        self.before.clear()
        self.after = 2
        self.last_printed = n
        self._block(out)

    def miss(self, n: int, line: str) -> None:
        if self.after:
            self._block([f"{n}-{line}"])
            self.after -= 1
            self.last_printed = n
        else:
            self.before.append((n, line))

    def _block(self, out: list[str]) -> None:
        if self.blocks_full:
            return
        add = sum(len(x) + 1 for x in out)
        if self.blocks_size + add > BLOCKS_BUDGET:
            self.blocks_full = True
            return
        self.blocks.extend(out)
        self.blocks_size += add


def distill(lines: Iterable[str]) -> str:
    """Return the errors.txt text for a build log given as lines."""
    scope = _Scope(None, 1)
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    n = 0
    after_rule = False
    for n, raw in enumerate(lines, 1):
        text = raw.rstrip("\r\n")
        line = _cap(text)
        tail.append(line)
        marker = _PHASE.match(text) if after_rule else None
        after_rule = bool(_RULE.match(text))
        if marker:
            scope = _Scope(marker.group(1), n + 1)
            continue
        scope.keep(n, line)
        if _matches(text):
            scope.hit(n, line)
        else:
            scope.miss(n, line)

    out = ["== Summary ==", f"log: {n} lines"]
    if scope.name is not None and scope.last >= scope.first:
        out.append(f"failed phase: {scope.name}, log lines "
                   f"{scope.first}-{scope.last}")
    elif scope.name is not None:
        out.append(f"failed phase: {scope.name}, no output")
    else:
        out.append("phase markers: none, so the whole log was searched")
    out.append("")
    if scope.name is not None:
        out.append(f"== Failed phase: {scope.name} ==")
        if scope.cut:
            out.append(f"[... first {scope.cut} lines of this phase cut]")
        out.extend(scope.lines)
        out.append("")
    out.append(f"== First error candidates (max {CANDIDATES_MAX} matches) ==")
    out.extend(scope.candidates)
    if scope.candidates_full:
        out.append("[... more candidates cut]")
    out.append("")
    out.append("== Error blocks (context +/-2, truncated later) ==")
    out.extend(scope.blocks)
    if scope.blocks_full:
        out.append("[... more blocks cut]")
    out.append("")
    out.append(f"== Tail (last {TAIL_LINES} lines) ==")

    # Less a line's worth for the cut marker.
    room = TOTAL_BUDGET - sum(len(x) + 1 for x in out) - 64
    kept = list(tail)
    size = sum(len(x) + 1 for x in kept)
    dropped = 0
    while kept and size > room:
        size -= len(kept.pop(0)) + 1
        dropped += 1
    if dropped:
        out.append(f"[... first {dropped} tail lines cut]")
    out.extend(kept)
    return "\n".join(out) + "\n"


def distill_file(path: Path | str) -> str:
    """Distill a log file, gzip-compressed or not."""
    path = Path(path)
    with path.open("rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    opener = gzip.open if gz else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        return distill(fh)
