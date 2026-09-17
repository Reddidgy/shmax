"""Per-session saved items (POS-1858).

Persists important tool results (task specs, codex content) so they survive
memory compaction and are re-injected into subsequent turns' stdin payloads.
Every public function degrades gracefully — a saved-items failure must never
break or block a chat turn.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from chat_session_paths import session_dir

logger = logging.getLogger(__name__)

__all__ = [
    "load_saved_items",
    "add_saved_item",
    "render_saved_items",
]

SAVED_ITEMS_FILENAME = "saved_items.json"


def _saved_items_path(project_root: str, session_id: str) -> str:
    return os.path.join(session_dir(project_root, session_id), SAVED_ITEMS_FILENAME)


def load_saved_items(project_root: str, session_id: str) -> list[dict]:
    """Load saved items from the session directory.

    Returns an empty list when the file is missing, unreadable, or corrupt.
    Never raises.
    """
    if not session_id:
        return []
    path = _saved_items_path(project_root, session_id)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("saved_items file at %s unreadable: %s", path, exc)
        return []
    try:
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [it for it in items if isinstance(it, dict) and "source" in it and "content" in it]
    except Exception as exc:
        logger.warning("load_saved_items parse failed for %s: %s", path, exc)
        return []


def add_saved_item(project_root: str, session_id: str, source: str, content: str) -> None:
    """Add an item to saved_items.json, deduplicating by source.

    If an item with the same source already exists, it is replaced with the
    new content. Creates the session directory and file if needed.
    Atomic write via tempfile+rename. Never raises.
    """
    if not session_id or not source or not content:
        return
    path = _saved_items_path(project_root, session_id)
    directory = os.path.dirname(path)
    tmp_path: str | None = None
    try:
        existing = load_saved_items(project_root, session_id)
        updated = [it for it in existing if it.get("source") != source]
        updated.append({"source": source, "content": content})

        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False, encoding="utf-8"
        ) as fh:
            tmp_path = fh.name
            json.dump({"items": updated}, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.warning("add_saved_item failed for session %s: %s", session_id, exc)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def render_saved_items(items: list[dict]) -> str:
    """Render saved items as a ``### Saved items`` section for the stdin payload.

    Each item becomes a fenced code block labeled with its source.
    Returns ``""`` for an empty list. Never raises.
    """
    if not items:
        return ""
    try:
        blocks: list[str] = []
        for item in items:
            source = item.get("source", "")
            content = item.get("content", "")
            if source and content:
                blocks.append(f"```{source}\n{content}\n```")
        if not blocks:
            return ""
        return "### Saved items\n\n" + "\n\n".join(blocks)
    except Exception as exc:
        logger.warning("render_saved_items failed: %s", exc)
        return ""
