"""Where the two source trees live inside a chroot env.

An env holds two checkouts, and the distinction between them is the whole
point of this module.

``PORTS_DIR`` is the ports tree. It is the agent's only legal edit surface:
the literal path is written into LLM prompt text and is enforced by the
worker guardrails, which reject writes under any other prefix. Moving it
would invalidate prompt behaviour and guardrail logic at the same time, so
it does not move — not for the repository split, not for the package rename.

``TOOL_DIR`` is this tool's own checkout. Before the split the tool shipped
as a subdirectory of the ports checkout and so had no path of its own; it
has one now. Naming it after the repository rather than after the command
means the pending ``dportsv3`` -> ``polytropos`` rename does not move it a
second time.

These are string constants rather than ``Path`` because most consumers
interpolate them into shell snippets that run inside the chroot. Callers
addressing the same trees from the host side join the ``*_RELATIVE`` forms
onto ``state.root_dir``.
"""

from __future__ import annotations

PORTS_DIR = "/work/DeltaPorts"
TOOL_DIR = "/work/polytropos"
FREEBSD_DIR = "/work/freebsd-ports"
LOCK_DIR = "/work/DPorts"

#: The wrapper the env invokes for every compose/reapply. It bootstraps its
#: own venv on first use, which is what the provisioning readiness probe
#: relies on.
#:
#: Note the ``bin/``: in the ports repo the wrapper sat at the repository
#: root, so the old path was ``/work/DeltaPorts/dportsv3``. In this repo that
#: name is taken by the Python package, and the wrapper lives in ``bin/``.
TOOL_BIN = f"{TOOL_DIR}/bin/dportsv3"

PORTS_RELATIVE = PORTS_DIR.removeprefix("/")
TOOL_RELATIVE = TOOL_DIR.removeprefix("/")
FREEBSD_RELATIVE = FREEBSD_DIR.removeprefix("/")
LOCK_RELATIVE = LOCK_DIR.removeprefix("/")

#: The generator venv, relative to the env root. It lives inside the tool
#: checkout because ``bin/dportsv3`` puts it there.
TOOL_VENV_RELATIVE = f"{TOOL_RELATIVE}/.venv"
