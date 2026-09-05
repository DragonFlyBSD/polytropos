"""Canonical error fingerprint — the issue-identity primitive.

A *fingerprint* is a stable short digest of a build failure's root-cause
line, computed by canonicalizing away incidental noise (absolute paths,
line/column numbers, hex addresses, PIDs, tmpdir names, timestamps) so
that "the same failure" collapses to the same value instead of splitting
on run-to-run detail.

This is the single definition of a fingerprint in the system. It is used
in two places, which MUST agree:

- at ingest (the failure hook) to stamp ``bundles.error_signature`` the
  moment an occurrence is born, so issues can be keyed immediately; and
- by the runner's sticky-signature retry cap, which asks "is this the
  same wall we keep hitting?".

The digest shape is ``sha256(normalized)[:16]`` — 16 hex chars, matching
the pre-existing ``error_signature`` column so nothing downstream needs
to change width.
"""

from __future__ import annotations

import hashlib
import re

# Ordered canonicalization rules. Each strips one class of incidental
# variation. Order matters: hex/timestamp/path reductions run before the
# generic long-digit rule so structured tokens are handled by their
# specific rule rather than the catch-all.
_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?")
# A maximal run of path-ish chars containing at least one '/'. Reduced to
# its basename — the directory (build root, tmpdir, ports/ prefix) is
# incidental; the origin already lives in the issue key.
_PATH = re.compile(r"[\w.+\-]*/[\w./+\-]*")
# Trailing/embedded ":line" or ":line:col" after a filename.
_LINECOL = re.compile(r":\d+(?::\d+)?(?=:|\s|\)|,|$)")
# Long standalone integer runs: PIDs, inodes, byte offsets. Word-bounded
# so digits embedded in identifiers (python312, libssl3) survive.
_LONG_INT = re.compile(r"\b\d{3,}\b")
_WS = re.compile(r"\s+")

# The failure hook's distiller (dports_dev_env dsynth-hooks/hook_common.sh,
# distill_log) does not write the root-cause line first. It writes
# banner-delimited sections:
#
#     == Summary ==
#     logfile: /work/dsynth/logs/devel___json-glib.log
#
#     == First error candidates (max 60 matches) ==
#     1234:configure: error: C compiler cannot create executables
#     ...
#     == Error blocks (context +/-2, truncated later) ==
#     == Tail (last 200 lines) ==
#
# So "the first non-empty line" is the literal "== Summary ==" for every
# failure in the tree. Measured on a 317-package build: 28 bundles, 19
# origins, ONE distinct signature (poly-awz). The root cause is the first
# entry under "First error candidates".
_SECTION_HEAD = re.compile(r"^==\s.*\s==$")
_CANDIDATES_HEAD = re.compile(r"^==\s*First error candidates\b")
# grep -n prefixes every hit with its line number in the source log. That is
# incidental, and _LONG_INT only reduces runs of 3+ digits, so a two-digit
# line number would otherwise split one failure into several signatures.
_GREP_LINENO = re.compile(r"^\d+:")


def _root_cause_line(text: str) -> str | None:
    """The line to fingerprint, from either shape of ``logs/errors.txt``.

    Distilled output is section-delimited (see ``_SECTION_HEAD``): take the
    first entry under "First error candidates" and strip grep's line-number
    prefix. Anything else — the ``missing_log=1`` key/value file the hook
    writes when the log is unreadable, or a caller passing a raw error line
    — keeps the original "first non-empty line" rule.
    """
    lines = text.splitlines()
    in_candidates = False
    distilled = False
    for raw in lines:
        stripped = raw.strip()
        if _SECTION_HEAD.match(stripped):
            distilled = True
            in_candidates = bool(_CANDIDATES_HEAD.match(stripped))
            continue
        if in_candidates and stripped:
            return _GREP_LINENO.sub("", stripped, count=1).strip() or None
    if distilled:
        # Distilled, but no candidate matched: a failure phase the
        # distiller has no pattern for (fetch, patch — poly-m8m). There is
        # no root-cause line, and saying so is right. issue_key collapses
        # signatureless failures for one (target, origin) into a single
        # fallback issue instead of inventing a shared constant.
        return None
    for raw in lines:
        stripped = raw.strip()
        if stripped:
            return stripped
    return None


def normalize_error(text: str | None) -> str | None:
    """Return the canonicalized root-cause line of ``text``, or ``None``
    when it carries no usable one.

    The returned string is what gets hashed; it is deterministic and
    stable across builds of the same failure.
    """
    if not text:
        return None
    line = _root_cause_line(text)
    if not line:
        return None
    line = _HEX_ADDR.sub("0xADDR", line)
    line = _TIMESTAMP.sub("TIMESTAMP", line)
    line = _PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], line)
    line = _LINECOL.sub("", line)
    line = _LONG_INT.sub("N", line)
    line = _WS.sub(" ", line).strip()
    return line or None


def compute_fingerprint(text: str | None) -> str | None:
    """Return ``sha256(normalize_error(text))[:16]``, or ``None`` when
    there is no usable line to fingerprint."""
    norm = normalize_error(text)
    if norm is None:
        return None
    return hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()[:16]


def issue_key(target: str | None, origin: str | None,
              fingerprint: str | None) -> str:
    """Return the issue identity for a failure: a short hash of the
    ``(target, origin, fingerprint)`` triple.

    This is the find-or-create key for the ``issues`` table — two
    occurrences with the same triple are the same problem. When a failure
    has no fingerprint (no usable errors text), the empty component makes
    all such failures for one ``(target, origin)`` collapse to a single
    fallback issue rather than each spawning its own.
    """
    triple = f"{target or ''}\x00{origin or ''}\x00{fingerprint or ''}"
    return hashlib.sha256(triple.encode("utf-8", errors="replace")).hexdigest()[:16]
