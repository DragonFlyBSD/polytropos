"""poly-9u2: a prior attempt is a record, not a recipe.

devel/libunwind produced five byte-identical ``agent_fixed`` diffs
across two models and four harness changes. The constant input was the
payload: ``PriorAttemptsSection`` inlined the earlier bundle's
``changes.diff`` verbatim under that bundle's ``Rebuild Status:
success``, and the agent copied it. ``rebuild_ok`` only ever meant
"dsynth compiled the port".

These tests pin the three properties that break the loop: the edit is
summarised rather than reproduced, the fenced blocks that carried it in
``patch.md`` are elided, and the build signal is not called success.
"""
from __future__ import annotations

from dportsv3.agent import context as ctx_mod
from dportsv3.agent.context import (
    ContextCtx,
    PriorAttemptsSection,
    PriorTriagesSection,
    _strip_fenced_blocks,
    _summarize_diff,
)


_DIFF = (
    "diff --git a/ports/devel/libunwind/overlay.dops "
    "b/ports/devel/libunwind/overlay.dops\n"
    "--- a/ports/devel/libunwind/overlay.dops\n"
    "+++ b/ports/devel/libunwind/overlay.dops\n"
    "@@ -1,0 +1,3 @@\n"
    "+mk target set post-patch <<'MK'\n"
    "+\t${REINPLACE_CMD} -e 's|#else|#elif defined(__DragonFly__)|'\n"
    "+MK\n"
    "-dropped\n"
)

_PATCH_MD = (
    "## Patch Log\n"
    "Added a DragonFly branch to the ptrace switch.\n"
    "\n"
    "## Rebuild Status\n"
    "success\n"
    "\n"
    "## Patch Plan (JSON)\n"
    "```json\n"
    '{"ops": [{"op": "mk", "body": "REINPLACE_CMD -e s|#else|#elif|"}]}\n'
    "```\n"
)


def _read(_bundle_dir, bundle_id, relpath):
    if bundle_id != "past-a":
        return None
    return {
        "analysis/changes.diff": _DIFF,
        "analysis/patch.md": _PATCH_MD,
        "analysis/triage.md": "## Classification\ncompile-error\n",
        "analysis/patch_audit.json": (
            '{"status":"success","attempts":'
            '[{"attempt":1,"tokens":9,"rebuild_ok":true}]}'
        ),
    }.get(relpath)


def _patch_render() -> str:
    return PriorAttemptsSection().render(
        ContextCtx(prior_patch_bundle_ids=["past-a"], read_bundle_text=_read)
    )


def _triage_render() -> str:
    return PriorTriagesSection().render(
        ContextCtx(prior_triage_bundle_ids=["past-a"], read_bundle_text=_read)
    )


# --- the diff body never reaches the prompt ----------------------------------


def test_patch_flow_never_reproduces_the_prior_diff():
    out = _patch_render()
    assert "REINPLACE_CMD" not in out
    assert "__DragonFly__" not in out
    assert "```diff" not in out


def test_triage_flow_never_reproduces_the_prior_diff():
    out = _triage_render()
    assert "REINPLACE_CMD" not in out
    assert "__DragonFly__" not in out
    assert "```diff" not in out


def test_the_files_touched_do_survive():
    """Summarising must not amount to hiding the attempt — the whole
    point of history is knowing where the last one went."""
    for out in (_patch_render(), _triage_render()):
        assert "#### Files Changed" in out
        assert "ports/devel/libunwind/overlay.dops (+3/-1)" in out


# --- the second channel: fenced blocks inside patch.md -----------------------


def test_the_patch_plan_block_is_elided_but_the_narrative_is_kept():
    """``patch.md`` carries the ops in a ``## Patch Plan (JSON)`` fence.
    Dropping only changes.diff would leave that channel open."""
    for out in (_patch_render(), _triage_render()):
        assert "Added a DragonFly branch to the ptrace switch." in out
        assert '"op": "mk"' not in out
        assert "line(s) of literal content elided" in out


# --- the build signal is not called success ----------------------------------


def test_the_compile_signal_is_labelled_for_what_it_measures():
    out = _patch_render()
    assert "compiled=True" in out
    assert "rebuild_ok" not in out


def test_the_header_says_the_attempts_did_not_resolve_the_port():
    """Both headers must make the same two claims: the port is not
    working despite these attempts, and a green build did not check
    whether the change was correct."""
    for out in (_patch_render(), _triage_render()):
        low = out.lower()
        assert "left it working" in low
        assert "compiled" in low
        assert "correct" in low


# --- helper units ------------------------------------------------------------


def test_summarize_diff_counts_per_file():
    diff = (
        "diff --git a/x/A b/x/A\n--- a/x/A\n+++ b/x/A\n@@\n+one\n+two\n-gone\n"
        "diff --git a/x/B b/x/B\n--- a/x/B\n+++ b/x/B\n@@\n+only\n"
    )
    assert _summarize_diff(diff) == "- x/A (+2/-1)\n- x/B (+1/-0)"


def test_summarize_diff_ignores_the_file_headers_themselves():
    """`---`/`+++` are not content lines; counting them would inflate
    every file by one add and one removal."""
    diff = "diff --git a/x/A b/x/A\n--- a/x/A\n+++ b/x/A\n@@\n+one\n"
    assert _summarize_diff(diff) == "- x/A (+1/-0)"


def test_summarize_diff_survives_a_body_with_no_headers():
    assert "no per-file changes" in _summarize_diff("not a diff at all\n")


def test_strip_fenced_blocks_closes_an_unterminated_fence():
    """A report clipped mid-block leaves an open fence; without the
    end-of-text close the remaining lines would leak through."""
    out = _strip_fenced_blocks("prose\n```json\n{secret}\n")
    assert "prose" in out
    assert "{secret}" not in out
    assert "1 line(s) of literal content elided" in out


def test_strip_fenced_blocks_keeps_text_between_two_blocks():
    out = _strip_fenced_blocks("a\n```\nX\n```\nb\n```\nY\n```\nc\n")
    assert [line for line in out.splitlines() if line in ("a", "b", "c")] == [
        "a", "b", "c",
    ]
    assert "X" not in out and "Y" not in out


def test_module_exposes_no_diff_char_cap_anymore():
    """The cap only made sense while the diff body was being rendered."""
    assert not hasattr(PriorAttemptsSection(), "max_diff_chars")
    assert not hasattr(PriorTriagesSection(), "max_diff_chars")
    assert hasattr(ctx_mod, "_summarize_diff")


def test_summarize_diff_counts_content_that_looks_like_a_header():
    """Inside a hunk a leading `-` is always the change marker, so a
    removed line whose own text starts with `--` arrives as `---...`
    and must still be counted. Skipping every `---` would undercount
    any diff of a Makefile comment or a patch file."""
    diff = (
        "diff --git a/x/A b/x/A\n"
        "--- a/x/A\n"
        "+++ b/x/A\n"
        "@@ -1,2 +1,2 @@\n"
        "--- old comment\n"
        "+++ new comment\n"
    )
    assert _summarize_diff(diff) == "- x/A (+1/-1)"


def test_summarize_diff_still_tallies_a_diff_with_no_hunk_header():
    """Malformed or abbreviated bodies should degrade to a rough count
    rather than silently reporting zero changes."""
    assert _summarize_diff("diff --git a/foo b/foo\n+changed\n") == "- foo (+1/-0)"


# --- fallback paths must not reopen the channel ------------------------------


def test_an_unparseable_tool_trace_is_not_dumped_verbatim():
    """Tool-call args carry put_file/edit_file bodies. The pre-poly-9u2
    fallback dumped 2000 raw chars of the trace when no line parsed as
    JSON, which would hand back the edit this section just summarised."""
    corrupt = (
        '{"type":"tool_call","tool":"put_file","args":{"content":'
        '"REINPLACE_CMD -e s|#else|#elif defined(__DragonFly__)|"}}'
    )[:60]  # truncated mid-object: valid-looking, unparseable

    def read(_bd, bid, relpath):
        if bid == "past-a" and relpath == "analysis/tool_trace.jsonl":
            return corrupt
        return None

    out = PriorAttemptsSection().render(
        ContextCtx(prior_patch_bundle_ids=["past-a"], read_bundle_text=read)
    )
    assert "REINPLACE_CMD" not in out
    assert "did not parse" in out


def test_the_rebuild_proof_block_is_bounded():
    """rebuild_proof.json is written verbatim from the model's own
    Rebuild Proof block, so it is model-authored text and gets a cap."""

    def read(_bd, bid, relpath):
        if bid == "past-a" and relpath == "analysis/rebuild_proof.json":
            return '{"rebuild_ok": true, "log": "' + "x" * 5000 + '"}'
        return None

    out = PriorTriagesSection().render(
        ContextCtx(prior_triage_bundle_ids=["past-a"], read_bundle_text=read)
    )
    assert "[...truncated to 1000 chars...]" in out
