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
            f"Set delivery.type in {config_dir}/polytropos.toml to enable "
            f"it — start with 'local-patch', which writes the diff to "
            f"delivery.outbox instead of pushing.",
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
    out.extend(_check_clone_dir(cfg.clone_dir, cfg.base_branch, who,
                                from_default=_clone_dir_is_default(
                                    cfg.clone_dir)))
    return out


def _clone_dir_is_default(clone_dir: str | None) -> bool:
    """True when nobody chose this path and it is the shipped default.

    Worth telling apart. Giving ``delivery.clone_dir`` a default means
    the first Accept no longer fails on an unset path — but it also
    means an operator who sets ``delivery.type`` and nothing else gets
    an error naming a directory they have never seen. Saying "you did
    not set this, here is where it points, here is what to run" turns
    that from a puzzle into an instruction.
    """
    try:
        from dportsv3 import settings  # noqa: PLC0415
        resolved = settings.resolve("delivery.clone_dir")
    except Exception:  # noqa: BLE001 — startup must not die here
        return False
    if resolved.overridden or resolved.value is None:
        return False
    return str(resolved.value) == str(clone_dir)


def _token_candidates(config_dir: str) -> list[Path]:
    """Every place a delivery token can actually be, in precedence order.

    Two, because two loaders are live. ``delivery.token_file`` is where
    the settings put it (``secrets/delivery.token`` by default); the
    legacy ``delivery.toml`` path still reads ``<config dir>/delivery.token``.

    ``config_dir`` comes from the caller's env rather than
    ``settings.secret_path``, which reads the real process environment —
    ``check`` takes an ``env`` argument and has to honour it.
    """
    out: list[Path] = []
    try:
        from dportsv3 import settings  # noqa: PLC0415
        declared = settings.get("delivery.token_file")
    except Exception:  # noqa: BLE001 — startup must not die here
        declared = None
    if declared is not None:
        p = Path(declared)
        if p.is_absolute():
            out.append(p)
        elif config_dir:
            out.append(Path(config_dir) / p)
    if config_dir:
        legacy = Path(config_dir) / "delivery.token"
        if legacy not in out:
            out.append(legacy)
    return out


def _check_token_mode(config_dir: str) -> list[Finding]:
    """The token is a forge credential sitting in a config directory.

    Nothing enforces its mode, and the documentation used to prescribe the
    wrong one, so say what is actually on disk. Absent is not a fault here:
    the config loaded, so the token came from somewhere — the env var, or
    an explicitly-named path.

    This used to rebuild ``<config dir>/delivery.token`` itself, which
    stopped being the token's home when the settings file replaced
    ``delivery.toml``. The effect was silent: the check stat'd a file
    nobody writes any more, found nothing, and returned no finding at
    all — so a world-readable credential at the documented path drew no
    warning. Ask the setting instead.
    """
    out: list[Finding] = []
    for token in _token_candidates(config_dir):
        try:
            if not token.is_file():
                continue
            mode = token.stat().st_mode & 0o777
        except OSError as exc:
            out.append(Finding("warn", f"could not stat {token}: {exc}"))
            continue
        if mode & 0o007:
            out.append(Finding(
                "warn",
                f"{token} is world-readable (mode {mode:04o}); it is a forge "
                f"credential — 0640 root:<service group> is enough",
            ))
        else:
            out.append(Finding("ok", f"{token} mode is {mode:04o}"))
    return out


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
                     who: str, *, from_default: bool = False) -> list[Finding]:
    if not clone_dir:
        return [Finding("error", "provider.clone_dir is unset")]

    p = Path(clone_dir)
    if not p.is_dir():
        if from_default:
            return [Finding(
                "error",
                f"delivery.clone_dir is not set, so it defaults to {p}, "
                f"which does not exist — nothing clones it for you. Either "
                f"set delivery.clone_dir, or create it: git clone "
                f"<repo url> {p} && chown -R {who} {p}",
            )]
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
