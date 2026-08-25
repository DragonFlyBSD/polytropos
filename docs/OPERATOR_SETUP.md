# Operator setup — agentic DeltaPorts builds, zero to running

This is the single walkthrough from a fresh DragonFly box to a live
agentic dsynth build with the tracker UI open. For architecture see
`docs/AGENTIC_BUILDS.md`; for testing see `docs/TESTING_E2E.md`.

## 1. System prerequisites

DragonFly packages required by the generator + tracker venv:

```sh
pkg install py311-sqlite3 py311-pydantic2 py311-pydantic-core \
            py311-fastapi py311-uvicorn py311-watchfiles \
            py311-uvloop py311-httptools py311-websockets \
            py311-python-dotenv
```

(These keep us off Rust-built wheels — the venv is set up with
`--system-site-packages` to pick them up.)

You also need `dsynth` itself, of course, and a profile set up
(`/usr/local/etc/dsynth/<profile>-make.conf` etc).

## 2. Clone + bootstrap

Two repositories: the tool and the ports data it reads. The tool no longer
lives inside the ports tree.

```sh
cd /build/synth                 # or wherever you keep build trees
git clone https://github.com/DragonFlyBSD/polytropos.git
git clone https://github.com/DragonFlyBSD/DeltaPorts.git
cd polytropos
bin/dportsv3 --help               # first run builds the venv at .venv
```

Point the tool at the ports tree with `--delta-root`, or set it once:

```sh
export DPORTS_DELTA_ROOT=/build/synth/DeltaPorts
export DPORTS_DEV_DELTA_ROOT=/build/synth/DeltaPorts   # for dev-env
```

The wrapper script:
- creates `.venv/` with `--system-site-packages`
- `pip install -e .` (editable, source changes pick up live)
- caches a stamp so subsequent calls skip re-install

If you also want pytest + mypy in there:

```sh
.venv/bin/pip install -e '.[dev]'
```

## 3. Create a dev-env for your target

`dev-env create` needs the tool tree to be a **git checkout**, not an
installed copy or an unpacked tarball: it clones the branch you are on
into the chroot, so the environment matches what you are working on.

That requirement is limited to *creating* an env. The three services only
ever exec into an env that already exists, so a packaged install runs the
whole stack without any source tree — see `deploy/README.md`.


The dev-env is a chroot with a writable copy-on-write overlay where
the agent edits files. One per target/origin you want to iterate on.

```sh
bin/dportsv3 dev-env create --name myenv --target @2026Q2 --origin devel/foo
bin/dportsv3 dev-env status myenv     # expect: status=ready, backend=chroot, root_mounted=true
```

`dev-env path myenv --writable` will print the overlay path
(`/var/cache/dports-dev/myenv/writable`) — that's where the agent's
dirty edits land.

For details (mounts, FPORTS pinning, materialization), see
`docs/dev-chroot-environment.md`.

## 4. Install dsynth hooks into the dev-env

dsynth runs inside the chroot, so its hooks belong in the env's
writable `/etc/dsynth` overlay rather than on the host:

```sh
bin/dportsv3 dev-env hooks-install myenv
```

This copies the hook scripts + `dportsv3-hooks.conf.example` →
`dportsv3-hooks.conf` into `${env_dir}/writable/etc_dsynth/`, which
bind-mounts to `/etc/dsynth` inside the chroot. Existing
`dportsv3-hooks.conf` is preserved (pass `--force` to overwrite).

Edit the conf and set:

```sh
ARTIFACT_STORE_URL=http://127.0.0.1:8788
DPORTSV3_TRACKER_URL=http://127.0.0.1:8080
DPORTSV3_TRACKER_TARGET=@2026Q2     # defaults from $PROFILE if unset
DPORTSV3_BIN=/build/synth/polytropos/bin/dportsv3
```

Verify with:

```sh
bin/dportsv3 dev-env hooks-status myenv
```

Reports which hooks are present, whether they're executable, and
whether any are stale vs. the in-repo source. Re-run
`hooks-install` after a `git pull` to refresh them.

## 5. Configure env for the services

Pick a logs root that artifact-store + tracker share:

```sh
LOGS_ROOT=/build/synth/logs
STATE_DB=$LOGS_ROOT/evidence/state.db
ARTIFACT_ROOT=$LOGS_ROOT/evidence
QUEUE_ROOT=$LOGS_ROOT/evidence/queue   # where hooks write .job files
```

LLM credentials — pick a provider for each phase:

```sh
export DP_HARNESS_TRIAGE_MODEL=deepseek/deepseek-v4-flash
export DP_HARNESS_TRIAGE_API_KEY=...
export DP_HARNESS_PATCH_MODEL=anthropic/claude-sonnet-4
export DP_HARNESS_PATCH_API_KEY=...
```

For DeepSeek thinking-mode the harness keeps `reasoning_content` on
all turns — works out of the box, no extra config.

## 6. Start the three services

Two ways. Use the first on a machine that should keep running; the
second is for a checkout you are iterating on.

### As services (supported)

```sh
sudo bin/dportsv3 deploy install --dry-run   # shows every step, changes nothing
sudo bin/dportsv3 deploy install
```

That creates the `polytropos` account, installs three rc.d scripts, a
shared config, two credential stubs and a newsyslog entry, creates the
queue directories, and hands `$LOGS_ROOT` to the service account.

Installing from a checkout does **not** put the console scripts in
`/usr/local/bin`, so tell the scripts where the commands are — the
installer prints these exact lines when it detects that:

```sh
# /usr/local/etc/polytropos.conf
: ${polytropos_cmd:="/home/you/polytropos/bin/dportsv3"}
: ${polytropos_dev_env_cmd:="/home/you/polytropos/bin/dportsv3 dev-env"}
```

Put real credentials in `/usr/local/etc/polytropos/harness.env`, then:

```sh
# /etc/rc.conf
polytropos_artifact_store_enable="YES"
polytropos_tracker_enable="YES"
polytropos_runner_enable="YES"

service polytropos_artifact_store start
service polytropos_tracker start
service polytropos_runner start
```

Each service refuses to start rather than starting broken: if its
command does not run, or the runner cannot read the dev-env store, you
get an error at `service ... start` instead of a service that comes up
and fails every job. `deploy/README.md` has the full reference.

### In three shells (development)

```sh
# Shell A — artifact-store (receives bundles, writes state.db + blobs)
bin/dportsv3 artifact-store --logs-root $LOGS_ROOT

# Shell B — tracker (UI + read API + SSE)
DPORTSV3_STATE_DB=$STATE_DB \
DPORTSV3_ARTIFACT_ROOT=$ARTIFACT_ROOT \
  bin/dportsv3 tracker serve --port 8080 --bind 0.0.0.0

# Shell C — queue runner (claims jobs, runs triage/patch)
DPORTSV3_STATE_DB=$STATE_DB \
DPORTSV3_TRACKER_URL=http://127.0.0.1:8080 \
ARTIFACT_STORE_URL=http://127.0.0.1:8788 \
  bin/dportsv3 agent-queue-runner --queue-root $QUEUE_ROOT
```

Order doesn't matter; each is idempotent on schema init. Open
`http://localhost:8080/` in a browser and confirm the dashboard
loads (it'll be empty until a build runs).

`--bind` defaults to `0.0.0.0`, so the tracker is reachable from the
whole network. That is deliberate — the dashboard is meant to be opened
from your desk, not from the build box — but the tracker has **no
authentication**, and its API can start builds and spend LLM credit. Run
it on a trusted network, or pass `--bind 127.0.0.1` and reach it through
an ssh tunnel:

```sh
ssh -L 8080:localhost:8080 buildbox
```

artifact-store defaults to `127.0.0.1` instead: it only ever receives
bundles from hooks running on the same host.

## 7. Run a build

```sh
dsynth -p 2026Q2 -S -y build devel/known-failing-port
```

The dsynth profile must source `/etc/dsynth/dportsv3-hooks.conf` for
the hooks to fire. With hooks live, watch:

- `http://localhost:8080/target/@2026Q2` — the dsynth-progress view
  updates as builders move through phases. Failed ports appear with a
  red pill.
- `http://localhost:8080/agentic/bundles?target=@2026Q2` — failure
  bundles as they upload.
- `http://localhost:8080/agentic/jobs?target=@2026Q2&state=pending`
  — triage jobs as the runner picks them up.

## 8. Inspect a result

For a job that landed `rebuild_ok=true`:

```sh
curl -s http://localhost:8080/api/bundles/<bundle_id> | python3 -m json.tool
```

The `artifacts` array points at `analysis/triage.md`,
`analysis/patch_audit.json`, `analysis/rebuild_proof.json`, and
`analysis/changes.diff`. Each is streamable from
`/api/bundles/<id>/artifacts/<relpath>`.

The actual edits live in the dev-env's writable overlay:

```sh
ENV_DIR=$(bin/dportsv3 dev-env path myenv --writable)
git -C $ENV_DIR/work/DeltaPorts diff
```

If you accept the change, apply that diff in your own DeltaPorts
clone, review, sign, and commit there. The agentic loop never
touches your authoritative working tree.

## Running it day to day

Once the services are installed, everything below is the whole
operational surface.

| Want to | Do |
|---|---|
| See what is happening | `http://<host>:8080/` |
| Check a service | `service polytropos_runner status` |
| Stop one | `service polytropos_runner stop` |
| Restart after an upgrade | `service polytropos_runner restart` |
| Read process output | `/var/log/polytropos/<service>.log` |
| Read the runner's own log | `<queue-root>/runner.log` |
| Check an env | `bin/dportsv3 env-health <env>` |

**Restarting the runner is safe by design.** It takes an exclusive lock
on `<queue-root>/runner.lock`, so a second one refuses to start rather
than racing the first, and on start it cleans up what a dead runner left
behind: in-flight rows are marked dead, stranded job files move to
`failed/`, stale per-job worktrees are removed. A job that was mid-flight
is lost, not corrupted — re-enqueue it from the bundle.

**Upgrading.** Pull, then restart the services. From a checkout, run
`bin/dportsv3 --version` once first: the wrapper rebuilds the venv when
`pyproject.toml` changed, and the services deliberately refuse to do that
themselves at boot. If you skip it they will not start, and will say so.

### When a service will not run

The table below is about the services themselves. For a stack that is up
but behaving oddly — hooks not firing, triage 401s, budget exhausted —
see *Common stumbles*.

| Symptom | Cause |
|---|---|
| `service start` says the command does not run | Stale or missing venv. Run the command it names. |
| Runner exits 4 | Not running as root, or the dev-env store is unreadable. Every `dev-env` subcommand needs root. |
| Runner exits 3 | Another runner holds the queue lock. `service polytropos_runner status`. |
| Runner is up but claims nothing | No dev-env resolved. Select one in the tracker UI, or set `polytropos_runner_dev_env`. It holds rather than running with the dsynth gate unanswerable. |
| Runner paused, health broken | `bin/dportsv3 env-health <env>` reports the failing check and the fix. |
| Every job fails at a chroot step | `polytropos_dev_env_cmd` is wrong or its venv is stale. |
| Tracker 503s on the chat panel | No `DP_HARNESS_CHAT_MODEL`. That file is optional; the rest works without it. |

## Common stumbles

| Symptom | Fix |
|---|---|
| `dportsv3` says "missing DragonFly packages" | install the `pkg install` list from §1 |
| Hooks don't fire on failure | `bin/dportsv3 dev-env hooks-status myenv` for stale/missing; confirm the env's `/etc/dsynth/dportsv3-hooks.conf` is being sourced by the dsynth profile inside the chroot |
| Tracker 500s on artifact stream | `DPORTSV3_ARTIFACT_ROOT` doesn't match `--logs-root`/evidence on the artifact-store |
| Triage 401s | provider key wrong, or `DP_HARNESS_TRIAGE_API_BASE` needs to be set for non-default endpoints |
| Patch loop stops with `budget-exhausted` | check trust tier classification in `analysis/triage.md`; consider bumping the tier in `config/agentic-policy.json` |
| Runner sees no jobs after a failure | check `bundles` row exists in `state.db` (hook side); check classification didn't resolve to MANUAL |

## Upgrading

Restarting the services is covered under *Running it day to day*. The
part that is easy to forget is the hooks, which live inside each env and
do not move when you pull:

```sh
git pull
bin/dportsv3 dev-env hooks-status myenv     # are any hooks stale vs. the new source?
bin/dportsv3 dev-env hooks-install myenv    # re-copy (config preserved)
```

The tracker must be restarted for template and static-file changes —
uvicorn does not reload them. Artifact-store and runner read the state.db
schema at startup and the migrations are idempotent, so their order does
not matter.
