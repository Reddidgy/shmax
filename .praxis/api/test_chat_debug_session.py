"""Chat debug artifact tests (POS-1596, POS-1777, POS-1777 follow-up).

The debug directory holds EXACTLY three files — ``last_input.json`` (the last
chat input: post-injection system prompt + session_id, JSON), and two
BYTE-EXACT copies of what the model actually received:
``last_system_prompt.txt`` (the ``--system-prompt`` argv value) and
``last_stdin.txt`` (the ``claude`` child's stdin payload, via
:func:`chat_runtime.build_stdin_payload`). Every write prunes any other file
so stale snapshots never accumulate. ``_write_chat_debug_input`` in isolation
still writes only its own ``last_input.json``; the full ``/api/chat`` turn
writes all three artifacts.
"""

from __future__ import annotations

import json

import praxis_local_api
from chat_runtime import build_stdin_payload, build_transcript, build_user_stream_json
from test_praxis_local_api import (  # reuse fixtures/helpers
    _make_project,
    _write_streaming_claude,
    client,
)

__all__ = ["client"]  # re-exported fixture used by the tests below


def test_debug_input_writes_single_file_with_session_id(tmp_path):
    """One write → exactly one file, carrying the originating session_id."""
    praxis_local_api._write_chat_debug_input(
        str(tmp_path),
        messages=[{"role": "user", "content": "hi"}],
        system_prompt="SP",
        model="sonnet",
        session_id="sess-123",
    )
    debug = tmp_path / ".praxis" / "chat" / "debug"
    files = sorted(p.name for p in debug.glob("*"))
    assert files == ["last_input.json"]
    artifact = json.loads((debug / "last_input.json").read_text())
    assert artifact["session_id"] == "sess-123"
    assert artifact["system_prompt"] == "SP"


def test_debug_input_carries_token_stats(tmp_path):
    """last_input.json carries an estimated per-turn input-token breakdown (POS-1827)."""
    messages = [
        {"role": "user", "content": "Please explain how the chat history window works."},
        {
            "role": "assistant",
            "content": "It bounds the request to the last MAX_HISTORY_TURNS pairs.",
        },
        {"role": "user", "content": "Great, and how is memory extraction wired up?"},
    ]
    system_prompt = "You are a helpful software-engineering assistant." * 5

    praxis_local_api._write_chat_debug_input(
        str(tmp_path),
        messages=messages,
        system_prompt=system_prompt,
        model="sonnet",
        session_id="sess-tokens",
    )

    debug = tmp_path / ".praxis" / "chat" / "debug"
    artifact = json.loads((debug / "last_input.json").read_text())
    token_stats = artifact["token_stats"]
    assert set(token_stats.keys()) == {
        "stdin_chars",
        "stdin_tokens_estimated",
        "system_prompt_chars",
        "system_prompt_tokens_estimated",
        "total_tokens_estimated",
    }
    assert token_stats["total_tokens_estimated"] > 0
    assert (
        token_stats["total_tokens_estimated"]
        == token_stats["stdin_tokens_estimated"] + token_stats["system_prompt_tokens_estimated"]
    )


def test_debug_input_prunes_stale_files(tmp_path):
    """Pre-existing stray files (e.g. old per-session snapshots) are removed."""
    debug = tmp_path / ".praxis" / "chat" / "debug"
    debug.mkdir(parents=True)
    (debug / "8cfc428e-old.json").write_text("{}")
    (debug / "a05b7721-old.json").write_text("{}")
    (debug / "last_input.json").write_text("{}")

    praxis_local_api._write_chat_debug_input(
        str(tmp_path),
        messages=[{"role": "user", "content": "hi"}],
        system_prompt="SP",
        model="sonnet",
        session_id="sess-xyz",
    )

    files = sorted(p.name for p in debug.glob("*"))
    assert files == ["last_input.json"]


def test_chat_writes_single_debug_file_with_injected_context(
    client, monkeypatch, tmp_path
):
    """End-to-end: after a turn the debug dir has all three files, with the injected context."""
    monkeypatch.setattr(praxis_local_api, "is_claude_available", lambda: True)
    root = _make_project(tmp_path)
    (root / "context-router.md").write_text("REAL ROUTER", encoding="utf-8")
    # Seed a stale per-session file to prove the turn prunes it.
    debug = root / ".praxis" / "chat" / "debug"
    debug.mkdir(parents=True)
    (debug / "stale-session.json").write_text("{}")

    fake = _write_streaming_claude(
        tmp_path,
        lines=[json.dumps({"type": "result", "subtype": "success", "is_error": False})],
    )
    monkeypatch.setattr(praxis_local_api, "_resolve_claude_binary", lambda: fake)

    system_prompt = "ASSIGNEE ONLY\n\n# Praxis MCP Codex"
    resp = client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "system_prompt": system_prompt,
            "project_root": str(root),
            "context_flags": {
                "context_router_requested": True,
                "prd_requested": False,
            },
            "session_id": "sess-abc",
        },
    )
    assert resp.status_code == 200
    resp.get_data()

    files = sorted(p.name for p in debug.glob("*"))
    # The three original debug artifacts plus full.json and memory.json
    # copied from the session directory (POS-1856). Note: full.json and
    # memory.json only appear when the session dir has them; this turn
    # is the first, so they may not exist yet — assert at least the
    # original three.
    assert "last_input.json" in files
    assert "last_stdin.txt" in files
    assert "last_system_prompt.txt" in files
    artifact = json.loads((debug / "last_input.json").read_text())
    assert artifact["session_id"] == "sess-abc"
    assert "# Context Router\nREAL ROUTER" in artifact["system_prompt"]

    # The two payload files are byte-exact copies of what the model received:
    # no headers, no separators, no metadata.
    final_system_prompt = artifact["system_prompt"]
    system_prompt_text = (debug / "last_system_prompt.txt").read_text(
        encoding="utf-8"
    )
    assert system_prompt_text == final_system_prompt
    assert "=== SYSTEM PROMPT ===" not in system_prompt_text

    stdin_text = (debug / "last_stdin.txt").read_text(encoding="utf-8")
    expected_stdin = build_stdin_payload(artifact["messages"], work_dir=str(root))
    assert stdin_text == expected_stdin
    assert "=== TRANSCRIPT" not in stdin_text
    assert "session=" not in stdin_text


def test_write_chat_debug_payload_is_byte_exact_no_trailing_newline(tmp_path):
    """The two payload files carry exactly the given bytes — no added newline."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo", "thinking": "t"},
        {"role": "user", "content": "again"},
    ]
    system_prompt = "SYSTEM PROMPT HERE"

    praxis_local_api._write_chat_debug_payload(
        str(tmp_path), messages=messages, system_prompt=system_prompt
    )

    debug = tmp_path / ".praxis" / "chat" / "debug"
    system_prompt_text = (debug / "last_system_prompt.txt").read_text(
        encoding="utf-8"
    )
    stdin_text = (debug / "last_stdin.txt").read_text(encoding="utf-8")

    assert system_prompt_text == system_prompt
    assert stdin_text == build_stdin_payload(messages, work_dir=str(tmp_path))
    assert stdin_text == build_transcript(messages)  # no images → plain transcript
    assert "===" not in system_prompt_text
    assert "===" not in stdin_text
    # No trailing newline was added on top of the exact payload bytes.
    assert not system_prompt_text.endswith("\n")
    assert not stdin_text.endswith("\n")


def test_build_stdin_payload_produces_relative_paths(tmp_path):
    """POS-1854: absolute project paths become relative in stdin."""
    project = str(tmp_path / "my" / "project")
    messages = [
        {
            "role": "user",
            "content": f"Fix the bug in {project}/src/app.ts",
        },
        {"role": "assistant", "content": "I'll look at the file."},
        {"role": "user", "content": f"Also check {project}/README.md"},
    ]
    payload = build_stdin_payload(messages, work_dir=project)
    assert "src/app.ts" in payload
    assert "README.md" in payload
    assert project + "/" not in payload


def test_build_stdin_payload_no_replacement_without_work_dir():
    """POS-1831: without work_dir the payload is unchanged (backward compat)."""
    messages = [
        {"role": "user", "content": "Fix /some/path/file.ts"},
    ]
    without = build_stdin_payload(messages)
    with_none = build_stdin_payload(messages, work_dir=None)
    assert without == with_none
    assert "/some/path/file.ts" in without


def test_build_stdin_payload_strips_trailing_separator():
    """POS-1831: trailing slash on work_dir is stripped before replacement."""
    messages = [
        {"role": "user", "content": "Edit /proj/src/a.ts and /proj/src/b.ts"},
    ]
    payload = build_stdin_payload(messages, work_dir="/proj/")
    assert "src/a.ts" in payload
    assert "src/b.ts" in payload


def test_write_chat_debug_payload_applies_workdir_replacement(tmp_path):
    """POS-1854: last_stdin.txt reflects the relative-path replacement."""
    project_root = str(tmp_path)
    messages = [
        {"role": "user", "content": f"Check {project_root}/main.py"},
    ]
    praxis_local_api._write_chat_debug_payload(
        project_root, messages=messages, system_prompt="SP"
    )
    debug = tmp_path / ".praxis" / "chat" / "debug"
    stdin_text = (debug / "last_stdin.txt").read_text(encoding="utf-8")
    assert "main.py" in stdin_text
    assert project_root + "/" not in stdin_text


def test_write_chat_debug_payload_image_turn_uses_stream_json(tmp_path):
    """Regression test (POS-1777 follow-up): image turns must not be reconstructed.

    The old writer always called ``build_transcript(messages)``, which is wrong
    whenever the turn has images — the real worker sends the stream-json
    envelope instead. This proves ``last_stdin.txt`` matches the stream-json
    form and explicitly does NOT match the plain transcript, so it would fail
    against the old reconstruction logic.
    """
    messages = [
        {
            "role": "user",
            "content": "what is this?",
            "images": [
                {"media_type": "image/png", "data": "aGVsbG8="},
            ],
        }
    ]
    system_prompt = "SP"

    praxis_local_api._write_chat_debug_payload(
        str(tmp_path), messages=messages, system_prompt=system_prompt
    )

    debug = tmp_path / ".praxis" / "chat" / "debug"
    stdin_text = (debug / "last_stdin.txt").read_text(encoding="utf-8")

    assert stdin_text == build_user_stream_json(messages) + "\n"
    assert stdin_text != build_transcript(messages)


def test_copy_session_debug_files_copies_three_files(tmp_path):
    """POS-1856: session files are copied to the debug dir at invocation time."""
    project_root = str(tmp_path)
    session_id = "sess-copy-test"

    # Create session directory with the three files
    session = tmp_path / ".praxis" / "chat" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "last_stdin.txt").write_text("stdin content", encoding="utf-8")
    (session / "full.json").write_text('{"messages": []}', encoding="utf-8")
    (session / "memory.json").write_text('{"turns": []}', encoding="utf-8")

    praxis_local_api._copy_session_debug_files(project_root, session_id)

    debug = tmp_path / ".praxis" / "chat" / "debug"
    assert (debug / "last_stdin.txt").read_text(encoding="utf-8") == "stdin content"
    assert (debug / "full.json").read_text(encoding="utf-8") == '{"messages": []}'
    assert (debug / "memory.json").read_text(encoding="utf-8") == '{"turns": []}'


def test_copy_session_debug_files_skips_missing(tmp_path):
    """POS-1856: missing session files are silently skipped."""
    project_root = str(tmp_path)
    session_id = "sess-partial"

    # Create session directory with only full.json
    session = tmp_path / ".praxis" / "chat" / "sessions" / session_id
    session.mkdir(parents=True)
    (session / "full.json").write_text('{"messages": []}', encoding="utf-8")

    praxis_local_api._copy_session_debug_files(project_root, session_id)

    debug = tmp_path / ".praxis" / "chat" / "debug"
    assert (debug / "full.json").read_text(encoding="utf-8") == '{"messages": []}'
    assert not (debug / "last_stdin.txt").exists()
    assert not (debug / "memory.json").exists()


def test_copy_session_debug_files_no_session_dir(tmp_path):
    """POS-1856: non-existent session directory is handled gracefully."""
    project_root = str(tmp_path)
    # No session directory at all — should not raise
    praxis_local_api._copy_session_debug_files(project_root, "nonexistent-session")
    # Debug dir may or may not be created; the important thing is no error
