"""get_file returns line-numbered content (poly-pg4).

Measured from a session dump: the single largest reasoning turn of a
whole patch attempt was 53,124 chars, and it opened by transcribing the
file to attach numbers before it could write anything::

    Line 975: (blank)
    Line 976: # Dependencies used by executables below
    Line 977: have_libelf = false

Across one attempt, 9 of 21 assistant turns did some form of this,
roughly 20,951 chars of reasoning. The tool already knew the numbers —
it returned ``first_line=166`` as metadata and then unnumbered text,
leaving the model to rebuild the mapping itself.

Lowering ``reasoning_effort`` does not fix it: median output per turn
fell 87% while the *maximum* rose from 8,221 to 16,248, because the
behaviour is structural rather than a matter of effort.

This is the established convention, not an invention. Claude Code
returns ``cat -n`` format; Roo Code returns ``N | content``; Zed added
it in PR #56779 (merged 2026-05-22), whose reviewer noted models "are
all passing -n to every tool already". Zed also pins the detail that
matters here: numbering reflects the *file's* lines, so a ranged read
starting at 42 emits 42, not 1.

The hazard it introduces is that the model can echo numbered text back
into ``put_file`` and bake the numbers into the file, so that write is
refused — see ``_reject_line_numbered_content``.
"""

from __future__ import annotations

import types

import pytest

from dportsv3.agent import tools, worker


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "writable"
    (root / "work").mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda e: types.SimpleNamespace(writable=root),
    )
    monkeypatch.setattr(
        worker, "_resolve_chroot_path",
        lambda paths, p: paths.writable / p.lstrip("/"),
    )
    worker.reset_attempt_caches()
    return root


def _write(root, rel, text):
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# --- the numbering ----------------------------------------------------------

def test_content_is_numbered_cat_n_style(env) -> None:
    _write(env, "work/a.c", "alpha\nbeta\ngamma\n")
    out = worker.get_file("e", "/work/a.c")
    assert out["content"] == (
        "     1\talpha\n"
        "     2\tbeta\n"
        "     3\tgamma\n"
    )
    assert out["content_is_line_numbered"] is True


def test_a_window_carries_the_files_own_numbers(env) -> None:
    """The whole point. A window starting at line 166 must begin at 166,
    not 1 — otherwise the model still has to do the arithmetic, which is
    the cost this exists to remove."""
    _write(env, "work/a.c", "".join(f"line{i}\n" for i in range(1, 301)))
    out = worker.get_file("e", "/work/a.c", offset_lines=165, limit_lines=3)
    assert out["content"] == (
        "   166\tline166\n"
        "   167\tline167\n"
        "   168\tline168\n"
    )
    assert out["first_line"] == 166


def test_numbers_stay_aligned_past_five_digits(env) -> None:
    """Six-wide right alignment is the cat -n convention; a longer number
    must not lose its tab separator, or the strip-before-write rule
    stops being mechanical."""
    _write(env, "work/big.c", "".join(f"l{i}\n" for i in range(1, 100_002)))
    out = worker.get_file("e", "/work/big.c", start_line=99_999, end_line=100_001)
    assert out["content"] == (
        " 99999\tl99999\n"
        "100000\tl100000\n"
        "100001\tl100001\n"
    )


def test_an_empty_window_is_still_empty(env) -> None:
    _write(env, "work/a.c", "one\n")
    out = worker.get_file("e", "/work/a.c", offset_lines=50)
    assert out["content"] == ""
    assert out["first_line"] == 0


# --- the 1-indexed range ----------------------------------------------------

def test_a_start_end_range_is_inclusive(env) -> None:
    """'lines 201-212' is how the model states it and how sed, Roo and
    Zed all take it. Inclusive on both ends."""
    _write(env, "work/a.c", "".join(f"l{i}\n" for i in range(1, 21)))
    out = worker.get_file("e", "/work/a.c", start_line=5, end_line=7)
    assert [ln.split("\t", 1)[1] for ln in out["content"].splitlines()] == \
        ["l5", "l6", "l7"]
    assert (out["first_line"], out["last_line"]) == (5, 7)


def test_start_line_without_end_line_falls_back_to_the_count(env) -> None:
    _write(env, "work/a.c", "".join(f"l{i}\n" for i in range(1, 51)))
    out = worker.get_file("e", "/work/a.c", start_line=10, limit_lines=3)
    assert (out["first_line"], out["last_line"]) == (10, 12)


def test_the_offset_form_still_works(env) -> None:
    """Existing callers pass offset_lines; the range is an addition, not
    a replacement."""
    _write(env, "work/a.c", "".join(f"l{i}\n" for i in range(1, 51)))
    out = worker.get_file("e", "/work/a.c", offset_lines=9, limit_lines=3)
    assert (out["first_line"], out["last_line"]) == (10, 12)


def test_a_range_past_eof_is_clamped(env) -> None:
    _write(env, "work/a.c", "a\nb\n")
    out = worker.get_file("e", "/work/a.c", start_line=1, end_line=9999)
    assert out["last_line"] == 2
    assert out["truncated"] is False


# --- the hazard the numbering introduces ------------------------------------

def test_writing_numbered_content_back_is_refused(env) -> None:
    """The model can copy what it was shown straight into put_file. That
    would bake a number and a tab into every line of a real file, and it
    would look plausible in a diff."""
    _write(env, "work/a.c", "alpha\nbeta\ngamma\n")
    shown = worker.get_file("e", "/work/a.c")["content"]

    out = worker.put_file("e", "/work/a.c", shown)
    assert out["ok"] is False
    assert out["kind"] == "line_numbered_content"
    assert "strip" in out["error"]
    assert (env / "work/a.c").read_text() == "alpha\nbeta\ngamma\n", \
        "the file must be untouched by a refused write"


def test_stripped_content_writes_fine(env) -> None:
    _write(env, "work/a.c", "alpha\nbeta\ngamma\n")
    shown = worker.get_file("e", "/work/a.c")["content"]
    stripped = "".join(
        ln.split("\t", 1)[1] + "\n" for ln in shown.splitlines()
    )
    # put_file returns {path, sha256, size} on success — no "ok" key;
    # only the refusal paths add one.
    out = worker.put_file("e", "/work/a.c", stripped)
    assert "error" not in out and out["sha256"]
    assert (env / "work/a.c").read_text() == "alpha\nbeta\ngamma\n"


@pytest.mark.parametrize("content", [
    "a\nb\nc\n",                                  # ordinary text
    "1\tone\n",                                   # one numeric-tab line
    "1\tone\n2\ttwo\n",                           # two — still under the run
    "col1\tcol2\nx\ty\nz\tw\n",                   # a TSV with no numbers
])
def test_ordinary_content_is_not_mistaken_for_numbered(env, content) -> None:
    """A file may legitimately contain number-then-tab lines. The guard
    keys on three consecutive matches of the emitted shape, so real
    content is not blocked."""
    assert worker._reject_line_numbered_content("/work/x", content) is None


def test_base64_writes_skip_the_guard(env) -> None:
    """Binary content is not line-numbered text and must not be scanned
    as though it were."""
    import base64

    blob = base64.b64encode(b"\x00\x01\x02" * 100).decode()
    out = worker.put_file("e", "/work/bin.dat", blob, encoding="base64")
    assert "error" not in out and out["sha256"]


# --- the model has to be told ------------------------------------------------

def test_the_schema_documents_the_numbering_and_the_strip_rule() -> None:
    """Numbered output the model does not expect is worse than none: it
    would either strip it uncertainly or write it back."""
    spec = next(s for s in tools.schemas()
                if s["function"]["name"] == "get_file")["function"]
    desc = spec["description"]
    assert "line-numbered" in desc
    assert "display only" in desc
    assert "start_line" in spec["parameters"]["properties"]
    assert "end_line" in spec["parameters"]["properties"]


def test_put_file_schema_warns_about_the_numbers() -> None:
    spec = next(s for s in tools.schemas()
                if s["function"]["name"] == "put_file")["function"]
    assert "line numbers" in spec["description"]
