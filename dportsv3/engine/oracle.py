"""Constrained bmake oracle checks for post-rewrite validation."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, Literal

OracleProfile = Literal["off", "local", "ci"]
_VALID_PROFILES = {"off", "local", "ci"}
_CI_PROBE_VARIABLES = ("PORTNAME", "CATEGORIES", "MAINTAINER")


@dataclass
class OracleResult:
    """Result row for bmake oracle execution."""

    ok: bool
    profile: OracleProfile
    checks_run: int = 0
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    unavailable: bool = False


RunCommand = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def normalize_oracle_profile(value: str | None) -> OracleProfile:
    """Normalize requested oracle profile."""
    if value is None:
        return "local"
    candidate = value.strip().lower()
    if candidate not in _VALID_PROFILES:
        raise ValueError(
            f"invalid oracle profile: {value!r} (expected off, local, or ci)"
        )
    return candidate  # type: ignore[return-value]


def _default_run_command(
    command: list[str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _format_failure(
    command: list[str], completed: subprocess.CompletedProcess[str]
) -> str:
    detail = (
        completed.stderr.strip() or completed.stdout.strip() or "bmake command failed"
    )
    return f"{' '.join(command)} -> {detail}"


def run_bmake_oracle(
    port_root: Path,
    *,
    profile: str = "local",
    run_command: RunCommand | None = None,
    bmake_path: str | None = None,
    expect: Mapping[str, str] | None = None,
) -> OracleResult:
    """Run constrained bmake checks for one rewritten port tree.

    ``expect`` asserts that a variable the overlay set actually holds
    that value once the framework has read the whole Makefile
    (poly-lt5q). It answers a question the rest of the oracle does not:
    whether the assignment *survived*. A slave port's Makefile ends
    with ``.include "${MASTERDIR}/Makefile"`` and an overlay's ``mk
    set`` lands above it, so a plain ``X=`` in the master overwrites
    the overlay's value and the port builds as though the fix were
    never written. Without this, that surfaces as a build failure with
    no indication the overlay was ignored.

    Callers pass only literal expectations — a value containing ``$``
    is expanded by bmake and cannot be compared to its source text.
    :func:`literal_expectations` does that filtering.
    """
    normalized = normalize_oracle_profile(profile)
    if normalized == "off":
        return OracleResult(ok=True, profile=normalized, skipped=True)

    makefile = port_root / "Makefile"
    if not makefile.exists() or not makefile.is_file():
        return OracleResult(
            ok=True,
            profile=normalized,
            skipped=True,
            warnings=["Makefile not found for oracle check"],
        )

    executable = bmake_path or shutil.which("bmake")
    if executable is None:
        if normalized == "ci":
            return OracleResult(
                ok=False,
                profile=normalized,
                unavailable=True,
                failures=["bmake not found in PATH"],
            )
        return OracleResult(
            ok=True,
            profile=normalized,
            skipped=True,
            unavailable=True,
            warnings=["bmake not found in PATH"],
        )

    runner = run_command or _default_run_command
    commands: list[list[str]] = [[executable, "-n", "-f", "Makefile"]]
    if normalized == "ci":
        for variable in _CI_PROBE_VARIABLES:
            commands.append([executable, "-f", "Makefile", "-V", variable])

    result = OracleResult(ok=True, profile=normalized)
    for command in commands:
        completed = runner(command, port_root)
        result.checks_run += 1
        if completed.returncode != 0:
            result.failures.append(_format_failure(command, completed))

    for variable, wanted in (expect or {}).items():
        command = [executable, "-f", "Makefile", "-V", variable]
        completed = runner(command, port_root)
        result.checks_run += 1
        if completed.returncode != 0:
            result.failures.append(_format_failure(command, completed))
            continue
        observed = (completed.stdout or "").strip()
        if observed != wanted.strip():
            result.failures.append(
                f"{variable}: overlay set {wanted.strip()!r} but the "
                f"framework reports {observed!r} — the assignment did "
                f"not survive (a later plain assignment, e.g. in a "
                f"master port's Makefile, overwrote it)"
            )

    result.ok = not result.failures
    return result


def literal_expectations(assignments: Mapping[str, str]) -> dict[str, str]:
    """Keep only the assignments an oracle can meaningfully compare.

    A value containing ``$`` is a make expression — bmake reports what
    it expands to, not the source text, so comparing the two produces a
    false mismatch every time. Values that are plain literals are the
    ones the mk-clobber hazard actually bites, so narrowing here loses
    little and keeps the check honest.
    """
    return {
        name: value
        for name, value in assignments.items()
        if value and "$" not in value
    }
