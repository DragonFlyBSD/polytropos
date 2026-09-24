"""Tool facts both the producer and the reader of a job's UI need.

``TAILABLE_TOOLS`` gates two halves that live in different packages: the
runner decides when to publish a build's last lines, and the tracker
decides which tool row to hang them on. Declared once here for the same
reason the artifact relpaths are (``common.artifacts``) -- with a copy on
each side, a tool added to one and not the other publishes a tail nothing
renders, or renders a row that never fills.
"""

from __future__ import annotations

#: Tools whose output is worth tailing while they run. Everything else
#: returns in milliseconds and has nothing to say in the meantime.
TAILABLE_TOOLS = frozenset({"dsynth_build", "dsynth_test"})
