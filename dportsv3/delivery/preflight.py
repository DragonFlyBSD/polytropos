"""Check the delivery configuration at startup, not at the first Accept.

Every precondition delivery has — a readable token, a clone directory
that exists and is a clean git tree, an outbox that can be written —
used to be discovered at the moment an operator clicked Accept on a
bundle they cared about. That is the worst possible time to learn that
``clone_dir`` belongs to root: the fix is already made, the review is
already approved, and the failure reads as a delivery bug rather than a
setup one.

So the tracker runs this once when it starts and logs what it finds.
Nothing here changes state or raises: a service that refuses to boot
because a forge credential is stale is worse than one that boots and
says so.

The permission checks are deliberately ``os.access`` against the
*running* account rather than a stat-and-reason exercise. This module
runs inside the tracker, and the tracker is the process that delivers,
so "can I write it" is the actual question and the answer is
authoritative. It also means the report names the account that will
really be doing the work, which on a packaged install is the
unprivileged service user and not root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import DeliveryConfigError


__all__ = ["Finding", "check", "format_report"]


@dataclass(frozen=True)
class Finding:
    """One line of the report.

    ``level`` is ``ok``, ``warn`` or ``error``. ``error`` means delivery
    will fail if attempted; ``warn`` means it may. ``detail`` names the
    path or setting, because a report that says "not writable" without
    saying what is not writable sends the operator looking.
    """
    level: str
    detail: str


def _account() -> str:
    """Who this process is, by name where we can get one."""
    try:
        import pwd  # noqa: PLC0415
        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError):
        return f"uid {os.geteuid()}"


def check(*, target: str | None = None,
          env: dict[str, str] | None = None) -> list[Finding]:
    """Report on the delivery configuration. Never raises.

    An empty-ish result — a single ``ok`` saying delivery is disabled —
    is the expected state on a host that has not opted in.
    """
    from .orchestrator import resolve_config  # noqa: PLC0415

    who = _account()
    try:
        cfg = resolve_config(target=target, env=env)
    except DeliveryConfigError as exc:
        return [Finding("error", f"delivery config is unusable: {exc}")]
    except Exception as exc:  # noqa: BLE001 — startup must not die here
        return [Finding("error", f"delivery config could not be read: {exc}")]

    real_env = dict(os.environ) if env is None else env
    config_dir = real_env.get("DPORTSV3_CONFIG_DIR", "").strip()

    if cfg is None:
        if not config_dir:
            # Worth separating from "no delivery.toml": an unset config dir
            # means *nothing* operator-owned can be found, policy included,
            # and on a packaged install it is a deployment fault rather
            # than a choice.
            return [Finding(
                "warn",
                "$DPORTSV3_CONFIG_DIR is unset, so no operator config can "
                "be found at all; delivery is off and Accept will skip "
                "with no_config",
            )]
        return [Finding(
            "ok",
            f"delivery is not configured; Accept will skip with no_config. "
            f"Copy delivery.toml.sample to delivery.toml in {config_dir} "
            f"to enable it.",
        )]

    out: list[Finding] = [
        Finding("ok", f"delivery provider is {cfg.provider_type!r}"),
    ]

    if cfg.provider_type == "local-patch":
        out.extend(_check_outbox(cfg.outbox, who))
        return out

    # Network providers. The loader has already refused a config with no
    # token and no repo, so reaching here means both are present.
    out.extend(_check_token_mode(config_dir))
    out.extend(_check_clone_dir(cfg.clone_dir, cfg.base_branch, who))
    return out


def _check_token_mode(config_dir: str) -> list[Finding]:
    """The token is a forge credential sitting in a config directory.

    Nothing enforces its mode, and the documentation used to prescribe the
    wrong one, so say what is actually on disk. Absent is not a fault here:
    the config loaded, so the token came from somewhere — the env var, or
    an explicitly-named path.
    """
    if not config_dir:
        return []
    token = Path(config_dir) / "delivery.token"
    try:
        if not token.is_file():
            return []
        mode = token.stat().st_mode & 0o777
    except OSError as exc:
        return [Finding("warn", f"could not stat {token}: {exc}")]
    if mode & 0o007:
        return [Finding(
            "warn",
            f"{token} is world-readable (mode {mode:04o}); it is a forge "
            f"credential — 0640 root:<service group> is enough",
        )]
    return [Finding("ok", f"{token} mode is {mode:04o}")]


def _check_outbox(outbox: str | None, who: str) -> list[Finding]:
    if not outbox:
        return [Finding("error", "provider.outbox is unset")]
    p = Path(outbox)
    if not p.is_dir():
        # Not an error: LocalPatchProvider creates it on first use. It is
        # worth saying so, because an operator watching for a file needs
        # to know nothing has gone wrong yet.
        return [Finding(
            "ok",
            f"outbox {p} does not exist yet; it is created on the first "
            f"delivery",
        )]
    if not os.access(p, os.W_OK | os.X_OK):
        return [Finding(
            "error",
            f"outbox {p} is not writable by {who}; delivery will fail",
        )]
    return [Finding("ok", f"outbox {p} is writable")]


def _check_clone_dir(clone_dir: str | None, base_branch: str,
                     who: str) -> list[Finding]:
    if not clone_dir:
        return [Finding("error", "provider.clone_dir is unset")]

    p = Path(clone_dir)
    if not p.is_dir():
        return [Finding(
            "error",
            f"clone_dir {p} does not exist; point provider.clone_dir at a "
            f"DeltaPorts working tree owned by {who}",
        )]
    if not (p / ".git").exists():
        return [Finding(
            "error", f"clone_dir {p} is not a git working tree (no .git)",
        )]
    if not os.access(p, os.W_OK | os.X_OK):
        return [Finding(
            "error",
            f"clone_dir {p} is not writable by {who}; delivery resets and "
            f"commits in this tree, so it must belong to the account "
            f"running the tracker",
        )]

    out: list[Finding] = [Finding("ok", f"clone_dir {p} is a writable git tree")]
    out.extend(_check_clone_state(p, base_branch))
    return out


def _check_clone_state(clone_dir: Path, base_branch: str) -> list[Finding]:
    """Branch and cleanliness. Warnings, not errors — delivery resets the
    tree itself, so a dirty clone is a sign of trouble rather than proof
    of it, and refusing to start over one would be wrong."""
    from ._git import GitError, _current_branch, _is_dirty  # noqa: PLC0415

    out: list[Finding] = []
    try:
        branch = _current_branch(clone_dir)
    except (GitError, OSError) as exc:
        return [Finding("warn", f"could not read clone_dir's branch: {exc}")]
    if branch != base_branch:
        out.append(Finding(
            "warn",
            f"clone_dir is on {branch!r}, not provider.base_branch "
            f"{base_branch!r}; delivery resets onto {base_branch!r}",
        ))
    try:
        if _is_dirty(clone_dir):
            out.append(Finding(
                "warn",
                "clone_dir has uncommitted changes; delivery resets the "
                "tree and they would be lost",
            ))
    except (GitError, OSError) as exc:
        out.append(Finding("warn", f"could not check clone_dir status: {exc}"))
    if not out:
        out.append(Finding(
            "ok", f"clone_dir is clean and on {base_branch!r}",
        ))
    return out


def format_report(findings: list[Finding]) -> list[tuple[str, str]]:
    """``(level, message)`` pairs ready for a logger.

    Kept separate from ``check`` so the same findings can go to a log,
    a CLI, or a test without this module choosing a logger.
    """
    return [(f.level, f"delivery preflight: {f.detail}") for f in findings]
