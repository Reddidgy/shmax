"""Shared constants for the Praxis Local API chat subsystem."""

from __future__ import annotations

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"


def is_ask_user_question(name: str) -> bool:
    """Match AskUserQuestion regardless of MCP server prefix.

    The tool may appear as ``"AskUserQuestion"`` (legacy built-in) or as
    ``"mcp__praxis__AskUserQuestion"`` (MCP-registered).
    """
    return name == ASK_USER_QUESTION_TOOL_NAME or name.endswith(
        "__" + ASK_USER_QUESTION_TOOL_NAME
    )


# --- Chat debug artifacts ---
# The debug directory <project_root>/.praxis/chat/debug/ holds exactly these
# three files. The two payload files are BYTE-EXACT copies of what the model
# received — no framing, no metadata, nothing the model did not see.
DEBUG_INPUT_FILENAME = "last_input.json"
DEBUG_SYSTEM_PROMPT_FILENAME = "last_system_prompt.txt"
DEBUG_STDIN_FILENAME = "last_stdin.txt"

# Files copied from the active session directory to .praxis/chat/debug/
# at AI chat invocation time (POS-1856). Exact copies, no transformation.
SESSION_DEBUG_COPY_FILES = ("last_stdin.txt", "full.json", "memory.json", "saved_items.json")


__all__ = [
    "ASK_USER_QUESTION_TOOL_NAME",
    "is_ask_user_question",
    "DEBUG_INPUT_FILENAME",
    "DEBUG_SYSTEM_PROMPT_FILENAME",
    "DEBUG_STDIN_FILENAME",
    "SESSION_DEBUG_COPY_FILES",
]
