"""Where the tool finds its inputs.

Every input the tool needs falls into one of two kinds, and the difference
decides how it is found:

**Tool-owned data** — agent playbooks, the dops quick reference, config
templates. These belong to this package and ship with it, so they are located
*co-located with the module that uses them* (``Path(__file__).parent / ...``).
That resolves correctly whether the tool is run from a checkout or installed
into site-packages, and it never depends on what lives above the package.

**Operator- and site-owned inputs** — the live config directory, the ports
tree. These cannot ship with the package because they differ per install, so
they are named explicitly: a CLI flag, or an environment variable, or a
packaged default that is documented as a default.

What is *not* allowed is the third thing this module exists to remove:
``Path(__file__).parents[N]`` walking up out of the package to guess at a
surrounding repository. That worked only while the tool lived inside the
DeltaPorts checkout at one exact depth. Once it moved, those walks either
resolved to a path outside the repo entirely or — worse — returned ``None``
and let the caller carry on without the data it asked for.

Resolution order, most specific first:

1. an explicit argument passed by the caller (usually a CLI flag);
2. the input's own environment variable, if it has one;
3. ``$DPORTSV3_CONFIG_DIR`` for config files;
4. the template bundled with the package.

``bin/dportsv3`` sets ``DPORTSV3_CONFIG_DIR`` to the checkout's ``config/``
when the operator has not set it. That wrapper is the one component entitled
to know the repository layout — it is part of the repository — which is what
keeps that knowledge out of the package itself.
"""

from __future__ import annotations

import os
from pathlib import Path

# This package's own directory. Not a search root: everything below is
# addressed *downward* from here, never upward.
_PKG = Path(__file__).resolve().parent

#: Config templates shipped with the package. Each is named ``<name>.sample``
#: and serves as the last-resort default for ``config_file()``.
BUNDLED_CONFIG_DIR = _PKG / "data" / "config"

#: Agent playbooks, read at runtime to select failure-repair patterns.
AGENT_PLAYBOOKS_DIR = _PKG / "agent" / "playbooks"


class MissingInput(RuntimeError):
    """A required input could not be resolved.

    Raised instead of returning ``None`` so that a misconfigured install
    fails where the problem is, naming what was missing and where it was
    looked for. Silent degradation is the specific failure this module was
    written to prevent.
    """


def config_dir() -> Path | None:
    """The operator's live config directory, or None if unset.

    Set by ``$DPORTSV3_CONFIG_DIR``; ``bin/dportsv3`` points it at the
    checkout's ``config/`` when the operator has not.
    """
    raw = os.environ.get("DPORTSV3_CONFIG_DIR", "").strip()
    return Path(raw) if raw else None


def config_file(name: str) -> Path | None:
    """Resolve one config file by name, e.g. ``agentic-policy.json``.

    Order: the live copy in ``$DPORTSV3_CONFIG_DIR``, then a ``.sample``
    alongside it, then the template bundled with the package. Returns None
    when even the bundled template is absent — callers that cannot proceed
    without it should use `require_config_file` instead.

    The sample/live split is deliberate: ``<name>.sample`` is tracked and
    ``<name>`` is gitignored, so an operator edits a local copy while a fresh
    checkout still works with no setup.
    """
    for candidate in _config_candidates(name):
        if candidate.is_file():
            return candidate
    return None


def require_config_file(name: str) -> Path:
    """`config_file`, but raise `MissingInput` rather than return None."""
    found = config_file(name)
    if found is not None:
        return found
    looked = "\n  ".join(str(p) for p in _config_candidates(name))
    raise MissingInput(
        f"required config file {name!r} was not found. Looked in:\n  {looked}\n"
        f"Set DPORTSV3_CONFIG_DIR to the directory holding it."
    )


def _config_candidates(name: str) -> list[Path]:
    candidates: list[Path] = []
    live = config_dir()
    if live is not None:
        candidates.append(live / name)
        candidates.append(live / f"{name}.sample")
    candidates.append(BUNDLED_CONFIG_DIR / f"{name}.sample")
    return candidates


def resolve_delta_root(explicit: Path | str | None = None) -> Path:
    """The DeltaPorts checkout — the ports tree this tool reads.

    Order: an explicit ``--delta-root`` / ``--root``, then
    ``$DPORTS_DELTA_ROOT``, then the current directory.

    The result is checked for a ``ports/`` or ``special/`` subdirectory before
    being returned, and that check is the point of this function. The current
    directory used to be a bare default, which was fine while the tool ran
    from inside the ports checkout and silently wrong the moment it did not:
    composing against a tree with neither directory produces an empty result
    rather than an error, and an empty result is hard to tell apart from
    "nothing needed doing".

    Either directory counts, not both — composing only ``special/`` is a
    supported operation, so requiring ``ports/`` would reject valid roots.
    """
    if explicit is not None:
        root, source = Path(explicit), "--delta-root"
    else:
        env_value = os.environ.get("DPORTS_DELTA_ROOT", "").strip()
        if env_value:
            root, source = Path(env_value), "$DPORTS_DELTA_ROOT"
        else:
            root, source = Path.cwd(), "the current directory"

    root = root.expanduser()
    if not (root / "ports").is_dir() and not (root / "special").is_dir():
        raise MissingInput(
            f"{root} does not look like a DeltaPorts checkout — it has "
            f"neither a ports/ nor a special/ directory. (Taken from "
            f"{source}.) Pass --delta-root, or set $DPORTS_DELTA_ROOT."
        )
    return root


def require_dir(path: Path, what: str) -> Path:
    """Assert a directory of package data is present.

    Package data can go missing for real — a partial install, or a build
    backend that was never told to include the directory. That should stop
    the caller, not quietly reduce what it can do.
    """
    if not path.is_dir():
        raise MissingInput(
            f"{what} is missing at {path}. It ships with the dportsv3 package; "
            f"a partial or broken install is the usual cause."
        )
    return path
