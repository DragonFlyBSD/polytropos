"""The bmake oracle expects what THIS build line's ops left behind.

apply_plan built the oracle's expectation from every `mk set` in the plan
that had not failed. Another build line's op is "skipped", not "failed",
so it counted, and in file order the value written last in the file was
expected on every line (poly-lt5q's oracle, 3e08487). A later op that
changes the same variable (`mk add` after `mk set`) leaves no literal to
compare against either.
"""

from __future__ import annotations

import pytest

import dportsv3.engine.apply as apply_mod
from dportsv3.engine.api import apply_dsl
from dportsv3.engine.oracle import OracleResult

HEAD = 'port x/y\ntype port\nreason "r"\n'


@pytest.fixture
def expected(monkeypatch):
    """The ``expect`` mapping apply hands the oracle (bmake stubbed)."""
    seen: dict[str, str] = {}

    def fake(root, *, profile, expect=None, **_):
        seen.update(expect or {})
        return OracleResult(ok=True, profile=profile)

    monkeypatch.setattr(apply_mod, "run_bmake_oracle", fake)
    return seen


def _apply(tmp_path, text, target, makefile="FOO= upstream\n"):
    (tmp_path / "Makefile").write_text(
        makefile + ".include <bsd.port.mk>\n"
    )
    result = apply_dsl(
        HEAD + text, port_root=tmp_path, target=target,
        oracle_profile="local",
    )
    assert result.ok, result.diagnostics
    return (tmp_path / "Makefile").read_text()


@pytest.mark.parametrize(
    "target, want",
    [
        ("@2026Q3", {"FOO": "q3"}),
        ("@main", {"FOO": "main", "BAR": "main-only"}),
    ],
)
def test_the_oracle_expects_what_this_build_line_set(
    tmp_path, expected, target, want
):
    text = (
        'target @main\nmk set FOO "main"\nmk set BAR "main-only"\n'
        'target @2026Q3\nmk set FOO "q3"\n'
    )
    _apply(tmp_path, text, target)
    assert expected == want


def test_the_oracle_expects_the_value_that_ran_last(tmp_path, expected):
    """The @any op is written last but runs first on @main (.11)."""
    text = (
        'target @main\nmk set FOO "main"\n'
        'target @any\nmk set FOO "any"\n'
    )
    makefile = _apply(tmp_path, text, "@main")
    assert "FOO= main" in makefile
    assert expected == {"FOO": "main"}


def test_a_variable_a_later_op_changes_is_not_expected(
    tmp_path, expected
):
    """ports-mgmt/pkg's LDFLAGS shape: mk set, then mk add on it."""
    text = 'mk set LDFLAGS "-L/a"\nmk add LDFLAGS -R/b\nmk set KEEP "k"\n'
    _apply(tmp_path, text, "@main", makefile="LDFLAGS= x\nKEEP= y\n")
    assert expected == {"KEEP": "k"}
