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


def _logical_lines(text):
    """Collapse `\\`-continued shell lines into single logical lines."""
    out, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((buf + line).strip())
        buf = ""
    if buf:
        out.append(buf.strip())
    return out


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


# --- the stale-venv guard (poly-abr.7) ----------------------------------

def run_precmd(tmp_path, service, wrapper_rc=0, extra_vars=None):
    """Execute a script's start_precmd with a fake bin/dportsv3.

    Returns (returncode, stderr). The wrapper stub exits ``wrapper_rc``,
    which is how a stale install stamp presents: bin/dportsv3 compares a
    stamp on every run and, with DPORTSV3_NO_BOOTSTRAP=1 set, exits 1
    instead of installing.
    """
    root = tmp_path / "checkout"
    (root / "bin").mkdir(parents=True)
    wrapper = root / "bin" / "dportsv3"
    wrapper.write_text(f"#!/bin/sh\nexit {wrapper_rc}\n")
    wrapper.chmod(0o755)
    dev_env = root / "bin" / "dports-dev-env"
    dev_env.write_text(f"#!/bin/sh\nexit {wrapper_rc}\n")
    dev_env.chmod(0o755)

    stub = tmp_path / "rc.subr"
    stub.write_text(STUB_RC_SUBR)

    src = (RC_D / service).read_text().replace(". /etc/rc.subr", f'. "{stub}"')
    script = tmp_path / service
    script.write_text(src)

    import getpass
    variables = {
        "polytropos_cmd": str(wrapper),
        "polytropos_dev_env_cmd": str(dev_env),
        "polytropos_user": getpass.getuser(),
        "polytropos_log_dir": str(tmp_path / "log"),
        "polytropos_home": str(tmp_path / "home"),
        "polytropos_queue_root": str(tmp_path / "queue"),
        "polytropos_conf": str(tmp_path / "absent.conf"),
        "polytropos_harness_env": str(tmp_path / "absent.env"),
        "polytropos_chat_env": str(tmp_path / "absent.env"),
        **(extra_vars or {}),
    }
    preamble = "".join(f'{k}="{v}"\n' for k, v in variables.items())
    driver = tmp_path / "driver.sh"
    driver.write_text(f'{preamble}. "{script}" start\neval "$start_precmd"\n')
    p = subprocess.run(["/bin/sh", str(driver)], capture_output=True, text=True)
    return p.returncode, p.stderr


@pytest.mark.parametrize("service", SERVICES)
def test_stale_venv_refuses_to_start(tmp_path, service) -> None:
    """A stale stamp must stop the service, not every job it runs.

    Verified on x6: with DPORTSV3_NO_BOOTSTRAP=1 the wrapper exits 1 and
    prints 'dev-env bootstrap required'. The runner reaches a dev-env
    from 21 functions, so without this guard the service starts fine and
    then fails everything, with the reason in per-job output.
    """
    rc, err = run_precmd(tmp_path, service, wrapper_rc=1)
    assert rc != 0, "started with a stale venv"
    assert "is stale" in err, err


@pytest.mark.parametrize("service", SERVICES)
def test_healthy_venv_starts(tmp_path, service) -> None:
    rc, err = run_precmd(tmp_path, service, wrapper_rc=0)
    assert rc == 0, err


def test_runner_probes_both_entry_points() -> None:
    """Two distributions, two venvs, two install stamps. Probing only the
    generator would pass while every chroot call still failed."""
    logical = _logical_lines((RC_D / "polytropos_runner").read_text())
    probes = [l for l in logical if "NO_BOOTSTRAP" in l and l.startswith("if !")]
    assert len(probes) == 2, probes
    assert any("agent-queue-runner --help" in l for l in probes), probes
    assert any("${polytropos_dev_env_cmd}" in l for l in probes), probes


def test_runner_tells_the_agent_which_dev_env_command_to_use() -> None:
    """Otherwise worker.py falls back to PATH, which may hold a different
    copy than the one the operator configured here."""
    text = (RC_D / "polytropos_runner").read_text()
    assert "DPORTS_DEV_ENV_CMD=${polytropos_dev_env_cmd}" in text


@pytest.mark.parametrize("service,argv", [
    ("polytropos_artifact_store", "artifact-store --help"),
    ("polytropos_tracker", "tracker serve --help"),
])
def test_probe_uses_the_services_own_argv(service, argv) -> None:
    """The install profile is part of the stamp: bin/dportsv3 records
    "<profile>:<digest>" and picks INSTALL_PROFILE=tracker only when argv
    is exactly `tracker serve`. A `--version` probe compares against the
    base stamp, which fails both ways — refusing to start on a
    tracker-profile venv, and passing on a base-profile venv that
    `tracker serve` then rejects."""
    text = (RC_D / service).read_text()
    # Join backslash continuations first: the probe is written across two
    # lines, so a naive per-line check sees "bin/dportsv3" and the flag on
    # different lines and can never catch a wrong one.
    logical = _logical_lines(text)
    probes = [l for l in logical
              if "${polytropos_cmd}" in l and "NO_BOOTSTRAP" in l]
    assert len(probes) == 1, probes
    assert argv in probes[0], probes[0]
    assert "--version" not in probes[0], f"probes the wrong profile: {probes[0]}"


def test_missing_command_is_caught_before_the_probe(tmp_path) -> None:
    """The clearer error wins: 'set polytropos_cmd' beats 'does not run'
    when the command simply is not there."""
    stub = tmp_path / "rc.subr"
    stub.write_text(STUB_RC_SUBR)
    src = (RC_D / "polytropos_runner").read_text().replace(
        ". /etc/rc.subr", f'. "{stub}"')
    script = tmp_path / "polytropos_runner"
    script.write_text(src)
    driver = tmp_path / "d.sh"
    driver.write_text(
        f'polytropos_cmd="{tmp_path}/nope/dportsv3"\n'
        f'polytropos_conf="{tmp_path}/absent.conf"\n'
        f'. "{script}" start\neval "$start_precmd"\n')
    p = subprocess.run(["/bin/sh", str(driver)], capture_output=True, text=True)
    assert p.returncode != 0
    assert "set polytropos_cmd" in p.stderr


def test_runner_creates_the_queue_subdirs(tmp_path) -> None:
    """The runner exits 1 naming a missing queue subdirectory, and x6 has
    no queue root at all today."""
    rc, err = run_precmd(tmp_path, "polytropos_runner")
    assert rc == 0, err
    for sub in ("pending", "inflight", "done", "failed"):
        assert (tmp_path / "queue" / sub).is_dir()


def test_world_readable_secrets_warn(tmp_path) -> None:
    """find -perm +077, verified on DragonFly: quiet at 600, warns at 640."""
    secret = tmp_path / "harness.env"
    secret.write_text("export DP_HARNESS_TRIAGE_API_KEY=x\n")
    secret.chmod(0o644)
    rc, err = run_precmd(tmp_path, "polytropos_runner",
                         extra_vars={"polytropos_harness_env": str(secret)})
    assert rc == 0
    assert "readable beyond its owner" in err


def test_mode_600_secrets_are_quiet(tmp_path) -> None:
    secret = tmp_path / "harness.env"
    secret.write_text("export DP_HARNESS_TRIAGE_API_KEY=x\n")
    secret.chmod(0o600)
    rc, err = run_precmd(tmp_path, "polytropos_runner",
                         extra_vars={"polytropos_harness_env": str(secret)})
    assert rc == 0
    assert "readable beyond its owner" not in err


def test_absent_credentials_warn_but_start(tmp_path) -> None:
    """Starting without keys is legal — every job fails, but the service
    coming up is what lets the operator see that in the tracker."""
    rc, err = run_precmd(tmp_path, "polytropos_runner")
    assert rc == 0
    assert "without LLM credentials" in err


# --- conventions taken from the ports tree ------------------------------

@pytest.mark.parametrize("path", [
    "rc.d/polytropos_artifact_store", "rc.d/polytropos_tracker",
    "rc.d/polytropos_runner", "polytropos.conf.sample",
])
def test_defaults_use_the_colon_form(path) -> None:
    """`: ${x:="v"}` not `: ${x="v"}`.

    Measured across 400 rc.d scripts in the FreeBSD ports tree: the colon
    form outnumbers the other roughly 3 to 1, and it is the safer one —
    it substitutes the default for an empty value too, so a knob left as
    polytropos_tracker_bind="" in rc.conf does not become an empty bind
    address.
    """
    import re
    text = (DEPLOY / path).read_text()
    bare = re.findall(r': \$\{([a-z_]+)=', text)
    assert bare == [], f"plain-equals defaults: {bare}"


@pytest.mark.parametrize("service", SERVICES)
def test_daemon_chdirs_to_root(tmp_path, service) -> None:
    """-c, as every ports example does: a service holding a cwd keeps that
    filesystem busy and blocks an unmount."""
    got = run_service(tmp_path, service)
    assert got["ARGS"].startswith("-c "), got["ARGS"]


@pytest.mark.parametrize("service", SERVICES)
def test_supervision_and_pidfile_flag_agree(tmp_path, service) -> None:
    """-r with -p is the trap: rc.subr signals whatever is in $pidfile, so
    with the *child* pidfile the supervisor just restarts it. Either both
    change together or neither does."""
    args = run_service(tmp_path, service)["ARGS"]
    supervised = " -r " in args or " -R " in args
    child_pidfile = " -p " in args
    assert not (supervised and child_pidfile), args


# --- HOME across the privilege drop -------------------------------------

@pytest.mark.parametrize("service", ["polytropos_artifact_store",
                                     "polytropos_tracker"])
def test_home_is_set_for_dropped_privileges(tmp_path, service) -> None:
    """Verified on DragonFly: `daemon -u` lowers the uid and leaves HOME
    alone, so a child running as the service user still sees HOME=/root
    and cannot read it. Anything reaching for Path.home() or a ~/.cache
    fails somewhere that points nowhere near this script."""
    got = run_service(tmp_path, service)
    assert "HOME=/var/db/polytropos" in got["ENV"], got["ENV"]


def test_runner_does_not_override_home(tmp_path) -> None:
    """It stays root, so the inherited HOME is already right."""
    got = run_service(tmp_path, "polytropos_runner")
    assert "HOME=" not in got["ENV"], got["ENV"]


def test_service_home_matches_the_installer() -> None:
    """The rc default and the account the installer creates must agree, or
    the services get a HOME that belongs to nobody."""
    from dportsv3.commands import deploy
    text = (DEPLOY / "polytropos.conf.sample").read_text()
    assert f'polytropos_home:="{deploy.SERVICE_HOME}"' in text


# --- no repository in the scripts (poly-abr.9) --------------------------

@pytest.mark.parametrize("path", [
    "rc.d/polytropos_artifact_store", "rc.d/polytropos_tracker",
    "rc.d/polytropos_runner", "polytropos.conf.sample", "README.md",
])
def test_nothing_assumes_a_checkout_layout(path) -> None:
    """The scripts must name commands, not a repository.

    Once this ships as a dport there is no checkout: the package installs
    the two console scripts into the prefix bindir. A checkout is then one
    possible value for polytropos_cmd, not the assumption.
    """
    text = (DEPLOY / path).read_text()
    offenders = [l for l in text.splitlines()
                 if "polytropos_root" in l and not l.strip().startswith("#")]
    assert offenders == [], offenders


@pytest.mark.parametrize("service,default", [
    ("polytropos_artifact_store", "/usr/local/bin/dportsv3"),
    ("polytropos_tracker", "/usr/local/bin/dportsv3"),
    ("polytropos_runner", "/usr/local/bin/dportsv3"),
])
def test_command_defaults_to_the_packaged_path(tmp_path, service, default) -> None:
    got = run_service(tmp_path, service)
    assert default in got["ARGS"], got["ARGS"]


def test_a_checkout_is_just_another_value(tmp_path) -> None:
    """Setting the knob to a wrapper has to work — that is the whole point
    of making it a command rather than a root."""
    got = run_service(tmp_path, "polytropos_tracker", rc_conf_vars={
        "polytropos_cmd": "/home/you/polytropos/bin/dportsv3"})
    assert "/home/you/polytropos/bin/dportsv3 tracker serve" in got["ARGS"]
