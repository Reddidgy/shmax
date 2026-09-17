"""Tests for chat_session_paths — shared session path helpers (POS-1846).

Run from the ``api/`` directory:

    python3 -m pytest test_chat_session_paths.py -v
"""

from __future__ import annotations

import os
import shutil
import tempfile

from chat_session_paths import sanitize_session_id, session_dir


def test_path_traversal_blocked():
    result = sanitize_session_id("../../../etc/passwd")
    assert ".." not in result
    assert "/" not in result
    assert "\\" not in result


def test_empty_string_defaults():
    assert sanitize_session_id("") == "default"


def test_whitespace_only_defaults():
    assert sanitize_session_id("   ") == "default"


def test_valid_id_passes_through():
    assert sanitize_session_id("my-session_01") == "my-session_01"


def test_long_id_truncated():
    long_id = "a" * 200
    result = sanitize_session_id(long_id)
    assert len(result) == 128


def test_session_dir_structure():
    result = session_dir("/project", "test-session")
    assert result == os.path.join("/project", ".praxis", "chat", "sessions", "test-session")


def test_claude_session_id_path_structure():
    from chat_session_paths import claude_session_id_path
    result = claude_session_id_path("/project", "test-session")
    assert result == os.path.join("/project", ".praxis", "chat", "sessions", "test-session", "claude_session_id.txt")


from chat_session_paths import enforce_session_limit, MAX_ACTIVE_SESSIONS


def _create_sessions(base_dir, count):
    """Helper: create numbered session directories with staggered mtimes."""
    sessions_dir = os.path.join(base_dir, ".praxis", "chat", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    for i in range(count):
        name = f"session-{i:03d}"
        path = os.path.join(sessions_dir, name)
        os.makedirs(path)
        # Write a dummy file so the dir has content
        with open(os.path.join(path, "memory.json"), "w") as f:
            f.write("{}")
        # Set mtime to staggered times (older = lower i)
        mtime = 1000000 + i * 100
        os.utime(path, (mtime, mtime))
    return sessions_dir


def test_enforce_session_limit_under_limit():
    """No archival when at or below limit."""
    with tempfile.TemporaryDirectory() as tmp:
        _create_sessions(tmp, 50)
        enforce_session_limit(tmp, max_sessions=50)
        sessions_dir = os.path.join(tmp, ".praxis", "chat", "sessions")
        archive_dir = os.path.join(tmp, ".praxis", "chat", "archive_sessions")
        assert len(os.listdir(sessions_dir)) == 50
        assert not os.path.exists(archive_dir)


def test_enforce_session_limit_over_limit():
    """Oldest sessions archived when over limit."""
    with tempfile.TemporaryDirectory() as tmp:
        _create_sessions(tmp, 53)
        enforce_session_limit(tmp, max_sessions=50)
        sessions_dir = os.path.join(tmp, ".praxis", "chat", "sessions")
        archive_dir = os.path.join(tmp, ".praxis", "chat", "archive_sessions")
        assert len(os.listdir(sessions_dir)) == 50
        assert os.path.isdir(archive_dir)
        archived = os.listdir(archive_dir)
        assert len(archived) == 3
        # The 3 oldest (session-000, session-001, session-002) should be archived
        assert set(archived) == {"session-000", "session-001", "session-002"}
        # Archived sessions should retain their contents
        assert os.path.isfile(os.path.join(archive_dir, "session-000", "memory.json"))


def test_enforce_session_limit_no_sessions_dir():
    """No error when sessions directory doesn't exist."""
    with tempfile.TemporaryDirectory() as tmp:
        enforce_session_limit(tmp, max_sessions=50)  # should not raise


def test_enforce_session_limit_exact_limit():
    """No archival at exactly the limit."""
    with tempfile.TemporaryDirectory() as tmp:
        _create_sessions(tmp, MAX_ACTIVE_SESSIONS)
        enforce_session_limit(tmp)
        sessions_dir = os.path.join(tmp, ".praxis", "chat", "sessions")
        assert len(os.listdir(sessions_dir)) == MAX_ACTIVE_SESSIONS
