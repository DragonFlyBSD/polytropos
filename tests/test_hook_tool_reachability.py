"""Where the dsynth hooks find the tool inside a dev-env chroot.

The answer was always the same place everything else in the env looks:
``layout.TOOL_DIR`` (/work/polytropos), a checkout of this repository that
env creation puts there, exported into the chroot as ``POLYTROPOS_ROOT``,
with the repo's own shell wrapper at ``TOOL_BIN``. ``health.py`` and
``worker.py`` have always invoked it that way.

The hooks did not. Their shipped conf named ``/build/synth/polytropos/
bin/dportsv3``, a path on no machine, so every tracker call in an env
soft-failed. The fix is that ``hooks-install`` writes the paths the env
actually has.

The first attempt at this bind-mounted the host's tool venv onto
TOOL_DIR, which shadowed the checkout and broke `dportsv3 --version`
inside the env with rc=127 — a venv console script's shebang names the
host's interpreter path. Hence the mount guard below.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from dports_dev_env import hooks

def _state(tmp_path, target="2026Q3", name="2026Q3-editors_vim"):
    from dports_dev_env.state import EnvironmentState
    return EnvironmentState(
        schema=1, name=name, backend="chroot", target=target,
        origin="editors/vim", status="ready",
        created_at="2026-08-27T00:00:00Z", updated_at="2026-08-27T00:00:00Z",
        root_dir=tmp_path / "root", writable_dir=tmp_path / "writable",
        provisioned_base_id="base", repos=None, source=None,
        runtime=SimpleNamespace(oracle_profile="dportsv3-py311"),
    )


# --- where the tools are -----------------------------------------------------

def test_the_tool_paths_are_the_checkout_the_env_already_carries(tmp_path):
    """Not a host path, and not somewhere hooks-install invented. The env
    has carried a checkout at TOOL_DIR since it was created."""
    from dports_dev_env.layout import TOOL_BIN, TOOL_DIR

    settings = hooks.env_hook_settings(_state(tmp_path))
    assert settings["DPORTSV3_BIN"] == TOOL_BIN
    assert settings["ARTIFACT_STORE_CLIENT"].startswith(TOOL_DIR + "/")


def test_the_hooks_agree_with_every_other_in_env_caller(tmp_path):
    """health.py probes "$POLYTROPOS_ROOT/bin/dportsv3 --version" and
    worker.py runs the same path for `dsl check`. A hook that reaches the
    tool by some other route is a second contract to keep in step."""
    from dports_dev_env.helpers import build_env_dict
    from dports_dev_env.layout import TOOL_BIN

    settings = hooks.env_hook_settings(_state(tmp_path))
    polytropos_root = build_env_dict(_state(tmp_path))["POLYTROPOS_ROOT"]
    assert settings["DPORTSV3_BIN"] == f"{polytropos_root}/bin/dportsv3" == TOOL_BIN


def test_the_checkout_ships_the_store_client_as_an_executable(tmp_path):
    """The hooks call ARTIFACT_STORE_CLIENT by path, and in an env that
    path is the checkout's bin/. Moving the implementation into the
    package once deleted this file, which left the hooks pointing at
    nothing in every env synced to that commit."""
    settings = hooks.env_hook_settings(_state(tmp_path))
    name = Path(settings["ARTIFACT_STORE_CLIENT"]).name
    shipped = Path(__file__).resolve().parents[1] / "bin" / name
    assert shipped.is_file(), f"{shipped} is what the hooks invoke"
    assert os.access(shipped, os.X_OK), f"{shipped} must be executable"


def test_the_store_client_stays_stdlib_only():
    """It runs from a chroot where the only guaranteed interpreter is a
    system python3. A dependency here is a dependency in every place a
    build can fail."""
    import ast
    import sys

    src = (Path(__file__).resolve().parents[1]
           / "dportsv3" / "artifact_store_client.py").read_text()
    tree = ast.parse(src)
    mods = {n.module.split(".")[0] for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.Import) for a in n.names}
    outside = sorted(m for m in mods
                     if m not in sys.stdlib_module_names and m != "__future__")
    assert not outside, f"non-stdlib imports: {outside}"


def test_nothing_is_mounted_onto_the_tool_checkout(set_setting, monkeypatch, tmp_path):
    """Regression guard. Bind-mounting anything at TOOL_DIR hides the
    checkout, and the failure is indirect: `dportsv3 --version` returns
    rc=127 from inside the env because the thing now at that path is a
    venv console script whose shebang names the host's interpreter."""
    from dports_dev_env import runtime
    from dports_dev_env.config import load_config
    from dports_dev_env.layout import TOOL_RELATIVE

    set_setting("dev_env.cache_root", str(tmp_path / "cache"))
    monkeypatch.setattr(runtime, "check_mount_target_length", lambda t: None)
    mounted: list[Path] = []
    monkeypatch.setattr(runtime, "mount_null",
                        lambda s, t, read_only=False: mounted.append(t) or True)
    monkeypatch.setattr(runtime, "mount_procfs", lambda t: True)

    root = tmp_path / "root"
    root.mkdir()
    runtime.prepare_root_runtime(load_config(), root)

    offenders = [t for t in mounted if TOOL_RELATIVE in str(t)]
    assert not offenders, f"mounted onto the tool checkout: {offenders}"


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
    out = hooks.render_conf(EXAMPLE, {"DPORTSV3_TRACKER_STATE_DIR": "/work/st"})
    assert out.rstrip().endswith("DPORTSV3_TRACKER_STATE_DIR=/work/st")
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


def test_the_clients_default_url_matches_the_shared_one():
    """The client cannot import dportsv3.common.endpoints — the test
    above forbids it — so its default is a copy. Pin them equal: a
    deployment that moves the tracker changes one constant, and a silent
    disagreement here sends every hook to the wrong port."""
    from dportsv3.artifact_store_client import DEFAULT_URL
    from dportsv3.common.endpoints import DEFAULT_TRACKER_URL

    assert DEFAULT_URL == DEFAULT_TRACKER_URL
