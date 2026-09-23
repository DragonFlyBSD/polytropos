"""Tracker presentation library (extracted from server.py, Phase 2)."""

from .text import render_markdown, render_diff, relative_age
from .artifacts import (
    artifact_view_data,
    artifact_media_type,
    artifact_raw_text,
    resolve_artifact_path,
    default_artifact_relpath,
    group_artifacts,
    load_tool_trace,
)
from .sessions import (
    session_view_data,
    is_session_relpath,
    parse_session_records,
    SESSION_ATTEMPT_RE,
)
from .activity import group_activity_into_cards
from .compare import (
    compare_artifacts,
    default_relpath,
    is_diffable,
    unified,
)

__all__ = [
    "render_markdown", "render_diff", "relative_age", "artifact_view_data",
    "artifact_raw_text",
    "artifact_media_type", "resolve_artifact_path",
    "default_artifact_relpath", "group_artifacts", "load_tool_trace",
    "compare_artifacts", "default_relpath", "is_diffable", "unified",
    "session_view_data", "is_session_relpath",
    "parse_session_records", "SESSION_ATTEMPT_RE",
    "group_activity_into_cards",
]
