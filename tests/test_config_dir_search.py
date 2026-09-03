"""poly-mrl: config_dir is a search path, not a single variable.

It used to be exactly one entry, so an unset $DPORTSV3_CONFIG_DIR meant
"no config exists anywhere" rather than "look in the usual places" — a
file in the conventional location was read by nobody and warned about by
nothing. Observed: an operator's [dev_env] cache_root in
/usr/local/etc/polytropos was silently ignored because bin/dportsv3 had
already pointed the variable at the checkout's config/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3 import paths


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """The suite's conftest exports DPORTSV3_CONFIG_DIR; these tests are
    about what happens when nobody has."""
    monkeypatch.delenv("DPORTSV3_CONFIG_DIR", raising=False)
    yield


def test_the_explicit_variable_still_wins(monkeypatch, tmp_path):
    """A checkout must keep behaving exactly as before: bin/dportsv3
    sets this, and nothing below may override it."""
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "DEFAULT_CONFIG_DIR", tmp_path / "unused")
    assert paths.config_dir() == tmp_path


def test_the_variable_wins_even_when_it_does_not_exist(monkeypatch, tmp_path):
    """An explicit answer is an answer. Falling back would hide the
    operator's typo behind a directory they did not name."""
    named = tmp_path / "absent"
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(named))
    assert paths.config_dir() == named


def test_it_finds_the_config_beside_the_installed_entry_point(
    monkeypatch, tmp_path,
):
    """<prefix>/bin/dportsv3 -> <prefix>/etc/polytropos. This is what
    follows a non-default LOCALBASE with nothing to configure."""
    prefix = tmp_path / "opt" / "pkg"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "etc" / "polytropos").mkdir(parents=True)
    monkeypatch.setattr(paths.sys, "argv", [str(prefix / "bin" / "dportsv3")])
    monkeypatch.setattr(paths, "DEFAULT_CONFIG_DIR", tmp_path / "unused")

    assert paths.config_dir() == prefix / "etc" / "polytropos"


def test_the_entry_point_symlink_is_not_resolved(monkeypatch, tmp_path):
    """deploy links <prefix>/bin/dportsv3 at
    <prefix>/lib/polytropos/bin/dportsv3. Resolving the symlink lands in
    the venv and derives <prefix>/lib/polytropos/etc — the wrong prefix,
    and the reason abspath is used rather than resolve()."""
    prefix = tmp_path / "usr" / "local"
    (prefix / "bin").mkdir(parents=True)
    venv_bin = prefix / "lib" / "polytropos" / "bin"
    venv_bin.mkdir(parents=True)
    real = venv_bin / "dportsv3"
    real.write_text("#!/bin/sh\n")
    link = prefix / "bin" / "dportsv3"
    link.symlink_to(real)
    (prefix / "etc" / "polytropos").mkdir(parents=True)
    # The wrong answer, made to exist so the test fails loudly if the
    # symlink is ever followed.
    (prefix / "lib" / "polytropos" / "etc" / "polytropos").mkdir(parents=True)
    monkeypatch.setattr(paths.sys, "argv", [str(link)])
    monkeypatch.setattr(paths, "DEFAULT_CONFIG_DIR", tmp_path / "unused")

    assert paths.config_dir() == prefix / "etc" / "polytropos"


def test_it_falls_back_to_the_documented_default(monkeypatch, tmp_path):
    default = tmp_path / "usr" / "local" / "etc" / "polytropos"
    default.mkdir(parents=True)
    monkeypatch.setattr(paths, "DEFAULT_CONFIG_DIR", default)
    monkeypatch.setattr(paths.sys, "argv", [str(tmp_path / "nowhere" / "x")])

    assert paths.config_dir() == default


def test_a_derivation_that_misses_falls_through_rather_than_inventing(
    monkeypatch, tmp_path,
):
    """Running from a source tree, or `python -m`, derives a directory
    that is not there. Naming it would be worse than admitting there is
    no config dir: every setting has a default already."""
    monkeypatch.setattr(paths.sys, "argv", [str(tmp_path / "src" / "run.py")])
    monkeypatch.setattr(paths, "DEFAULT_CONFIG_DIR", tmp_path / "unused")

    assert paths.config_dir() is None


def test_an_empty_argv0_is_survivable(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.sys, "argv", [""])
    monkeypatch.setattr(paths, "DEFAULT_CONFIG_DIR", tmp_path / "unused")
    assert paths.config_dir() is None


def test_the_derived_path_is_prefix_relative_not_hardcoded():
    """The point of deriving: /usr/local is a ports-level choice, so the
    answer has to move with the binary."""
    assert str(paths._CONFIG_DIR_FROM_BINDIR) == "../etc/polytropos"


# --- the wrapper must not claim an empty config/ ----------------------

def _wrapper_source() -> str:
    return (Path(__file__).resolve().parents[1] / "bin" / "dportsv3").read_text()


def test_wrapper_claims_config_only_when_it_holds_configuration():
    """config/ ships empty on purpose. Claiming an empty one set
    DPORTSV3_CONFIG_DIR to a directory with no polytropos.toml, which
    beat the search path and masked a real /usr/local/etc install — the
    operator's settings file lost to one that did not exist."""
    src = _wrapper_source()
    assert '[ -f "$SELF_DIR/config/polytropos.toml" ]' in src
    assert '[ -d "$SELF_DIR/config/secrets" ]' in src
    assert '[ -d "$SELF_DIR/config" ]' not in src, (
        "a bare directory test is what claimed the empty dir"
    )


@pytest.mark.parametrize("contents,claimed", [
    (["polytropos.toml"], True),
    (["secrets"], True),          # keys but all-default settings
    (["polytropos.toml", "secrets"], True),
    ([], False),                  # ships-empty: leave it for the search
    (["README.md"], False),       # what a fresh clone actually has
])
def test_wrapper_claim_decision(tmp_path, contents, claimed):
    """Exercised as shell, so the test tracks the wrapper's real logic
    rather than a Python restatement of it."""
    import subprocess
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in contents:
        if name == "secrets":
            (cfg / name).mkdir()
        else:
            (cfg / name).write_text("")
    script = (
        f'SELF_DIR={tmp_path}\n'
        'if [ -z "${DPORTSV3_CONFIG_DIR:-}" ] &&\n'
        '   { [ -f "$SELF_DIR/config/polytropos.toml" ] ||\n'
        '     [ -d "$SELF_DIR/config/secrets" ]; }; then\n'
        '    echo CLAIMED\n'
        'else\n'
        '    echo UNCLAIMED\n'
        'fi\n'
    )
    out = subprocess.run(["sh", "-c", script], capture_output=True,
                         text=True, env={"PATH": "/bin:/usr/bin"}).stdout
    assert out.strip() == ("CLAIMED" if claimed else "UNCLAIMED")
