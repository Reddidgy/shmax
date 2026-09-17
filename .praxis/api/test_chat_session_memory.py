"""Tests for chat_session_memory — per-session chat memory (POS-1838 / POS-1852).

Run from the ``api/`` directory:

    python3 -m pytest test_chat_session_memory.py -v

Covers: message-format memory load/save round trip, legacy format migration,
compaction persistence, full-detail message rendering with transcript-format
role labels, compacted-message passthrough rendering, and the final stdin
assembly + debug artifact write.
"""

from __future__ import annotations

import json

from chat_session_memory import (
    build_session_stdin,
    load_memory_messages,
    persist_compaction,
    render_memory_messages,
    save_memory_messages,
)
from chat_tool_actions import TOOL_ACTION_ROLE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _msg(role, content, **kwargs):
    message = {"role": role, "content": content}
    message.update(kwargs)
    return message


# ---------------------------------------------------------------------------
# load_memory_messages
# ---------------------------------------------------------------------------
def test_load_memory_messages_missing_file_returns_none(tmp_path):
    assert load_memory_messages(str(tmp_path), "no-such-session") is None


def test_load_memory_messages_corrupt_json_returns_none(tmp_path):
    session_dir = tmp_path / ".praxis" / "chat" / "sessions" / "corrupt-session"
    session_dir.mkdir(parents=True)
    (session_dir / "memory.json").write_text("{not valid json", encoding="utf-8")
    assert load_memory_messages(str(tmp_path), "corrupt-session") is None


def test_load_memory_messages_non_dict_json_returns_none(tmp_path):
    session_dir = tmp_path / ".praxis" / "chat" / "sessions" / "weird-session"
    session_dir.mkdir(parents=True)
    (session_dir / "memory.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_memory_messages(str(tmp_path), "weird-session") is None


def test_load_memory_messages_legacy_turns_format_returns_none(tmp_path):
    """Old SessionMemory format (has 'turns' key) triggers re-initialisation."""
    session_dir = tmp_path / ".praxis" / "chat" / "sessions" / "legacy-session"
    session_dir.mkdir(parents=True)
    legacy = {"turns": [{"turn_number": 1, "user_summary": "hi", "activities": [], "outcome": "ok"}]}
    (session_dir / "memory.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert load_memory_messages(str(tmp_path), "legacy-session") is None


def test_load_memory_messages_valid_new_format(tmp_path):
    session_dir = tmp_path / ".praxis" / "chat" / "sessions" / "valid-session"
    session_dir.mkdir(parents=True)
    data = {"messages": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]}
    (session_dir / "memory.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_memory_messages(str(tmp_path), "valid-session")
    assert result is not None
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[1]["content"] == "hi there"


def test_load_memory_messages_empty_session_id_returns_none():
    assert load_memory_messages("/tmp", "") is None


def test_load_memory_messages_skips_non_dict_entries(tmp_path):
    session_dir = tmp_path / ".praxis" / "chat" / "sessions" / "mixed-session"
    session_dir.mkdir(parents=True)
    data = {"messages": [
        {"role": "user", "content": "ok"},
        "not a dict",
        42,
        {"role": "assistant", "content": "done"},
    ]}
    (session_dir / "memory.json").write_text(json.dumps(data), encoding="utf-8")
    result = load_memory_messages(str(tmp_path), "mixed-session")
    assert result is not None
    assert len(result) == 2


# ---------------------------------------------------------------------------
# save_memory_messages
# ---------------------------------------------------------------------------
def test_save_and_load_round_trip(tmp_path):
    project_root = str(tmp_path)
    session_id = "session-abc123"
    messages = [
        _msg("user", "hello"),
        _msg("assistant", "hi", tool_summary="called Read path=a.py → ok"),
        _msg("user", "next"),
        _msg("assistant", "done"),
    ]
    save_memory_messages(project_root, session_id, messages)

    expected_path = tmp_path / ".praxis" / "chat" / "sessions" / session_id / "memory.json"
    assert expected_path.exists()

    loaded = load_memory_messages(project_root, session_id)
    assert loaded is not None
    assert len(loaded) == 4
    assert loaded[0]["content"] == "hello"
    assert loaded[1]["tool_summary"] == "called Read path=a.py → ok"
    assert loaded[3]["content"] == "done"


def test_save_memory_messages_empty_session_id_is_noop(tmp_path):
    save_memory_messages(str(tmp_path), "", [_msg("user", "hi")])
    assert not (tmp_path / ".praxis" / "chat").exists()


def test_save_memory_messages_sanitizes_unsafe_session_id(tmp_path):
    project_root = str(tmp_path)
    save_memory_messages(project_root, "../../etc/passwd", [_msg("user", "hi")])
    chat_dir = tmp_path / ".praxis" / "chat"
    for path in chat_dir.rglob("memory.json"):
        assert chat_dir in path.parents


def test_save_memory_messages_empty_list(tmp_path):
    project_root = str(tmp_path)
    save_memory_messages(project_root, "empty-session", [])
    loaded = load_memory_messages(project_root, "empty-session")
    assert loaded is not None
    assert loaded == []


# ---------------------------------------------------------------------------
# persist_compaction
# ---------------------------------------------------------------------------
def test_persist_compaction_replaces_with_single_message(tmp_path):
    project_root = str(tmp_path)
    session_id = "compact-session"
    # Pre-populate with several messages
    save_memory_messages(project_root, session_id, [
        _msg("user", "msg1"),
        _msg("assistant", "reply1"),
        _msg("user", "msg2"),
        _msg("assistant", "reply2"),
    ])

    compacted = "[User]: summarised request\n[Assistant]: summarised work"
    persist_compaction(project_root, session_id, compacted)

    loaded = load_memory_messages(project_root, session_id)
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0]["role"] == "assistant"
    assert loaded[0]["content"] == compacted
    assert loaded[0]["compacted"] is True


def test_persist_compaction_works_on_missing_memory(tmp_path):
    """persist_compaction must work even when memory.json doesn't exist yet."""
    persist_compaction(str(tmp_path), "new-session", "compacted text")
    loaded = load_memory_messages(str(tmp_path), "new-session")
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0]["compacted"] is True


# ---------------------------------------------------------------------------
# render_memory_messages
# ---------------------------------------------------------------------------
def test_render_memory_messages_empty_returns_empty_string():
    assert render_memory_messages([]) == ""


def test_render_memory_messages_basic_transcript():
    messages = [
        _msg("user", "hello world"),
        _msg("assistant", "hi there"),
    ]
    text = render_memory_messages(messages)
    assert "### Previous Turns" in text
    assert "[User]: hello world" in text
    assert "[Assistant]: hi there" in text


def test_render_memory_messages_with_tool_summary():
    """tool_summary is not included in rendered memory — only content is rendered."""
    messages = [
        _msg("user", "read a file"),
        _msg("assistant", "done", tool_summary="called Read path=a.py → ok"),
    ]
    text = render_memory_messages(messages)
    assert "(called Read path=a.py → ok)" not in text
    assert "[Assistant]: done" in text


def test_render_memory_messages_with_thinking():
    """thinking is not included in rendered memory — only content is rendered."""
    messages = [
        _msg("user", "think about this"),
        _msg("assistant", "here's my answer", thinking="considered option A and B"),
    ]
    text = render_memory_messages(messages)
    assert "(thinking: considered option A and B)" not in text
    assert "[Assistant]: here's my answer" in text


def test_render_memory_messages_compacted_passthrough():
    """A compacted message's content is output under a separate header."""
    compacted_content = "[User]: old request\n[Assistant]: old answer\n[User]: second\n[Assistant]: done"
    messages = [
        {"role": "assistant", "content": compacted_content, "compacted": True},
        _msg("user", "new question"),
        _msg("assistant", "new answer"),
    ]
    text = render_memory_messages(messages)
    # Compacted content appears under its own header
    assert "### Compacted data from previous turns" in text
    assert "[User]: old request" in text
    assert "[Assistant]: old answer" in text
    # New messages rendered under "### Previous Turns"
    assert "### Previous Turns" in text
    assert "[User]: new question" in text
    assert "[Assistant]: new answer" in text
    # Compacted header comes before Previous Turns
    compacted_idx = text.index("### Compacted data from previous turns")
    previous_idx = text.index("### Previous Turns")
    assert compacted_idx < previous_idx


def test_render_memory_messages_uses_assignee_name():
    messages = [
        _msg("user", "hi"),
        _msg("assistant", "hello"),
    ]
    text = render_memory_messages(messages, assignee_name="Critic")
    assert "[Critic]:" in text
    assert "[Assistant]:" not in text


def test_render_memory_messages_strips_bold_markers():
    messages = [
        _msg("user", "**bold** request"),
        _msg("assistant", "**bold** response"),
    ]
    text = render_memory_messages(messages)
    assert "**" not in text
    assert "bold request" in text
    assert "bold response" in text


def test_render_memory_messages_large_session_all_turns_present():
    """Even a very large session renders ALL turns at full detail."""
    messages = []
    for n in range(1, 101):
        messages.append(_msg("user", f"user message {n}"))
        messages.append(_msg("assistant", f"assistant reply {n}"))
    text = render_memory_messages(messages)
    for n in range(1, 101):
        assert f"user message {n}" in text
        assert f"assistant reply {n}" in text


def test_render_memory_messages_emits_tool_action_notes_unlabeled():
    """tool_action notes render verbatim, without [User]:/[Assistant]: labels."""
    messages = [
        _msg("user", "Fix bug"),
        {"role": TOOL_ACTION_ROLE, "content": "[Read]: api/chat_runs.py"},
        {"role": TOOL_ACTION_ROLE, "content": "[Bash]: pytest -q → ok"},
        _msg("assistant", "Fixed"),
    ]
    text = render_memory_messages(messages)
    assert "[Read]: api/chat_runs.py" in text
    assert "[Bash]: pytest -q → ok" in text
    assert "[User]: [Read]: api/chat_runs.py" not in text
    assert "[Assistant]: [Read]: api/chat_runs.py" not in text

    lines = text.split("\n")
    user_idx = next(i for i, ln in enumerate(lines) if ln == "[User]: Fix bug")
    read_idx = next(i for i, ln in enumerate(lines) if ln == "[Read]: api/chat_runs.py")
    bash_idx = next(i for i, ln in enumerate(lines) if ln == "[Bash]: pytest -q → ok")
    assistant_idx = next(i for i, ln in enumerate(lines) if ln == "[Assistant]: Fixed")
    assert user_idx < read_idx < bash_idx < assistant_idx


def test_render_memory_messages_skips_empty_tool_action():
    """An empty tool_action note produces no stray line."""
    messages = [
        _msg("user", "hi"),
        {"role": TOOL_ACTION_ROLE, "content": ""},
        _msg("assistant", "hello"),
    ]
    text = render_memory_messages(messages)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    assert lines == ["### Previous Turns", "[User]: hi", "[Assistant]: hello"]


# ---------------------------------------------------------------------------
# build_session_stdin
# ---------------------------------------------------------------------------
def test_build_session_stdin_format(tmp_path):
    project_root = str(tmp_path)
    session_id = "sess-1"
    text = build_session_stdin(
        project_root,
        session_id,
        "SYSTEM PROMPT HERE",
        "### Turn 1 ...\nsome memory text",
        "what should I do next?",
    )
    assert text == (
        "SYSTEM PROMPT HERE\n\n"
        "### Turn 1 ...\nsome memory text"
        "\n\n### Current Turn\nUser: what should I do next?"
    )


def test_build_session_stdin_writes_last_stdin_txt_byte_exact(tmp_path):
    project_root = str(tmp_path)
    session_id = "sess-2"
    text = build_session_stdin(
        project_root, session_id, "SYS", "MEMORY", "current message"
    )
    target = tmp_path / ".praxis" / "chat" / "sessions" / session_id / "last_stdin.txt"
    assert target.exists()
    with open(target, encoding="utf-8", newline="") as handle:
        on_disk = handle.read()
    assert on_disk == text


def test_build_session_stdin_returns_text_even_if_write_fails(tmp_path, monkeypatch):
    """A debug-write failure must never prevent the payload from being usable."""
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("chat_session_memory.os.makedirs", _boom)
    text = build_session_stdin(str(tmp_path), "sess-3", "SYS", "MEM", "hi")
    assert "SYS" in text and "MEM" in text and "hi" in text


def test_build_session_stdin_no_session_memory_header(tmp_path):
    """The '## Session Memory' wrapper header must not appear in the output."""
    text = build_session_stdin(
        str(tmp_path), "sess-no-header", "SYS PROMPT", "### Previous Turns\n...", "hello"
    )
    assert "## Session Memory" not in text
    assert "### Previous Turns" in text
    assert "### Current Turn\nUser: hello" in text


def test_build_session_stdin_warns_on_assignee_in_memory(tmp_path, caplog):
    """Defensive dedup: warn if memory_text contains assignee sentinel."""
    import logging
    with caplog.at_level(logging.WARNING, logger="chat_session_memory"):
        build_session_stdin(
            str(tmp_path),
            "sess-dedup",
            "SYSTEM PROMPT",
            "NAME: Jarvis\nROLE: Engineer\nMISSION: Ship code",
            "hello",
        )
    assert "assignee profile" in caplog.text


# ---------------------------------------------------------------------------
# save_claude_session_id / load_claude_session_id
# ---------------------------------------------------------------------------
def test_save_load_claude_session_id_round_trip(tmp_path):
    from chat_session_memory import save_claude_session_id, load_claude_session_id
    save_claude_session_id(str(tmp_path), "sid-1", "claude-abc-123")
    result = load_claude_session_id(str(tmp_path), "sid-1")
    assert result == "claude-abc-123"


def test_load_claude_session_id_missing_returns_none(tmp_path):
    from chat_session_memory import load_claude_session_id
    assert load_claude_session_id(str(tmp_path), "no-such-session") is None


def test_load_claude_session_id_empty_session_returns_none():
    from chat_session_memory import load_claude_session_id
    assert load_claude_session_id("/tmp", "") is None


def test_save_claude_session_id_creates_directory(tmp_path):
    from chat_session_memory import save_claude_session_id, load_claude_session_id
    save_claude_session_id(str(tmp_path), "new-session", "claude-xyz")
    assert load_claude_session_id(str(tmp_path), "new-session") == "claude-xyz"


def test_save_claude_session_id_empty_sid_is_noop(tmp_path):
    from chat_session_memory import save_claude_session_id, load_claude_session_id
    save_claude_session_id(str(tmp_path), "sid-2", "")
    assert load_claude_session_id(str(tmp_path), "sid-2") is None
