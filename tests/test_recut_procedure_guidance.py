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


def test_the_doc_states_the_scoped_shape_source_scoped_destination_flat():
    section = _section("### Re-cut a drifted source patch")
    assert "dragonfly/@<target>/<name>" in section
    # The half that makes it non-obvious: only the SOURCE is scoped.
    assert "destination stays flat" in section


def test_the_doc_frames_the_lane_as_per_file_not_per_port():
    """pkg is part flat and part scoped in ONE overlay.

    A per-port framing is the same error the first implementation made.
    """
    section = _section("### Re-cut a drifted source patch")
    assert "per file" in section
    assert "the overlay is the authority" in section


def test_the_doc_says_a_second_op_is_refused_not_merely_unreported():
    """poly-7pwa.3 landed a hard diagnostic; saying "nothing reports it"
    would now send the agent to burn a turn on a refused write."""
    section = _section("### Re-cut a drifted source patch")
    assert "E_SEM_DUPLICATE_DESTINATION" in section
    # The specific claim that is now false. Not a broad "nothing reports"
    # grep: a correct sentence further down says exactly that about a
    # different failure (a green build with the fix gone).
    assert "the engine reports nothing" not in section


def test_the_doc_explains_the_no_note_case():
    """`scope_note` is absent whenever nothing needed explaining."""
    section = _section("### Re-cut a drifted source patch")
    assert "installed" in section
    assert "No note plus a flat path" in section


def test_the_doc_warns_that_a_stranded_recut_is_deleted_as_an_orphan():
    """The consequence that turns a silent miss into lost work.

    Asserts the sentence, not the words: "orphan" already occurs elsewhere
    in this file, so grepping for it proved nothing.
    """
    section = _section("### Re-cut a drifted source patch")
    assert "no op names is **deleted** as an orphan" in section
    # And the slave-port exception, so the claim is not absolute.
    assert "slave port" in section


def test_the_doc_forbids_appending_a_second_materialize_op():
    """The repair an agent would otherwise reach for, which compounds .1 and .3."""
    text = _flat(DOC)
    assert "second `file materialize`" in text
    assert "Order is by scope, not by position" in text


def test_it_still_forbids_removing_the_materialize_line():
    """The original warning was right and must survive the rewrite."""
    text = _flat(DOC)
    assert "you are about to delete the fix rather than repair it" in text


def test_the_referenced_flow_patch_section_exists():
    """A cross-reference to a heading that does not exist is worse than none."""
    flow = (AGENT_PLAYBOOKS_DIR / "flow-patch.md").read_text()
    assert "## Order is by scope, not by position" in flow


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
