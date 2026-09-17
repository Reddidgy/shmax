"""Per-session tool-action notes for chat memory (POS-1862).

Captures the file operations (Read/Write/Edit) and CLI commands (Bash) the
model performs during a chat turn as one-line notes, so they are folded into
the next turn's ``### Previous Turns`` memory block alongside the conversation
messages.  Notes never contain file content or command output — only relative
paths and an ``ok``/``error`` status.

Notes for the in-flight turn are buffered in
``.praxis/chat/sessions/<session_id>/pending_tool_actions.json`` because a
turn's tool events are observed by the run worker AFTER that turn's memory has
already been written; the next turn merges the buffer into ``memory.json``
before the assistant reply it belongs to, then clears it.

Every public function degrades to an empty/no-op result instead of raising — a
tool-action failure must never break or block a chat turn.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from chat_session_paths import session_dir

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_ACTION_ROLE",
    "TRACKED_TOOLS",
    "normalize_tool_name",
    "build_tool_note",
    "with_status",
    "load_pending_actions",
    "save_pending_actions",
    "clear_pending_actions",
    "merge_tool_actions",
]

PENDING_TOOL_ACTIONS_FILENAME = "pending_tool_actions.json"
TOOL_ACTION_ROLE = "tool_action"
TRACKED_TOOLS = ("Read", "Write", "Edit", "Bash")


def normalize_tool_name(name: str) -> str:
    """Strip any MCP prefix, e.g. ``mcp__server__Read`` -> ``Read``.

    Returns ``""`` for non-str input.
    """
    if not isinstance(name, str):
        return ""
    return name.rsplit("__", 1)[-1].strip()


def _relative_path(project_root: str, path: str) -> str:
    """Strip the project-root prefix so stored paths are always relative."""
    if not isinstance(path, str) or not path:
        return ""
    root = (project_root or "").rstrip("/\\")
    if root and (path.startswith(root + "/") or path.startswith(root + "\\")):
        return path[len(root) + 1 :]
    return path


def build_tool_note(tool_name: str, tool_input: object, project_root: str) -> str | None:
    """Build a one-line note for a tracked tool, or ``None`` for anything else.

    Never raises.
    """
    try:
        name = normalize_tool_name(tool_name)
        if name not in TRACKED_TOOLS:
            return None
        data = tool_input if isinstance(tool_input, dict) else {}

        if name == "Bash":
            command = data.get("command", "")
            command = command if isinstance(command, str) else ""
            command = " ".join(command.split())
            if not command:
                return None
            return f"[Bash]: {command}"

        rel = ""
        for key in ("file_path", "path", "notebook_path"):
            value = data.get(key)
            if isinstance(value, str) and value:
                rel = _relative_path(project_root, value)
                break
        if not rel:
            return None
        return f"[{name}]: {rel}"
    except Exception as exc:
        logger.warning("build_tool_note failed for %s: %s", tool_name, exc)
        return None


def with_status(note: str, is_error: bool) -> str:
    """Append the short outcome marker to *note*."""
    if not isinstance(note, str):
        return ""
    return f"{note} → {'error' if is_error else 'ok'}"


def _pending_path(project_root: str, session_id: str) -> str:
    return os.path.join(session_dir(project_root, session_id), PENDING_TOOL_ACTIONS_FILENAME)


def load_pending_actions(project_root: str, session_id: str) -> list[str]:
    """Load buffered tool-action notes from the session directory.

    Returns an empty list when the file is missing, unreadable, or corrupt.
    Never raises.
    """
    if not session_id:
        return []
    path = _pending_path(project_root, session_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("pending_tool_actions file at %s unreadable: %s", path, exc)
        return []
    try:
        if not isinstance(data, dict):
            return []
        notes = data.get("notes")
        if not isinstance(notes, list):
            return []
        return [n for n in notes if isinstance(n, str) and n]
    except Exception as exc:
        logger.warning("load_pending_actions parse failed for %s: %s", path, exc)
        return []


def save_pending_actions(project_root: str, session_id: str, notes: list[str]) -> None:
    """Atomically persist *notes* to the pending-actions file.

    Creates the session directory if needed. Never raises.
    """
    if not session_id:
        return
    path = _pending_path(project_root, session_id)
    directory = os.path.dirname(path)
    tmp_path: str | None = None
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False, encoding="utf-8"
        ) as fh:
            tmp_path = fh.name
            json.dump({"notes": notes}, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("save_pending_actions failed for session %s: %s", session_id, exc)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def clear_pending_actions(project_root: str, session_id: str) -> None:
    """Remove the pending-actions file. No-op if already absent. Never raises."""
    if not session_id:
        return
    path = _pending_path(project_root, session_id)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("clear_pending_actions failed for %s: %s", path, exc)
    except Exception as exc:
        logger.warning("clear_pending_actions failed for %s: %s", path, exc)


def merge_tool_actions(messages: list[dict], notes: list[str]) -> list[dict]:
    """Insert tool-action *notes* immediately before the trailing assistant message.

    Returns a NEW list. If the last message is not an assistant message (or
    *messages* is empty), the notes are appended at the end instead. Returns
    ``list(messages)`` unchanged when *notes* is empty/all-invalid, and on any
    exception. Never raises.
    """
    try:
        valid_notes = [n for n in notes if isinstance(n, str) and n]
        if not valid_notes:
            return list(messages)

        entries = [{"role": TOOL_ACTION_ROLE, "content": n} for n in valid_notes]
        result = list(messages)

        if result and isinstance(result[-1], dict) and result[-1].get("role") == "assistant":
            return result[:-1] + entries + result[-1:]
        return result + entries
    except Exception as exc:
        logger.warning("merge_tool_actions failed: %s", exc)
        return list(messages)
