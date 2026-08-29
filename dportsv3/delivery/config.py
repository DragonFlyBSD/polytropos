"""Load + resolve ``config/delivery.toml`` for Step 11d.

Schema (per the plan §11d):

    [provider]
    type = "github"        # "github" | "gitlab" | "gitea" | "local-patch"
    repo = "DragonFlyBSD/DeltaPorts"
    clone_dir = "/srv/dports-clone"   # required for non-local-patch
    base_branch = "master"
    draft = true
    labels = ["agentic-fix", "needs-review"]
    branch_template = "agentic/{origin_safe}-{target_safe}-{signature_short}"
    committer_name = "Fred [bot]"           # commit author/committer identity
    committer_email = "github@dragonflybsd.org"

    [target."@2026Q2"]     # optional per-target override section
    base_branch = "2026Q2"
    repo = "DragonFlyBSD/DeltaPorts-2026Q2"

The TOP-LEVEL ``[provider]`` block is the default. Per-target
sections override individual fields for a specific target value;
unspecified fields fall back to the top-level.

Token resolution (highest precedence first):
- ``$DPORTSV3_DELIVERY_TOKEN`` env var.
- ``$DPORTSV3_CONFIG_DIR/delivery.token`` file.
- None — only valid when ``provider.type == "local-patch"``.

Tokens are the ONLY env-var input — they're secrets and don't
belong in a committable file. Everything else (clone path,
outbox) lives in this TOML.

**Who reads the token decides its mode: 0640 root:<service group>,
not 0400 root.** Delivery runs in the *tracker*
(``tracker/routes/bundle_actions.py`` on Accept, and
``tracker/delivery_sync.py`` when reconciling merges) and never in
the queue runner. The tracker drops to the unprivileged service
account, so a root-owned 0400 token is unreadable by the only
process that wants it — the same reason ``chat.env`` is 0640
root:group while ``harness.env``, which the root runner reads, is
0600. ``provider.clone_dir`` has to be writable by that same
account, for the same reason.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import DeliveryConfigError


__all__ = [
    "DeliveryConfig",
    "load_delivery_config",
]


_KNOWN_PROVIDERS = frozenset({"github", "gitlab", "gitea", "local-patch"})
_DEFAULT_BRANCH_TEMPLATE = "agentic/{origin_safe}-{target_safe}-{signature_short}"
_DEFAULT_COMMITTER_NAME = "Fred [bot]"
_DEFAULT_COMMITTER_EMAIL = "github@dragonflybsd.org"


@dataclass(frozen=True)
class DeliveryConfig:
    """Resolved per-target delivery configuration.

    ``token`` is the resolved secret (None for ``local-patch``).
    ``clone_dir`` is the operator's local DeltaPorts checkout —
    required for network providers, ignored for ``local-patch``.
    ``outbox`` is the local-patch destination directory —
    required for ``local-patch``, None otherwise.
    """
    provider_type: str
    repo: str | None
    base_branch: str
    draft: bool
    labels: tuple[str, ...]
    branch_template: str
    token: str | None
    clone_dir: str | None
    outbox: str | None
    committer_name: str = _DEFAULT_COMMITTER_NAME
    committer_email: str = _DEFAULT_COMMITTER_EMAIL
    extras: dict[str, object] = field(default_factory=dict)


def load_delivery_config(
    config_path: Path,
    *,
    target: str | None = None,
    env: dict[str, str] | None = None,
) -> DeliveryConfig:
    """Parse ``delivery.toml`` and resolve per-target overrides.

    Raises ``DeliveryConfigError`` on missing required fields,
    unknown provider types, or unreadable token files. Caller
    distinguishes by the message string; the exception type is
    intentionally flat for v1.
    """
    env = env if env is not None else dict(os.environ)
    if not config_path.is_file():
        raise DeliveryConfigError(
            f"delivery.toml not found at {config_path!s}"
        )
    try:
        raw = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise DeliveryConfigError(
            f"delivery.toml is not valid TOML: {exc}"
        ) from exc
    return config_from_document(raw, target=target, env=env)


def config_from_settings(
    *,
    target: str | None = None,
    env: dict[str, str] | None = None,
) -> DeliveryConfig:
    """Build a config from the ``[delivery]`` section of the settings.

    Assembles the same document shape the file loader parses and hands it
    to the same validator, rather than growing a second set of required-
    field checks that would drift from the first. The token is resolved
    through ``settings.read_secret`` so the ``token_file`` setting and the
    environment override behave identically here and there.
    """
    from dportsv3 import settings  # noqa: PLC0415

    provider: dict[str, object] = {
        "type": settings.get_str("delivery.type"),
        "base_branch": settings.get_str("delivery.base_branch"),
        "draft": bool(settings.get("delivery.draft")),
        "labels": list(settings.get("delivery.labels")),
        "branch_template": settings.get_str("delivery.branch_template"),
        "committer_name": settings.get_str("delivery.committer_name"),
        "committer_email": settings.get_str("delivery.committer_email"),
    }
    for key, path in (("repo", "delivery.repo"),
                      ("clone_dir", "delivery.clone_dir"),
                      ("outbox", "delivery.outbox")):
        value = str(settings.get(path) or "")
        if value:
            provider[key] = value
    document: dict[str, object] = {"provider": provider}
    targets = settings.get("delivery.target") or {}
    if targets:
        document["target"] = targets
    return config_from_document(
        document, target=target, env=env,
        token=settings.read_secret("delivery.token_file", env=env),
    )


def config_from_document(
    raw: dict,
    *,
    target: str | None = None,
    env: dict[str, str] | None = None,
    token: str | None = None,
) -> DeliveryConfig:
    """Validate one delivery document, however it was assembled.

    ``token`` short-circuits the file/env lookup for callers that have
    already resolved it — which is every caller coming from the settings,
    where the credential is named by a ``*_file`` setting.
    """
    env = env if env is not None else dict(os.environ)

    provider_block = raw.get("provider")
    if not isinstance(provider_block, dict):
        raise DeliveryConfigError(
            "delivery.toml: required [provider] block missing"
        )

    # Resolve per-target overrides. The target lookup is a nested
    # `[target."<name>"]` section. tomllib turns those into the
    # nested dict structure raw["target"][target].
    target_overrides: dict = {}
    if target:
        target_section = raw.get("target", {})
        if isinstance(target_section, dict):
            specific = target_section.get(target)
            if isinstance(specific, dict):
                target_overrides = specific

    def field_value(key: str, default=None):
        if key in target_overrides:
            return target_overrides[key]
        return provider_block.get(key, default)

    provider_type = field_value("type")
    if not provider_type or not isinstance(provider_type, str):
        raise DeliveryConfigError(
            "delivery.toml: required field provider.type missing "
            "(one of 'github', 'gitlab', 'gitea', 'local-patch')"
        )
    if provider_type not in _KNOWN_PROVIDERS:
        raise DeliveryConfigError(
            f"delivery.toml: unknown provider type {provider_type!r} "
            f"(known: {sorted(_KNOWN_PROVIDERS)!r})"
        )

    repo = field_value("repo")
    if provider_type != "local-patch" and not repo:
        raise DeliveryConfigError(
            f"delivery.toml: provider.repo is required for "
            f"type={provider_type!r}"
        )

    base_branch = field_value("base_branch") or "master"
    draft = bool(field_value("draft", True))
    labels_val = field_value("labels", [])
    if not isinstance(labels_val, list):
        raise DeliveryConfigError(
            "delivery.toml: provider.labels must be a list of strings"
        )
    labels = tuple(str(x) for x in labels_val)
    branch_template = field_value("branch_template", _DEFAULT_BRANCH_TEMPLATE)
    committer_name = str(
        field_value("committer_name", _DEFAULT_COMMITTER_NAME)
        or _DEFAULT_COMMITTER_NAME
    )
    committer_email = str(
        field_value("committer_email", _DEFAULT_COMMITTER_EMAIL)
        or _DEFAULT_COMMITTER_EMAIL
    )

    # Token: env var first, then file fallback. Local-patch never
    # needs one.
    if provider_type != "local-patch":
        token = token or _resolve_token(env)
        if not token:
            raise DeliveryConfigError(
                f"delivery.toml: provider type {provider_type!r} "
                f"requires a token. Set $DPORTSV3_DELIVERY_TOKEN or "
                f"place it at $DPORTSV3_CONFIG_DIR/delivery.token."
            )

    clone_dir_val = field_value("clone_dir")
    if provider_type != "local-patch":
        if not clone_dir_val or not isinstance(clone_dir_val, str):
            raise DeliveryConfigError(
                f"delivery.toml: provider.clone_dir is required "
                f"for type={provider_type!r} (the local DeltaPorts "
                f"checkout the tracker pushes from)"
            )
        clone_dir: str | None = clone_dir_val
    else:
        clone_dir = None

    outbox_val = field_value("outbox")
    if provider_type == "local-patch":
        if not outbox_val or not isinstance(outbox_val, str):
            raise DeliveryConfigError(
                "delivery.toml: provider.outbox is required for "
                "type='local-patch' (directory where patches get "
                "written)"
            )
        outbox: str | None = outbox_val
    else:
        outbox = None

    # Preserve any extra top-level fields so providers can read
    # implementation-specific knobs (e.g. gitea host) without
    # extending this dataclass for every variant.
    _known = {"type", "repo", "base_branch", "draft", "labels",
              "branch_template", "clone_dir", "outbox",
              "committer_name", "committer_email"}
    extras = {
        k: v for k, v in provider_block.items() if k not in _known
    }
    for k, v in target_overrides.items():
        if k not in _known:
            extras[k] = v

    return DeliveryConfig(
        provider_type=str(provider_type),
        repo=str(repo) if repo else None,
        base_branch=str(base_branch),
        draft=draft,
        labels=labels,
        branch_template=str(branch_template),
        token=token,
        clone_dir=clone_dir,
        outbox=outbox,
        committer_name=committer_name,
        committer_email=committer_email,
        extras=extras,
    )


def _resolve_token(env: dict[str, str]) -> str | None:
    """Token from env var, then from file.

    Search order:
      1. ``$DPORTSV3_DELIVERY_TOKEN`` env var.
      2. ``$DPORTSV3_CONFIG_DIR/delivery.token``.

    Both tiers sit beside ``orchestrator.resolve_config``'s, so a token
    dropped next to the ``delivery.toml`` the loader just read is found
    without exporting anything extra.

    There is no bundled default, and deliberately so: a token is a secret,
    so the only sensible thing to ship is nothing. Absent both, this returns
    None and the caller reports that delivery needs a token.
    """
    direct = env.get("DPORTSV3_DELIVERY_TOKEN", "").strip()
    if direct:
        return direct
    candidates: list[Path] = []
    config_dir = env.get("DPORTSV3_CONFIG_DIR", "").strip()
    if config_dir:
        candidates.append(Path(config_dir) / "delivery.token")
    for token_file in candidates:
        if not token_file.is_file():
            continue
        try:
            value = token_file.read_text().strip()
        except OSError as exc:
            raise DeliveryConfigError(
                f"delivery.token at {token_file!s} is unreadable: {exc}"
            ) from exc
        if value:
            return value
    return None
