"""The patch agent gets one condition for dops_reference, and no convert flow.

There is no convert job type: the runner dispatches patch, triage, verify
and confirm. A port with compat files and no overlay is handed to a human
(bootstrap_decision says abort), so the patch prompt must not tell the
agent to convert it.
"""

from __future__ import annotations

from dportsv3.agent import tools, worker
from dportsv3.agent.overlay_state import OverlayFacts, bootstrap_decision
from dportsv3.agent.prompts import PATCH_SYSTEM
from dportsv3.paths import AGENT_PLAYBOOKS_DIR


def _flat(text: str) -> str:
    return " ".join(text.split())


def _description(name: str) -> str:
    for tool in tools._TOOLS:
        if tool["function"]["name"] == name:
            return _flat(tool["function"]["description"])
    raise AssertionError(f"no tool named {name}")


def _playbook(name: str) -> str:
    return _flat((AGENT_PLAYBOOKS_DIR / name).read_text())


def test_no_tool_description_describes_the_retired_convert_flow():
    for tool in tools._TOOLS:
        name = tool["function"]["name"]
        description = tool["function"]["description"]
        assert "Convert flow" not in description, name
        assert "Conversion Proof" not in description, name


def test_every_site_gives_dops_reference_the_same_condition():
    sites = {
        "PATCH_SYSTEM": _flat(PATCH_SYSTEM),
        "description": _description("dops_reference"),
        "quickref": _flat(worker.dops_reference("any-env")["content"]),
        "flow-patch": _playbook("flow-patch.md"),
        "error-prefer": _playbook("error-prefer-dops-over-static-patches.md"),
    }
    for name, text in sites.items():
        for phrase in ("does NOT exist", "doesn't exist for the origin",
                       "only if Step 3 returned", "about to write a fresh",
                       "still on static patches"):
            assert phrase not in text, (name, phrase)
    description = sites["description"]
    assert "at most ONCE" in description
    assert "stays in context" in description


def test_the_patch_agent_does_not_convert_a_port_with_no_overlay():
    facts = OverlayFacts(origin="devel/thing", port_exists=True,
                         makefile_dragonfly=("Makefile.DragonFly",))
    assert bootstrap_decision(facts, None).action == "abort"
    prompt = _flat(PATCH_SYSTEM)
    assert "The durable fix is conversion to dops" not in prompt
    assert "do not create the overlay" in prompt
