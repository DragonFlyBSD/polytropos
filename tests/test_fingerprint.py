"""WS2 — the canonical error fingerprint (issue-identity primitive).

Two properties matter:

- **normalization** collapses incidental run-to-run noise (tmpdir names,
  line/column numbers, hex addresses, PIDs, timestamps) so "the same
  failure" hashes to one value, while genuinely different errors stay
  distinct; and
- the **ingest path** (``ArtifactStore.upsert_run_bundle``) stamps that
  fingerprint onto the occurrence the moment it is born, whether the
  caller ships raw ``errors_text`` or a precomputed ``error_signature``,
  and never nulls a real signature on a later signatureless update.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from dportsv3.fingerprint import compute_fingerprint, normalize_error
from dportsv3.db.schema import init_db


# --- normalization ---------------------------------------------------------


def test_empty_and_blank_have_no_fingerprint():
    for text in (None, "", "   \n\n\t\n"):
        assert normalize_error(text) is None
        assert compute_fingerprint(text) is None


def test_first_nonempty_line_only():
    # Leading blank lines are skipped; trailing lines are irrelevant.
    a = compute_fingerprint("\n\ncc: error: foo\ntrailing junk\n")
    b = compute_fingerprint("cc: error: foo\ndifferent trailing\n")
    assert a == b


def test_distinct_errors_stay_distinct():
    assert compute_fingerprint("cc: error: foo\n") != compute_fingerprint("cc: error: bar\n")


def test_digest_shape_is_16_hex():
    fp = compute_fingerprint("cc: error: foo\n")
    assert len(fp) == 16
    int(fp, 16)  # parses as hex


def test_tmpdir_path_and_linecol_are_normalized_away():
    """Same root cause under different build dirs / line numbers → one
    fingerprint. The origin lives in the issue key, so collapsing the
    directory prefix to a basename loses nothing."""
    a = "/tmp/portbuild.a1b2c3/work/foo.c:145:23: error: 'X' undeclared\n"
    b = "/var/tmp/portbuild.ZZZZ/work/foo.c:900:2: error: 'X' undeclared\n"
    assert normalize_error(a) == normalize_error(b) == "foo.c: error: 'X' undeclared"
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_hex_addresses_and_pids_are_normalized_away():
    a = "segfault at 0x7f3a1c00 ip 0x400abc sp 0x7ffd; child process 48213 died\n"
    b = "segfault at 0xdeadbeef ip 0x401fff sp 0x7ffe; child process 5551 died\n"
    assert compute_fingerprint(a) == compute_fingerprint(b)
    assert "0xADDR" in normalize_error(a)


def test_digits_embedded_in_identifiers_survive():
    """Version-bearing identifiers (python312, libssl3) are not PIDs and
    must not be blurred, or unrelated ports would collide."""
    assert compute_fingerprint("ImportError: python312 module missing\n") != \
        compute_fingerprint("ImportError: python311 module missing\n")


# --- distilled errors.txt (poly-awz) ---------------------------------------

def _distilled(*candidates: str, tail: str = "some tail line") -> str:
    """One errors.txt in the shape distill_log() actually writes."""
    body = "\n".join(candidates)
    return (
        "== Summary ==\n"
        "logfile: /work/dsynth/logs/devel___llvm19.log\n"
        "\n"
        "== First error candidates (max 60 matches) ==\n"
        f"{body}\n"
        "\n"
        "== Error blocks (context +/-2, truncated later) ==\n"
        "12:some context\n"
        "\n"
        "== Tail (last 200 lines) ==\n"
        f"{tail}\n"
    )


def test_banner_is_not_the_fingerprint():
    """The distiller writes '== Summary ==' first, so the old
    first-non-empty-line rule hashed a constant for every failure in the
    tree: 28 bundles over 19 origins shared one signature."""
    norm = normalize_error(_distilled("1234:configure: error: no acceptable C compiler"))
    assert norm == "configure: error: no acceptable C compiler"
    assert "Summary" not in norm


def test_two_failures_in_one_origin_stay_distinct():
    """The property poly-awz says is broken: llvm19@default failed at
    configure and llvm19@lite at build, and both fingerprinted alike."""
    configure = _distilled("1234:configure: error: no acceptable C compiler")
    build = _distilled("87:ld: error: undefined symbol: backtrace_symbols_fd")
    assert compute_fingerprint(configure) != compute_fingerprint(build)


def test_grep_line_number_prefix_is_stripped():
    """grep -n prefixes each hit with its line number in the source log.
    _LONG_INT only reduces 3+ digit runs, so without stripping, the same
    failure at line 87 and line 1234 would split into two signatures."""
    near = _distilled("87:configure: error: no acceptable C compiler")
    far = _distilled("1234:configure: error: no acceptable C compiler")
    assert normalize_error(near) == normalize_error(far)
    assert compute_fingerprint(near) == compute_fingerprint(far)


def test_tail_noise_does_not_reach_the_signature():
    """Only the candidate section is load-bearing; the tail varies run to
    run and must not move the fingerprint."""
    a = _distilled("5:CMake Error: could not find OpenSSL", tail="gmake[2]: *** [all] Error 2")
    b = _distilled("5:CMake Error: could not find OpenSSL", tail="totally different tail")
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_distilled_without_candidates_has_no_fingerprint():
    """A phase the distiller has no pattern for (fetch, patch) yields an
    empty candidate section. That is 'no root cause', not a shared
    constant -- issue_key collapses these per (target, origin)."""
    empty = (
        "== Summary ==\n"
        "logfile: /work/dsynth/logs/lang___rust.log\n"
        "\n"
        "== First error candidates (max 60 matches) ==\n"
        "\n"
        "== Error blocks (context +/-2, truncated later) ==\n"
        "\n"
        "== Tail (last 200 lines) ==\n"
        "=> Fetching rustc-1.85.1-src.tar.xz\n"
    )
    assert normalize_error(empty) is None
    assert compute_fingerprint(empty) is None


def test_missing_log_keyvalue_file_still_fingerprints():
    """When the log is unreadable the hook writes a key/value file with no
    section banners. That shape keeps the first-non-empty-line rule."""
    assert normalize_error("missing_log=1\nlogfile=/work/x.log\n") == "missing_log=1"


# --- ingest stamping -------------------------------------------------------


@pytest.fixture
def store():
    from dportsv3.artifact_store import ArtifactStore

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    s = ArtifactStore.__new__(ArtifactStore)
    s.conn = conn
    s._lock = threading.Lock()
    return s


def _upsert(store, bundle_id, **extra):
    payload = {
        "run_id": "r1", "profile": "p", "ts_utc": "2026-07-25T00:00:00Z",
        "bundle_id": bundle_id, "origin": "ftp/curl", "flavor": "",
        "result": "failure", "target": "@2026Q3",
    }
    payload.update(extra)
    store.upsert_run_bundle(payload)


def _sig(store, bundle_id):
    row = store.conn.execute(
        "SELECT error_signature FROM bundles WHERE bundle_id = ?", (bundle_id,)
    ).fetchone()
    return row["error_signature"]


def test_ingest_computes_signature_from_errors_text(store):
    errors = "/tmp/pb.xy/work/foo.c:12:3: error: 'X' undeclared\n"
    _upsert(store, "b_txt", errors_text=errors)
    assert _sig(store, "b_txt") == compute_fingerprint(errors)


def test_ingest_prefers_precomputed_signature(store):
    _upsert(store, "b_pre", error_signature="deadbeefdeadbeef",
            errors_text="cc: error: ignored\n")
    assert _sig(store, "b_pre") == "deadbeefdeadbeef"


def test_ingest_without_errors_leaves_signature_null(store):
    _upsert(store, "b_none")
    assert _sig(store, "b_none") is None


def test_signatureless_update_preserves_existing_signature(store):
    errors = "cc: error: boom\n"
    _upsert(store, "b", errors_text=errors)
    first = _sig(store, "b")
    assert first is not None
    # A later upsert (e.g. a status touch) carrying no errors must not
    # null the signature stamped at birth.
    _upsert(store, "b", ts_utc="2026-07-25T01:00:00Z")
    assert _sig(store, "b") == first
