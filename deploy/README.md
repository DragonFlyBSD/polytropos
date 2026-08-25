# Deploying Polytropos as services

Three rc.d scripts, one shared config file, and two credential files.
DragonFly BSD only — these use `rc.subr`, `daemon(8)` and `newsyslog`.

## What runs, and as whom

| Service | Runs as | Listens on |
|---|---|---|
| `polytropos_artifact_store` | `polytropos` | `127.0.0.1:8788` |
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

    /usr/local/etc/rc.d/polytropos_artifact_store
    /usr/local/etc/rc.d/polytropos_tracker
    /usr/local/etc/rc.d/polytropos_runner
    /usr/local/etc/polytropos.conf              shared, world-readable
    /usr/local/etc/polytropos/harness.env       root:wheel 0600
    /usr/local/etc/polytropos/chat.env          root:polytropos 0640

Precedence for every knob, highest first: `/etc/rc.conf`, then
`polytropos.conf`, then the script's own default. That works because all
three levels use `: ${var="value"}`, which assigns only when unset.

## Credentials

Two files, because two different services call an LLM and they run as
different users:

* `harness.env` — triage and patch keys, read by the runner (root only).
* `chat.env` — the tracker's fix-chat panel, read by `polytropos`.
  Entirely optional: leave it out and `DP_HARNESS_CHAT_MODEL` stays
  unset, which disables the endpoint (503) and hides the UI panel.

Neither belongs in `polytropos.conf` or `rc.conf` — both are
world-readable.

## Install

    bin/dportsv3 deploy install --dry-run     # show every step, change nothing
    sudo bin/dportsv3 deploy install

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

## Enable

    # /etc/rc.conf
    polytropos_artifact_store_enable="YES"
    polytropos_tracker_enable="YES"
    polytropos_runner_enable="YES"

    service polytropos_artifact_store start
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
