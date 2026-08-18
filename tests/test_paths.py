"""The input resolver (dportsv3.paths).

Pins the two properties X1 was about:

- there is one documented order per input, and it is honoured;
- a missing input raises and names where it looked, rather than returning
  None and letting the caller proceed without it.

The second is the one that matters. Every path this module replaced walked
a fixed number of parent directories up out of the package, which resolved
correctly only while the tool lived inside the DeltaPorts checkout — and
several of them degraded silently rather than failing when it stopped doing
so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3 import paths


# --- package data ----------------------------------------------------------


def test_package_data_lives_inside_the_package():
    """Not an incidental truth — it is what makes a wheel install work."""
    pkg = Path(paths.__file__).resolve().parent
    assert paths.AGENT_PLAYBOOKS_DIR.is_relative_to(pkg)
    assert paths.BUNDLED_CONFIG_DIR.is_relative_to(pkg)


def test_packaged_data_is_actually_present():
    assert (paths.AGENT_PLAYBOOKS_DIR / "README.md").is_file()
    assert list(paths.AGENT_PLAYBOOKS_DIR.glob("*.md"))
    assert (paths.BUNDLED_CONFIG_DIR / "agentic-policy.json.sample").is_file()


def test_require_dir_raises_and_names_the_path():
    with pytest.raises(paths.MissingInput, match="nowhere"):
        paths.require_dir(Path("/nonexistent/nowhere"), "the widget directory")


# --- config resolution -----------------------------------------------------


def test_config_falls_back_to_the_bundled_sample(monkeypatch):
    monkeypatch.delenv("DPORTSV3_CONFIG_DIR", raising=False)
    found = paths.config_file("agentic-policy.json")
    assert found == paths.BUNDLED_CONFIG_DIR / "agentic-policy.json.sample"


def test_operator_copy_outranks_the_bundled_sample(tmp_path, monkeypatch):
    live = tmp_path / "agentic-policy.json"
    live.write_text("{}")
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(tmp_path))
    assert paths.config_file("agentic-policy.json") == live


def test_sample_in_the_config_dir_outranks_the_bundled_one(tmp_path, monkeypatch):
    """The tracked/local split survives: an operator with only a .sample in
    their config dir gets theirs, not the packaged copy."""
    sample = tmp_path / "agentic-policy.json.sample"
    sample.write_text("{}")
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(tmp_path))
    assert paths.config_file("agentic-policy.json") == sample


def test_unknown_config_is_none_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(tmp_path))
    assert paths.config_file("no-such-thing.toml") is None


def test_require_config_file_lists_every_place_it_looked(tmp_path, monkeypatch):
    """The error has to be actionable: an operator who sees it should not
    have to read the source to find out where to put the file."""
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(tmp_path))
    with pytest.raises(paths.MissingInput) as excinfo:
        paths.require_config_file("no-such-thing.toml")
    message = str(excinfo.value)
    assert str(tmp_path / "no-such-thing.toml") in message
    assert str(paths.BUNDLED_CONFIG_DIR / "no-such-thing.toml.sample") in message
    assert "DPORTSV3_CONFIG_DIR" in message


# --- delta root ------------------------------------------------------------


def _make_checkout(root: Path, *subdirs: str) -> Path:
    for name in subdirs:
        (root / name).mkdir(parents=True)
    return root


def test_explicit_delta_root_wins_over_env(tmp_path, monkeypatch):
    explicit = _make_checkout(tmp_path / "explicit", "ports")
    from_env = _make_checkout(tmp_path / "from-env", "ports")
    monkeypatch.setenv("DPORTS_DELTA_ROOT", str(from_env))
    assert paths.resolve_delta_root(explicit) == explicit


def test_env_delta_root_wins_over_cwd(tmp_path, monkeypatch):
    from_env = _make_checkout(tmp_path / "from-env", "ports")
    cwd = _make_checkout(tmp_path / "cwd", "ports")
    monkeypatch.setenv("DPORTS_DELTA_ROOT", str(from_env))
    monkeypatch.chdir(cwd)
    assert paths.resolve_delta_root() == from_env


def test_cwd_is_the_last_resort(tmp_path, monkeypatch):
    cwd = _make_checkout(tmp_path / "cwd", "ports")
    monkeypatch.delenv("DPORTS_DELTA_ROOT", raising=False)
    monkeypatch.chdir(cwd)
    assert paths.resolve_delta_root() == cwd


@pytest.mark.parametrize("subdir", ["ports", "special"])
def test_either_ports_or_special_is_enough(tmp_path, subdir):
    """Composing only `special/` is supported, so requiring `ports/` would
    reject valid roots."""
    root = _make_checkout(tmp_path / subdir.upper(), subdir)
    assert paths.resolve_delta_root(root) == root


def test_a_directory_that_is_not_a_checkout_raises(tmp_path):
    """The whole point. Running from the tool's own repo used to compose
    against a tree with no ports in it and report success over an empty
    result."""
    not_a_checkout = tmp_path / "polytropos"
    (not_a_checkout / "dportsv3").mkdir(parents=True)
    with pytest.raises(paths.MissingInput, match="does not look like a DeltaPorts checkout"):
        paths.resolve_delta_root(not_a_checkout)


def test_the_error_says_where_the_bad_value_came_from(tmp_path, monkeypatch):
    """Three sources feed this, and the fix differs by source — so the
    message names which one was used."""
    monkeypatch.delenv("DPORTS_DELTA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(paths.MissingInput, match="the current directory"):
        paths.resolve_delta_root()

    monkeypatch.setenv("DPORTS_DELTA_ROOT", str(tmp_path))
    with pytest.raises(paths.MissingInput, match=r"\$DPORTS_DELTA_ROOT"):
        paths.resolve_delta_root()

    with pytest.raises(paths.MissingInput, match="--delta-root"):
        paths.resolve_delta_root(tmp_path)
