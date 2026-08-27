"""Making the hooks' tools reachable from inside a dev-env chroot.

A dsynth failure inside an env used to reach nothing. The hooks shell out
to `dportsv3` and `artifact-store-client`, both of which live in a venv on
the host; the chroot mounted no path that led to either, and the conf
`hooks-install` wrote still named `/build/synth/polytropos/...`, which
exists on no machine. Two separate holes had to close for that to work,
and a third for the record to land where anyone would look for it.

  * the venv has to be *there* — `prepare_root_runtime` bind-mounts it
  * the scripts have to *run* — their shebangs point at the venv's path
    on the host, so the interpreter is named explicitly instead
  * the failure has to be filed against the env's target, not the one
    the profile name implies
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dports_dev_env import hooks

_HOOKS = hooks.repo_hook_source()


def _sh(tmp_path, body: str) -> subprocess.CompletedProcess:
    """Source hook_common.sh and run `body` against it."""
    script = f'. "{_HOOKS / "hook_common.sh"}"\n{body}\n'
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "DIR_LOGS": str(tmp_path / "logs"),
             "DPORTSV3_HOOKS_CONFIG": str(tmp_path / "absent.conf")},
    )


def _state(tmp_path, target="2026Q3", name="2026Q3-editors_vim"):
    from dports_dev_env.state import EnvironmentState
    return EnvironmentState(
        schema=1, name=name, backend="chroot", target=target,
        origin="editors/vim", status="ready",
        created_at="2026-08-27T00:00:00Z", updated_at="2026-08-27T00:00:00Z",
        root_dir=tmp_path / "root", writable_dir=tmp_path / "writable",
        provisioned_base_id="base", repos=None, source=None, runtime=None,
    )


# --- where the tools are ----------------------------------------------------

def test_the_tool_paths_are_inside_the_chroot(tmp_path):
    """Not host paths. The env mounts the venv at one known place and
    every value has to agree with it."""
    settings = hooks.env_hook_settings(_state(tmp_path))
    from dports_dev_env.runtime import TOOL_VENV_TARGET

    for key in ("POLYTROPOS_PYTHON", "DPORTSV3_BIN", "ARTIFACT_STORE_CLIENT"):
        assert settings[key].startswith(f"/{TOOL_VENV_TARGET}/"), settings[key]


def test_the_interpreter_is_named_explicitly(tmp_path):
    """Both tools are venv console scripts, so their shebangs are absolute
    paths into the venv as it sits on the *host*. Inside the chroot that
    path does not exist and exec fails with ENOENT before python is ever
    reached — which reads, from dsynth, as a hook that did nothing."""
    settings = hooks.env_hook_settings(_state(tmp_path))
    assert settings["POLYTROPOS_PYTHON"].endswith("/bin/python")


def test_the_mount_puts_the_venv_where_the_conf_says(monkeypatch, tmp_path):
    from dports_dev_env import runtime
    from dports_dev_env.config import load_config

    monkeypatch.setenv("DPORTS_DEV_CACHE_ROOT", str(tmp_path / "cache"))
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/local/bin\n")
    monkeypatch.setenv("DPORTS_DEV_TOOL_VENV", str(venv))

    # The 79-char statfs limit has its own test; a tmp_path on macOS is
    # already past it before any of this gets a say.
    monkeypatch.setattr(runtime, "check_mount_target_length", lambda t: None)
    mounted: list[tuple[Path, Path, bool]] = []
    monkeypatch.setattr(runtime, "mount_null",
                        lambda s, t, read_only=False: mounted.append(
                            (s, t, read_only)) or True)
    monkeypatch.setattr(runtime, "mount_procfs", lambda t: True)

    root = tmp_path / "root"
    root.mkdir()
    runtime.prepare_root_runtime(load_config(), root)

    target = root / hooks.CHROOT_VENV.relative_to("/")
    assert (venv, target, True) in mounted, mounted


def test_a_missing_venv_is_a_warning_not_a_crash(monkeypatch, tmp_path):
    """Envs get used for things other than agentic builds. Not finding a
    venv to mount should cost the hooks their tracker, not the operator
    their shell."""
    from dports_dev_env import runtime
    from dports_dev_env.config import load_config

    monkeypatch.setenv("DPORTS_DEV_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("DPORTS_DEV_TOOL_VENV", str(tmp_path / "absent"))
    monkeypatch.setattr(runtime, "check_mount_target_length", lambda t: None)
    monkeypatch.setattr(runtime, "mount_null", lambda *a, **k: True)
    monkeypatch.setattr(runtime, "mount_procfs", lambda t: True)

    warned: list[str] = []
    monkeypatch.setattr(runtime, "warn", warned.append)

    root = tmp_path / "root"
    root.mkdir()
    runtime.prepare_root_runtime(load_config(), root)

    assert any("no venv" in w for w in warned), warned


# --- which target the failure lands on --------------------------------------

def test_the_target_comes_from_the_env_not_the_profile_name(tmp_path):
    """`DPORTSV3_TRACKER_TARGET` defaults to `@${PROFILE}`, and a dev-env
    names its dsynth profile after the env: `2026Q3-editors_vim`. Issue
    identity hashes the target, so leaving the default in place files every
    in-env failure against `@2026Q3-editors_vim` — an issue no farm build
    can ever produce, and nothing in the UI to say so."""
    settings = hooks.env_hook_settings(_state(tmp_path, target="2026Q3"))
    assert settings["DPORTSV3_TRACKER_TARGET"] == "@2026Q3"


def test_an_already_prefixed_target_is_not_doubled(tmp_path):
    settings = hooks.env_hook_settings(_state(tmp_path, target="@2026Q3"))
    assert settings["DPORTSV3_TRACKER_TARGET"] == "@2026Q3"


# --- how the conf gets written ----------------------------------------------

EXAMPLE = """\
# Override only if the client lives elsewhere.
# ARTIFACT_STORE_CLIENT=/usr/local/bin/artifact-store-client

# Build target. Defaults to @${PROFILE} if unset.
# DPORTSV3_TRACKER_TARGET=@2026Q2
"""


def test_a_setting_replaces_the_line_it_belongs_to():
    """In place, not appended: the comment above each value is the only
    explanation of what it does, and a value that drifts away from its
    explanation is how the wrong one gets left in."""
    out = hooks.render_conf(EXAMPLE, {"DPORTSV3_TRACKER_TARGET": "@2026Q3"})
    lines = out.splitlines()
    i = lines.index("DPORTSV3_TRACKER_TARGET=@2026Q3")
    assert lines[i - 1].startswith("# Build target."), lines
    assert "# DPORTSV3_TRACKER_TARGET=@2026Q2" not in out


def test_a_setting_the_example_never_mentions_is_appended():
    out = hooks.render_conf(EXAMPLE, {"POLYTROPOS_PYTHON": "/work/x/bin/python"})
    assert out.rstrip().endswith("POLYTROPOS_PYTHON=/work/x/bin/python")
    assert "written by" in out


def test_a_replaced_setting_is_not_also_appended():
    """Two assignments of the same variable is a file where the answer
    depends on read order — and the appended one, being last, wins over
    the one sitting under its explanation."""
    out = hooks.render_conf(EXAMPLE, {"DPORTSV3_TRACKER_TARGET": "@2026Q3"})
    assert out.count("DPORTSV3_TRACKER_TARGET=") == 1, out


def test_rendering_keeps_everything_it_was_not_asked_about():
    out = hooks.render_conf(EXAMPLE, {"DPORTSV3_TRACKER_TARGET": "@2026Q3"})
    assert "# ARTIFACT_STORE_CLIENT=/usr/local/bin/artifact-store-client" in out


def test_install_writes_the_settings_into_the_conf(tmp_path):
    target = tmp_path / "etc-dsynth"
    written, _ = hooks.install_hooks(
        target, settings={"DPORTSV3_TRACKER_TARGET": "@2026Q3"})
    assert hooks.CONF_TARGET in written
    assert "DPORTSV3_TRACKER_TARGET=@2026Q3" in (target / hooks.CONF_TARGET).read_text()


def test_install_does_not_rewrite_an_operator_edited_conf(tmp_path):
    target = tmp_path / "etc-dsynth"
    hooks.install_hooks(target, settings={"DPORTSV3_TRACKER_TARGET": "@2026Q3"})
    (target / hooks.CONF_TARGET).write_text("MINE=1\n")

    _, skipped = hooks.install_hooks(
        target, settings={"DPORTSV3_TRACKER_TARGET": "@other"})
    assert (target / hooks.CONF_TARGET).read_text() == "MINE=1\n"
    assert any(hooks.CONF_TARGET in note for note in skipped), skipped


# --- the indirection, executing ---------------------------------------------

def _fake_python(tmp_path) -> Path:
    py = tmp_path / "python"
    py.write_text('#!/bin/sh\necho "ran $*"\n')
    py.chmod(0o755)
    return py


def test_the_interpreter_runs_the_script_as_an_argument(tmp_path):
    tool = tmp_path / "dportsv3"
    tool.write_text("#!/nonexistent/venv/bin/python\n")
    tool.chmod(0o644)          # read-only mount: readable, not executable
    done = _sh(tmp_path, f"POLYTROPOS_PYTHON={_fake_python(tmp_path)}\n"
                         f"DPORTSV3_BIN={tool}\n"
                         "dportsv3_cli tracker record-result")
    assert done.stdout.strip() == f"ran {tool} tracker record-result", done


def test_without_it_a_venv_script_is_only_its_shebang(tmp_path):
    """The failure this exists to prevent. A console script names its
    venv's interpreter by absolute host path; inside the chroot that path
    is nothing, and exec fails before python is ever involved."""
    tool = tmp_path / "dportsv3"
    tool.write_text("#!/nonexistent/venv/bin/python\n")
    tool.chmod(0o755)
    done = _sh(tmp_path, f"DPORTSV3_BIN={tool}\n"
                         "dportsv3_cli tracker record-result || echo ENOENT")
    assert "ENOENT" in done.stdout, done


def test_the_store_client_goes_through_it_too(tmp_path):
    """Both tools come out of the same venv, so both need the same
    treatment. Fixing only the tracker half leaves every artifact upload
    dying at exec."""
    tool = tmp_path / "artifact-store-client"
    tool.write_text("#!/nonexistent/venv/bin/python\n")
    tool.chmod(0o644)
    done = _sh(tmp_path, f"POLYTROPOS_PYTHON={_fake_python(tmp_path)}\n"
                         f"ARTIFACT_STORE_CLIENT={tool}\n"
                         "ARTIFACT_STORE_URL=http://x\n"
                         "artifact_store health")
    assert done.stdout.strip() == f"ran {tool} --url http://x health", done


def test_config_accepts_a_tool_it_may_run_but_not_chmod(tmp_path):
    """`tracker_load_config` demanded +x on DPORTSV3_BIN. With an
    interpreter named it never gets exec'd directly, and it arrives on a
    read-only bind mount where nobody is going to be adding the bit."""
    tool = tmp_path / "dportsv3"
    tool.write_text("#!/nonexistent/venv/bin/python\n")
    tool.chmod(0o644)
    done = _sh(tmp_path, f"POLYTROPOS_PYTHON={_fake_python(tmp_path)}\n"
                         f"DPORTSV3_BIN={tool}\n"
                         "DPORTSV3_TRACKER_URL=http://127.0.0.1:8080\n"
                         "tracker_load_config\n"
                         "echo SURVIVED")
    assert "SURVIVED" in done.stdout, done


def test_an_interpreter_that_is_not_there_still_fails_soft(tmp_path):
    """Soft, not silent-and-wrong: dsynth must keep building, and the
    reason has to be somewhere an operator can find it."""
    tool = tmp_path / "dportsv3"
    tool.write_text("x")
    done = _sh(tmp_path, f"POLYTROPOS_PYTHON={tmp_path / 'absent'}\n"
                         f"DPORTSV3_BIN={tool}\n"
                         "DPORTSV3_TRACKER_URL=http://127.0.0.1:8080\n"
                         "tracker_load_config\n"
                         "echo REACHED-THE-TRACKER")
    assert done.returncode == 0, done.stderr
    assert "REACHED-THE-TRACKER" not in done.stdout
    log = (tmp_path / "logs" / "dportsv3-hooks.log").read_text()
    assert "POLYTROPOS_PYTHON is not executable" in log, log
