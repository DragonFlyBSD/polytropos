"""The rc.d scripts are executable code, so run them.

poly-abr.1 / poly-abr.2. Every check here either executes the script
against a stub rc.subr and inspects what it would hand to daemon(8), or
pins an invariant that a plausible edit would quietly break.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
RC_D = DEPLOY / "rc.d"
SERVICES = ["polytropos_artifact_store", "polytropos_tracker",
            "polytropos_runner"]

# Enough of rc.subr to let a script run to the point where it has built
# command/command_args, then print what it built instead of starting it.
STUB_RC_SUBR = """
load_rc_config() { :; }
err() { _rc=$1; shift; echo "ERR $*" >&2; exit "$_rc"; }
warn() { echo "WARN $*" >&2; }
run_rc_command() {
    echo "NAME=${name}"
    echo "RCVAR=${rcvar}"
    echo "PIDFILE=${pidfile}"
    echo "COMMAND=${command}"
    echo "ARGS=${command_args}"
    eval "echo \\"ENV=\\$${name}_env\\""
    echo "PRECMD=${start_precmd}"
}
"""


def run_service(tmp_path, service, conf=None, rc_conf_vars=None):
    """Execute one rc script far enough to see what it would launch."""
    stub = tmp_path / "rc.subr"
    stub.write_text(STUB_RC_SUBR)

    src = (RC_D / service).read_text()
    # The script sources an absolute path we cannot write to. Same
    # retarget-the-absolute-path trick the worktree sweep test uses.
    src = src.replace(". /etc/rc.subr", f'. "{stub}"')
    if conf is not None:
        conf_file = tmp_path / "polytropos.conf"
        conf_file.write_text(conf)
        src = src.replace('/usr/local/etc/polytropos.conf', str(conf_file))

    script = tmp_path / service
    script.write_text(src)

    preamble = ""
    for k, v in (rc_conf_vars or {}).items():
        preamble += f'{k}="{v}"\n'

    driver = tmp_path / "driver.sh"
    driver.write_text(f'{preamble}. "{script}" start\n')
    p = subprocess.run(["/bin/sh", str(driver)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    out = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


# --- they are valid shell -----------------------------------------------

@pytest.mark.parametrize("service", SERVICES)
def test_script_parses(service) -> None:
    p = subprocess.run(["/bin/sh", "-n", str(RC_D / service)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@pytest.mark.parametrize("name", ["polytropos.conf.sample",
                                  "harness.env.sample", "chat.env.sample"])
def test_sample_parses(name) -> None:
    p = subprocess.run(["/bin/sh", "-n", str(DEPLOY / name)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@pytest.mark.parametrize("service", SERVICES)
def test_script_is_executable(service) -> None:
    assert (RC_D / service).stat().st_mode & 0o111


# --- the privilege split ------------------------------------------------

def test_runner_never_drops_privileges(tmp_path) -> None:
    """dev-env calls require_root(); a non-root runner exits 4. Adding
    -u here would produce a service that starts and immediately dies."""
    got = run_service(tmp_path, "polytropos_runner")
    assert " -u " not in got["ARGS"], got["ARGS"]


@pytest.mark.parametrize("service", ["polytropos_artifact_store",
                                     "polytropos_tracker"])
def test_http_services_do_drop_privileges(tmp_path, service) -> None:
    got = run_service(tmp_path, service)
    assert "-u polytropos" in got["ARGS"], got["ARGS"]


def test_runner_sets_umask_for_the_shared_tree(tmp_path) -> None:
    """Root writing into a polytropos-owned tree. Without umask 002 the
    tracker cannot write state.db-wal, and the symptom is a read-only
    database rather than a permission error."""
    assert "umask 002" in (RC_D / "polytropos_runner").read_text()


# --- what actually gets launched ----------------------------------------

def test_artifact_store_stays_on_loopback(tmp_path) -> None:
    got = run_service(tmp_path, "polytropos_artifact_store")
    assert "--bind 127.0.0.1" in got["ARGS"]
    assert "--port 8788" in got["ARGS"]


def test_tracker_binds_the_lan_by_default(tmp_path) -> None:
    """Deliberate, per poly-abr.6 — pinned so it is never an accident."""
    got = run_service(tmp_path, "polytropos_tracker")
    assert "--bind 0.0.0.0" in got["ARGS"]
    assert "--port 8080" in got["ARGS"]


def test_runner_gets_the_queue_root(tmp_path) -> None:
    got = run_service(tmp_path, "polytropos_runner")
    assert "--queue-root /build/synth/logs/evidence/queue" in got["ARGS"]


def test_no_dev_env_flag_when_unset(tmp_path) -> None:
    """Empty means 'let the tracker or auto-pick decide'. Passing --env
    with an empty value would make argparse eat the next argument."""
    got = run_service(tmp_path, "polytropos_runner")
    assert "--env" not in got["ARGS"], got["ARGS"]


def test_dev_env_flag_appears_when_set(tmp_path) -> None:
    got = run_service(tmp_path, "polytropos_runner",
                      rc_conf_vars={"polytropos_runner_dev_env": "2026Q3-x"})
    assert "--env 2026Q3-x" in got["ARGS"]


@pytest.mark.parametrize("service", SERVICES)
def test_bootstrap_is_off(tmp_path, service) -> None:
    """bin/dportsv3 otherwise builds the venv on first run. A service
    start is the wrong moment to find out the network is down."""
    got = run_service(tmp_path, service)
    assert "DPORTSV3_NO_BOOTSTRAP=1" in got["ENV"]


@pytest.mark.parametrize("service", SERVICES)
def test_no_daemon_supervision(tmp_path, service) -> None:
    """daemon -r restarts on any exit code. The runner exits 3 when
    another holds the lock and 4 when it cannot read the env store —
    supervising those spins instead of surfacing them."""
    got = run_service(tmp_path, service)
    assert " -r " not in got["ARGS"], got["ARGS"]


@pytest.mark.parametrize("service", SERVICES)
def test_pidfile_is_set(tmp_path, service) -> None:
    """daemon(8) documents -p as the prerequisite for newsyslog rotation
    (poly-abr.4), so it has to be there before that work starts."""
    got = run_service(tmp_path, service)
    assert got["PIDFILE"] == f"/var/run/{service}.pid"
    assert f"-p /var/run/{service}.pid" in got["ARGS"]


# --- configuration precedence -------------------------------------------

def test_rc_conf_beats_the_config_file(tmp_path) -> None:
    """rc.subr reads /etc/rc.conf first, so the config file must only
    fill in what is still unset — hence `: ${var=...}` everywhere."""
    conf = ': ${polytropos_tracker_port="9999"}\n'
    got = run_service(tmp_path, "polytropos_tracker", conf=conf,
                      rc_conf_vars={"polytropos_tracker_port": "7777"})
    assert "--port 7777" in got["ARGS"], got["ARGS"]


def test_config_file_beats_the_script_default(tmp_path) -> None:
    conf = ': ${polytropos_tracker_port="9999"}\n'
    got = run_service(tmp_path, "polytropos_tracker", conf=conf)
    assert "--port 9999" in got["ARGS"]


def test_logs_root_drives_the_derived_paths(tmp_path) -> None:
    """One knob to move the whole tree."""
    conf = ': ${polytropos_logs_root="/other/place"}\n'
    got = run_service(tmp_path, "polytropos_runner", conf=conf)
    assert "--queue-root /other/place/evidence/queue" in got["ARGS"]
    assert "DPORTSV3_STATE_DB=/other/place/evidence/state.db" in got["ENV"]


def test_sample_config_assigns_conditionally_everywhere() -> None:
    """A plain `var=value` in the sample would silently outrank rc.conf."""
    for line in (DEPLOY / "polytropos.conf.sample").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert line.startswith(": ${"), f"unconditional assignment: {line}"


def test_missing_config_file_is_not_fatal(tmp_path) -> None:
    """A host that never wrote polytropos.conf still starts on defaults."""
    got = run_service(tmp_path, "polytropos_tracker",
                      conf=None)  # path points at a file that isn't there
    assert "--port 8080" in got["ARGS"]


# --- the rc.subr name collision -----------------------------------------

def test_dev_env_knob_avoids_the_rc_subr_collision() -> None:
    """rc.subr reads ${name}_env as the child environment. Naming the
    dev-env knob polytropos_runner_env would replace it wholesale."""
    text = (RC_D / "polytropos_runner").read_text()
    assert "polytropos_runner_dev_env" in text
    assert ': ${polytropos_runner_env=' not in text


def test_runner_env_carries_the_service_urls(tmp_path) -> None:
    got = run_service(tmp_path, "polytropos_runner")
    assert "DPORTSV3_TRACKER_URL=http://127.0.0.1:8080" in got["ENV"]
    assert "ARTIFACT_STORE_URL=http://127.0.0.1:8788" in got["ENV"]


# --- credentials --------------------------------------------------------

@pytest.mark.parametrize("name", ["harness.env.sample", "chat.env.sample"])
def test_secret_samples_use_export(name) -> None:
    """They are sourced by the rc script, so a bare assignment would set
    a shell variable the child process never sees."""
    body = [l.strip() for l in (DEPLOY / name).read_text().splitlines()]
    assigns = [l for l in body if l and not l.startswith("#")]
    assert assigns
    for line in assigns:
        assert line.startswith("export "), line


def test_no_credentials_in_the_world_readable_config() -> None:
    text = (DEPLOY / "polytropos.conf.sample").read_text()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "API_KEY" not in line, line
