"""Where a builder talks to the tracker. One knob, one default.

The tracker is a single service: the UI, the read API and the ``/v1/``
ingest surface are all on the same base URL, so there is exactly one
address a builder needs and exactly one variable that sets it.

Stdlib only. ``artifact_store_client`` imports this and runs from dsynth
hooks inside a chroot, where nothing beyond the interpreter is
guaranteed.

Note what is deliberately NOT here: the dsynth hooks' own
``ARTIFACT_STORE_URL``. That one is loopback permanently and on purpose
— a hook posts to whatever is listening on this host, which today is the
tracker and on a remote builder will be a local forwarder holding the
host's credential. Keeping it separate is what lets an env's hooks
config stay identical whether the tracker is local or three hops away.
"""

from __future__ import annotations

import os

#: Used when ``$DPORTSV3_TRACKER_URL`` says nothing. Loopback because the
#: single-host deployment is still the common one; it is a default rather
#: than a literal precisely so a remote builder can point elsewhere
#: without touching code.
DEFAULT_TRACKER_URL = "http://127.0.0.1:8080"

TRACKER_URL_ENV = "DPORTSV3_TRACKER_URL"


def tracker_url() -> str:
    """The tracker base URL, without a trailing slash.

    Callers join paths onto this, so a trailing slash produces ``//v1``
    and a 404 that looks like a missing endpoint rather than a config
    typo. Stripped once, here.
    """
    return os.environ.get(TRACKER_URL_ENV, DEFAULT_TRACKER_URL).rstrip("/")
