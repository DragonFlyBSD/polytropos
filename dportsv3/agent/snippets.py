"""Thin wrapper around the snippet extractor.

The extractor reads snippet requests from ``analysis/triage.md`` (or
``analysis/patch.md``) in the bundle and writes results under
``analysis/snippets/round_N/``. We invoke it as a subprocess and return
the list of files it produced so the harness can append their content
to the next LLM call.

It is a module in this package (``dportsv3.snippet_extractor``), not a
standalone script found on disk, so it is run as ``python -m`` with the
current interpreter. That means it cannot go missing independently of the
package, and it needs no execute bit, no PATH entry, and no shebang that
happens to name an interpreter the package is not installed into.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: How the extractor is invoked. Overridable per-call for tests.
DEFAULT_EXTRACTOR_CMD: list[str] = [sys.executable, "-m", "dportsv3.snippet_extractor"]


def extract_round(
    bundle_dir: Path,
    round_number: int,
    *,
    extractor: Path | list[str] | None = None,
    prefer_workdir: bool = True,
) -> tuple[int, list[Path]]:
    """Run the snippet extractor for one round; return (exit code, snippet files).

    Exit codes (from the extractor):
      0 — success, at least some snippets extracted
      1 — no snippet requests found
      2 — all requests failed (nothing extracted)
      3 — configuration error
    """
    if extractor is None:
        base = list(DEFAULT_EXTRACTOR_CMD)
    elif isinstance(extractor, (str, Path)):
        base = [str(extractor)]
    else:
        base = list(extractor)
    cmd: list[str] = [
        *base,
        "--bundle", str(bundle_dir),
        "--round", str(round_number),
    ]
    if prefer_workdir:
        cmd.append("--prefer-workdir")

    result = subprocess.run(cmd, capture_output=True, text=True)

    round_dir = bundle_dir / "analysis" / "snippets" / f"round_{round_number}"
    files: list[Path] = []
    if round_dir.is_dir():
        for sub in sorted(round_dir.rglob("*")):
            if sub.is_file() and sub.suffix in (".txt", ".log"):
                files.append(sub)

    return result.returncode, files


def format_for_prompt(bundle_dir: Path, files: list[Path]) -> str:
    """Render extracted snippet files as a single string for the next user message."""
    parts: list[str] = ["## Extracted Snippets", ""]
    for path in files:
        try:
            rel = path.relative_to(bundle_dir)
        except ValueError:
            rel = path
        try:
            content = path.read_text(errors="replace")
        except OSError as exc:
            content = f"<failed to read: {exc}>"
        parts.append(f"### `{rel}`")
        parts.append("```")
        parts.append(content)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)
