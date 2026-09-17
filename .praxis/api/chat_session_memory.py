"""Per-session chat memory (POS-1838 / POS-1852).

``memory.json`` uses the same message format as ``full.json`` — each entry
is a dict with at minimum ``role`` and ``content``.  Before compaction the
two files are kept in sync (identical messages); after a Sonnet compaction
pass, ``memory.json`` is wiped and replaced with a single compacted-summary
message so subsequent turns start from a short baseline while ``full.json``
preserves the complete, unmodified record for the frontend.

Every public function degrades to an empty/``None`` result instead of
raising: a memory failure must never break or block a chat turn.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from chat_session_paths import (
    sanitize_session_id,
    session_dir,
    claude_session_id_path,
    claude_code_session_id_path,
    claude_code_system_prompt_path,
)
from chat_tool_actions import TOOL_ACTION_ROLE

logger = logging.getLogger(__name__)

__all__ = [
    "load_memory_messages",
    "save_memory_messages",
    "persist_compaction",
    "render_memory_messages",
    "build_session_stdin",
    "save_claude_session_id",
    "load_claude_session_id",
    "save_claude_code_session_id",
    "load_claude_code_session_id",
    "delete_claude_code_session_id",
    "save_claude_code_system_prompt",
    "load_claude_code_system_prompt_path",
]

# Session memory lives at <project_root>/.praxis/chat/sessions/<session_id>/memory.json
# — one file per session, distinct from the durable project-scoped facts the
# now-retired chat_memory.py used to keep at .praxis/chat/memory.json.
SESSION_MEMORY_FILENAME = "memory.json"
SESSION_LAST_STDIN_FILENAME = "last_stdin.txt"


# ---------------------------------------------------------------------------
# Internal path helpers
# ---------------------------------------------------------------------------


def _sanitize_session_id(session_id: str) -> str:
    """Delegate to :func:`chat_session_paths.sanitize_session_id`."""
    return sanitize_session_id(session_id)


def _session_dir(project_root: str, session_id: str) -> str:
    """Delegate to :func:`chat_session_paths.session_dir`."""
    return session_dir(project_root, session_id)


def _memory_file_path(project_root: str, session_id: str) -> str:
    return os.path.join(_session_dir(project_root, session_id), SESSION_MEMORY_FILENAME)


def save_claude_session_id(project_root: str, session_id: str, claude_sid: str) -> None:
    """Persist the Claude CLI session ID for ``--resume`` on subsequent turns.

    Never raises — a failed write is logged but does not break a turn.
    """
    if not session_id or not claude_sid:
        return
    path = claude_session_id_path(project_root, session_id)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(claude_sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("save_claude_session_id failed for session %s: %s", session_id, exc)


def load_claude_session_id(project_root: str, session_id: str) -> str | None:
    """Load a previously saved Claude CLI session ID, or ``None`` if absent.

    Never raises — a missing or unreadable file returns ``None``.
    """
    if not session_id:
        return None
    path = claude_session_id_path(project_root, session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        return value if value else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_claude_session_id failed for session %s: %s", session_id, exc)
        return None


def save_claude_code_session_id(project_root: str, session_id: str, claude_sid: str) -> None:
    """Persist the Claude Code (interactive PTY) session ID (POS-2219).

    Never raises — a failed write is logged but does not break a turn.
    """
    if not session_id or not claude_sid:
        return
    path = claude_code_session_id_path(project_root, session_id)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(claude_sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "save_claude_code_session_id failed for session %s: %s", session_id, exc
        )


def load_claude_code_session_id(project_root: str, session_id: str) -> str | None:
    """Load a previously saved Claude Code session ID, or ``None`` if absent.

    Never raises — a missing or unreadable file returns ``None``.
    """
    if not session_id:
        return None
    path = claude_code_session_id_path(project_root, session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        return value if value else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "load_claude_code_session_id failed for session %s: %s", session_id, exc
        )
        return None


def delete_claude_code_session_id(project_root: str, session_id: str) -> None:
    """Remove a stale Claude Code session ID file (POS-2307).

    Called when ``claude --resume <stored id>`` fails, which means the stored
    ID no longer maps to a resumable conversation. Never raises.
    """
    if not session_id:
        return
    path = claude_code_session_id_path(project_root, session_id)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "delete_claude_code_session_id failed for session %s: %s", session_id, exc
        )


def save_fork_source_claude_sid(
    project_root: str, session_id: str, source_claude_sid: str
) -> None:
    """Persist the source Claude Code session ID for a pending fork (POS-2240).

    Written into the *new* session's directory so the WebSocket handler can
    detect "this session should fork from <source>" on first connect.
    Never raises — a failed write is logged but does not block the fork setup.
    """
    if not session_id or not source_claude_sid:
        return
    from chat_session_paths import fork_source_claude_sid_path

    path = fork_source_claude_sid_path(project_root, session_id)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source_claude_sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "save_fork_source_claude_sid failed for session %s: %s", session_id, exc
        )


def load_fork_source_claude_sid(project_root: str, session_id: str) -> str | None:
    """Load a pending fork-source Claude Code session ID, or ``None``.

    Never raises — a missing or unreadable file returns ``None``.
    """
    if not session_id:
        return None
    from chat_session_paths import fork_source_claude_sid_path

    path = fork_source_claude_sid_path(project_root, session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
        return value if value else None
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "load_fork_source_claude_sid failed for session %s: %s", session_id, exc
        )
        return None


def delete_fork_source_claude_sid(project_root: str, session_id: str) -> None:
    """Remove the fork-source marker after a successful fork (POS-2240).

    Never raises.
    """
    if not session_id:
        return
    from chat_session_paths import fork_source_claude_sid_path

    path = fork_source_claude_sid_path(project_root, session_id)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "delete_fork_source_claude_sid failed for session %s: %s", session_id, exc
        )


def save_claude_code_system_prompt(
    project_root: str, session_id: str, text: str
) -> str | None:
    """Write the Claude Code system prompt for *session_id* (POS-2219).

    Byte-exact write (``newline=""``, no OS newline translation) so the file
    can be passed verbatim to ``claude --append-system-prompt-file``. Returns
    the written path, or ``None`` on failure or empty ``text``/``session_id``.
    Never raises.
    """
    if not session_id or not text:
        return None
    path = claude_code_system_prompt_path(project_root, session_id)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "save_claude_code_system_prompt failed for session %s: %s", session_id, exc
        )
        return None


def load_claude_code_system_prompt_path(project_root: str, session_id: str) -> str | None:
    """Return the Claude Code system prompt path if it exists and is non-empty.

    Never raises.
    """
    if not session_id:
        return None
    path = claude_code_system_prompt_path(project_root, session_id)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "load_claude_code_system_prompt_path failed for session %s: %s",
            session_id,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_memory_messages(project_root: str, session_id: str) -> list[dict] | None:
    """Load the session's stored memory messages, or ``None`` on missing/corrupt/old format.

    Returns ``None`` when the file does not exist, is unreadable, or uses
    the legacy ``SessionMemory`` format (has a ``turns`` key instead of
    ``messages``).  The caller is expected to initialise from the current
    POST body when ``None`` is returned.  Never raises.
    """
    if not session_id:
        return None
    path = _memory_file_path(project_root, session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        logger.debug("memory file not found at %s", path)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("memory file at %s is unreadable/corrupt: %s", path, exc)
        return None

    try:
        if not isinstance(data, dict):
            return None
        # Legacy format detection: old memory.json has "turns" key
        if "turns" in data and "messages" not in data:
            logger.info("legacy memory format detected at %s; will re-initialise", path)
            return None
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            return None
        # Validate each message is a dict with at least a role
        messages: list[dict] = []
        for entry in raw_messages:
            if isinstance(entry, dict) and "role" in entry:
                messages.append(entry)
        return messages
    except Exception as exc:  # noqa: BLE001 — must never raise
        logger.warning("load_memory_messages failed to parse %s: %s", path, exc)
        return None


def save_memory_messages(project_root: str, session_id: str, messages: list[dict]) -> None:
    """Atomically write *messages* to ``.praxis/chat/sessions/<session_id>/memory.json``.

    Creates the session directory if needed.  Never raises: any failure is
    logged as a warning.
    """
    if not session_id:
        return
    path = _memory_file_path(project_root, session_id)
    directory = os.path.dirname(path)
    tmp_path: str | None = None
    try:
        os.makedirs(directory, exist_ok=True)
        payload = {"messages": messages}
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False, encoding="utf-8"
        ) as handle:
            tmp_path = handle.name
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:  # noqa: BLE001 — memory writes must never raise
        logger.warning("save_memory_messages failed for session %s: %s", session_id, exc)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def persist_compaction(
    project_root: str, session_id: str, compacted_text: str
) -> None:
    """Replace ``memory.json`` with a single compacted-summary message.

    Called after a successful Sonnet compaction pass.  ``full.json`` is
    NOT modified — it retains the complete, uncompacted history.  From
    this point forward ``memory.json`` diverges permanently from
    ``full.json``.  Never raises.
    """
    compacted_msg: dict = {
        "role": "assistant",
        "content": compacted_text,
        "compacted": True,
    }
    save_memory_messages(project_root, session_id, [compacted_msg])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_memory_messages(
    messages: list[dict], assignee_name: str = "Assistant"
) -> str:
    """Render *messages* as section blocks for the stdin payload.

    When compacted messages are present, they are output under a separate
    ``### Compacted data from previous turns`` header so downstream consumers
    (the LLM and future rotation compactions) can distinguish prior-rotation
    summaries from new post-rotation turns.  Non-compacted messages appear
    under ``### Previous Turns``.

    For regular messages, uses ``[User]:`` / ``[{assignee_name}]:`` markers
    consistent with :func:`chat_runtime.build_transcript`.  For compacted
    messages (``compacted: true``), outputs the content directly — it is
    already a formatted transcript from the Sonnet compaction pass.  Messages
    with role ``tool_action`` are emitted verbatim (they already carry their
    own bracketed label).

    Returns ``""`` for an empty list — never raises.
    """
    if not messages:
        return ""
    try:
        compacted_lines: list[str] = []
        turn_lines: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue

            # Compacted message: collect separately
            if msg.get("compacted"):
                content = msg.get("content", "")
                if content:
                    compacted_lines.append(content)
                continue

            # POS-1862: tool-action notes already carry their own
            # ``[Read]:``/``[Bash]:`` label — emit them verbatim.
            if msg.get("role") == TOOL_ACTION_ROLE:
                note = msg.get("content", "")
                if isinstance(note, str) and note:
                    turn_lines.append(note)
                continue

            role = msg.get("role")
            content = msg.get("content", "")
            content = content if isinstance(content, str) else ""
            label = assignee_name if role == "assistant" else "User"

            line = f"[{label}]: {content}"
            turn_lines.append(line)

        if not compacted_lines and not turn_lines:
            return ""

        parts: list[str] = []
        if compacted_lines:
            compacted_transcript = "\n".join(compacted_lines)
            compacted_transcript = compacted_transcript.replace("**", "")
            parts.append(
                f"### Compacted data from previous turns\n\n{compacted_transcript}"
            )
        if turn_lines:
            turns_transcript = "\n".join(turn_lines)
            turns_transcript = turns_transcript.replace("**", "")
            parts.append(f"### Previous Turns\n\n{turns_transcript}")

        return "\n\n".join(parts)
    except Exception as exc:  # noqa: BLE001 — must never raise
        logger.warning("render_memory_messages failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Stdin assembly
# ---------------------------------------------------------------------------


def build_session_stdin(
    project_root: str,
    session_id: str,
    system_prompt: str,
    memory_text: str,
    current_user_msg: str,
    saved_items_text: str = "",
    current_date: str = "",
) -> str:
    """Assemble the final stdin payload from the system prompt + memory + saved items + current turn.

    The string returned by this function is identical to what is written to
    ``last_stdin.txt`` and piped to the ``claude`` child's stdin — this is the
    byte-exact contract.

    Combines ``system_prompt + "\\n\\n" + memory_text +
    "\\n\\n### Current Turn\\nUser: " + current_user_msg``, writes the result
    byte-exact (``newline=""``, no translation) to
    ``.praxis/chat/sessions/<session_id>/last_stdin.txt`` for offline inspection
    (mirrors the existing chat debug artifact contract), and returns it.
    The debug write is best-effort; a failure there is logged but does not
    prevent the combined text from being returned and used for the turn.
    """
    parts = [system_prompt]
    if saved_items_text:
        parts.append(saved_items_text)
    parts.append(memory_text)
    date_line = f"\nYou must be aware about current date: {current_date}" if current_date else ""
    combined = "\n\n".join(parts) + f"\n\n### Current Turn{date_line}\nUser: {current_user_msg}"
    if memory_text and "NAME:" in memory_text and "ROLE:" in memory_text and "MISSION:" in memory_text:
        logger.warning(
            "build_session_stdin: memory_text may contain assignee profile "
            "(NAME/ROLE/MISSION sentinel detected) — possible duplication"
        )
    try:
        directory = _session_dir(project_root, session_id)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, SESSION_LAST_STDIN_FILENAME)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(combined)
    except Exception as exc:  # noqa: BLE001 — debug write must never break a turn
        logger.warning("build_session_stdin: last_stdin.txt write failed: %s", exc)
    return combined
