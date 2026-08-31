"""Filesystem helpers for safe apply-stage writes."""

from __future__ import annotations

import tempfile
from pathlib import Path


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as temp:
        temp.write(content)
        temp_path = Path(temp.name)
    temp_path.replace(path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=str(path.parent), delete=False
    ) as temp:
        temp.write(data)
        temp_path = Path(temp.name)
    temp_path.replace(path)


class FileTransaction:
    """Collect staged file writes/removals and commit atomically per file."""

    def __init__(self, *, dry_run: bool) -> None:
        self.dry_run = dry_run
        self._writes: dict[Path, str] = {}
        # Verbatim byte writes (file.materialize) — the staged file may
        # not be valid UTF-8 (e.g. a Latin-1 patch with a 0xa0 byte), so
        # it can't go through the text path.
        self._writes_bytes: dict[Path, bytes] = {}
        self._removes: set[Path] = set()

    def read_text(self, path: Path) -> str:
        if path in self._writes:
            return self._writes[path]
        if path in self._removes:
            raise FileNotFoundError(path)
        return path.read_text()

    def stage_write(self, path: Path, content: str) -> None:
        self._writes[path] = content
        self._writes_bytes.pop(path, None)
        self._removes.discard(path)

    def stage_write_bytes(self, path: Path, data: bytes) -> None:
        self._writes_bytes[path] = data
        self._writes.pop(path, None)
        self._removes.discard(path)

    def stage_remove(self, path: Path) -> None:
        self._removes.add(path)
        self._writes.pop(path, None)
        self._writes_bytes.pop(path, None)

    def staged_paths(self) -> list[Path]:
        paths = set(self._writes) | set(self._writes_bytes) | set(self._removes)
        return sorted(paths, key=lambda path: str(path))

    def staged_writes(self) -> dict[Path, str]:
        return dict(self._writes)

    def staged_byte_writes(self) -> dict[Path, bytes]:
        return dict(self._writes_bytes)

    def staged_removes(self) -> set[Path]:
        return set(self._removes)

    def staged_change_snapshot(self, path: Path) -> tuple[str | None, str | None]:
        before: str | None
        try:
            before = path.read_text()
        except (FileNotFoundError, UnicodeDecodeError):
            # UnicodeDecodeError: a byte-staged (file.materialize) path
            # whose dest exists but isn't UTF-8 — there's no text "before"
            # to diff against. Treat as absent for the text-diff preview.
            before = None

        if path in self._writes:
            after: str | None = self._writes[path]
        elif path in self._removes:
            after = None
        else:
            after = before
        return before, after

    def flush_pending(self) -> list[Path]:
        """Write every staged change to disk now and forget it.

        For executors that hand the file to an external process instead
        of editing text in the buffer — today only ``patch.apply``,
        which shells out to patch(1). Such an executor both reads and
        writes the file on disk, so it has to see the staged edits, and
        the later commit must not write a pre-subprocess buffer back
        over its result. Flushing satisfies both: disk becomes the one
        truth for these paths, and ``read_text`` falls through to it for
        the rest of the run.

        The staged paths lose their all-or-nothing commit, which is the
        price of an executor that cannot work on the buffer. It is not a
        new exposure: ``patch.apply`` already wrote to disk directly, and
        ``rollback`` never undid disk writes.

        No-op under dry_run, where commit writes nothing either. That
        leaves one known gap: a dry run's patch(1) still reads the
        unflushed file, so if an earlier op rewrote the region the patch
        targets, the dry run can report a hunk result the real run would
        not. Narrow, and it predates this method — dry runs never saw
        staged edits — but the real path is now correct while dry run is
        not, so the two can disagree.
        """
        if self.dry_run:
            return []

        flushed: list[Path] = []
        for path, content in self._writes.items():
            _atomic_write_text(path, content)
            flushed.append(path)
        for path, data in self._writes_bytes.items():
            _atomic_write_bytes(path, data)
            flushed.append(path)
        for path in self._removes:
            if path.exists():
                path.unlink()
            flushed.append(path)

        self._writes.clear()
        self._writes_bytes.clear()
        self._removes.clear()
        return sorted(set(flushed), key=str)

    def commit(self) -> None:
        if self.dry_run:
            return

        for path, content in self._writes.items():
            _atomic_write_text(path, content)

        for path, data in self._writes_bytes.items():
            _atomic_write_bytes(path, data)

        for path in self._removes:
            if path.exists():
                path.unlink()

    def rollback(self) -> None:
        self._writes.clear()
        self._writes_bytes.clear()
        self._removes.clear()
