"""What a dport's ``newport/`` may hold (poly-7pwa.35).

``newport/`` is the dport's origin, read-only like a FreeBSD port
directory: the Makefile and its includes, ``distinfo``, ``pkg-*`` and
``files/``. ``files/patch-*`` is the port's own, DragonFly-specific or
not: that is the author's choice. It never holds compat content. A
``dragonfly/`` or a ``Makefile.DragonFly*`` is overlay content and goes
beside ``newport/``, as it would beside a FreeBSD port.

Only the top level is read: the ports framework takes ``Makefile.${OPSYS}``
from MASTERDIR and the DragonFly patches from ``${PATCHDIR:H}/dragonfly``,
so nothing deeper acts as either.

One rule, read by everything that has to agree about it: compose warns on
it, and the agent's overlay assessment will not call such a dport
converted.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

#: The compose warning for a dport whose newport/ holds compat content.
WARNING_CODE = "W_COMPOSE_DPORT_NEWPORT_COMPAT"


def compat_entries(names: Iterable[str]) -> list[str]:
    """The compat content among ``newport/``'s top-level entry names, as
    port-relative paths, sorted. A directory name may end in ``/``."""
    found = []
    for name in names:
        bare = name.rstrip("/")
        if bare == "dragonfly":
            found.append("newport/dragonfly/")
        elif bare.startswith("Makefile.DragonFly"):
            found.append(f"newport/{bare}")
    return sorted(found)


def newport_compat_content(newport_dir: Path) -> list[str]:
    """The compat content in ``newport_dir``, or [] when it has none or
    does not exist."""
    if not newport_dir.is_dir():
        return []
    return compat_entries(entry.name for entry in newport_dir.iterdir())


def warning_message(origin: str, found: list[str]) -> str:
    """The compose warning's text for ``origin``."""
    return (
        f"{origin}: newport/ holds compat content ({', '.join(found)}); "
        f"newport/ is the dport's origin and holds FreeBSD-port content "
        f"only, so it belongs beside newport/ (poly-7pwa.35)"
    )
