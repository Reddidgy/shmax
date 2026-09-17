"""Shared path helpers for per-session chat storage (POS-1846).

Extracted from ``chat_session_memory`` so ``chat_session_queue`` (and any
future per-session module) can reuse the sanitization and directory logic
without importing the memory module.
"""

from __future__ import annotations

import logging
import os
import re
import shutil

__all__ = [
    "sanitize_session_id",
    "session_dir",
    "claude_session_id_path",
    "enforce_session_limit",
    "claude_code_session_id_path",
    "fork_source_claude_sid_path",
    "claude_code_system_prompt_path",
]

MAX_ACTIVE_SESSIONS = 50

logger = logging.getLogger(__name__)


def sanitize_session_id(session_id: str) -> str:
    """Collapse a client-supplied session id to a safe directory name.

    ``session_id`` rides in from the request body largely unvalidated (only
    "is a string" is enforced upstream); this keeps it from ever being used
    to escape ``.praxis/chat/`` via path separators or ``..``.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", session_id.strip())
    return cleaned[:128] or "default"


def session_dir(project_root: str, session_id: str) -> str:
    return os.path.join(
        project_root, ".praxis", "chat", "sessions", sanitize_session_id(session_id)
    )


def claude_session_id_path(project_root: str, session_id: str) -> str:
    return os.path.join(session_dir(project_root, session_id), "claude_session_id.txt")


def claude_code_session_id_path(project_root: str, session_id: str) -> str:
    return os.path.join(session_dir(project_root, session_id), "claude_code_session_id.txt")


def claude_code_system_prompt_path(project_root: str, session_id: str) -> str:
    return os.path.join(session_dir(project_root, session_id), "claude_code_system_prompt.txt")


def fork_source_claude_sid_path(project_root: str, session_id: str) -> str:
    return os.path.join(session_dir(project_root, session_id), "fork_source_claude_sid.txt")


def archive_sessions_base(project_root: str) -> str:
    return os.path.join(project_root, ".praxis", "chat", "archive_sessions")


def enforce_session_limit(project_root: str, max_sessions: int = MAX_ACTIVE_SESSIONS) -> None:
    """Archive the oldest sessions once the active session count exceeds ``max_sessions``.

    Never raises; failures are logged and swallowed so this can be called
    opportunistically from hot paths without risking request failures.
    """
    try:
        sessions_base = os.path.join(project_root, ".praxis", "chat", "sessions")
        if not os.path.isdir(sessions_base):
            return

        entries = [
            name
            for name in os.listdir(sessions_base)
            if os.path.isdir(os.path.join(sessions_base, name))
        ]
        if len(entries) <= max_sessions:
            return

        entries_with_mtime = [
            (name, os.path.getmtime(os.path.join(sessions_base, name))) for name in entries
        ]
        entries_with_mtime.sort(key=lambda item: item[1])

        overflow = len(entries_with_mtime) - max_sessions
        for session_name, _mtime in entries_with_mtime[:overflow]:
            src = os.path.join(sessions_base, session_name)
            dest = os.path.join(archive_sessions_base(project_root), session_name)
            os.makedirs(archive_sessions_base(project_root), exist_ok=True)
            shutil.move(src, dest)
            logger.info("Session archived: %s → archive_sessions/", session_name)
    except Exception:
        logger.warning("Failed to enforce session limit for %s", project_root, exc_info=True)
