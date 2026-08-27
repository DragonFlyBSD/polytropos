"""The tracker's listen address is a choice, not a literal.

poly-abr.6. The address stays 0.0.0.0 — a deliberate call for a build box
on a trusted network — but it has to be visible and overridable, because
the tracker has no authentication and 24 mutating routes.
"""
from __future__ import annotations

import sys
import types
from argparse import Namespace

import pytest

from dportsv3 import cli
from dportsv3.commands import tracker as tracker_cmd


@pytest.fixture
def fake_uvicorn(monkeypatch):
    """Stand in for uvicorn and record what run() was asked to bind."""
    calls = []
    mod = types.ModuleType("uvicorn")
    mod.__spec__ = types.SimpleNamespace(name="uvicorn")
    mod.run = lambda app, host, port: calls.append({"host": host, "port": port})
    monkeypatch.setitem(sys.modules, "uvicorn", mod)
    monkeypatch.setattr("dportsv3.tracker.server.create_app",
                        lambda db_path: "<app>")
    return calls


def _serve(argv):
    return cli.create_parser().parse_args(["tracker", "serve", *argv])


# --- the flag exists and carries the value through ----------------------

def test_default_is_unchanged(fake_uvicorn, tmp_path) -> None:
    """Adding the flag must not quietly move an operator off the LAN."""
    args = _serve(["--db", str(tmp_path / "state.db")])
    assert args.bind == "0.0.0.0"
    tracker_cmd._cmd_serve(args)
    assert fake_uvicorn == [{"host": "0.0.0.0", "port": 8080}]


def test_bind_reaches_uvicorn(fake_uvicorn, tmp_path) -> None:
    args = _serve(["--bind", "127.0.0.1", "--port", "9001",
                   "--db", str(tmp_path / "state.db")])
    tracker_cmd._cmd_serve(args)
    assert fake_uvicorn == [{"host": "127.0.0.1", "port": 9001}]


def test_a_namespace_without_bind_still_serves(fake_uvicorn, tmp_path) -> None:
    """_cmd_serve is reachable with a hand-built Namespace; an older one
    has no bind attribute and must not crash on it."""
    tracker_cmd._cmd_serve(Namespace(port=8080, db=tmp_path / "state.db"))
    assert fake_uvicorn == [{"host": "0.0.0.0", "port": 8080}]


def test_empty_bind_falls_back(fake_uvicorn, tmp_path) -> None:
    """An unset rc.conf knob arrives as an empty string, not as absent."""
    tracker_cmd._cmd_serve(
        Namespace(port=8080, bind="", db=tmp_path / "state.db"))
    assert fake_uvicorn == [{"host": "0.0.0.0", "port": 8080}]


# --- one service, one bind ----------------------------------------------

def test_the_store_no_longer_has_a_bind_of_its_own() -> None:
    """artifact-store was a second listener on loopback. It is a library
    now, so the tracker's bind is the only one there is, and the ingest
    surface is exposed on exactly the same interface as the UI."""
    from dportsv3 import artifact_store
    assert not hasattr(artifact_store, "DEFAULT_BIND")
    assert not hasattr(artifact_store, "DEFAULT_PORT")
    assert tracker_cmd.DEFAULT_BIND == "0.0.0.0"


def test_help_warns_about_the_exposure() -> None:
    """The operator reading --help is the last place this can be caught."""
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        cli.create_parser().parse_args(["tracker", "serve", "--help"])
    out = buf.getvalue()
    assert "no authentication" in out
