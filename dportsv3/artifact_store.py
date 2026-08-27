"""The evidence store: state.db rows, content-addressed blobs, full logs.

A library, not a service. It used to run as its own HTTP daemon on
:8788; the tracker now mounts its endpoints in-process and calls these
methods directly, so there is one service and one port. See
``dportsv3.tracker.routes.ingest_api`` for the /v1/ surface.

The tracker and the runner also write state.db; WAL serializes them.
Schema lives in ``dportsv3.db.schema`` and is shared.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db.schema import init_db as _init_state_db

DEFAULT_LOGS_ROOT = "/build/synth/logs"


def log(level: str, message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {level:5} {message}")


def emit_event(conn: sqlite3.Connection, event_type: str, data: dict[str, Any]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO events (ts, type, data_json) VALUES (?, ?, ?)",
        (ts, event_type, json.dumps(data)),
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_path(root: Path, sha: str) -> Path:
    return root / "objects" / "sha256" / sha[0:2] / sha[2:4] / sha


def _coerce_build_run_id(raw: Any) -> int | None:
    """The tracker build-run ordinal from a hook payload, or None.

    The hooks are stdlib shell and send whatever the tracker printed, so an
    empty string arrives whenever tracking is disabled, and an absent key
    arrives as None from an older client. Anything that isn't an integer
    becomes None rather than an error: an occurrence with no ordinal is still
    a real occurrence, and the C3 derivation degrades to timestamps for it.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _normalize_ts(raw: str | None) -> str | None:
    """Canonicalize a hook timestamp to ISO-8601 UTC.

    The dsynth hooks stamp the compact ``YYYYmmdd-HHMMSSZ`` form
    (``hook_common.now_utc``), but the rest of the system speaks ISO —
    the issue ``*_at`` timestamps, ``render.relative_age``, and the
    regression derivation's timestamp fallback (``ts_utc > resolved_at``,
    used when no build ordinal is available) all assume it.
    Storing the compact form raw left ``ts_utc`` lexicographically
    incomparable with those ISO values. Normalize here, at the single
    writer, so every timestamp column is one comparable format. Values
    already ISO (tests, other clients) or otherwise unrecognized pass
    through unchanged, so this is idempotent.
    """
    if not raw:
        return raw
    try:
        return (
            datetime.strptime(raw, "%Y%m%d-%H%M%SZ")
            .replace(tzinfo=timezone.utc)
            .isoformat()
        )
    except (ValueError, TypeError):
        return raw


class ArtifactStore:
    def __init__(self, logs_root: Path, evidence_root: Path | None = None) -> None:
        self.logs_root = logs_root
        self.evidence_root = (
            Path(evidence_root) if evidence_root is not None else logs_root / "evidence"
        )
        self.blob_root = self.evidence_root / "blobstore"
        self.full_logs_root = self.evidence_root / "full-logs"
        self.db_path = self.evidence_root / "state.db"
        self._lock = threading.Lock()

        self._ensure_dirs()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        _init_state_db(self.conn)

    @classmethod
    def from_evidence_root(cls, evidence_root: Path | str) -> "ArtifactStore":
        """Build a store around an evidence directory directly.

        The tracker is configured with the evidence root
        (``DPORTSV3_ARTIFACT_ROOT``), not the logs root the standalone
        service took, and the two are not required to be one level apart.
        """
        evidence_root = Path(evidence_root)
        return cls(evidence_root.parent, evidence_root=evidence_root)

    def _ensure_dirs(self) -> None:
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        (self.blob_root / "objects" / "sha256").mkdir(parents=True, exist_ok=True)
        self.full_logs_root.mkdir(parents=True, exist_ok=True)

    def upsert_run_bundle(self, payload: dict[str, Any]) -> None:
        run_id = payload.get("run_id")
        profile = payload.get("profile")
        bundle_id = payload.get("bundle_id")
        origin = payload.get("origin")
        flavor = payload.get("flavor")
        ts_utc = _normalize_ts(payload.get("ts_utc"))
        result = payload.get("result")
        target = payload.get("target")
        # The tracker `build_runs` ordinal this occurrence came from, sent by
        # the hook alongside its own run id. This is the link that lets C3
        # place an occurrence against a fix's Green-Head watermark instead of
        # comparing wall clocks across hosts. Absent whenever tracking is off,
        # which the derivation handles by falling back to timestamps.
        build_run_id = _coerce_build_run_id(payload.get("build_run_id"))
        # Fingerprint at ingest: prefer a caller-supplied signature, else
        # derive it here from the distilled errors text. Computing it in
        # the store keeps a single normalization rule (dportsv3.fingerprint,
        # shared with the runner's sticky-signature check) and lets the
        # stdlib-only hook client stay dumb.
        error_signature = payload.get("error_signature") or None
        if not error_signature:
            errors_text = payload.get("errors_text")
            if errors_text:
                from dportsv3.fingerprint import compute_fingerprint
                error_signature = compute_fingerprint(errors_text)
        now = datetime.now(timezone.utc).isoformat()

        # The issue this occurrence belongs to: the fingerprinted problem
        # keyed by (target, origin, fingerprint). Established at birth so
        # the occurrence never hops issues later. Only meaningful with an
        # origin (issues.origin is NOT NULL).
        from dportsv3.fingerprint import issue_key as _issue_key
        ikey = _issue_key(target, origin, error_signature) if origin else None
        seen_ts = ts_utc or now

        with self._lock:
            if run_id:
                self.conn.execute(
                    """INSERT INTO runs (run_id, profile, target, path, ts_start, ts_end, last_seen_at, build_run_id)
                       VALUES (?, ?, ?, NULL, ?, NULL, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                         profile=excluded.profile,
                         target=COALESCE(excluded.target, runs.target),
                         build_run_id=COALESCE(excluded.build_run_id, runs.build_run_id),
                         last_seen_at=excluded.last_seen_at""",
                    (run_id, profile, target, ts_utc, now, build_run_id),
                )

            # A brand-new bundle_id is a new occurrence; a re-upsert (status
            # touch) is not. Only new occurrences bump issue rollups, and
            # issue_key is set once at birth — never rewritten on update
            # (a signatureless touch would otherwise recompute a wrong key).
            is_new_occurrence = self.conn.execute(
                "SELECT 1 FROM bundles WHERE bundle_id = ?", (bundle_id,)
            ).fetchone() is None

            self.conn.execute(
                """INSERT INTO bundles (bundle_id, run_id, origin, flavor, ts_utc, result, target, error_signature, issue_key, path, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                   ON CONFLICT(bundle_id) DO UPDATE SET
                     run_id=excluded.run_id,
                     origin=excluded.origin,
                     flavor=excluded.flavor,
                     ts_utc=excluded.ts_utc,
                     result=excluded.result,
                     target=COALESCE(excluded.target, bundles.target),
                     error_signature=COALESCE(excluded.error_signature, bundles.error_signature),
                     last_seen_at=excluded.last_seen_at""",
                (bundle_id, run_id, origin, flavor, ts_utc, result, target,
                 error_signature, ikey, now),
            )
            emit_event(self.conn, "bundle_upserted", {
                "bundle_id": bundle_id,
                "run_id": run_id,
                "origin": origin,
                "result": result,
                "target": target,
            })

            if is_new_occurrence and ikey and origin:
                self._upsert_issue_for_occurrence(
                    issue_key=ikey, target=target, origin=origin,
                    fingerprint=error_signature, bundle_id=bundle_id,
                    seen_ts=seen_ts, now=now, build_run_id=build_run_id,
                )
            self.conn.commit()

    def _upsert_issue_for_occurrence(self, *, issue_key: str, target: str | None,
                                     origin: str, fingerprint: str | None,
                                     bundle_id: str, seen_ts: str, now: str,
                                     build_run_id: int | None = None) -> None:
        """Find-or-create the issue for a **new** occurrence and roll it up.

        First occurrence for a key creates the issue (``unresolved``,
        ``times_seen=1``). A later occurrence bumps ``times_seen`` and, if
        it is the newest by timestamp, advances ``last_seen_at`` +
        ``latest_bundle_id``.

        Rollups only — an arriving occurrence NEVER moves the issue's state
        (C3). It used to rewrite ``resolved``/``resolving`` to ``regressed``,
        which was wrong in both directions: it fired without checking the
        occurrence against the fix's known-good boundary, and it clobbered a
        `resolving` issue mid-confirm-build, where a still-failing farm build
        is the unfixed port being observed rather than a fix that came back.
        ``regressed`` is now derived on read
        (:func:`issue_state.derived_regression`) and the confirm verdict
        (A2/A3) owns the `resolving` exit.

        The ``issue_regressed`` event survives as a notification, fired on
        the same predicate the projection uses so the feed and the badge
        cannot disagree.

        Caller holds ``self._lock`` and owns the surrounding commit.
        """
        row = self.conn.execute(
            "SELECT state, last_seen_at, resolved_at, green_head_run_id "
            "FROM issues WHERE issue_key = ?",
            (issue_key,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO issues
                     (issue_key, target, origin, fingerprint, state, times_seen,
                      first_seen_at, last_seen_at, latest_bundle_id, updated_at)
                   VALUES (?, ?, ?, ?, 'unresolved', 1, ?, ?, ?, ?)""",
                (issue_key, target, origin, fingerprint, seen_ts, seen_ts,
                 bundle_id, now),
            )
            emit_event(self.conn, "issue_created", {
                "issue_key": issue_key, "origin": origin,
                "target": target, "bundle_id": bundle_id,
            })
            return

        prev_last = row["last_seen_at"]
        is_newest = prev_last is None or seen_ts >= prev_last
        self.conn.execute(
            """UPDATE issues SET
                 times_seen = times_seen + 1,
                 last_seen_at = CASE WHEN ? THEN ? ELSE last_seen_at END,
                 latest_bundle_id = CASE WHEN ? THEN ? ELSE latest_bundle_id END,
                 fingerprint = COALESCE(fingerprint, ?),
                 updated_at = ?
               WHERE issue_key = ?""",
            (is_newest, seen_ts, is_newest, bundle_id, fingerprint, now,
             issue_key),
        )
        # Same predicate the projection derives the badge from — imported
        # here rather than restated, because three independent answers to
        # "did this regress?" is what C3 exists to remove.
        from dportsv3.tracker.issue_state import (  # noqa: PLC0415
            ISSUE_RESOLVED, occurrence_past_boundary,
        )
        regressed = (
            row["state"] == ISSUE_RESOLVED
            and occurrence_past_boundary(
                {"green_head_run_id": row["green_head_run_id"],
                 "resolved_at": row["resolved_at"]},
                {"build_run_id": build_run_id, "ts_utc": seen_ts},
            )
        )
        if regressed:
            emit_event(self.conn, "issue_regressed", {
                "issue_key": issue_key, "origin": origin,
                "target": target, "bundle_id": bundle_id,
            })

    def apply_transition(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hook-side entry point for the lifecycle state machine.

        Expects: {job_id, event, actor?, detail?}. Returns a dict with
        the new state or an error message. The actual transition logic
        lives in ``dportsv3.agent.lifecycle``; this method is a thin
        wrapper that also populates the metadata columns on
        HOOK_ENQUEUED (origin, type, flavor, etc.) since those come
        from the hook's payload, not from any later transition.
        """
        from dportsv3.agent import lifecycle  # local import to avoid agent-package import on store-only deployments
        job_id = payload.get("job_id")
        event_name = payload.get("event")
        actor = payload.get("actor") or "hook"
        detail = payload.get("detail") or {}
        if not job_id or not event_name:
            return {"ok": False, "error": "job_id and event required"}
        try:
            event = lifecycle.JobEvent(event_name)
        except ValueError:
            return {"ok": False, "error": f"unknown event: {event_name}"}
        with self._lock:
            try:
                new_state = lifecycle.apply(self.conn, job_id, event,
                                            actor=actor, detail=detail)
            except lifecycle.IllegalTransition as exc:
                return {"ok": False, "error": str(exc)}
            # For HOOK_ENQUEUED, populate metadata columns from the
            # detail payload (origin/type/flavor/etc.). Later events
            # don't carry these — they're stable across the job's life.
            if event == lifecycle.JobEvent.HOOK_ENQUEUED:
                now = datetime.now(timezone.utc).isoformat()
                # Fall back to the bundle's target when the hook did
                # not pass one. Hooks run with a possibly-empty
                # DPORTSV3_TRACKER_TARGET env var, and stripping
                # empty strings client-side (artifact-store-client)
                # leaves jobs.target NULL while the bundle has the
                # real value. The bundle is the canonical source.
                target = detail.get("target") or ""
                bundle_id_hint = detail.get("bundle_id")
                if not target and bundle_id_hint:
                    row = self.conn.execute(
                        "SELECT target FROM bundles WHERE bundle_id = ?",
                        (bundle_id_hint,),
                    ).fetchone()
                    if row is not None and row[0]:
                        target = row[0]
                self.conn.execute(
                    """UPDATE jobs SET
                           type = COALESCE(?, type),
                           origin = COALESCE(?, origin),
                           flavor = COALESCE(?, flavor),
                           bundle_dir = COALESCE(?, bundle_dir),
                           bundle_id = COALESCE(?, bundle_id),
                           created_ts_utc = COALESCE(?, created_ts_utc),
                           path = COALESCE(?, path),
                           target = COALESCE(NULLIF(?, ''), target),
                           last_seen_at = ?
                       WHERE job_id = ?""",
                    (
                        detail.get("type"),
                        detail.get("origin"),
                        detail.get("flavor"),
                        detail.get("bundle_dir"),
                        detail.get("bundle_id"),
                        detail.get("created_ts_utc"),
                        detail.get("path"),
                        target,
                        now,
                        job_id,
                    ),
                )
            emit_event(self.conn, "job_transitioned", {
                "job_id": job_id,
                "event": event.value,
                "to_state": new_state.value,
                "origin": detail.get("origin"),
                "target": detail.get("target"),
            })
            self.conn.commit()
        return {"ok": True, "state": new_state.value}

    def put_blob(self, bundle_id: str, relpath: str, data: bytes, kind: str | None) -> dict[str, Any]:
        sha = sha256_bytes(data)
        obj_path = blob_path(self.blob_root, sha)
        if not obj_path.exists():
            obj_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = obj_path.with_suffix(".tmp")
            tmp_path.write_bytes(data)
            tmp_path.rename(obj_path)

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                """INSERT INTO blob_objects (sha256, size, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(sha256) DO NOTHING""",
                (sha, len(data), now),
            )
            self.conn.execute(
                """INSERT INTO artifact_refs (bundle_id, relpath, backend, sha256, fs_path, kind, size, created_at)
                   VALUES (?, ?, 'blob', ?, NULL, ?, ?, ?)
                   ON CONFLICT(bundle_id, relpath) DO UPDATE SET
                     backend='blob', sha256=excluded.sha256, fs_path=NULL,
                     kind=excluded.kind, size=excluded.size, created_at=excluded.created_at""",
                (bundle_id, relpath, sha, kind, len(data), now),
            )
            emit_event(self.conn, "artifact_put", {
                "bundle_id": bundle_id,
                "artifact": relpath,
                "backend": "blob",
            })
            self.conn.commit()

        return {"sha256": sha, "size": len(data)}

    def put_fs_ref(self, bundle_id: str, relpath: str, fs_path: str, kind: str | None) -> dict[str, Any]:
        path = Path(fs_path)
        size = path.stat().st_size if path.exists() else None
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                """INSERT INTO artifact_refs (bundle_id, relpath, backend, sha256, fs_path, kind, size, created_at)
                   VALUES (?, ?, 'fs', NULL, ?, ?, ?, ?)
                   ON CONFLICT(bundle_id, relpath) DO UPDATE SET
                     backend='fs', sha256=NULL, fs_path=excluded.fs_path,
                     kind=excluded.kind, size=excluded.size, created_at=excluded.created_at""",
                (bundle_id, relpath, fs_path, kind, size, now),
            )
            emit_event(self.conn, "artifact_put", {
                "bundle_id": bundle_id,
                "artifact": relpath,
                "backend": "fs",
            })
            self.conn.commit()

        return {"size": size}

    def get_artifact(self, bundle_id: str, relpath: str) -> tuple[str, Path] | None:
        row = self.conn.execute(
            """SELECT backend, sha256, fs_path FROM artifact_refs
               WHERE bundle_id = ? AND relpath = ?""",
            (bundle_id, relpath),
        ).fetchone()
        if not row:
            return None
        if row["backend"] == "blob":
            obj_path = blob_path(self.blob_root, row["sha256"])
            return "blob", obj_path
        return "fs", Path(row["fs_path"])

    def upsert_user_context(self, run_id: str, origin: str, context_text: str) -> int:
        """Set or update the operator's hint text for one (run_id, origin).

        Bumps ``context_rev`` by 1 on every write so the runner's
        ``process_user_context_updates`` loop can detect new input and
        re-enqueue a triage retry.

        Returns the new ``context_rev``.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            row = self.conn.execute(
                "SELECT context_rev FROM user_context WHERE run_id = ? AND origin = ?",
                (run_id, origin),
            ).fetchone()
            if row:
                new_rev = int(row["context_rev"]) + 1
                self.conn.execute(
                    """UPDATE user_context
                       SET context_text = ?, updated_at = ?, context_rev = ?
                       WHERE run_id = ? AND origin = ?""",
                    (context_text, now, new_rev, run_id, origin),
                )
            else:
                new_rev = 1
                self.conn.execute(
                    """INSERT INTO user_context
                       (run_id, origin, context_text, updated_at, context_rev)
                       VALUES (?, ?, ?, ?, ?)""",
                    (run_id, origin, context_text, now, new_rev),
                )
            emit_event(self.conn, "user_context_updated", {
                "run_id": run_id,
                "origin": origin,
                "context_rev": new_rev,
                "updated_at": now,
            })
            self.conn.commit()
        return new_rev
