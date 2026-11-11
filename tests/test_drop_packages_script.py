"""The package-drop script, run for real (poly-jao2).

A build that proves a candidate fix, the agent's or verify's, leaves its
package in the env's dsynth repository. After the reset that package no
longer matches the tree, but it has the version dsynth compares, so the
next ``dsynth build`` skips the port. ``DROP_PACKAGES_SH`` removes it.

Every other test fakes the exec, which is how poly-6mpg's probe script
shipped exiting 1 for nearly every port. These run the script through
``/bin/sh`` against a temporary compose tree and repository, with a stub
``make`` that answers ``-V`` from files beside the port.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dports_dev_env.dsynth import DROP_PACKAGES_SH, dropped_packages

_STUB_MAKE = """\
#!/bin/sh
# Answers `make [VAR=value...] -V NAME` from files in the current
# directory: .FLAVORS, and .PKGNAME or .PKGNAME@<flavor>.
flavor=
var=
for a in "$@"; do
    case $a in
        FLAVOR=*) flavor=${a#FLAVOR=} ;;
        -V) ;;
        *=*) ;;
        *) var=$a ;;
    esac
done
case $var in
    FLAVORS) cat .FLAVORS 2>/dev/null; exit 0 ;;
    PKGNAME) cat ".PKGNAME${flavor:+@$flavor}" 2>/dev/null || exit 1 ;;
    *) exit 1 ;;
esac
"""


@pytest.fixture
def tree(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    make = bindir / "make"
    make.write_text(_STUB_MAKE)
    make.chmod(0o755)
    compose = tmp_path / "compose"
    repo = tmp_path / "packages" / "All"
    repo.mkdir(parents=True)

    def port(origin: str, pkgname: str | None, flavors: dict | None = None):
        d = compose / origin
        d.mkdir(parents=True)
        if pkgname is not None:
            (d / ".PKGNAME").write_text(pkgname + "\n")
        for flavor, name in (flavors or {}).items():
            (d / f".PKGNAME@{flavor}").write_text(name + "\n")
        if flavors:
            (d / ".FLAVORS").write_text(" ".join(flavors) + "\n")

    def packages(*names: str):
        for n in names:
            (repo / n).write_bytes(b"pkg")

    def run(*origins: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/sh", "-c", DROP_PACKAGES_SH, "_", str(repo), *origins],
            env=env, capture_output=True, text=True, check=False)

    def left() -> list[str]:
        return sorted(p.name for p in repo.iterdir())

    env = dict(os.environ,
               PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               DPORTS_COMPOSE_ROOT=str(compose))
    return SimpleNamespace(port=port, packages=packages, run=run, left=left,
                           repo=repo, env=env)


def test_the_ports_package_goes_and_the_rest_stay(tree):
    tree.port("net/socketapi", "socketapi-2.2.28")
    tree.packages("socketapi-2.2.28.pkg", "socketapi-2.2.24.pkg",
             "socketapi-2.2.28_1.pkg", "sctplib-1.0.35.pkg")

    p = tree.run("net/socketapi")

    assert p.returncode == 0, p.stderr
    assert dropped_packages(p.stdout) == ["socketapi-2.2.28.pkg"]
    assert "DROPPED=net/socketapi socketapi-2.2.28.pkg" in p.stdout
    assert tree.left() == ["sctplib-1.0.35.pkg", "socketapi-2.2.24.pkg",
                      "socketapi-2.2.28_1.pkg"]


def test_every_flavor_goes(tree):
    tree.port("devel/py-foo", "py311-foo-1.0",
         {"py311": "py311-foo-1.0", "py312": "py312-foo-1.0"})
    tree.packages("py311-foo-1.0.pkg", "py312-foo-1.0.pkg", "py313-foo-1.0.pkg")

    p = tree.run("devel/py-foo")

    assert p.returncode == 0, p.stderr
    assert sorted(dropped_packages(p.stdout)) == ["py311-foo-1.0.pkg",
                                                  "py312-foo-1.0.pkg"]
    assert tree.left() == ["py313-foo-1.0.pkg"]


def test_any_suffix_pkg_has_used_goes(tree):
    """dsynth.ini says .txz; the files a builder holds end in .pkg."""
    tree.port("devel/foo", "foo-1.0")
    tree.packages("foo-1.0.pkg", "foo-1.0.txz", "foo-1.0.tzst")

    p = tree.run("devel/foo")

    assert p.returncode == 0, p.stderr
    assert tree.left() == []


def test_every_origin_is_covered(tree):
    tree.port("multimedia/libx264", "libx264-0.164")
    tree.port("multimedia/x264", "x264-0.164")
    tree.packages("libx264-0.164.pkg", "x264-0.164.pkg")

    p = tree.run("multimedia/libx264", "multimedia/x264")

    assert p.returncode == 0, p.stderr
    assert dropped_packages(p.stdout) == ["libx264-0.164.pkg",
                                          "x264-0.164.pkg"]
    assert tree.left() == []


def test_nothing_to_drop_still_exits_0(tree):
    """No package, an origin missing from the compose tree, a PKGNAME
    make cannot give: none of them is a failure, and the script's status
    is not the last test's (poly-6mpg)."""
    tree.port("devel/foo", "foo-1.0")
    tree.port("devel/broken", None)
    tree.packages("bar-2.0.pkg")

    p = tree.run("devel/foo", "devel/missing", "devel/broken")

    assert p.returncode == 0, p.stderr
    assert dropped_packages(p.stdout) == []
    assert tree.left() == ["bar-2.0.pkg"]


def test_worker_runs_it_with_the_repository_first(tree, monkeypatch):
    """reset_port's copy, through the worker's argv: ``_`` as $0, then the
    repository, then the origins. The repository is swapped for the
    temporary one on the way to the shell."""
    from dportsv3.agent import worker

    tree.port("net/socketapi", "socketapi-2.2.28")
    tree.packages("socketapi-2.2.28.pkg")

    def _local_exec(env, *argv, **kwargs):
        argv = [str(tree.repo) if a == worker.DSYNTH_REPO_DIR else a
                for a in argv]
        return subprocess.run(argv, env=tree.env, capture_output=True,
                              text=True, check=False)

    monkeypatch.setattr(worker, "_exec", _local_exec)

    result = worker._drop_built_packages("test-env", ["net/socketapi"])

    assert result == {"ok": True, "dropped": ["socketapi-2.2.28.pkg"]}
    assert tree.left() == []
