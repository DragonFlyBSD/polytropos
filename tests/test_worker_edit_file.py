"""poly-7jw — the targeted edit primitive and the patch-install guard.

``get_file`` windows its reads so a large file never lands whole in the
prompt. With ``put_file`` as the only write, changing one line of a 19KB
source meant re-emitting all 19KB from a 200-line view of it, and the
model truncated instead. These pin the two halves of the fix: an
anchored replace that never puts the untouched bytes in the model's
hands, and an install step that refuses the malformed diffs the old
truncation path produced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_GEN = Path(__file__).resolve().parents[1]
if str(_GEN) not in sys.path:
    sys.path.insert(0, str(_GEN))


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    from dportsv3.agent import worker

    writable = tmp_path / "writable"
    deltaports = writable / "work" / "DeltaPorts"
    for d in (writable, deltaports):
        d.mkdir(parents=True)
    fake_paths = worker.EnvPaths(env_dir=tmp_path, writable=writable)
    monkeypatch.setattr(worker, "env_paths", lambda env: fake_paths)
    return writable


def _source(env_dir, name: str, body: str) -> Path:
    path = env_dir / "work" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


# --- edit_file ------------------------------------------------------------


def test_edit_file_replaces_without_resending_the_file(env_dir):
    """The point of the tool: a big file changes via a small argument."""
    from dportsv3.agent import worker

    body = "\n".join(f"line {n}" for n in range(1, 501)) + "\n"
    host = _source(env_dir, "big.c", body)

    res = worker.edit_file("env", "/work/big.c", "line 250", "line 250 patched")

    assert res["ok"] is True
    assert res["replacements"] == 1
    assert res["first_edit_line"] == 250
    updated = host.read_text()
    assert "line 250 patched" in updated
    # Every other byte survives — this is the truncation the bead measured.
    assert len(updated.splitlines()) == 500
    assert updated.startswith("line 1\n")
    assert updated.endswith("line 500\n")


def test_edit_file_returns_context_around_the_edit(env_dir):
    from dportsv3.agent import worker
    _source(env_dir, "f.c", "a\nb\nc\nd\ne\nf\ng\nh\n")

    res = worker.edit_file("env", "/work/f.c", "d", "D")

    assert res["ok"] is True
    assert "D" in res["context"]
    assert "\n" in res["context"]  # a window, not just the changed line


def test_edit_file_refuses_an_ambiguous_match(env_dir):
    from dportsv3.agent import worker
    host = _source(env_dir, "f.c", "dup\nother\ndup\n")

    res = worker.edit_file("env", "/work/f.c", "dup", "changed")

    assert res["ok"] is False
    assert res["matches"] == 2
    assert "replace_all" in res["error"]
    assert host.read_text() == "dup\nother\ndup\n"  # untouched


def test_edit_file_replace_all_takes_every_occurrence(env_dir):
    from dportsv3.agent import worker
    host = _source(env_dir, "f.c", "dup\nother\ndup\n")

    res = worker.edit_file("env", "/work/f.c", "dup", "changed", replace_all=True)

    assert res["ok"] is True
    assert res["replacements"] == 2
    assert host.read_text() == "changed\nother\nchanged\n"


def test_edit_file_reports_a_missing_anchor(env_dir):
    from dportsv3.agent import worker
    _source(env_dir, "f.c", "hello\n")

    res = worker.edit_file("env", "/work/f.c", "goodbye", "hi")

    assert res["ok"] is False
    assert res["matches"] == 0
    assert "byte for byte" in res["error"]


def test_edit_file_honours_the_sha256_lock(env_dir):
    from dportsv3.agent import worker
    host = _source(env_dir, "f.c", "hello\n")

    res = worker.edit_file(
        "env", "/work/f.c", "hello", "hi", expected_sha256="0" * 64
    )

    assert res["ok"] is False
    assert "sha256 mismatch" in res["error"]
    assert host.read_text() == "hello\n"


def test_edit_file_rejects_line_numbered_anchors(env_dir):
    """get_file's numbers would otherwise fail as a bare 'no match'."""
    from dportsv3.agent import worker
    _source(env_dir, "f.c", "a\nb\nc\n")

    numbered = "     1\ta\n     2\tb\n     3\tc\n"
    res = worker.edit_file("env", "/work/f.c", numbered, "x")

    assert res["ok"] is False
    assert res["kind"] == "line_numbered_content"


def test_edit_file_keeps_put_file_write_guards(env_dir):
    from dportsv3.agent import worker

    res = worker.edit_file("env", "/work/DPorts/devel/foo/Makefile", "a", "b")

    assert res["ok"] is False
    assert res["kind"] == "regenerated_tree_write_refused"
    assert "/work/DeltaPorts" in res["error"]


def test_edit_file_keeps_the_dops_authoring_lock(env_dir):
    from dportsv3.agent import worker

    res = worker.edit_file(
        "env", "/work/DeltaPorts/ports/devel/foo/Makefile.DragonFly", "a", "b"
    )

    assert res["ok"] is False
    assert res["blocked_by"] == "compat_makefile_authoring_lock"


def test_edit_file_rejects_a_no_op_edit(env_dir):
    from dportsv3.agent import worker
    _source(env_dir, "f.c", "hello\n")

    res = worker.edit_file("env", "/work/f.c", "hello", "hello")

    assert res["ok"] is False
    assert "identical" in res["error"]


def test_edit_file_preserves_mode(env_dir):
    from dportsv3.agent import worker
    host = _source(env_dir, "run.sh", "#!/bin/sh\nfalse\n")
    host.chmod(0o755)

    res = worker.edit_file("env", "/work/run.sh", "false", "true")

    assert res["ok"] is True
    assert host.stat().st_mode & 0o777 == 0o755


def test_edit_file_directs_binary_to_put_file(env_dir):
    from dportsv3.agent import worker
    host = env_dir / "work" / "blob.bin"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_bytes(b"\xff\xfe\x00binary")

    res = worker.edit_file("env", "/work/blob.bin", "binary", "text")

    assert res["ok"] is False
    assert "base64" in res["error"]


def test_edit_file_is_registered_as_a_tool():
    from dportsv3.agent import tools
    assert "edit_file" in tools.names()
    assert "edit_file" in tools.patch_tool_names()


# --- unified-diff validation ---------------------------------------------


def test_validator_accepts_a_well_formed_diff():
    from dportsv3.agent.worker import _validate_unified_diff

    diff = (
        "--- a/foo.c\n"
        "+++ b/foo.c\n"
        "@@ -1,3 +1,4 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        "+two and a half\n"
        " three\n"
    )
    assert _validate_unified_diff("patch-foo.c", diff) is None


def test_validator_catches_the_libunwind_signature():
    """Header claims 13 -> 16; body is all context and changes nothing."""
    from dportsv3.agent.worker import _validate_unified_diff

    body = "".join(f" context {n}\n" for n in range(28))
    diff = "--- a/t.c\n+++ b/t.c\n@@ -358,13 +358,16 @@\n" + body

    problem = _validate_unified_diff("patch-tests_test-ptrace.c", diff)
    assert problem is not None
    assert "declares 13 old / 16 new" in problem
    assert "carries more" in problem


def test_validator_tolerates_the_gcc12_signature_at_eof():
    """Header claims 8, body supplies 5, and the file ends there.

    Deliberately allowed (poly-dq5): a final hunk short by trailing
    context is what devel/readline, shells/bash, net/openslp,
    net/hostapd210 and graphics/dcp2icc all ship, and patch(1) applies
    them. Rejecting it cost a whole job and caught nothing real.
    """
    from dportsv3.agent.worker import _validate_unified_diff

    diff = (
        "--- a/t.c\n+++ b/t.c\n@@ -1,8 +1,8 @@\n"
        " one\n-two\n+TWO\n three\n four\n"
    )
    assert _validate_unified_diff("patch-t.c", diff) is None


def test_validator_accepts_blank_separated_hunks():
    """A hunk stops at its own line counts, not at the next header.

    Reading on would fold the blank separator into this hunk's tally
    and reject a valid patch.
    """
    from dportsv3.agent.worker import _validate_unified_diff

    diff = (
        "--- a/t.c\n+++ b/t.c\n"
        "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
        "\n"
        "@@ -20,2 +20,2 @@\n ten\n-x\n+X\n"
    )
    assert _validate_unified_diff("patch-t.c", diff) is None


def test_validator_accepts_a_trailing_blank_line():
    from dportsv3.agent.worker import _validate_unified_diff

    diff = "--- a/t.c\n+++ b/t.c\n@@ -1,1 +1,1 @@\n-a\n+b\n\n"
    assert _validate_unified_diff("patch-t.c", diff) is None


def test_validator_accepts_a_commentary_preamble():
    """Ports patches often carry a description block before the header."""
    from dportsv3.agent.worker import _validate_unified_diff

    diff = (
        "Fix the build on DragonFly.\n\nObtained from: upstream\n\n"
        "--- a/t.c\n+++ b/t.c\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    )
    assert _validate_unified_diff("patch-t.c", diff) is None


def test_validator_catches_a_context_only_patch():
    from dportsv3.agent.worker import _validate_unified_diff

    diff = "--- a/t.c\n+++ b/t.c\n@@ -1,2 +1,2 @@\n one\n two\n"
    problem = _validate_unified_diff("patch-t.c", diff)
    assert problem is not None
    assert "no '+' or '-' line" in problem


def test_validator_no_longer_rejects_a_missing_trailing_newline():
    """Removed in poly-dq5 — see the docstring for why it earned nothing."""
    from dportsv3.agent.worker import _validate_unified_diff

    diff = "--- a/t.c\n+++ b/t.c\n@@ -1,1 +1,1 @@\n-a\n+b"
    assert _validate_unified_diff("patch-t.c", diff) is None


def test_validator_catches_an_empty_patch():
    from dportsv3.agent.worker import _validate_unified_diff
    assert "empty" in _validate_unified_diff("patch-t.c", "")


def test_validator_accepts_the_no_newline_marker():
    from dportsv3.agent.worker import _validate_unified_diff

    diff = (
        "--- a/t.c\n+++ b/t.c\n@@ -1,1 +1,1 @@\n"
        "-a\n"
        "\\ No newline at end of file\n"
        "+b\n"
    )
    assert _validate_unified_diff("patch-t.c", diff) is None


def test_validator_accepts_single_line_hunk_counts():
    """'@@ -5 +5 @@' means one line, with the count elided."""
    from dportsv3.agent.worker import _validate_unified_diff

    diff = "--- a/t.c\n+++ b/t.c\n@@ -5 +5 @@\n-a\n+b\n"
    assert _validate_unified_diff("patch-t.c", diff) is None


def test_validator_handles_a_multi_file_diff():
    from dportsv3.agent.worker import _validate_unified_diff

    diff = (
        "--- a/one.c\n+++ b/one.c\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        "--- a/two.c\n+++ b/two.c\n@@ -1,1 +1,1 @@\n-c\n+d\n"
    )
    assert _validate_unified_diff("patch-multi", diff) is None


# --- install_patches ------------------------------------------------------


def test_install_patches_fails_loudly_on_an_empty_output_dir(env_dir):
    """Was ok=True with installed=[] — the silent success the bead measured."""
    from dportsv3.agent import worker
    (env_dir / "work" / "genpatch-out").mkdir(parents=True)

    res = worker.install_patches("env", "devel/foo")

    assert res["ok"] is False
    assert res["installed"] == []
    assert "nothing to install" in res["error"]


def test_install_patches_refuses_a_malformed_diff(env_dir):
    from dportsv3.agent import worker
    out = env_dir / "work" / "genpatch-out"
    out.mkdir(parents=True)
    (out / "patch-t.c").write_text(
        "--- a/t.c\n+++ b/t.c\n@@ -358,13 +358,16 @@\n" + " ctx\n" * 28
    )

    res = worker.install_patches("env", "devel/foo")

    assert res["ok"] is False
    assert res["installed"] == []
    assert len(res["rejected"]) == 1
    dst = env_dir / "work" / "DeltaPorts" / "ports" / "devel" / "foo" / "dragonfly"
    assert not dst.exists()


def test_install_patches_installs_a_valid_diff(env_dir):
    from dportsv3.agent import worker
    out = env_dir / "work" / "genpatch-out"
    out.mkdir(parents=True)
    (out / "patch-t.c").write_text("--- a/t.c\n+++ b/t.c\n@@ -1,1 +1,1 @@\n-a\n+b\n")

    res = worker.install_patches("env", "devel/foo")

    assert res.get("ok", True) is True
    assert res["installed"] == ["ports/devel/foo/dragonfly/patch-t.c"]


def test_install_patches_rejects_the_batch_if_any_patch_is_bad(env_dir):
    """All-or-nothing: a half-installed set is worse than a clean refusal."""
    from dportsv3.agent import worker
    out = env_dir / "work" / "genpatch-out"
    out.mkdir(parents=True)
    (out / "patch-good.c").write_text("--- a/g.c\n+++ b/g.c\n@@ -1,1 +1,1 @@\n-a\n+b\n")
    # body longer than the header declares — a real malformation
    (out / "patch-bad.c").write_text(
        "--- a/b.c\n+++ b/b.c\n@@ -1,2 +1,2 @@\n" + " ctx\n" * 6
    )

    res = worker.install_patches("env", "devel/foo")

    assert res["ok"] is False
    dst = env_dir / "work" / "DeltaPorts" / "ports" / "devel" / "foo" / "dragonfly"
    assert not dst.exists()


# --- the write path the bad patch actually took --------------------------


_BAD_PATCH = "--- a/t.c\n+++ b/t.c\n@@ -358,13 +358,16 @@\n" + " ctx\n" * 28
_GOOD_PATCH = "--- a/t.c\n+++ b/t.c\n@@ -1,1 +1,1 @@\n-a\n+b\n"


def test_put_file_refuses_a_malformed_dragonfly_patch(env_dir):
    """install_patches never saw this one — the agent wrote it directly."""
    from dportsv3.agent import worker

    dest = "/work/DeltaPorts/ports/devel/foo/dragonfly/patch-t.c"
    res = worker.put_file("env", dest, _BAD_PATCH)

    assert res["ok"] is False
    assert res["kind"] == "malformed_patch_write"
    host = env_dir / "work" / "DeltaPorts" / "ports" / "devel" / "foo" / "dragonfly"
    assert not host.exists()


def test_put_file_allows_a_well_formed_dragonfly_patch(env_dir):
    from dportsv3.agent import worker

    dest = "/work/DeltaPorts/ports/devel/foo/dragonfly/patch-t.c"
    res = worker.put_file("env", dest, _GOOD_PATCH)

    assert res.get("ok", True) is True


def test_put_file_does_not_parse_non_patch_dragonfly_files(env_dir):
    """`file materialize` sources live here too and are not diffs."""
    from dportsv3.agent import worker

    dest = "/work/DeltaPorts/ports/devel/foo/dragonfly/extra-source.c"
    res = worker.put_file("env", dest, "int main(void) { return 0; }\n")

    assert res.get("ok", True) is True


def test_edit_file_refuses_to_break_a_dragonfly_patch(env_dir):
    """Nudging a hunk body without its header is the classic corruption."""
    from dportsv3.agent import worker

    rel = "work/DeltaPorts/ports/devel/foo/dragonfly/patch-t.c"
    host = env_dir / rel[len("work/"):] if False else env_dir / rel
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(_GOOD_PATCH)

    res = worker.edit_file(
        "env",
        "/work/DeltaPorts/ports/devel/foo/dragonfly/patch-t.c",
        "-a\n+b\n",
        "-a\n+b\n+c\n",
    )

    assert res["ok"] is False
    assert res["kind"] == "malformed_patch_write"
    assert host.read_text() == _GOOD_PATCH  # unchanged on disk


def test_edit_file_write_is_atomic(env_dir, monkeypatch):
    """A write that dies partway must not leave a truncated file."""
    from dportsv3.agent import worker

    host = _source(env_dir, "big.c", "keep\n" * 200 + "UNIQUE\n")
    original = host.read_text()

    real_replace = worker.Path.replace

    def boom(self, target):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(worker.Path, "replace", boom)
    with pytest.raises(OSError):
        worker.edit_file("env", "/work/big.c", "UNIQUE", "CHANGED")

    monkeypatch.setattr(worker.Path, "replace", real_replace)
    assert host.read_text() == original


# --- flag coercion --------------------------------------------------------


def test_replace_all_string_false_does_not_enable_it(env_dir):
    """`not "false"` is False in Python — the string must not sneak through.

    replace_all is the only boolean in the tool registry and dispatch
    passes LLM arguments through untouched, so the coercion has to
    happen here.
    """
    from dportsv3.agent import worker
    host = _source(env_dir, "f.c", "dup\nother\ndup\n")

    res = worker.edit_file("env", "/work/f.c", "dup", "changed", replace_all="false")

    assert res["ok"] is False
    assert res["matches"] == 2  # refused as ambiguous, not silently replaced
    assert host.read_text() == "dup\nother\ndup\n"


def test_replace_all_string_true_is_accepted(env_dir):
    from dportsv3.agent import worker
    host = _source(env_dir, "f.c", "dup\nother\ndup\n")

    res = worker.edit_file("env", "/work/f.c", "dup", "changed", replace_all="true")

    assert res["ok"] is True
    assert host.read_text() == "changed\nother\nchanged\n"


def test_replace_all_rejects_a_nonsense_value(env_dir):
    from dportsv3.agent import worker
    _source(env_dir, "f.c", "dup\n")

    res = worker.edit_file("env", "/work/f.c", "dup", "changed", replace_all="maybe")

    assert res["ok"] is False
    assert "must be true or false" in res["error"]


def test_replace_all_accepts_real_booleans(env_dir):
    from dportsv3.agent import worker
    host = _source(env_dir, "f.c", "dup\nother\ndup\n")

    res = worker.edit_file("env", "/work/f.c", "dup", "changed", replace_all=True)

    assert res["ok"] is True
    assert res["replacements"] == 2
    assert host.read_text() == "changed\nother\nchanged\n"


# --- poly-dq5: the near-miss hint ----------------------------------------


_TAB_INDENTED = (
    "\tcase SYSCALL:\n"
    "\t  if (!state)\n"
    "#if HAVE_DECL_PT_SYSCALL\n"
    "\t  ptrace (PT_SYSCALL, target_pid, 0);\n"
    "#else\n"
    "#error Syscall me\n"
    "#endif\n"
)


def test_not_found_returns_the_nearest_region_verbatim(env_dir):
    """The exact failure from devel/libunwind: two tabs vs tab-plus-spaces.

    The model guessed at the indentation 13 times across that job and
    never hit it, because nothing ever showed it the real bytes.
    """
    from dportsv3.agent import worker
    _source(env_dir, "t.c", _TAB_INDENTED)

    wrong = (
        "#if HAVE_DECL_PT_SYSCALL\n"
        "\t\tptrace (PT_SYSCALL, target_pid, 0);\n"
        "#else\n"
    )
    res = worker.edit_file("env", "/work/t.c", wrong, "X")

    assert res["ok"] is False
    assert res["matches"] == 0
    # The real bytes, tab + two spaces — copyable as the next old_string.
    assert "\t  ptrace (PT_SYSCALL, target_pid, 0);" in res["nearest_text"]
    assert res["nearest_line"] == 3
    assert "nearest_text" in res["error"]


def test_not_found_warns_when_the_region_holds_tabs(env_dir):
    from dportsv3.agent import worker
    _source(env_dir, "t.c", _TAB_INDENTED)

    res = worker.edit_file("env", "/work/t.c", "#if HAVE_DECL_PT_SYSCALL\n  nope\n", "X")

    assert res["ok"] is False
    assert "TAB characters" in res["error"]


def test_nearest_text_is_actually_usable_as_the_next_anchor(env_dir):
    """Round-trip: the hint must be copy-pasteable, not just informative."""
    from dportsv3.agent import worker
    host = _source(env_dir, "t.c", _TAB_INDENTED)

    wrong = "#if HAVE_DECL_PT_SYSCALL\n\t\tptrace (PT_SYSCALL, target_pid, 0);\n#else\n"
    first = worker.edit_file("env", "/work/t.c", wrong, "X")
    assert first["ok"] is False

    second = worker.edit_file("env", "/work/t.c", first["nearest_text"], "REPLACED\n")
    assert second["ok"] is True
    assert "REPLACED" in host.read_text()


def test_no_hint_when_nothing_resembles_the_anchor(env_dir):
    from dportsv3.agent import worker
    _source(env_dir, "t.c", "alpha\nbeta\ngamma\n")

    res = worker.edit_file("env", "/work/t.c", "zzz\nqqq\n", "X")

    assert res["ok"] is False
    assert "nearest_text" not in res
    assert "Nothing in the file resembles it" in res["error"]


# --- poly-dq5: the validator no longer rejects benign shapes -------------


def test_validator_accepts_a_patch_with_no_trailing_newline():
    """Removed rule. It cost devel/libunwind two whole attempts."""
    from dportsv3.agent.worker import _validate_unified_diff
    assert _validate_unified_diff("p", "--- a\n+++ b\n@@ -1,1 +1,1 @@\n-a\n+b") is None


def test_validator_accepts_a_final_hunk_short_at_eof():
    """The readline / bash / openslp / hostapd210 / dcp2icc shape."""
    from dportsv3.agent.worker import _validate_unified_diff
    diff = "--- a\n+++ b\n@@ -1,7 +1,7 @@\n c1\n c2\n c3\n-old\n+new\n c4\n"
    assert _validate_unified_diff("p", diff) is None


def test_validator_still_catches_a_short_hunk_mid_file():
    """Short is only forgiven at EOF — a header followed by more hunks is real."""
    from dportsv3.agent.worker import _validate_unified_diff
    diff = "--- a\n+++ b\n@@ -1,9 +1,9 @@\n-a\n+b\n@@ -50,1 +50,1 @@\n-c\n+d\n"
    problem = _validate_unified_diff("p", diff)
    assert problem is not None
    assert "supplies 1 / 1" in problem


def test_validator_still_catches_the_libunwind_artifact():
    from dportsv3.agent.worker import _validate_unified_diff
    diff = "--- a\n+++ b\n@@ -358,13 +358,16 @@\n" + " ctx\n" * 28
    problem = _validate_unified_diff("p", diff)
    assert problem is not None
    assert "carries more" in problem


# --- poly-dq5: the staging dead-end and the per-attempt workspace --------


def test_reset_attempt_workspace_clears_genpatch_out(env_dir):
    """One attempt's output must not be installable by the next.

    install_patches with no explicit list installs every patch-* it
    finds, so a leftover can land in an unrelated port. Measured:
    libunwind's malformed patch sat there for nine hours.
    """
    from dportsv3.agent import worker
    out = env_dir / "work" / "genpatch-out"
    out.mkdir(parents=True)
    (out / "patch-stale.c").write_text("--- a\n+++ b\n@@ -1,1 +1,1 @@\n-a\n+b\n")
    (out / "keep-me.txt").write_text("not a patch")

    res = worker.reset_attempt_workspace("env", None)

    assert res["ok"] is True
    assert res["genpatch_out_cleared"] == ["patch-stale.c"]
    assert not (out / "patch-stale.c").exists()
    assert (out / "keep-me.txt").exists()  # only patch-* is scratch


def test_reset_attempt_workspace_survives_a_missing_dir(env_dir):
    from dportsv3.agent import worker
    res = worker.reset_attempt_workspace("env", None)
    assert res["ok"] is True
    assert res["genpatch_out_cleared"] == []


def test_attempt_loop_resets_the_workspace_each_attempt():
    """Wiring check: the loop must call it, not just define it."""
    import inspect
    from dportsv3.agent import attempt_loop
    src = inspect.getsource(attempt_loop.run)
    assert "reset_attempt_workspace(" in src
    assert "attempt_idx=attempt_idx" in src
    # and it must sit with the cache reset, at the top of each attempt
    assert src.index("reset_attempt_caches()") < src.index("reset_attempt_workspace")


def test_patch_run_forwards_origin_to_the_attempt_loop():
    """Without origin the workspace reset cannot clean the port's WRKDIR."""
    import inspect
    from dportsv3.agent import patch
    assert "origin=origin" in inspect.getsource(patch.run)


def test_genpatch_stages_into_genpatch_out_on_a_cache_hit(env_dir, monkeypatch):
    """The dead-end: install_patches only reads genpatch-out.

    On a _WRKSRC_CACHE hit genpatch used to leave the patch in WRKSRC
    only, so install_patches raised FileNotFoundError and the agent was
    left hand-authoring the diff.
    """
    from dportsv3.agent import worker

    wrksrc = env_dir / "work" / "obj" / "devel" / "foo" / "foo-1.0"
    wrksrc.mkdir(parents=True)
    monkeypatch.setitem(
        worker._WRKSRC_CACHE, ("env", "devel/foo"), "/work/obj/devel/foo/foo-1.0"
    )

    diff = "--- a\n+++ b\n@@ -1,1 +1,1 @@\n-a\n+b\n"

    def fake_exec(env, *argv, **kw):
        # genpatch's real side effect: the patch appears in WRKSRC.
        (wrksrc / "patch-tests_t.c").write_text(diff)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, "generated patch-tests_t.c\n", "")

    monkeypatch.setattr(worker, "_exec", fake_exec)

    res = worker.genpatch("env", "/work/obj/devel/foo/foo-1.0/tests/t.c")

    assert res["ok"] is True
    staged = env_dir / "work" / "genpatch-out" / "patch-tests_t.c"
    assert staged.is_file(), "genpatch must stage where install_patches reads"
    assert staged.read_text() == diff
    # install_patches can now find it
    out = worker.install_patches("env", "devel/foo")
    assert out.get("ok", True) is True
    assert out["installed"] == ["ports/devel/foo/dragonfly/patch-tests_t.c"]


def test_workdir_is_not_cleaned_on_the_first_attempt(env_dir, monkeypatch):
    """reset_port already cleans it when the previous job ended.

    Cleaning again on attempt 1 would force a needless re-extract of a
    tree that is already pristine.
    """
    from dportsv3.agent import worker
    calls = []
    monkeypatch.setattr(
        worker, "_clean_port_workdir",
        lambda env, origin: calls.append(origin) or {"ok": True},
    )

    worker.reset_attempt_workspace("env", "devel/foo", attempt_idx=1)
    assert calls == []

    worker.reset_attempt_workspace("env", "devel/foo", attempt_idx=2)
    assert calls == ["devel/foo"]


def test_genpatch_out_is_cleared_on_every_attempt(env_dir, monkeypatch):
    """Unlike the WRKDIR, this is shared across jobs and never swept."""
    from dportsv3.agent import worker
    monkeypatch.setattr(worker, "_clean_port_workdir", lambda e, o: {"ok": True})
    out = env_dir / "work" / "genpatch-out"
    out.mkdir(parents=True)
    (out / "patch-stale.c").write_text("x")

    res = worker.reset_attempt_workspace("env", "devel/foo", attempt_idx=1)
    assert res["genpatch_out_cleared"] == ["patch-stale.c"]


def test_nearest_region_declines_a_weak_resemblance(env_dir):
    """One incidental line matching is not a near miss.

    Pointing the model at an unrelated region is worse than saying
    nothing.
    """
    from dportsv3.agent import worker
    body = "".join(f"unrelated line {n}\n" for n in range(40))
    _source(env_dir, "t.c", body + "int main(void) { return 0; }\n")

    anchor = "int main(void) { return 0; }\n" + "".join(
        f"totally different {n}\n" for n in range(20)
    )
    res = worker.edit_file("env", "/work/t.c", anchor, "X")

    assert res["ok"] is False
    assert "nearest_text" not in res


def test_nearest_region_still_fires_on_a_real_near_miss(env_dir):
    """The libunwind shape: most lines right, indentation wrong."""
    from dportsv3.agent import worker
    _source(env_dir, "t.c", _TAB_INDENTED)

    anchor = (
        "#if HAVE_DECL_PT_SYSCALL\n"
        "        ptrace (PT_SYSCALL, target_pid, 0);\n"
        "#else\n"
        "#error Syscall me\n"
        "#endif\n"
    )
    res = worker.edit_file("env", "/work/t.c", anchor, "X")

    assert res["ok"] is False
    assert "\t  ptrace (PT_SYSCALL, target_pid, 0);" in res["nearest_text"]


def test_workspace_reset_keeps_the_materialize_baseline(env_dir, monkeypatch):
    """Cleaning a WRKDIR must not make dsynth_build refuse.

    _clean_port_workdir drops _MATERIALIZE_STATE, which dsynth_build
    gates on — losing it would cost a turn per retry to
    blocked_by=stale_compose for a compose tree that is still valid.
    """
    from dportsv3.agent import worker
    monkeypatch.setitem(worker._MATERIALIZE_STATE, ("env", "devel/foo"), "abc123")
    monkeypatch.setattr(
        worker, "_clean_port_workdir",
        lambda e, o: worker._MATERIALIZE_STATE.pop((e, o), None) or {"ok": True},
    )

    worker.reset_attempt_workspace("env", "devel/foo", attempt_idx=2)

    assert worker._MATERIALIZE_STATE.get(("env", "devel/foo")) == "abc123"


def test_workspace_reset_event_reaches_the_activity_log():
    """Without a dispatcher branch the reset is invisible in the tracker."""
    import inspect
    from dportsv3.agent import steps
    src = inspect.getsource(steps.PatchEventDispatcher.__call__)
    assert 'et == "attempt_workspace_reset"' in src
