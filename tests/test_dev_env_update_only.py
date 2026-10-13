"""`dev-env update --only` moves only the repos a deploy needs (poly-jwkf).

A deploy has to fast-forward an env's own tool checkout -- the agent's
compose runs from it -- and its ports checkout, which job worktrees are cut
from. A plain `update` also fetches freebsd-ports and DPorts from the
network and fast-forwards the env's freebsd-ports: an upstream bump nobody
asked the deploy for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dports_dev_env import update
from dports_dev_env.errors import UsageError


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An env with all three checkouts, and every git and mirror call recorded."""
    writable = tmp_path / "writable"
    for _, rel in update.ENV_REPOS:
        (writable / rel / ".git").mkdir(parents=True)
    delta_root, tool_root = tmp_path / "DeltaPorts", tmp_path / "polytropos"
    delta_root.mkdir()
    tool_root.mkdir()
    state = SimpleNamespace(source=SimpleNamespace(
        delta_root=str(delta_root), tool_root=str(tool_root)))
    store = SimpleNamespace(load=lambda name: state,
                            writable_dir=lambda name: writable)
    calls = {"mirrors": [], "pulled": []}

    class Cache:
        def __init__(self, config):
            pass

        def refresh_all(self, d, t):
            calls["mirrors"].append("all")

        def refresh_local_mirror(self, name, root):
            calls["mirrors"].append(name)

        def refresh_remote_mirror(self, name, url):
            calls["mirrors"].append(name)

    monkeypatch.setattr(update, "RepoCache", Cache)

    def run(cmd):
        if "pull" in cmd:
            calls["pulled"].append(cmd[2].removeprefix(str(writable) + "/"))
        return "deadbeef" if "rev-parse" in cmd else ""

    monkeypatch.setattr(update, "_run", run)
    config = SimpleNamespace(freebsd_ports_url="https://example/fbsd.git")
    return lambda **kw: (update.update_env(config, store, "e", **kw), calls)[1]


def test_only_the_named_repos_are_refreshed_and_pulled(env):
    calls = env(only=["polytropos", "DeltaPorts"])
    assert sorted(calls["mirrors"]) == ["deltaports", "polytropos"]
    assert calls["pulled"] == ["work/ports-main", "work/polytropos"]


def test_without_only_everything_moves_as_before(env):
    calls = env()
    assert calls["mirrors"] == ["all"]
    assert len(calls["pulled"]) == len(update.ENV_REPOS)


def test_an_unknown_repo_is_refused(env):
    with pytest.raises(UsageError, match="unknown repo DPorts"):
        env(only=["DPorts"])


def test_the_cli_offers_exactly_the_repos_update_knows():
    from dports_dev_env import cli

    parser = cli.build_parser()
    update_cmd = next(a for a in parser._subparsers._group_actions
                      if a.dest).choices["update"]
    only = next(a for a in update_cmd._actions if a.dest == "only")
    assert sorted(only.choices) == sorted(l for l, _ in update.ENV_REPOS)
    args = parser.parse_args(["update", "--only", "polytropos",
                              "--only", "DeltaPorts", "e"])
    assert args.only == ["polytropos", "DeltaPorts"]
    with pytest.raises(SystemExit):
        parser.parse_args(["update", "--only", "nope", "e"])
