"""Tests for chat_tool_actions — per-session tool-action notes (POS-1862).

Run from the ``api/`` directory:

    python3 -m pytest test_chat_tool_actions.py -v

Covers: note construction for tracked tools (Read/Write/Edit/Bash), tool-name
normalization, ok/error status suffixing, the pending-actions buffer's
load/save/clear round trip, and merging notes into the memory message list.
"""

from __future__ import annotations

from chat_tool_actions import (
    TOOL_ACTION_ROLE,
    build_tool_note,
    clear_pending_actions,
    load_pending_actions,
    merge_tool_actions,
    normalize_tool_name,
    save_pending_actions,
    with_status,
)


# ---------------------------------------------------------------------------
# normalize_tool_name
# ---------------------------------------------------------------------------
def test_normalize_tool_name_strips_mcp_prefix():
    assert normalize_tool_name("mcp__x__Read") == "Read"


def test_normalize_tool_name_plain_name_unchanged():
    assert normalize_tool_name("Read") == "Read"


def test_normalize_tool_name_non_str_returns_empty():
    assert normalize_tool_name(None) == ""


# ---------------------------------------------------------------------------
# build_tool_note — file tools
# ---------------------------------------------------------------------------
def test_build_tool_note_read_strips_project_root():
    note = build_tool_note("Read", {"file_path": "/proj/api/chat_runs.py"}, "/proj")
    assert note == "[Read]: api/chat_runs.py"


def test_build_tool_note_write():
    note = build_tool_note("Write", {"file_path": "/proj/api/foo.py"}, "/proj")
    assert note == "[Write]: api/foo.py"


def test_build_tool_note_edit():
    note = build_tool_note("Edit", {"file_path": "/proj/api/foo.py"}, "/proj")
    assert note == "[Edit]: api/foo.py"


def test_build_tool_note_relative_path_unchanged():
    note = build_tool_note("Read", {"file_path": "api/foo.py"}, "/proj")
    assert note == "[Read]: api/foo.py"


def test_build_tool_note_mcp_prefixed_name_normalizes():
    note = build_tool_note("mcp__x__Read", {"file_path": "/proj/api/foo.py"}, "/proj")
    assert note == "[Read]: api/foo.py"


def test_build_tool_note_falls_back_to_path_key():
    note = build_tool_note("Read", {"path": "/proj/api/foo.py"}, "/proj")
    assert note == "[Read]: api/foo.py"


def test_build_tool_note_falls_back_to_notebook_path_key():
    note = build_tool_note("Read", {"notebook_path": "/proj/api/nb.ipynb"}, "/proj")
    assert note == "[Read]: api/nb.ipynb"


def test_build_tool_note_unknown_tool_returns_none():
    assert build_tool_note("Glob", {"file_path": "/proj/api/foo.py"}, "/proj") is None


def test_build_tool_note_untracked_mcp_tool_returns_none():
    assert build_tool_note("get_task", {"task_id": "1"}, "/proj") is None


def test_build_tool_note_missing_file_path_returns_none():
    assert build_tool_note("Read", {}, "/proj") is None


def test_build_tool_note_empty_file_path_returns_none():
    assert build_tool_note("Read", {"file_path": ""}, "/proj") is None


def test_build_tool_note_non_dict_input_returns_none():
    assert build_tool_note("Read", "not-a-dict", "/proj") is None


# ---------------------------------------------------------------------------
# build_tool_note — Bash
# ---------------------------------------------------------------------------
def test_build_tool_note_bash_command():
    note = build_tool_note("Bash", {"command": "git status"}, "/proj")
    assert note == "[Bash]: git status"


def test_build_tool_note_bash_multiline_collapses_to_one_line():
    note = build_tool_note("Bash", {"command": "git status\n  --short\n"}, "/proj")
    assert note == "[Bash]: git status --short"
    assert "\n" not in note


def test_build_tool_note_bash_empty_command_returns_none():
    assert build_tool_note("Bash", {"command": ""}, "/proj") is None
    assert build_tool_note("Bash", {"command": "   "}, "/proj") is None


# ---------------------------------------------------------------------------
# with_status
# ---------------------------------------------------------------------------
def test_with_status_ok():
    note = with_status("[Bash]: git status", False)
    assert note.endswith("→ ok")


def test_with_status_error():
    note = with_status("[Bash]: git status", True)
    assert note.endswith("→ error")


def test_with_status_non_str_note_returns_empty():
    assert with_status(None, False) == ""


# ---------------------------------------------------------------------------
# pending-actions buffer
# ---------------------------------------------------------------------------
def test_save_and_load_pending_actions_round_trip(tmp_path):
    project_root = str(tmp_path)
    session_id = "sess-1"
    notes = ["[Read]: api/foo.py", "[Bash]: git status → ok"]

    save_pending_actions(project_root, session_id, notes)
    loaded = load_pending_actions(project_root, session_id)
    assert loaded == notes

    target = tmp_path / ".praxis" / "chat" / "sessions" / session_id / "pending_tool_actions.json"
    assert target.exists()


def test_load_pending_actions_missing_file_returns_empty(tmp_path):
    assert load_pending_actions(str(tmp_path), "no-such-session") == []


def test_load_pending_actions_corrupt_json_returns_empty(tmp_path):
    session_dir = tmp_path / ".praxis" / "chat" / "sessions" / "sess-2"
    session_dir.mkdir(parents=True)
    (session_dir / "pending_tool_actions.json").write_text("{not json", encoding="utf-8")
    assert load_pending_actions(str(tmp_path), "sess-2") == []


def test_load_pending_actions_empty_session_id_returns_empty(tmp_path):
    assert load_pending_actions(str(tmp_path), "") == []


def test_clear_pending_actions_removes_file(tmp_path):
    project_root = str(tmp_path)
    session_id = "sess-3"
    save_pending_actions(project_root, session_id, ["[Read]: api/foo.py"])
    clear_pending_actions(project_root, session_id)
    assert load_pending_actions(project_root, session_id) == []


def test_clear_pending_actions_noop_when_already_absent(tmp_path):
    # Must not raise.
    clear_pending_actions(str(tmp_path), "no-such-session")


# ---------------------------------------------------------------------------
# merge_tool_actions
# ---------------------------------------------------------------------------
def test_merge_tool_actions_inserts_before_trailing_assistant_message():
    messages = [
        {"role": "user", "content": "do stuff"},
        {"role": "assistant", "content": "done"},
    ]
    notes = ["[Read]: api/foo.py", "[Bash]: git status → ok"]
    merged = merge_tool_actions(messages, notes)

    roles = [m["role"] for m in merged]
    assert roles == ["user", TOOL_ACTION_ROLE, TOOL_ACTION_ROLE, "assistant"]

    user_idx = roles.index("user")
    assistant_idx = roles.index("assistant")
    tool_action_indices = [i for i, r in enumerate(roles) if r == TOOL_ACTION_ROLE]
    assert user_idx < min(tool_action_indices) < assistant_idx
    assert max(tool_action_indices) < assistant_idx


def test_merge_tool_actions_appends_when_last_message_is_user():
    messages = [{"role": "user", "content": "hi"}]
    merged = merge_tool_actions(messages, ["[Read]: api/foo.py"])
    assert [m["role"] for m in merged] == ["user", TOOL_ACTION_ROLE]


def test_merge_tool_actions_empty_notes_returns_unchanged():
    messages = [{"role": "user", "content": "hi"}]
    merged = merge_tool_actions(messages, [])
    assert merged == messages
    assert merged is not messages


def test_merge_tool_actions_does_not_mutate_input_list():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    original = list(messages)
    merge_tool_actions(messages, ["[Read]: api/foo.py"])
    assert messages == original
