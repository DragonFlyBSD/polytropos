"""Tests for `dportsv3 dev-env apply-and-build` (plan Step 11b
Slice 1).

The command is a substrate primitive — no bundles, no tracker, no
artifact-store. It optionally applies a diff to the env's
DeltaPorts overlay, runs `reapply ORIGIN`, then `dtest ORIGIN`
(dsynth test), capturing combined output to a log file under writable.

We can't run a real DragonFly chroot in CI, so we mock
``ChrootRunner`` + ``EnvironmentSession.prepare`` and assert on the
command surface, return JSON, exit codes, and the diff staging
mechanics.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest




@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch):
    """Mount a fake env layout + monkeypatch every chroot/session hop
    so `cmd_apply_and_build` runs end-to-end against the filesystem
    without needing a real DragonFly env."""
    from dports_dev_env import cli

    env_name = "verify-test"
    env_dir = tmp_path / "envs" / env_name
    writable = env_dir / "writable"
    (writable / "work" / "DeltaPorts").mkdir(parents=True)
    (writable / "work" / "artifacts").mkdir(parents=True)

    monkeypatch.setattr(cli, "require_root", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(cache_root=tmp_path))
    monkeypatch.setattr(cli, "validate_cache_root", lambda _root: None)

    class _Store:
        def __init__(self, _config):
            pass

        def env_dir(self, _name):
            return env_dir

        def writable_dir(self, _name):
            return writable

    monkeypatch.setattr(cli, "EnvironmentStore", _Store)

    state = SimpleNamespace(
        name=env_name,
        root_dir=env_dir / "root",
        target="@main",
        origin=None,
        backend="chroot",
        status="ready",
    )

    class _Session:
        def __init__(self, _config, _store):
            pass

        def prepare(self, _name):
            return state

    monkeypatch.setattr(cli, "EnvironmentSession", _Session)

    # Capture ChrootRunner invocations so tests can inspect what
    # would have run in the chroot.
    calls: list[dict] = []

    class _Runner:
        def __init__(self, root_dir):
            self._root_dir = root_dir
            self.outcomes: list[subprocess.CompletedProcess] = []

        def run(self, argv, env=None, capture_output=False, check=False):
            calls.append({
                "argv": list(argv),
                "capture": capture_output,
                "env_keys": sorted((env or {}).keys()),
            })
            if self.outcomes:
                return self.outcomes.pop(0)
            return subprocess.CompletedProcess(argv, 0, "", "")

        def run_shell(self, script, *args, env=None, check=False,
                      capture_output=False):
            # Same body as the real ChrootRunner.run_shell, so the argv
            # these tests inspect is what the chroot would have seen.
            return self.run(["/bin/sh", "-c", script, "_", *args],
                            env=env, check=check,
                            capture_output=capture_output)

    runner = _Runner(state.root_dir)

    def _make_runner(_root_dir):
        return runner

    # The cmd does `from .chroot import ChrootRunner, chroot_env` and
    # `from .helpers import build_env_dict` inside the function. Patch
    # via the source modules so the local imports pick up the fakes.
    from dports_dev_env import chroot as _chroot_mod
    from dports_dev_env import helpers as _helpers_mod
    monkeypatch.setattr(_chroot_mod, "ChrootRunner", _make_runner)
    monkeypatch.setattr(_chroot_mod, "chroot_env", lambda: {"PATH": "/usr/local/bin:/usr/bin:/bin"})
    monkeypatch.setattr(_helpers_mod, "build_env_dict", lambda _state: {"DPORTS_TARGET": "@main"})

    return SimpleNamespace(
        env_dir=env_dir,
        writable=writable,
        calls=calls,
        runner=runner,
        env_name=env_name,
    )


def _args(name: str, origin: str, **kw) -> argparse.Namespace:
    return argparse.Namespace(
        name=name, origin=origin,
        diff=kw.get("diff"),
        json=kw.get("json", False),
    )


def _capture_stdout(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


def test_no_diff_happy_path_returns_zero(fake_env, monkeypatch) -> None:
    from dports_dev_env.cli import cmd_apply_and_build
    out = _capture_stdout(monkeypatch)

    rc = cmd_apply_and_build(_args(fake_env.env_name, "devel/foo", json=True))

    assert rc == 0
    result = json.loads(out.getvalue())
    assert result["ok"] is True
    assert result["apply_exit"] is None
    assert result["reapply_exit"] == 0
    assert result["dsynth_exit"] == 0
    assert result["applied_diff_sha256"] is None
    assert "apply-and-build-devel_foo.log" in result["log_path"]
    # reapply + dtest, then post-build cleanup (WRKDIR clean +
    # baseline compose). No substrate reset: the worktree is thrown
    # away instead. No package drop: without a diff the build was the
    # baseline's own.
    assert len(fake_env.calls) == 4
    assert result["packages_dropped"] == []
    reapply_script, reapply_args = _shell(fake_env.calls[0]["argv"])
    dtest_script, dtest_args = _shell(fake_env.calls[1]["argv"])
    assert 'reapply "$@"' in reapply_script
    assert reapply_args == ["devel/foo"]
    assert 'dtest "$@"' in dtest_script
    assert dtest_args[-1] == "devel/foo"
    dtest_argv = " ".join(fake_env.calls[1]["argv"])
    # The verify build suppresses the dsynth failure hooks (sentinel
    # file + trap) so a failed verify doesn't upload a new bundle and
    # re-trigger triage for an origin the loop is already handling.
    assert ".dports-agent-hooks-disabled" in dtest_argv
    assert "trap" in dtest_argv and "EXIT" in dtest_argv


def test_diff_path_is_staged_into_writable_and_applied(fake_env, monkeypatch, tmp_path) -> None:
    from dports_dev_env.cli import cmd_apply_and_build
    _capture_stdout(monkeypatch)

    diff = tmp_path / "fix.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")

    rc = cmd_apply_and_build(_args(fake_env.env_name, "devel/foo",
                                   diff=str(diff), json=True))

    assert rc == 0
    # dirty check, apply, reapply, dtest, then post-build cleanup
    # (WRKDIR wipe, package drop, baseline compose).
    assert len(fake_env.calls) == 7
    assert "git status --porcelain" in " ".join(fake_env.calls[0]["argv"])
    apply_argv = " ".join(fake_env.calls[1]["argv"])
    assert "git apply --3way" in apply_argv
    assert "/work/.apply-and-build.diff" in apply_argv
    # Staged file should have been cleaned up after the apply.
    assert not (fake_env.writable / "work" / ".apply-and-build.diff").exists()


def test_diff_mode_refuses_dirty_port_tree(fake_env, monkeypatch, tmp_path) -> None:
    """Diff replay must refuse when ports/<origin>/ is dirty —
    applying onto stale state would make the verdict meaningless. The
    refusal happens BEFORE any chroot work, so no apply/reapply/dtest
    AND no cleanup runs (cleanup would reset the operator's state)."""
    from dports_dev_env import cli
    from dports_dev_env.cli import cmd_apply_and_build
    out = _capture_stdout(monkeypatch)

    monkeypatch.setattr(
        cli, "_port_dirty_paths",
        lambda root_dir, origin: [" M ports/devel/foo/Makefile"],
    )
    diff = tmp_path / "fix.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")

    rc = cmd_apply_and_build(_args(fake_env.env_name, "devel/foo",
                                   diff=str(diff), json=True))

    assert rc == 1
    result = json.loads(out.getvalue())
    assert result["apply_exit"] == 1
    assert result["ok"] is False
    assert result["dirty_paths"] == [" M ports/devel/foo/Makefile"]
    # Refused before touching the chroot: no apply, no cleanup.
    all_argv = [" ".join(c["argv"]) for c in fake_env.calls]
    assert not any("git apply --3way" in a for a in all_argv)
    assert not any("git reset" in a for a in all_argv)


def test_diff_apply_failure_short_circuits(fake_env, monkeypatch, tmp_path) -> None:
    from dports_dev_env.cli import cmd_apply_and_build
    out = _capture_stdout(monkeypatch)

    # The pre-replay dirty check runs first (in-chroot since B1), then the
    # git apply returns non-zero with an error.
    fake_env.runner.outcomes = [
        subprocess.CompletedProcess([], 0, "", ""),   # dirty check → clean
        subprocess.CompletedProcess([], 1, "", "error: patch failed"),
    ]
    diff = tmp_path / "bad.diff"
    diff.write_bytes(b"not actually a diff\n")

    rc = cmd_apply_and_build(_args(fake_env.env_name, "devel/foo",
                                   diff=str(diff), json=True))

    assert rc == 1
    result = json.loads(out.getvalue())
    assert result["apply_exit"] == 1
    assert result["reapply_exit"] is None
    assert result["dsynth_exit"] is None
    assert result["ok"] is False
    # apply failed → build-stage reapply + dtest skipped; cleanup
    # still runs in the finally.
    all_argv = [" ".join(c["argv"]) for c in fake_env.calls]
    assert any("git apply --3way" in a for a in all_argv)
    assert not any('reapply "$@"' in a for a in all_argv)
    baseline = [
        c for c in _post_build_calls(fake_env.calls)
        if "compose" in _shell(c)[0] and _shell(c)[1] == ["devel/foo"]
    ]
    assert len(baseline) == 1  # only the cleanup-stage baseline compose
    assert not any("dtest devel/foo" in a for a in all_argv)
    # sha256 still recorded so the orchestrator can dedupe.
    assert result["applied_diff_sha256"] is not None


def test_reapply_failure_short_circuits_dsynth(fake_env, monkeypatch) -> None:
    from dports_dev_env.cli import cmd_apply_and_build
    out = _capture_stdout(monkeypatch)

    # Reapply (first call here, no --diff) returns non-zero.
    fake_env.runner.outcomes = [
        subprocess.CompletedProcess([], 2, "", "compose failed"),
    ]
    rc = cmd_apply_and_build(_args(fake_env.env_name, "devel/foo", json=True))

    # CLI exit is binary (0 if ok, 1 otherwise) so shell pipelines
    # behave consistently. The per-stage exit code lives in the
    # JSON for callers who want it.
    assert rc == 1
    result = json.loads(out.getvalue())
    assert result["reapply_exit"] == 2
    assert result["dsynth_exit"] is None
    assert result["ok"] is False
    # reapply failed → dtest skipped (cleanup still runs in finally).
    all_argv = [" ".join(c["argv"]) for c in fake_env.calls]
    assert any('reapply "$@"' in a for a in all_argv)
    assert not any("dtest devel/foo" in a for a in all_argv)


def test_dsynth_failure_reports_ok_false_but_returns_one(fake_env, monkeypatch) -> None:
    from dports_dev_env.cli import cmd_apply_and_build
    out = _capture_stdout(monkeypatch)

    # reapply ok, dtest fails with 1.
    fake_env.runner.outcomes = [
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 1, "", ""),
    ]
    rc = cmd_apply_and_build(_args(fake_env.env_name, "devel/foo", json=True))

    assert rc == 1
    result = json.loads(out.getvalue())
    assert result["reapply_exit"] == 0
    assert result["dsynth_exit"] == 1
    assert result["ok"] is False
    assert result["log_path"] is not None


def test_missing_diff_raises_usage_error(fake_env, monkeypatch) -> None:
    from dports_dev_env.cli import cmd_apply_and_build
    from dports_dev_env.errors import UsageError
    _capture_stdout(monkeypatch)

    with pytest.raises(UsageError, match="--diff: file not found"):
        cmd_apply_and_build(_args(fake_env.env_name, "devel/foo",
                                  diff="/nonexistent/path.diff",
                                  json=True))


def test_non_json_output_is_one_line_summary(fake_env, monkeypatch) -> None:
    from dports_dev_env.cli import cmd_apply_and_build
    out = _capture_stdout(monkeypatch)

    rc = cmd_apply_and_build(_args(fake_env.env_name, "devel/foo", json=False))

    assert rc == 0
    text = out.getvalue().strip()
    assert "\n" not in text  # one line
    assert "ok=True" in text
    assert "dsynth=0" in text
    assert "reapply=0" in text
    assert "log=" in text


# --- Q1 follow-up: post-build cleanup wipes substrate + WRKDIR -------------


def _shell(argv: list[str]) -> tuple[str, list[str]]:
    """Split a recorded ``/bin/sh -c SCRIPT _ ARGS...`` argv."""
    return argv[2], list(argv[4:])


def _post_build_calls(calls: list[dict]) -> list[list[str]]:
    """Pick out the runner.run invocations that look like the
    post-build cleanup shell commands. Both stages route through
    ``/bin/sh -c <cmd> _``."""
    out: list[list[str]] = []
    for c in calls:
        argv = c["argv"]
        if (
            len(argv) >= 3
            and argv[0] == "/bin/sh"
            and argv[1] == "-c"
        ):
            out.append(argv)
    return out


def test_diff_path_runs_post_build_cleanup(
    fake_env, monkeypatch, tmp_path,
) -> None:
    """Post-build cleanup runs for the diff path too. Pre-slice-5
    the diff path was exempted, but ``--diff`` is now the only
    verify path, so skipping cleanup left ports/<origin>/ + the
    WRKDIR dirty after every verify (and blocked the runner's
    verify-branch drop, which needs a clean tree to switch off
    the throwaway branch)."""
    from dports_dev_env.cli import apply_and_build

    diff = tmp_path / "fix.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")
    apply_and_build(fake_env.env_name, "devel/foo", diff_path=str(diff))

    shell_calls = _post_build_calls(fake_env.calls)
    # The WRKDIR lives outside the job's worktree, so it still needs wiping.
    assert any(
        "WRKDIRPREFIX=/work/obj" in c[2] and "clean" in c[2]
        for c in shell_calls
    )
    # The source checkout does not: it goes with the worktree.
    assert not any("git reset" in c[2] for c in shell_calls)


def test_cleanup_baseline_compose_failure_does_not_flip_ok(
    fake_env, monkeypatch,
) -> None:
    """Baseline-compose failure is informational: warn in
    ``stderr_tail``, don't flip ``ok``."""
    from dports_dev_env.cli import apply_and_build
    import subprocess as _sp

    # No-diff path: reapply (build), dtest, WRKDIR wipe, baseline
    # compose. Fail the last one.
    fake_env.runner.outcomes = [
        _sp.CompletedProcess([], 0, "", ""),     # reapply (build)
        _sp.CompletedProcess([], 0, "", ""),     # dtest
        _sp.CompletedProcess([], 0, "", ""),     # WRKDIR wipe
        _sp.CompletedProcess(                    # baseline compose → fails
            [], 2, "", "compose: E_COMPOSE_APPLY_FAILED on ports/devel/foo",
        ),
    ]

    result = apply_and_build(fake_env.env_name, "devel/foo")

    # Build phases succeeded → ok stays True.
    assert result["ok"] is True
    assert result["dsynth_exit"] == 0
    # Warning names the origin and carries the compose stderr.
    tail = result.get("stderr_tail") or ""
    assert "post-build baseline compose failed for devel/foo" in tail
    assert "E_COMPOSE_APPLY_FAILED" in tail


def test_cleanup_composes_from_baseline_not_the_worktree(
    fake_env, tmp_path,
) -> None:
    """Cleanup composes from the baseline checkout, not through
    ``reapply`` — which would compose the job's own worktree into the
    shared tree (poly-mdf7)."""
    from dports_dev_env.cli import apply_and_build

    diff = tmp_path / "fix.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")
    apply_and_build(fake_env.env_name, "devel/foo", diff_path=str(diff))

    calls = _post_build_calls(fake_env.calls)
    baseline = [c for c in calls if "compose" in _shell(c)[0]]
    assert len(baseline) == 1
    script, args = _shell(baseline[0])
    assert args == ["devel/foo"]
    assert "--delta-root /work/ports-main" in script
    assert "--delta-root /work/DeltaPorts" not in script
    # Cleanup bypasses reapply; the build stage still uses it.
    assert "reapply" not in script
    build_stage = [c for c in calls if 'reapply "$@"' in _shell(c)[0]]
    assert len(build_stage) == 1
    assert "cd /work/DeltaPorts" in _shell(build_stage[0])[0]


def test_cleanup_continues_past_a_sibling_that_cannot_compose(
    fake_env, tmp_path,
) -> None:
    """One origin failing must not skip the others."""
    from dports_dev_env.cli import apply_and_build
    import subprocess as _sp

    diff = tmp_path / "fix.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")

    # Fail the first of the two cleanup composes.
    fake_env.runner.outcomes = [
        _sp.CompletedProcess([], 0, "", ""),   # dirty check → clean
        _sp.CompletedProcess([], 0, "", ""),   # dirty check (sibling)
        _sp.CompletedProcess([], 0, "", ""),   # git apply
        _sp.CompletedProcess([], 0, "", ""),   # reapply (build)
        _sp.CompletedProcess([], 0, "", ""),   # dtest
        _sp.CompletedProcess([], 0, "", ""),   # WRKDIR wipe
        _sp.CompletedProcess([], 0, "", ""),   # package drop
        _sp.CompletedProcess(                  # baseline compose: master fails
            [], 2, "", "E_COMPOSE_SELECTED_ORIGIN_NOT_FOUND",
        ),
        _sp.CompletedProcess([], 0, "", ""),   # baseline compose: sibling runs
    ]

    result = apply_and_build(
        fake_env.env_name, "devel/foo",
        diff_path=str(diff), also=["devel/foo-slave"],
    )

    composed = [
        _shell(c)[1][0] for c in _post_build_calls(fake_env.calls)
        if "compose" in _shell(c)[0]
    ]
    # Both origins composed; the first failing did not skip the second.
    assert composed == ["devel/foo", "devel/foo-slave"]
    # Only the failing origin is named in the warning.
    tail = result.get("stderr_tail") or ""
    assert "post-build baseline compose failed for devel/foo:" in tail
    assert "devel/foo-slave:" not in tail


# --- the package drop (poly-jao2) ------------------------------------------


def test_cleanup_drops_the_built_packages_before_the_compose(
    fake_env, tmp_path,
) -> None:
    """dtest leaves the packages it built from the diff in the env's
    repository; dsynth would take them for the baseline's and skip the
    port. They go before the compose, which replaces the tree their names
    are read from."""
    from dports_dev_env.cli import apply_and_build
    from dports_dev_env.dsynth import DROP_PACKAGES_SH, DSYNTH_REPO_DIR
    import subprocess as _sp

    diff = tmp_path / "fix.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")
    fake_env.runner.outcomes = [
        _sp.CompletedProcess([], 0, "", ""),   # dirty check
        _sp.CompletedProcess([], 0, "", ""),   # dirty check (sibling)
        _sp.CompletedProcess([], 0, "", ""),   # git apply
        _sp.CompletedProcess([], 0, "", ""),   # reapply (build)
        _sp.CompletedProcess([], 0, "", ""),   # dtest
        _sp.CompletedProcess([], 0, "", ""),   # WRKDIR wipe
        _sp.CompletedProcess(                  # package drop
            [], 0, "DROPPED=devel/foo foo-1.0.pkg\n"
                   "DROPPED=devel/foo-slave foo-slave-1.0.pkg\n", ""),
    ]

    result = apply_and_build(
        fake_env.env_name, "devel/foo",
        diff_path=str(diff), also=["devel/foo-slave"],
    )

    calls = _post_build_calls(fake_env.calls)
    drops = [i for i, c in enumerate(calls) if _shell(c)[0] == DROP_PACKAGES_SH]
    composes = [i for i, c in enumerate(calls)
                if "--delta-root /work/ports-main" in _shell(c)[0]]
    assert len(drops) == 1
    assert drops[0] < min(composes)
    assert _shell(calls[drops[0]])[1] == [DSYNTH_REPO_DIR,
                                          "devel/foo", "devel/foo-slave"]
    assert result["packages_dropped"] == ["foo-1.0.pkg", "foo-slave-1.0.pkg"]
    assert result["ok"] is True


def _diff(tmp_path):
    diff = tmp_path / "fix.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")
    return str(diff)


def _drops(calls):
    from dports_dev_env.dsynth import DROP_PACKAGES_SH
    return [c for c in _post_build_calls(calls)
            if _shell(c)[0] == DROP_PACKAGES_SH]


def test_a_failed_drop_warns_and_does_not_flip_ok(fake_env, tmp_path) -> None:
    from dports_dev_env.cli import apply_and_build
    import subprocess as _sp

    fake_env.runner.outcomes = [
        _sp.CompletedProcess([], 0, "", ""),     # dirty check
        _sp.CompletedProcess([], 0, "", ""),     # git apply
        _sp.CompletedProcess([], 0, "", ""),     # reapply (build)
        _sp.CompletedProcess([], 0, "", ""),     # dtest
        _sp.CompletedProcess([], 0, "", ""),     # WRKDIR wipe
        _sp.CompletedProcess([], 1, "", "rm: Permission denied"),  # drop
    ]

    result = apply_and_build(fake_env.env_name, "devel/foo",
                             diff_path=_diff(tmp_path))

    assert result["ok"] is True
    assert result["packages_dropped"] == []
    tail = result.get("stderr_tail") or ""
    assert "post-build package drop failed: rc=1" in tail
    assert "rm: Permission denied" in tail


def test_no_drop_when_the_diff_does_not_apply(fake_env, tmp_path) -> None:
    """Nothing was built from the diff, so the repository holds only
    baseline packages — for a master, the whole sibling set's."""
    from dports_dev_env.cli import apply_and_build
    import subprocess as _sp

    fake_env.runner.outcomes = [
        _sp.CompletedProcess([], 0, "", ""),                 # dirty check
        _sp.CompletedProcess([], 1, "", "patch does not apply"),  # git apply
    ]

    result = apply_and_build(fake_env.env_name, "devel/foo",
                             diff_path=_diff(tmp_path))

    assert result["apply_exit"] == 1
    assert _drops(fake_env.calls) == []
    assert result["packages_dropped"] == []


def test_no_drop_when_reapply_fails(fake_env, tmp_path) -> None:
    from dports_dev_env.cli import apply_and_build
    import subprocess as _sp

    fake_env.runner.outcomes = [
        _sp.CompletedProcess([], 0, "", ""),                # dirty check
        _sp.CompletedProcess([], 0, "", ""),                # git apply
        _sp.CompletedProcess([], 2, "", "compose failed"),  # reapply
    ]

    result = apply_and_build(fake_env.env_name, "devel/foo",
                             diff_path=_diff(tmp_path))

    assert result["reapply_exit"] == 2
    assert result["dsynth_exit"] is None
    assert _drops(fake_env.calls) == []


def test_a_failed_dtest_still_drops(fake_env, tmp_path) -> None:
    """dtest over a sibling set can fail on one port and still have
    built the others from the diff."""
    from dports_dev_env.cli import apply_and_build
    import subprocess as _sp

    fake_env.runner.outcomes = [
        _sp.CompletedProcess([], 0, "", ""),     # dirty check
        _sp.CompletedProcess([], 0, "", ""),     # git apply
        _sp.CompletedProcess([], 0, "", ""),     # reapply (build)
        _sp.CompletedProcess([], 1, "", ""),     # dtest fails
    ]

    result = apply_and_build(fake_env.env_name, "devel/foo",
                             diff_path=_diff(tmp_path))

    assert result["ok"] is False
    assert len(_drops(fake_env.calls)) == 1
