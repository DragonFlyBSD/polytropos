"""A schema-driven reader for one TOML settings file.

This is the engine, and it knows nothing about ports, builds or agents.
Each package declares its own table of :class:`Setting` and asks this
module to resolve it. That split is not stylistic — it is the only shape
that works here. ``dportsv3`` imports ``dports_dev_env`` and nothing
imports back, so anything both distributions need has to live on this
side of that arrow. Putting the engine in the generator and importing it
from the dev-env would invert the dependency and reintroduce exactly the
coupling ``dportsv3/paths.py`` exists to prevent.

The table is the single source of truth, and everything else is derived
from it:

* the value a caller gets, with its type and default applied;
* where that value came from, so ``config show`` can say;
* the commented sample file, so the shipped sample cannot drift from
  the code that reads it;
* the list of recognised settings, so an unknown key in an operator's
  file is a warning rather than a silent no-op.

**Environment variables are opt-in per setting**, via ``env=``. That
restraint is the point of the whole exercise. A blanket "env overrides
file" layer is three lines and recreates the problem it was meant to
solve: this project reached 67 environment variables that way, 39 of
which no sample or script mentioned at all, because adding one cost
nothing and documenting one was a separate act of will. Here a variable
does not exist until someone writes it in the table next to the setting
it overrides, which is also where the reader will find it.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


__all__ = [
    "ConfigError",
    "Setting",
    "Schema",
    "Resolved",
]


class ConfigError(Exception):
    """A settings file could not be read, or holds an unusable value."""


_MISSING = object()


@dataclass(frozen=True)
class Setting:
    """One configurable value.

    ``path`` is the dotted location in the TOML (``llm.patch.model`` is
    ``[llm.patch] model = ...``). ``kind`` drives both coercion and how
    the sample renders the default.

    ``env`` names an environment variable that overrides the file. Leave
    it None unless the value genuinely cannot be known when the file is
    written — a per-run debug switch, a credential injected by a secret
    store, an escape hatch documented as temporary. "It might be handy"
    is how the previous surface grew.

    ``secret`` marks a value that names a *file* holding a credential
    rather than the credential itself. The sample writes it out with the
    mode the reader needs, and ``config show`` never prints the contents.
    """
    path: str
    kind: str  # str | int | float | bool | path | list | table
    default: Any
    help: str
    env: str | None = None
    secret: bool = False

    @property
    def section(self) -> str:
        head, _, _tail = self.path.rpartition(".")
        return head

    @property
    def key(self) -> str:
        return self.path.rpartition(".")[2]


@dataclass(frozen=True)
class Resolved:
    """A value and where it came from, which is half of what an operator
    asking "why is it doing that" needs."""
    setting: Setting
    value: Any
    source: str  # "default" | "file" | "$VAR"

    @property
    def overridden(self) -> bool:
        return self.source != "default"


def _coerce(setting: Setting, raw: Any, where: str) -> Any:
    """Turn a TOML or environment value into the declared type.

    Environment values arrive as strings, so every kind has to accept
    one; TOML values arrive already typed, and are passed through when
    they already match. A wrong type is an error naming the setting,
    never a silent fallback to the default — a typo that quietly reverts
    to a default is the failure mode this whole change exists to remove.
    """
    kind = setting.kind
    try:
        if kind == "str":
            return str(raw)
        if kind == "path":
            # An empty value means UNSET, and has to stay falsy. Path("")
            # normalises to Path("."), so returning it would silently turn
            # "no policy file configured" into "read the current
            # directory" — which is exactly the shape of bug this file is
            # meant to make impossible.
            text = str(raw).strip()
            return Path(text).expanduser() if text else None
        if kind == "int":
            return int(str(raw).strip())
        if kind == "float":
            return float(str(raw).strip())
        if kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"{raw!r} is not a boolean")
        if kind == "list":
            if isinstance(raw, (list, tuple)):
                return [str(x) for x in raw]
            # From the environment a list is whitespace- or comma-separated,
            # matching how the dev-env's package lists have always been set.
            return [w for w in str(raw).replace(",", " ").split() if w]
        if kind == "table":
            if isinstance(raw, dict):
                return raw
            raise ValueError("expected a TOML table")
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{setting.path}: {exc} (from {where})"
        ) from exc
    raise ConfigError(f"{setting.path}: unknown kind {kind!r}")


def _dig(data: dict, path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


#: Last-resort config directory, for an install whose entry point tells
#: us nothing. A documented default rather than the mechanism — the
#: derivation below is what actually follows a non-default LOCALBASE.
DEFAULT_CONFIG_DIR = Path("/usr/local/etc/polytropos")

#: Where the config dir sits relative to the directory holding the
#: installed entry point: ``<prefix>/bin/dportsv3`` -> ``<prefix>/etc/
#: polytropos``.
_CONFIG_DIR_FROM_BINDIR = Path("..") / "etc" / "polytropos"


def _config_dir_beside_entry_point() -> Path | None:
    """``<prefix>/etc/polytropos``, derived from where the running entry
    point lives, or None when that yields nothing usable.

    This is the one derivation that follows LOCALBASE without being
    told: a port built with ``PREFIX=/opt/pkg`` installs the binary at
    ``/opt/pkg/bin/dportsv3``, so the answer moves with it and there is
    nothing for an operator to keep in sync. Supervisor resolves its own
    config the same way, and lists the absolute path last.

    ``argv[0]`` is deliberately NOT resolved. ``deploy`` links
    ``<prefix>/bin/dportsv3`` at ``<prefix>/lib/polytropos/bin/dportsv3``,
    so following the symlink lands in the venv and derives the wrong
    prefix. The unresolved parent is the whole point.

    Yes, this walks upward, which is the shape the module docstring
    forbids. The distinction: it walks from the *installed entry point*,
    a location the packaging system controls, not from ``__file__``
    inside site-packages guessing at a surrounding repository — which is
    the walk that broke when this tool moved out of DeltaPorts. The
    result is used only if it exists, so a bad guess costs nothing.
    """
    argv0 = (sys.argv[0] or "").strip()
    if not argv0:
        return None
    try:
        # abspath normalises without resolving symlinks; see above.
        bindir = Path(os.path.abspath(argv0)).parent
    except (OSError, ValueError):
        return None
    return Path(os.path.normpath(bindir / _CONFIG_DIR_FROM_BINDIR))


def config_dir() -> Path | None:
    """The operator's live config directory, or None when nothing has one.

    An ordered search, first match wins, which is what every comparable
    tool does and what this used to lack: ``$DPORTSV3_CONFIG_DIR`` was
    the only entry, so an unset variable meant "no config exists"
    anywhere rather than "look in the usual places", and a file sitting
    in the conventional location was read by nobody and warned about by
    nothing.

    1. ``$DPORTSV3_CONFIG_DIR`` — explicit, and still wins. ``bin/
       dportsv3`` sets it to the checkout's ``config/``, so a checkout
       keeps behaving exactly as before.
    2. ``<prefix>/etc/polytropos`` beside the installed entry point.
    3. ``/usr/local/etc/polytropos``, the documented default.
    4. None — every setting then uses its table default.

    2 and 3 must exist to be chosen: a derivation that misses (running
    from a source tree, ``python -m``) falls through instead of naming a
    directory that isn't there.
    """
    raw = os.environ.get("DPORTSV3_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw)
    derived = _config_dir_beside_entry_point()
    for candidate in (derived, DEFAULT_CONFIG_DIR):
        if candidate is not None and candidate.is_dir():
            return candidate
    return None


class Schema:
    """A named table of settings, and the reader for it.

    Construct one per package. Several may share a file: each resolves
    only the paths it declares, and :meth:`unknown_keys` is what notices
    a key nobody claims.
    """

    def __init__(self, settings: list[Setting], *, name: str = "config"):
        self.name = name
        self._settings = list(settings)
        by_path: dict[str, Setting] = {}
        for s in self._settings:
            if s.path in by_path:
                raise ConfigError(f"duplicate setting {s.path!r} in {name}")
            by_path[s.path] = s
        self._by_path = by_path
        self._data: dict[str, Any] = {}
        self._file: Path | None = None
        self._loaded = False

    # --- loading ------------------------------------------------------

    def load(self, path: Path | str | None) -> None:
        """Read the file, or accept that there isn't one.

        A missing file is not an error: every setting has a default, so
        an install with no file is a working install with stock
        behaviour. A file that exists and does not parse *is* an error,
        because someone wrote it and meant something by it.
        """
        self._data = {}
        self._file = None
        self._loaded = True
        if path is None:
            return
        p = Path(path)
        if not p.is_file():
            return
        try:
            self._data = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{p}: not valid TOML: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{p}: cannot be read: {exc}") from exc
        self._file = p

    def load_from(self, data: dict[str, Any]) -> None:
        """Load an already-parsed table. For tests and for callers that
        hold the document for another reason."""
        self._data = dict(data)
        self._file = None
        self._loaded = True

    @property
    def file(self) -> Path | None:
        return self._file

    @property
    def loaded(self) -> bool:
        return self._loaded

    # --- reading ------------------------------------------------------

    def resolve(self, path: str, *,
                env: dict[str, str] | None = None) -> Resolved:
        """One setting's value and its provenance.

        Order: the setting's own environment variable if it declares one
        and it is set to a non-empty value, then the file, then the
        declared default.

        The environment comes first because that is what an override
        means, and an empty value is treated as unset so that clearing a
        variable in a shell file falls back rather than forcing an empty
        string on a caller that wanted a path.
        """
        setting = self._by_path.get(path)
        if setting is None:
            raise ConfigError(f"{self.name}: no such setting {path!r}")
        env = os.environ if env is None else env  # type: ignore[assignment]

        if setting.env:
            raw = (env.get(setting.env) or "").strip()
            if raw:
                return Resolved(setting, _coerce(setting, raw, f"${setting.env}"),
                                f"${setting.env}")

        raw = _dig(self._data, setting.path)
        if raw is not _MISSING:
            where = str(self._file) if self._file else "the settings file"
            return Resolved(setting, _coerce(setting, raw, where), "file")

        return Resolved(setting, setting.default, "default")

    def get(self, path: str, *, env: dict[str, str] | None = None) -> Any:
        return self.resolve(path, env=env).value

    def all_resolved(self, *,
                     env: dict[str, str] | None = None) -> list[Resolved]:
        return [self.resolve(s.path, env=env) for s in self._settings]

    @property
    def settings(self) -> list[Setting]:
        return list(self._settings)

    def has(self, path: str) -> bool:
        return path in self._by_path

    # --- telling the operator what is wrong ---------------------------

    def unknown_keys(self, *, claimed: set[str] | None = None) -> list[str]:
        """Dotted paths present in the file that no setting declares.

        A misspelled key is otherwise perfectly silent: the file parses,
        the setting keeps its default, and nothing anywhere says why the
        change had no effect. ``claimed`` lets a caller pass the paths
        another schema owns, so sharing one file does not make every key
        look unknown to everybody.
        """
        known = set(self._by_path) | (claimed or set())
        # A setting of kind "table" owns everything beneath it.
        tables = [s.path for s in self._settings if s.kind == "table"]
        out: list[str] = []
        for dotted in _walk(self._data):
            if dotted in known:
                continue
            if any(dotted == t or dotted.startswith(t + ".") for t in tables):
                continue
            if any(k.startswith(dotted + ".") for k in known):
                continue  # an intermediate section, not a leaf
            out.append(dotted)
        return sorted(out)


def _walk(data: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        out.append(dotted)
        out.extend(_walk(value, dotted))
    return out


# --- rendering the sample ---------------------------------------------------

def _toml_value(setting: Setting, value: Any) -> str:
    if value is None:
        # A None default means UNSET. Rendering it as the *string*
        # "None" would hand an operator who uncomments the line a path
        # named None — and _coerce reads "" back as None, so the empty
        # string is both honest and round-trips.
        return '""'
    if setting.kind == "bool":
        return "true" if value else "false"
    if setting.kind in ("int", "float"):
        return str(value)
    if setting.kind == "list":
        inner = ", ".join(f'"{v}"' for v in (value or []))
        return f"[{inner}]"
    if setting.kind == "table":
        return "{ }"
    return f'"{value}"'


def render_sample(
    schema: Schema, *,
    header: str = "",
    section_help: dict[str, str] | None = None,
    include: Callable[[Setting], bool] | None = None,
) -> str:
    """Write the commented sample this schema describes.

    Generated rather than maintained, which is the only way a sample
    stays true: the previous arrangement had 39 settings the code read
    and no shipped file mentioned, because the file was written by hand
    and nobody was obliged to update it.

    Every line is commented out. An operator uncomments what they mean
    to change, so the file on disk records their decisions rather than
    restating every default — and a default we later improve reaches
    them instead of being pinned by a copy of its old value.
    """
    section_help = section_help or {}
    out: list[str] = []
    if header:
        out.append(header.rstrip("\n"))
        out.append("")

    current = object()
    for setting in schema.settings:
        if include is not None and not include(setting):
            continue
        if setting.section != current:
            current = setting.section
            out.append("")
            if setting.section:
                out.append(f"[{setting.section}]")
            blurb = section_help.get(str(setting.section))
            if blurb:
                out.extend(f"# {line}" for line in blurb.strip().splitlines())
        for line in setting.help.strip().splitlines():
            # rstrip so a blank line in a help string is "#", not "# ".
            out.append(f"# {line}".rstrip())
        if setting.env:
            out.append(f"# Override for one run with ${setting.env}.")
        out.append(f"#{setting.key} = {_toml_value(setting, setting.default)}")
        out.append("")
    return "\n".join(out).strip() + "\n"
