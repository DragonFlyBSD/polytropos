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
    assert "claims 13 old / 16 new" in problem


def test_validator_catches_the_gcc12_signature():
    """Header claims 8 lines, body supplies 5 — it runs out early."""
    from dportsv3.agent.worker import _validate_unified_diff

    diff = (
        "--- a/t.c\n+++ b/t.c\n@@ -1,8 +1,8 @@\n"
        " one\n-two\n+TWO\n three\n four\n"
    )
    problem = _validate_unified_diff("patch-t.c", diff)
    assert problem is not None
    assert "claims 8 old / 8 new" in problem


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


def test_validator_catches_a_missing_trailing_newline():
    from dportsv3.agent.worker import _validate_unified_diff

    diff = "--- a/t.c\n+++ b/t.c\n@@ -1,1 +1,1 @@\n-a\n+b"
    problem = _validate_unified_diff("patch-t.c", diff)
    assert problem is not None
    assert "newline" in problem


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
    (out / "patch-bad.c").write_text("--- a/b.c\n+++ b/b.c\n@@ -1,9 +1,9 @@\n-a\n+b\n")

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
