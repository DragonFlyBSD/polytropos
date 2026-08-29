# Deploying Polytropos as services

Three rc.d scripts, one shared config file, and two credential files.
DragonFly BSD only — these use `rc.subr`, `daemon(8)` and `newsyslog`.

## What runs, and as whom

| Service | Runs as | Listens on |
|---|---|---|
| `polytropos_tracker` | `polytropos` | `0.0.0.0:8080` |
| `polytropos_runner` | **root** | nothing |

The runner runs as root and cannot be changed: every `dev-env`
subcommand calls `require_root()`, and a non-root runner refuses to
start with exit 4 rather than running with the dsynth-busy gate off.

Because root writes into a tree owned by `polytropos`, the runner starts
with `umask 002`. Without it the tracker cannot write `state.db-wal`,
and SQLite reports that as a *read-only database* rather than as a
permission error — you would go looking in the wrong place.

`umask 002` is the whole mechanism, and it is enough. Measured on
DragonFly 6.5 (HAMMER2): a new file inherits its group from the parent
directory whether or not that directory is setgid, so the setgid bit
Linux would need here is redundant. What is *not* redundant is the
umask — at root's default 022 the same file comes out `rw-r--r--` and
the service user cannot write it:

    umask 002, plain dir   -> -rw-rw-r-- root polytropos   works
    umask 002, setgid dir  -> -rw-rw-r-- root polytropos   identical
    umask 022, either      -> -rw-r--r-- root polytropos   broken

The umask survives the whole path — rc script, `bin/dportsv3`, the
Python CLI, and into the chroot, where `umask` reports 0002.

Group inheritance only applies to *new* files, so the install has to
`chgrp -R` an existing tree once.

## Files

    /usr/local/etc/rc.d/polytropos_tracker
    /usr/local/etc/rc.d/polytropos_runner
    /usr/local/etc/polytropos.conf                 rc only, world-readable
    /usr/local/etc/polytropos/polytropos.toml      every setting
    /usr/local/etc/polytropos/secrets/             one file per credential

`/usr/local/etc/polytropos/` is `$DPORTSV3_CONFIG_DIR`: both services
export it from `polytropos_config_dir`, and it is the only way anything
in the Python packages finds a config file — nothing searches for a
surrounding directory.

Precedence for the rc knobs, highest first: `/etc/rc.conf`, then
`polytropos.conf`, then the script's own default. That works because all
three levels use `: ${var="value"}`, which assigns only when unset.

## Two files, and why the split is where it is

`polytropos.conf` holds what rc needs *before any Python runs*: which
command to exec, which account to drop to, where to write logs.
`polytropos.toml` holds everything the tool reads for itself.

It used to be otherwise, and the shape is worth recording because it is
the thing this layout fixes. `polytropos.conf` declared
`polytropos_state_db`; the rc script translated it into
`$DPORTSV3_STATE_DB`; the Python read that. Same for the artifact root,
the tracker URL, the bind address, the port and the default dev-env —
one setting with two names in two formats, kept in step by hand. Two more
shell files, `harness.env` and `chat.env`, existed *only* so `export`
could carry values into `os.environ`, because that was the one channel a
setting had into the code. Underneath sat 69 environment variables, 39 of
which the code read and no sample or script mentioned anywhere.

The rc scripts now source no credential file and translate no variable.
Their child environment carries three things, none of them configuration:
`HOME` (because `daemon -u` does not set it), `DPORTSV3_NO_BOOTSTRAP`
(which tells the wrapper this is a service), and `DPORTSV3_CONFIG_DIR`
(which names the settings file and so cannot live inside it).

    dportsv3 config show      every value, and where it came from
    dportsv3 config check     validate without starting anything
    dportsv3 config migrate   fold an old .conf/.env install into the toml

The runner's prestart asks `dportsv3 config get paths.queue_root` rather
than keeping a second copy of that path in a shell variable. One exec at
service start, and no way for the two to disagree.

## No repository required

The scripts name two **commands**, not a checkout:

    polytropos_cmd          default /usr/local/bin/dportsv3
    polytropos_dev_env_cmd  default /usr/local/bin/dports-dev-env   (runner only)

Both are console scripts a package installs into the prefix bindir, so a
packaged polytropos runs the whole stack with no source tree and no venv.
A git checkout is then just another value:

    : ${polytropos_cmd:="/home/you/polytropos/bin/dportsv3"}
    : ${polytropos_dev_env_cmd:="/home/you/polytropos/bin/dportsv3 dev-env"}

The second knob exists because the `dportsv3` console script deliberately
does **not** implement `dev-env` — only the checkout wrapper routes that
word, by exec'ing `dports-dev-env`. The runner exports
`DPORTS_DEV_ENV_CMD` from this knob so the agent uses the command you
configured rather than whatever `PATH` happens to hold.

**What still needs a git checkout:** `dev-env create`. It makes a
`git clone --mirror` of the tool tree and clones your current branch into
the chroot, so the env tracks the branch you are working on. A packaged
install can run the services against envs that already exist; creating
one is a developer action. See `poly-abr.11`.

## Credentials

One value per file under `/usr/local/etc/polytropos/secrets/`, named by a
`*_file` setting. A TOML syntax error then cannot take out the whole
credential set, rotation is a single write, and the mode follows
whichever service reads it — which is the part that matters, because the
two services run as different users:

    secrets/triage.key      0600 root                the runner is root
    secrets/patch.key       0600 root
    secrets/chat.key        0640 root:polytropos     the tracker is not
    secrets/delivery.token  0640 root:polytropos

Nothing is in `polytropos.conf` or `polytropos.toml`: both are
world-readable.

`deploy install` migrates an existing `harness.env` / `chat.env` on the
way past, because the same upgrade stops the rc scripts sourcing them —
skip that and the runner comes back up with no API keys and every job
fails. The originals are left where they are.

## Delivery

Off until `delivery.type` is set in `polytropos.toml`. Until then Accept
stays a pure tracker-side action and logs `skip_reason=no_config`.

Start with `local-patch`, which writes the diff to `delivery.outbox`
instead of pushing anywhere: that proves the whole Accept path with no
credentials and no network. Switch to `github` once a patch has landed in
the outbox.

For a forge provider, `secrets/delivery.token` is `root:polytropos 0640`
— **not** `0400 root`. Delivery runs inside the *tracker*, which drops to
the unprivileged account, so a root-only token is unreadable by the only
process that wants it, and `delivery.clone_dir` has to be writable by
that same account. The tracker checks all of this when it starts and logs
what is wrong, rather than waiting for the first Accept to find out.

A standalone `delivery.toml` in the config directory is still read where
one exists, and still wins, so an install that predates the settings file
keeps delivering. The log says which file won.

## Logs and rotation

`daemon(8)` appends each service's stdout and stderr to
`/var/log/polytropos/<service>.log`. The runner also keeps its own
structured `runner.log` under the queue root — that one is unaffected by
any of this; these files carry tracebacks, uvicorn's access log, and
anything written before the structured log is open.

Rotation is a newsyslog drop-in at
`/usr/local/etc/newsyslog.conf.d/polytropos.conf`, which
`/etc/newsyslog.conf` already includes.

**The pidfile column points at the supervisor, not the service.**
`daemon(8)` writes two: `-p` holds the child (the Python process) and
`-P` holds the supervisor. Only the supervisor reopens the output file
on `SIGUSR1`. Its own rotation advice mentions only `-p`, which reads
like the fatal version — measured on DragonFly 6.5:

    SIGUSR1 -> child        service and supervisor both died
    SIGUSR1 -> supervisor   log reopened, both still running

`SIGUSR1`'s default disposition terminates a process, so a rotation
configured the obvious way would take the stack down nightly.

`service ... stop` is unaffected: rc.subr signals `$pidfile`, which
stays the child, so stopping remains a clean child exit.

## Install

    bin/dportsv3 deploy install --dry-run     # show every step, change nothing
    sudo bin/dportsv3 deploy install

This installs the software as well as wiring the host: it builds a venv
at `<prefix>/lib/polytropos`, installs both distributions into it, and
links `dportsv3` and `dports-dev-env` into `<prefix>/bin` — which is
where the rc.d defaults look. The services then run from that install,
not from the checkout, and deleting the checkout does not stop them.

**Re-running is the upgrade path.** Pull, run `deploy install` again,
restart the services. The venv is reused and both distributions are
reinstalled; rc.d scripts are replaced; your config is left alone.

The venv is built with `--system-site-packages` so the pkg-installed
`py311-fastapi`, `py311-pydantic` and friends are visible. Without that
pip builds them from source and wants a Rust toolchain the base system
does not have.

`--no-software` skips all of that and only wires the host, for the case
where a port owns the software.

Run it from the checkout: `bin/dportsv3` exports `$DPORTS_DEV_TOOL_ROOT`,
which is how the command finds `deploy/` without any package guessing a
repository path. Pass `--tool-root` if you invoke the console script
directly.

It creates the `polytropos` user and group, installs the rc.d scripts,
copies the samples into place, creates the queue subdirectories, and
hands `$LOGS_ROOT` to the service account. `--prefix`, `--user`,
`--group` and `--logs-root` override the defaults.

Two rules, and the difference is the point:

* **rc.d scripts are replaced every time.** An upgrade that leaves a
  stale script behind is worse than one that overwrites it.
* **Config and credential files are written once and never touched
  again** — the same rule as the ports framework's `@sample` keyword,
  which copies `<file>.sample` to `<file>` only when the target is
  absent. Anything you have edited is yours.

Re-running is safe: every step reports itself as `do` or `skip` first,
and skipped steps do nothing.

Installing from a checkout needs the venvs bootstrapped once, per entry
point — the install profile is part of each stamp, so one command does
not cover the others:

    bin/dportsv3 tracker serve --help   # the [tracker] extra
    bin/dportsv3 dev-env --help         # the dev-env venv

`deploy install` prints these when it sees the console scripts are not in
the prefix.

## Enable

    # /etc/rc.conf
    polytropos_tracker_enable="YES"
    polytropos_runner_enable="YES"

    service polytropos_tracker start
    service polytropos_runner start

Order does not matter — each service is idempotent on schema init. The
`REQUIRE:` line on the runner sequences it after the other two at boot
anyway, so a fresh boot brings up the HTTP services first.

## Restarting the runner is safe

It takes an exclusive `flock` on `<queue-root>/runner.lock` and exits 3
if another holds it, so a stray manual start cannot race the service. On
restart it sweeps what a dead runner left: in-flight rows are reaped,
stranded job files move to `failed/`, stale worktrees are removed.

There is deliberately no `daemon -r` supervision. It would restart the
child whatever the exit code, and exits 3 and 4 are configuration
errors — supervising them spins instead of surfacing them.
