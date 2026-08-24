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
with `umask 002` and the evidence directories are setgid. Without that,
the tracker fails to write `state.db-wal` and the symptom looks like a
read-only database rather than a permission problem.

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

    dportsv3 deploy install

See `poly-abr.3`. Until that lands, copy the files by hand: the rc.d
scripts to `/usr/local/etc/rc.d/` (mode 755), the samples to their
locations above with the `.sample` suffix dropped, then create the
service user and hand it the evidence tree.

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
