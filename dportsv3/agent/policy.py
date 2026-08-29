"""Trust-tier + budget policy.

Maps a triage ``(classification, confidence)`` pair to a tier with a
budget. The tables come from ``[policy]`` in the settings file; an
explicit ``agentic-policy.json`` is still honoured when one is named,
which is how an operator tries an alternative policy for a single run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Triage confidence is a closed vocabulary, shared by the producer
# (triage parses/stores it) and the consumer (tier_for floors on it).
Confidence = Literal["low", "medium", "high"]
CONFIDENCE_ORDER: list[Confidence] = ["low", "medium", "high"]


@dataclass
class Tier:
    name: str  # "AUTO" | "ASSIST" | "MANUAL"
    max_iterations: int = 0
    max_tokens: int = 0


@dataclass
class Policy:
    tiers: dict[str, Tier]
    classification_to_tier: dict[str, str]
    confidence_floor: dict[str, str]


def load_policy(path: Path | str | None) -> Policy:
    """Read a policy from a JSON file, or from the settings when None.

    Both sources produce the same three tables, so this is one shape with
    two readers rather than two policies. The JSON path stays because a
    file is the convenient thing to hand around when comparing policies —
    it is a whole policy in one argument, which a settings file is not.
    """
    if path is None:
        from dportsv3 import settings  # noqa: PLC0415
        return _from_tables(
            settings.get("policy.tiers"),
            settings.get("policy.classification_to_tier"),
            settings.get("policy.confidence_floor"),
        )
    raw = json.loads(Path(path).read_text())
    return _from_tables(
        raw.get("tiers", {}),
        raw.get("classification_to_tier", {}),
        raw.get("confidence_floor", {}),
    )


def _from_tables(tiers_raw: dict, classification_to_tier: dict,
                 confidence_floor: dict) -> Policy:
    tiers = {
        name: Tier(
            name=name,
            max_iterations=int(spec.get("max_iterations", 0)),
            max_tokens=int(spec.get("max_tokens", 0)),
        )
        for name, spec in (tiers_raw or {}).items()
    }
    return Policy(
        tiers=tiers,
        classification_to_tier=dict(classification_to_tier or {}),
        confidence_floor=dict(confidence_floor or {}),
    )


def _confidence_at_least(value: str, floor: str) -> bool:
    if value not in CONFIDENCE_ORDER or floor not in CONFIDENCE_ORDER:
        return False
    return CONFIDENCE_ORDER.index(value) >= CONFIDENCE_ORDER.index(floor)


def tier_for(policy: Policy, classification: str, confidence: str) -> Tier:
    """Resolve the tier for a triage outcome, cascading confidence_floor downgrades.

    Each tier carries a ``confidence_floor`` that the triage confidence
    must meet. If confidence is below the floor, the tier is downgraded
    one step (AUTO → ASSIST → MANUAL) and the new tier's floor is
    re-evaluated. Cascades until either the floor is met or MANUAL is
    reached. Unknown classifications start at MANUAL.

    Examples (with floors AUTO=high, ASSIST=medium):
        plist-error + high   → AUTO
        plist-error + medium → ASSIST (AUTO floor not met → downgrade)
        plist-error + low    → MANUAL (cascades AUTO → ASSIST → MANUAL)
        compile-error + low  → MANUAL (ASSIST floor not met → downgrade)
    """
    tier_name = policy.classification_to_tier.get(classification, "MANUAL")
    # Cascade downgrades until the confidence floor is satisfied or we
    # land at MANUAL (no further downgrade possible).
    while True:
        floor = policy.confidence_floor.get(tier_name)
        if not floor or _confidence_at_least(confidence, floor):
            break
        next_name = _downgrade(tier_name)
        if next_name == tier_name:
            break
        tier_name = next_name
    return policy.tiers.get(tier_name) or Tier(name="MANUAL")


def _downgrade(tier_name: str) -> str:
    if tier_name == "AUTO":
        return "ASSIST"
    if tier_name == "ASSIST":
        return "MANUAL"
    return tier_name
