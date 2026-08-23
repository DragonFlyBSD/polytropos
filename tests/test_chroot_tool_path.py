"""The tool is not in the ports tree.

Before the repository split the wrapper sat at the ports checkout's root,
so ``$DELTAPORTS_ROOT/dportsv3`` was correct. It is now two mistakes at
once: wrong tree, and in this repo that name belongs to the Python package
rather than the wrapper (layout.py:50). Two call sites kept the old path
and shipped broken — this is the guard.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from dportsv3.agent import health, worker

AGENT_DIR = pathlib.Path(worker.__file__).parent


def test_no_source_file_locates_the_tool_in_the_ports_tree() -> None:
    offenders = []
    for path in sorted(AGENT_DIR.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"DELTAPORTS_ROOT[^\"']*/dportsv3", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the wrapper lives at $POLYTROPOS_ROOT/bin/dportsv3, not under the "
        "ports tree:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        health._check_dports_compose,
        worker.validate_dops,
    ],
)
def test_tool_invocations_use_the_tool_root(snippet, monkeypatch) -> None:
    """Capture the actual shell command each helper builds."""
    seen: list[str] = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _capture(*args, **kwargs):
        seen.extend(a for a in args if isinstance(a, str))
        return _Result()

    monkeypatch.setattr(health, "_run_in_env", _capture, raising=False)
    monkeypatch.setattr(worker, "_exec", _capture, raising=False)

    try:
        snippet("env-name", "x11/foo") if snippet is worker.validate_dops else snippet("env-name")
    except Exception:
        pass  # only the command text matters

    cmd = " ".join(seen)
    assert "$POLYTROPOS_ROOT/bin/dportsv3" in cmd, cmd
    assert "$DELTAPORTS_ROOT/dportsv3" not in cmd, cmd
