"""Per-session realtime conversation log (POS-1890).

``realtime_log.txt`` is a lightweight, token-efficient view of the session
conversation — no system prompt, includes AI responses, updated during
streaming.  Format::

    workDir: /absolute/path

    ### Saved items

    <saved items blocks>

    ### Realtime log (previous turns + realtime answers from AI)

    [User]: message
    [Read]: path/to/file
    [Jarvis]: response text

Designed so another AI can read session history without parsing ``full.json``
or consuming the system prompt tokens in ``last_stdin.txt``.

Every public function degrades to no-op/empty on error — a realtime log
failure must never break or block a chat turn.
"""

from __future__ import annotations

import logging
import os

from chat_session_paths import session_dir

logger = logging.getLogger(__name__)

REALTIME_LOG_FILENAME = "realtime_log.txt"


def build_realtime_log_base(
    work_dir: str,
    saved_items_text: str,
    memory_text: str,
    current_user_msg: str,
) -> str:
    """Build the initial realtime_log content (before AI response starts).

    ``memory_text`` is expected in the format returned by
    :func:`chat_session_memory.render_memory_messages`.  When it contains
    ``### Compacted data from previous turns``, that section is preserved
    verbatim (it feeds future rotation compactions).
    ``### Previous Turns`` is replaced with the realtime log header.
    ``saved_items_text`` is the output of
    :func:`chat_saved_items.render_saved_items`.
    """
    parts: list[str] = []

    # workDir header (no system prompt — token-efficient)
    normalized = work_dir.rstrip("/\\") if work_dir else ""
    if normalized:
        parts.append(f"workDir: {normalized}")

    # Saved items section (render_saved_items already includes the ### header)
    if saved_items_text:
        parts.append(saved_items_text)

    rt_header = "### Realtime log (previous turns + realtime answers from AI)"

    if memory_text:
        has_previous = "### Previous Turns" in memory_text

        if has_previous:
            # Replace "### Previous Turns" with realtime log header;
            # "### Compacted data from previous turns" stays as-is.
            log_text = memory_text.replace(
                "### Previous Turns", rt_header, 1
            )
            parts.append(log_text)
        else:
            # Only compacted data or raw text — keep it and add the
            # realtime log header for the current turn.
            parts.append(memory_text)
            parts.append(rt_header)
    else:
        parts.append(rt_header)

    base = "\n\n".join(parts)
    # Append current user message (starts the current turn)
    base += f"\n[User]: {current_user_msg}"

    return base


def build_realtime_log_content(
    base: str,
    tool_notes: list[str],
    ai_text: str,
    assignee_name: str = "Assistant",
) -> str:
    """Combine the base with current-turn tool notes and AI response text."""
    content = base
    if tool_notes:
        content += "\n" + "\n".join(tool_notes)
    if ai_text:
        # Strip bold markdown (matches render_memory_messages behaviour)
        clean_text = ai_text.replace("**", "")
        content += f"\n[{assignee_name}]: {clean_text}"
    return content


def write_realtime_log(
    project_root: str,
    session_id: str,
    content: str,
) -> None:
    """Write (overwrite) ``realtime_log.txt`` in the session directory.

    Best-effort — a failure is logged but never raises.
    """
    try:
        directory = session_dir(project_root, session_id)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, REALTIME_LOG_FILENAME)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    except Exception as exc:  # noqa: BLE001 — must never break a chat turn
        logger.warning("write_realtime_log failed: %s", exc)
