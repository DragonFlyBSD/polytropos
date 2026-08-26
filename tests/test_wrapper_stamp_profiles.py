"""A tracker-stamped venv can run the base-profile commands.

bin/dportsv3 records one install profile in its stamp, so bootstrapping
`tracker serve` writes "tracker:" and `artifact-store` then sees a
mismatch. Under DPORTSV3_NO_BOOTSTRAP=1, which is how the rc.d services
run, that is a refusal to start against a venv that can run it perfectly
well.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "dportsv3"


def satisfied(recorded: str, wanted: str, profile: str) -> bool:
    """Run the wrapper's own predicate, lifted out of the script."""
    src = WRAPPER.read_text()
    start = src.index("stamp_satisfied() {")
    end = src.index("}", src.index("return 1", start)) + 1
    fn = src[start:end]
    script = (f'INSTALL_PROFILE={profile}\n{fn}\n'
              f'if stamp_satisfied "{recorded}" "{wanted}"; then echo YES; '
              f'else echo NO; fi\n')
    out = subprocess.run(["/bin/sh", "-c", script],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip() == "YES"


D = "abc123+def456"


def test_tracker_stamp_satisfies_a_base_request() -> None:
    """The superset direction. This is the bug: without it the two HTTP
    services evict each other on a checkout."""
    assert satisfied(f"tracker:{D}", f"base:{D}", "base")


def test_base_stamp_does_not_satisfy_tracker() -> None:
    """One-way only: base has no uvicorn, so `tracker serve` must still
    reinstall."""
    assert not satisfied(f"base:{D}", f"tracker:{D}", "tracker")


def test_a_genuinely_stale_digest_is_still_stale() -> None:
    """The relaxation is about the profile, never the content hash."""
    assert not satisfied("base:OLD+OLD", f"base:{D}", "base")
    assert not satisfied("tracker:OLD+OLD", f"base:{D}", "base")


def test_exact_match_is_satisfied() -> None:
    assert satisfied(f"base:{D}", f"base:{D}", "base")
    assert satisfied(f"tracker:{D}", f"tracker:{D}", "tracker")


def test_a_changed_dev_env_half_still_invalidates() -> None:
    """The stamp is <profile>:<generator digest>+<dev-env digest>; a
    dev-env change has to invalidate the generator venv too."""
    assert not satisfied("tracker:abc123+OLD", f"base:{D}", "base")


def test_the_wrapper_uses_the_predicate() -> None:
    """Guards against the comparison being inlined again."""
    src = WRAPPER.read_text()
    assert "stamp_satisfied " in src
    assert '"$(cat "$STAMP_FILE")" != "$STAMP_VALUE"' not in src
