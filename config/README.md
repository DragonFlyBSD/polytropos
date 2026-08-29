# Local configuration

This directory is where operator-edited config lives. It ships empty on
purpose: every setting has a default in the code, so a fresh clone and a
`pip install` both work with no setup.

To change a setting, write a file here and edit it. Everything in the
generated sample is commented out and shows its default, so uncomment
only what you mean to change — the file then records your decisions
rather than pinning a copy of every default we might later improve:

```sh
dportsv3 config sample > config/polytropos.toml
```

`.gitignore` covers the copies, so local edits are never committed.

## How the tool finds this directory

Through `$DPORTSV3_CONFIG_DIR`, and only through it. `bin/dportsv3` sets that
variable to this directory when the operator has not set it themselves, which
is what makes a plain checkout behave like a configured install.

Nothing in the Python packages searches for a surrounding repository. It used
to — several modules walked a fixed number of parent directories up to find
`config/` — and that broke the moment the tool moved out of the DeltaPorts
checkout. `dportsv3/paths.py` is the single resolver now.

Running the tool without the wrapper (a bare `python -m dportsv3`, a service
unit, a container) means setting `DPORTSV3_CONFIG_DIR` yourself, or accepting
the packaged defaults.

## Files

| File | Holds | Missing? |
|---|---|---|
| `polytropos.toml` | every setting: paths, tracker, runner, models, policy, delivery | everything is at its default |
| `secrets/triage.key` | the triage API key | triage cannot call a model |
| `secrets/patch.key` | the patch API key | falls back to the triage key |
| `secrets/chat.key` | the fix-chat API key | chat is disabled anyway unless `llm.chat.model` is set |
| `secrets/delivery.token` | the forge credential | delivery reports it needs a token |

There used to be five files in four formats here and in `/usr/local/etc`,
plus 69 environment variables — 39 of which the code read and nothing
shipped ever mentioned, so on a packaged install there was nowhere to put
a value for them at all. `dportsv3/settings.py` is the single table now:
the value, the generated sample, `config show`'s provenance column and
the warning about an unrecognised key all come from it, so a setting the
code reads and no file mentions is no longer possible to write.

## Seeing what is in effect

```sh
dportsv3 config show            # every value, and where it came from
dportsv3 config show --changed  # only what is not at its default
dportsv3 config check           # validate without starting anything
dportsv3 config get paths.queue_root
```

The source column is the point. "It is 3" does not help someone whose
file says 5; "3, from the default" tells them the file is not being read.

## Coming from the old layout

```sh
dportsv3 config migrate --dry-run   # then without --dry-run
```

Reads `polytropos.conf`, `harness.env` and `chat.env`, writes
`polytropos.toml` and the secret files, and leaves the originals alone.
Anything it does not recognise is named rather than dropped.

## Credentials

One value per file under `secrets/`, named by a `*_file` setting. A TOML
syntax error then cannot take out the whole credential set, rotation is a
single write, and the mode can follow whichever service reads it — which
is the part that matters:

| File | Mode | Reader |
|---|---|---|
| `secrets/triage.key`, `secrets/patch.key` | `0600 root` | the queue runner, which is root |
| `secrets/chat.key`, `secrets/delivery.token` | `0640 root:<group>` | the tracker, which is not |

`delivery.token` has no template and never will — it is a secret, so the
only sensible thing to ship is nothing. Supply it as a file, or via
`$DPORTSV3_DELIVERY_TOKEN`.

Delivery runs inside the **tracker** (`tracker/routes/bundle_actions.py`
on Accept, `tracker/delivery_sync.py` when reconciling merges) and never
in the queue runner, so a root-only `0400` token is unreadable by the
only process that wants it. `delivery.clone_dir` has to be writable by
that same account. The tracker checks both at startup.

## Environment variables

Seven, and each had to be argued for: a value earns one only if it cannot
be known when the file is written.

| Variable | Why it survives |
|---|---|
| `$DPORTSV3_CONFIG_DIR` | names this directory, so it cannot live in it |
| `$DPORTS_DELTA_ROOT` | already paired with `--delta-root` |
| `$DPORTSV3_TRACKER_URL` | has to cross into a build chroot, where nothing is importable |
| `$DPORTSV3_DELIVERY_TOKEN` | a secret store may inject it |
| `$DP_HARNESS_DUMP_SESSION` | a per-run debug switch |
| `$DP_HARNESS_LLM_BACKEND` | documented escape hatch, temporary (poly-r1g) |
| `$DP_HARNESS_{TRIAGE,PATCH}_REASONING` | temporary, tied to a litellm version (poly-r1g) |

Separately, five are inter-process arguments that happen to be spelled as
environment — `$DPORTSV3_CMD`, `$DPORTS_DEV_ENV_CMD`,
`$DPORTS_DEV_TOOL_ROOT`, `$DPORTSV3_NO_BOOTSTRAP`,
`$DPORTSV3_TRACKER_TARGET` — a parent telling a child about one exec.
They would be better as command-line flags, but they are not
configuration.

`tests/test_settings_unification.py` enforces this: a variable that is
neither in a schema table nor on that short list fails the suite.
