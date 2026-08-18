# Local configuration

This directory is where operator-edited config lives. It ships empty on
purpose: the tracked templates are package data, under
`dportsv3/data/config/`, so a fresh clone and a `pip install` both work with
no setup.

To change a setting, copy the template here and edit the copy:

```sh
cp dportsv3/data/config/agentic-policy.json.sample config/agentic-policy.json
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

| File | Read by | Missing? |
|---|---|---|
| `agentic-policy.json` | the agent runner: (classification, confidence) → tier + budget | falls back to the packaged sample |
| `delivery.toml` | upstream delivery: which provider, which repo | delivery stays disabled |
| `delivery.token` | the GitHub delivery provider | delivery reports it needs a token |

`delivery.token` has no template and never will — it is a secret, so the only
sensible thing to ship is nothing. Supply it as a file here or via
`$DPORTSV3_DELIVERY_TOKEN`.
