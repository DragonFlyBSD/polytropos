"""The re-cut procedure must not promise a path it cannot deliver.

poly-7pwa.13. error-prefer-dops-over-static-patches.md told the agent the
refreshed patch "overwrites the old one at the same path", so overlay.dops
needs no edit. For a target-scoped port the old one is at
``dragonfly/@<target>/<name>`` and a flat write is not that path -- the kept
``file materialize`` goes on staging the stale file, the fresh one is
referenced by nothing, and reconcile deletes it as an orphan once the build
passes.

One paragraph of guidance routed into three other beads at once: .2 (the
destination), .1 (an appended op inherits the wrong scope) and .3 (two ops
on one destination, no diagnostic). So the fix is to name the mechanism --
install_patches -- rather than restate a path.
"""

from __future__ import annotations


from dportsv3.paths import AGENT_PLAYBOOKS_DIR

DOC = AGENT_PLAYBOOKS_DIR / "error-prefer-dops-over-static-patches.md"


def _flat(path) -> str:
    """Text with line wrapping collapsed, so claims survive a reflow."""
    return " ".join(path.read_text().split())


def _section(heading: str) -> str:
    """Just the named section, so a later occurrence cannot satisfy a test.

    Fails readably when the heading is reworded, rather than raising
    IndexError from a split that found nothing.
    """
    text = DOC.read_text()
    assert heading in text, f"heading moved: {heading!r}"
    body = text.split(heading, 1)[1]
    return " ".join(body.split("\n### ", 1)[0].split())


def test_the_doc_no_longer_claims_a_flat_same_path_overwrite():
    text = _flat(DOC)
    # The exact false promise.
    assert "overwrites the old one at the same path" not in text


def test_the_procedure_names_install_patches_rather_than_a_path():
    section = _section("### Re-cut a drifted source patch")
    assert "install_patches" in section
    assert "do not assume the path is" in section.lower()
    assert "take it from `installed`" in section


def test_the_doc_names_the_scoped_path():
    section = _section("### Re-cut a drifted source patch")
    assert "dragonfly/@<target>/<name>" in section


def test_the_doc_forbids_a_second_materialize_op_without_a_false_refusal():
    """A second op is forbidden, but not because the engine refuses it.

    What a second op does (dead under the same target, an override under
    @any) is install_patches' rerouted note, shown only when it applies.
    """
    section = _section("### Re-cut a drifted source patch")
    assert "Do not add a second `file materialize` for it" in section
    assert "refused" not in section
    assert "E_SEM_" not in section


def test_it_still_forbids_removing_the_materialize_line():
    """The original warning was right and must survive the rewrite."""
    text = _flat(DOC)
    assert "you are about to delete the fix rather than repair it" in text


def test_no_playbook_or_tool_description_still_promises_the_flat_path():
    """The recurrence guard. This is the miss class that keeps happening.

    A corrected playbook is worthless while another document -- especially
    a tool description, which is in the model's context every turn -- still
    states the claim. Both earlier beads in this epic shipped with exactly
    that gap.
    """
    from dportsv3.agent import tools

    corpus = {
        path.name: _flat(path)
        for path in AGENT_PLAYBOOKS_DIR.glob("*.md")
    }
    corpus["dops_quickref.md"] = _flat(
        AGENT_PLAYBOOKS_DIR.parent / "dops_quickref.md"
    )
    spec = next(
        t for t in tools._TOOLS
        if (t.get("name") or t.get("function", {}).get("name")) == "install_patches"
    )
    corpus["tools.py:install_patches"] = " ".join(str(spec).split())

    offenders = [
        name for name, text in corpus.items()
        if "into `ports/<origin>/dragonfly/`" in text
        or "into DeltaPorts/ports/<origin>/dragonfly/" in text
    ]
    assert offenders == [], offenders
