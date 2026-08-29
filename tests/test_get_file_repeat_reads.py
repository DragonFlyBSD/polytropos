"""Re-reading a file the model already has costs tokens for nothing (poly-47d).

Measured on hardware 2026-08-29, devel/glib20, one 30-turn attempt::

    2x get_file  .../devel/glib20/overlay.dops
    2x get_file  .../dragonfly/patch-gmodule_gmodule-dl.c
    2x get_file  .../dragonfly/patch-gio_meson.build

Each second read returned bytes already sitting verbatim in the
conversation. The prompt is cumulative, so the content was still there;
returning it again re-bills it as fresh uncached input and spends a turn.

Two things make the elision safe:

* the sha256 is compared, so a file changed by ``put_file`` is returned
  in full rather than withheld;
* the cache is cleared at every attempt start. A retry begins from a
  fresh message list, so carrying this across attempts would refuse the
  model content it genuinely does not have — the opposite of the point.
"""

from __future__ import annotations

import types

import pytest

from dportsv3.agent import worker


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


def _lines(text: str) -> list[str]:
    """Content underneath get_file's cat -n prefix (poly-pg4)."""
    return [ln.split("\t", 1)[1] if "\t" in ln else ln
            for ln in text.splitlines()]


def _write(root, rel, text):
    p = root / rel.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# --- the reported waste -----------------------------------------------------

def test_the_first_read_returns_content(env) -> None:
    _write(env, "work/a.txt", "one\ntwo\nthree\n")
    out = worker.get_file("e", "/work/a.txt")
    assert _lines(out["content"]) == ["one", "two", "three"]
    assert "unchanged" not in out


def test_an_identical_second_read_omits_the_content(env) -> None:
    _write(env, "work/a.txt", "one\ntwo\nthree\n")
    first = worker.get_file("e", "/work/a.txt")
    second = worker.get_file("e", "/work/a.txt")

    assert second["unchanged"] is True
    assert "content" not in second
    assert second["sha256"] == first["sha256"]
    assert "scroll back" in second["note"]


def test_the_metadata_survives_the_elision(env) -> None:
    """The model still needs to know what it is looking at — eliding the
    bytes must not elide the line range, size or hash it reasons with."""
    _write(env, "work/a.txt", "one\ntwo\nthree\n")
    first = worker.get_file("e", "/work/a.txt")
    second = worker.get_file("e", "/work/a.txt")
    for k in ("sha256", "size", "total_lines", "first_line", "last_line"):
        assert second[k] == first[k], f"{k} changed on the elided read"


# --- what must never be elided ----------------------------------------------

def test_a_changed_file_is_returned_in_full(env) -> None:
    """The sha is the whole safety argument. After a put_file the model
    must see the new bytes, not a pointer at the old ones."""
    p = _write(env, "work/a.txt", "one\ntwo\n")
    worker.get_file("e", "/work/a.txt")
    p.write_text("one\ntwo\nthree\n")

    out = worker.get_file("e", "/work/a.txt")
    assert out.get("unchanged") is not True
    assert _lines(out["content"]) == ["one", "two", "three"]


def test_a_different_window_is_always_read(env) -> None:
    """Paging through a file is not a repeat read."""
    _write(env, "work/a.txt", "".join(f"{i}\n" for i in range(100)))
    worker.get_file("e", "/work/a.txt", limit_lines=10)
    out = worker.get_file("e", "/work/a.txt", offset_lines=10, limit_lines=10)
    assert "content" in out
    assert out["first_line"] == 11


def test_a_different_path_is_always_read(env) -> None:
    _write(env, "work/a.txt", "same\n")
    _write(env, "work/b.txt", "same\n")
    worker.get_file("e", "/work/a.txt")
    out = worker.get_file("e", "/work/b.txt")
    assert _lines(out["content"]) == ["same"], (
        "two files with identical content are still two files"
    )


# --- attempt scoping is the correctness condition ---------------------------

def test_a_new_attempt_sees_the_content_again(env) -> None:
    """A retry starts from a fresh message list. Without the reset it
    would be told 'you already have this' about bytes it has never
    been shown — strictly worse than the waste being fixed."""
    _write(env, "work/a.txt", "one\n")
    worker.get_file("e", "/work/a.txt")
    assert worker.get_file("e", "/work/a.txt")["unchanged"] is True

    worker.reset_attempt_caches()

    out = worker.get_file("e", "/work/a.txt")
    assert out.get("unchanged") is not True
    assert _lines(out["content"]) == ["one"]


def test_the_attempt_loop_resets_before_every_attempt() -> None:
    """The reset has to run for attempt 1 too, so a previous job in the
    same runner process cannot leak into this one."""
    import inspect

    from dportsv3.agent import attempt_loop

    src = inspect.getsource(attempt_loop.run)
    loop = src[src.index("for attempt_idx in range"):]
    reset = loop.index("reset_attempt_caches()")
    branch = loop.index("if attempt_idx == 1")
    assert reset < branch, "the reset must precede the per-attempt branch"


def test_the_schema_explains_the_elision() -> None:
    """`unchanged=true` with no content looks like a failure unless the
    model has been told otherwise, and a model that reads it as failure
    will retry — turning a saving into a loop."""
    from dportsv3.agent import tools

    spec = next(s for s in tools.schemas()
                if s["function"]["name"] == "get_file")
    desc = spec["function"]["description"]
    assert "unchanged=true" in desc
    assert "not an error" in desc
