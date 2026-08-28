"""What the shipped playbooks say about repairing a patch (poly-2xd).

devel/glib20 failed because a DragonFly overlay patch stopped applying
after upstream moved to glib 2.86.4. The patch agent's fix deleted the
``file materialize`` line that stages that patch, which would have made
the port build green with the platform fix silently gone (poly-28j).

That was not the agent inventing something. Two of the three playbooks
attached to a ``patch-error`` payload told it to remove the install
line, and one of them said so for a "drifted" patch specifically. A
drifted patch is well-formed and still encodes the wanted change; it
needs refreshing, not deleting. Only a *malformed* patch — one whose
hunk headers disagree with its own body — is worth throwing away.

tests/test_playbooks.py covers the parser and the selector against
tmp_path fixtures. Nothing covered the shipped corpus, so the
contradiction cost nothing to reintroduce and stayed invisible. These
tests read the real files.
"""

from __future__ import annotations

import re

import pytest

from dportsv3.agent.playbooks import find_playbooks_dir, list_entries

DRIFT = re.compile(r"drift|version bump|stopped applying", re.I)
REMOVE_INSTALL = re.compile(
    r"remove (?:its|the) (?:install line|`file materialize`)"
    r"|remove its install line",
    re.I,
)
KEEP = re.compile(r"\bkeep\b|do not remove|not this section|stop —", re.I)


def _entries():
    return list_entries(find_playbooks_dir())


def _paragraphs(text: str):
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _entry(name: str):
    for e in _entries():
        if e.path.name == name:
            return e
    raise AssertionError(f"{name} is gone from the corpus")


# --- the contradiction itself ----------------------------------------------

def test_no_paragraph_tells_the_agent_to_delete_a_drifted_patch() -> None:
    """The defect in one assertion. A paragraph that talks about drift
    and about removing the install line, without also saying to keep it
    or routing elsewhere, is the instruction that deleted the fix."""
    offenders = []
    for entry in _entries():
        for para in _paragraphs(entry.body):
            if DRIFT.search(para) and REMOVE_INSTALL.search(para):
                if not KEEP.search(para):
                    offenders.append(f"{entry.path.name}: {' '.join(para.split())[:160]}")
    assert offenders == [], "drift + removal in one breath:\n" + "\n".join(offenders)


def test_the_recut_flow_keeps_the_materialize_line() -> None:
    body = _entry("error-prefer-dops-over-static-patches.md").body
    recut = body[body.index("Re-cut a drifted source patch"):]
    recut = recut[:recut.index("### Option A")]
    assert re.search(r"\*\*keep\*\* the `file materialize`", recut), recut[:400]


def test_the_recut_flow_warns_against_touching_the_overlay() -> None:
    """The agent reached for overlay.dops anyway. Saying 'keep' is not
    the same as saying 'if you are editing this file, you are wrong'."""
    body = _entry("error-prefer-dops-over-static-patches.md").body
    recut = body[body.index("Re-cut a drifted source patch"):]
    recut = recut[:recut.index("### Option A")]
    assert "overlay.dops` is not edited at all" in recut


# --- the two situations must be told apart ---------------------------------

def _recovery_section() -> str:
    body = _entry("flow-patch.md").body
    start = body.index("Recovering from a broken patch")
    return body[start:body.index("## Removing directives and files")]


def test_the_recovery_section_separates_malformed_from_drifted() -> None:
    """Its symptom list — 'applies dirty', 'line-number mismatch' — is
    also how a drifted patch presents, so a reader holding one lands
    here legitimately. It has to say which case it is for."""
    sec = _recovery_section()
    assert DRIFT.search(sec), "the drift case is not named"
    assert re.search(r"malformed", sec, re.I), "the malformed case is not named"


def test_the_recovery_section_routes_drift_to_the_recut_entry() -> None:
    sec = _recovery_section()
    assert "error-prefer-dops-over-static-patches.md" in sec


def test_the_authoring_entry_does_not_prescribe_a_repair() -> None:
    """error-dragonfly-source-patches.md is titled 'Creating
    DragonFly-specific source patches'. Its repair sentence was outside
    its own scope and contradicted the entry that owns the job."""
    body = _entry("error-dragonfly-source-patches.md").body
    for para in _paragraphs(body):
        if REMOVE_INSTALL.search(para):
            pytest.fail(f"authoring entry still prescribes removal: {' '.join(para.split())[:200]}")


# --- the ordering ----------------------------------------------------------

def test_regeneration_comes_before_any_overlay_edit() -> None:
    """Remove-then-regenerate leaves a window where the port composes
    green with no fix in it. Dying in that window — which is what
    budget exhaustion did — leaves that state on disk."""
    sec = _recovery_section()
    steps = sec[sec.index("1."):]
    regen = steps.lower().index("regenerate a correct patch")
    edit = steps.lower().index("overlay.dops")
    assert regen < edit, "the overlay is still reached before the replacement exists"


def test_the_safe_swap_needs_no_overlay_edit_at_all() -> None:
    """Strongest form of the fix: overwriting the broken patch at its
    existing path is the entire swap, because the file materialize line
    already names it. A procedure that never opens overlay.dops cannot
    leave it half-edited."""
    sec = _recovery_section()
    assert "`overlay.dops` needs no edit at all" in sec
    assert re.search(r"same `dragonfly/` path", sec)


def test_the_green_window_is_named_as_the_hazard() -> None:
    """A reader who does not know why the order matters will reorder
    the steps back. The consequence has to be on the page."""
    sec = _recovery_section()
    assert re.search(r"green", sec, re.I), sec[:300]
    assert re.search(r"never leave `overlay\.dops`", sec, re.I)


# --- the corpus stays navigable --------------------------------------------

def test_every_cross_reference_names_a_file_that_exists() -> None:
    """These entries now route to each other by filename. A rename that
    misses one turns the routing into a dead end the agent cannot follow."""
    names = {e.path.name for e in _entries()} | {"README.md", "TEMPLATE.md"}
    missing = []
    for entry in _entries():
        for ref in re.findall(r"`([a-z0-9-]+\.md)`", entry.body):
            if ref not in names:
                missing.append(f"{entry.path.name} -> {ref}")
    assert missing == [], f"dangling playbook references: {missing}"
