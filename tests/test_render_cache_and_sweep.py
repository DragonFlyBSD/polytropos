"""Two per-render costs the redesign multiplies, and what bounds them.

Rendering an artifact gunzips up to 4 MiB, decodes it and highlights it. The
worklist used to sweep every open delivery inline, opening one SQLite
connection per candidate. Both are paid on pages that a room full of readers
polls every few seconds.
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest

from dportsv3.tracker import delivery_sync
from dportsv3.tracker.render import artifacts


@pytest.fixture(autouse=True)
def _clean_cache():
    artifacts.cache_clear()
    yield
    artifacts.cache_clear()


def _blob(root: Path, data: bytes, *, gz: bool = True) -> dict:
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    path = root / "blobs" / (sha + (".gz" if gz else ""))
    path.write_bytes(gzip.compress(data) if gz else data)
    return {"backend": "fs", "sha256": sha, "fs_path": str(path),
            "kind": "gzip" if gz else None, "size": path.stat().st_size}


# --- the rendered-artifact cache -------------------------------------------


def test_the_same_blob_is_rendered_once(tmp_path):
    ref = _blob(tmp_path, b"configure: error: no acceptable C compiler\n" * 500)

    first = artifacts.artifact_view_data(tmp_path, "b-1", "logs/full.log.gz", ref)
    second = artifacts.artifact_view_data(tmp_path, "b-1", "logs/full.log.gz", ref)

    assert first["content"] == second["content"]
    assert len(artifacts._CACHE) == 1


def test_a_shared_blob_serves_every_bundle_that_points_at_it(tmp_path):
    """Blobs are deduplicated by content, so one log can hang off several
    bundles. The render is keyed on the bytes; the identity fields are not."""
    ref = _blob(tmp_path, b"ld: error: undefined symbol: foo\n" * 200)

    a = artifacts.artifact_view_data(tmp_path, "b-1", "logs/full.log.gz", ref)
    b = artifacts.artifact_view_data(tmp_path, "b-2", "logs/full.log.gz", ref)

    assert a["content"] == b["content"]
    assert a["bundle_id"] == "b-1" and b["bundle_id"] == "b-2"
    assert b["filename"] == "full.log.gz"
    assert len(artifacts._CACHE) == 1


def test_the_name_is_part_of_the_key(tmp_path):
    """The same bytes render differently under a different name -- a diff is
    not a log -- so relpath belongs in the key."""
    body = b"--- a/Makefile\n+++ b/Makefile\n@@ -1 +1 @@\n-A\n+B\n"
    ref = _blob(tmp_path, body, gz=False)
    ref["kind"] = None

    as_diff = artifacts.artifact_view_data(tmp_path, "b-1", "analysis/x.diff", ref)
    as_text = artifacts.artifact_view_data(tmp_path, "b-1", "notes.txt", ref)

    assert as_diff["render_kind"] == "diff"
    assert as_text["render_kind"] == "text"
    assert len(artifacts._CACHE) == 2


def test_bytes_that_cannot_be_addressed_are_not_cached(tmp_path):
    """An fs ref with no sha256 points at a path whose contents can change
    under us. There is no key that stays true, so there is no cache entry."""
    ref = _blob(tmp_path, b"hello\n", gz=False)
    ref.pop("sha256")

    assert artifacts.artifact_view_data(
        tmp_path, "b-1", "notes.txt", ref) is not None
    assert len(artifacts._CACHE) == 0


def test_the_cache_is_bounded_by_bytes(tmp_path, monkeypatch):
    """Entries differ by three orders of magnitude, so a count-based bound
    would be meaningless."""
    monkeypatch.setattr(artifacts, "_cache_limit", lambda: 4096)

    for i in range(12):
        ref = _blob(tmp_path, f"line {i}\n".encode() * 400, gz=False)
        ref["kind"] = None
        artifacts.artifact_view_data(tmp_path, "b-1", f"logs/{i}.txt", ref)

    total = sum(cost for _, cost in artifacts._CACHE.values())
    assert 0 < len(artifacts._CACHE) < 12
    assert total <= 4096


def test_a_disabled_cache_still_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_cache_limit", lambda: 0)
    ref = _blob(tmp_path, b"still works\n", gz=False)
    ref["kind"] = None

    view = artifacts.artifact_view_data(tmp_path, "b-1", "notes.txt", ref)

    assert view["content"].strip() == "still works"
    assert len(artifacts._CACHE) == 0


# --- the delivery sweep ----------------------------------------------------


def _db_with_open_delivery(tmp_path) -> str:
    from dportsv3.tracker.db import init_db

    db = tmp_path / "state.db"
    conn = init_db(str(db))
    conn.execute(
        "INSERT INTO bundles(bundle_id, run_id, origin, ts_utc, target, "
        "result, path, last_seen_at) "
        "VALUES ('b-1', 'r', 'devel/foo', 't', '@main', 'failure', '', 't')")
    conn.execute(
        "INSERT INTO bundle_review_requests(bundle_id, provider, status, "
        "created_at, url) VALUES ('b-1', 'github', 'created', 't', 'u')")
    conn.commit()
    conn.close()
    return str(db)


def test_three_renders_cause_one_sweep(tmp_path, monkeypatch):
    """Each bundle is rate-limited to delivery.reconcile_min_interval
    regardless. What this bounds is how often a RENDER pays to find out --
    the worklist is polled every few seconds by everyone watching."""
    db = _db_with_open_delivery(tmp_path)
    delivery_sync.reset_sweep_throttle()
    calls: list[dict] = []
    monkeypatch.setattr(delivery_sync, "reconcile_bundle_delivery",
                        lambda **kw: calls.append(kw))

    swept = [delivery_sync.reconcile_open_deliveries(db_path=db)
             for _ in range(3)]

    assert swept == [1, 0, 0]
    assert [c["bundle_id"] for c in calls] == ["b-1"]
    assert calls[0]["target"] == "@main"


def test_a_sweep_survives_one_unreachable_forge(tmp_path, monkeypatch):
    """Best-effort: a page render must not fail because GitHub is down."""
    db = _db_with_open_delivery(tmp_path)
    delivery_sync.reset_sweep_throttle()

    def _boom(**kw):
        raise OSError("github unreachable")

    monkeypatch.setattr(delivery_sync, "reconcile_bundle_delivery", _boom)

    assert delivery_sync.reconcile_open_deliveries(db_path=db) == 1


def test_forcing_a_sweep_ignores_the_throttle(tmp_path):
    from dportsv3.tracker.db import init_db

    db = tmp_path / "state.db"
    init_db(str(db)).close()

    delivery_sync.reconcile_open_deliveries(db_path=str(db))
    assert delivery_sync.reconcile_open_deliveries(
        db_path=str(db), force=True) == 0
