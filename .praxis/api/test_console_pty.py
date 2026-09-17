"""Tests for console_pty — persistent shared PTY terminal session."""

from __future__ import annotations

import os
import time

import pytest

import console_pty

pytestmark = pytest.mark.skipif(
    not console_pty.is_supported(), reason="PTY unsupported on this platform"
)

_DEADLINE_S = 15.0
_POLL_S = 0.05


@pytest.fixture()
def cwd(tmp_path):
    root = str(tmp_path)
    console_pty.reset(root)
    try:
        yield root
    finally:
        console_pty.get_session(root).close()


def _poll_until(predicate, deadline_s: float = _DEADLINE_S):
    deadline = time.time() + deadline_s
    result = predicate()
    while not result and time.time() < deadline:
        time.sleep(_POLL_S)
        result = predicate()
    return result


def test_echo_returns_output_and_zero_exit(cwd):
    result = console_pty.execute_and_wait(cwd, "echo praxis-hello", 15)
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "praxis-hello" in result["output"]


def test_nonzero_exit_code(cwd):
    result = console_pty.execute_and_wait(cwd, "false", 15)
    assert result["ok"] is True
    assert result["exit_code"] == 1


def test_session_state_persists_between_commands(cwd):
    first = console_pty.execute_and_wait(cwd, "cd /tmp", 15)
    assert first["ok"] is True
    assert first["exit_code"] == 0

    second = console_pty.execute_and_wait(cwd, "pwd", 15)
    assert second["ok"] is True
    assert second["exit_code"] == 0
    # bash's `pwd` builtin prints the logical path (what was passed to `cd`,
    # i.e. "/tmp"), not the resolved symlink target ("/private/tmp" on
    # macOS) — normalize both sides via realpath so the comparison holds
    # either way.
    assert os.path.realpath(second["output"].strip()) == os.path.realpath("/tmp")


def test_snapshot_lists_commands_and_history(cwd):
    console_pty.execute_and_wait(cwd, "echo one", 15)
    console_pty.execute_and_wait(cwd, "echo two", 15)

    snap = console_pty.snapshot(cwd)
    assert snap["ok"] is True

    commands = [c["command"] for c in snap["commands"]]
    assert commands.count("echo one") >= 1
    assert commands.count("echo two") >= 1
    assert len(snap["commands"]) >= 2

    assert "echo one" in snap["history"]
    assert "echo two" in snap["history"]


def test_busy_rejects_second_command(cwd):
    session = console_pty.get_session(cwd)
    first = console_pty.run_command(cwd, "sleep 5")
    assert first["ok"] is True
    first_id = first["command"]["id"]

    second = console_pty.run_command(cwd, "echo should-not-run")
    assert second["ok"] is False
    assert second.get("busy") is True

    interrupted = console_pty.interrupt(cwd)
    assert interrupted["ok"] is True

    def _first_finished():
        for rec in session.snapshot()["commands"]:
            if rec["id"] == first_id:
                return not rec["running"]
        return False

    assert _poll_until(_first_finished) is True


def test_events_since_streams_chunks(cwd):
    before = console_pty.snapshot(cwd)["next_event"]

    result = console_pty.run_command(cwd, "echo streamed")
    assert result["ok"] is True

    seen_chunk = {"found": False}
    seen_exit = {"found": False}

    def _has_events():
        batch = console_pty.events_since(cwd, before)
        assert batch["resync"] is False
        for event in batch["events"]:
            if event["name"] == "chunk" and "streamed" in event["data"].get("text", ""):
                seen_chunk["found"] = True
            if event["name"] == "exit":
                seen_exit["found"] = True
        return seen_chunk["found"] and seen_exit["found"]

    assert _poll_until(_has_events) is True


def test_interrupt_aborts_rest_of_command_list(cwd):
    started = console_pty.run_command(cwd, "echo started; sleep 30; echo never-runs")
    assert started["ok"] is True
    cmd_id = started["command"]["id"]

    def _saw_start():
        for rec in console_pty.snapshot(cwd)["commands"]:
            if rec["id"] == cmd_id and "started" in rec["output"]:
                return True
        return False

    assert _poll_until(_saw_start) is True

    assert console_pty.interrupt(cwd)["ok"] is True

    def _finished():
        for rec in console_pty.snapshot(cwd)["commands"]:
            if rec["id"] == cmd_id:
                return not rec["running"]
        return False

    assert _poll_until(_finished) is True

    record = next(r for r in console_pty.snapshot(cwd)["commands"] if r["id"] == cmd_id)
    # bash must NOT continue past the interrupted command...
    assert "never-runs" not in record["output"]
    # ...and the interrupted line must not look successful.
    assert record["exit_code"] != 0


# ---------------------------------------------------------------------------
# Per-chat-session Claude Code PTYs (POS-2219)
# ---------------------------------------------------------------------------


@pytest.fixture()
def chat_root(tmp_path):
    root = str(tmp_path)
    try:
        yield root
    finally:
        console_pty.destroy_all_chat_ptys(root)


def test_chat_pty_create_list_destroy(chat_root):
    s = console_pty.get_or_create_chat_pty(
        chat_root, "s1", "bash", ["-c", "echo ready; sleep 30"]
    )
    assert s is not None
    assert s.is_alive()

    assert _poll_until(lambda: "ready" in s.get_raw_buffer()) is True

    sessions = console_pty.list_chat_ptys(chat_root)
    entry = next((e for e in sessions if e["session_id"] == "s1"), None)
    assert entry is not None
    assert entry["active"] is True

    same = console_pty.get_or_create_chat_pty(
        chat_root, "s1", "bash", ["-c", "echo ready; sleep 30"]
    )
    assert same is s

    console_pty.destroy_chat_pty(chat_root, "s1")
    assert console_pty.list_chat_ptys(chat_root) == []

    # Destroying a missing session must not raise.
    console_pty.destroy_chat_pty(chat_root, "missing")


def test_chat_pty_first_message_and_exit_code(chat_root):
    s = console_pty.get_or_create_chat_pty(
        chat_root,
        "s2",
        "bash",
        ["-c", "echo ready; read -r line; echo got:$line; exit 3"],
    )
    assert s is not None

    assert s.queue_first_message("hello world") is True
    assert s.queue_first_message("hello again") is False

    assert _poll_until(lambda: "got:hello world" in s.get_raw_buffer(), 15.0) is True
    assert _poll_until(lambda: not s.is_alive(), 15.0) is True
    assert s.exit_code == 3

    assert console_pty.chat_pty_status(chat_root, "s2") == {
        "active": False,
        "exit_code": 3,
    }


def test_chat_pty_subscriber_receives_eof(chat_root):
    s = console_pty.get_or_create_chat_pty(chat_root, "s3", "bash", ["-c", "echo bye"])
    assert s is not None

    q = s.subscribe_raw()
    collected: list[str] = []
    deadline = time.time() + 15.0
    saw_eof = False
    while time.time() < deadline:
        try:
            item = q.get(timeout=0.5)
        except Exception:
            continue
        if item is None:
            saw_eof = True
            break
        collected.append(item)

    assert saw_eof is True
    assert "bye" in "".join(collected) or "bye" in s.get_raw_buffer()


def test_chat_pty_eviction(chat_root):
    sessions = []
    for i in range(5):
        s = console_pty.get_or_create_chat_pty(
            chat_root, f"s-{i}", "bash", ["-c", "sleep 30"]
        )
        assert s is not None
        s.last_activity = time.time() + i
        sessions.append(s)
        time.sleep(0.05)

    s5 = console_pty.get_or_create_chat_pty(chat_root, "s-5", "bash", ["-c", "sleep 30"])
    assert s5 is not None

    listed = console_pty.list_chat_ptys(chat_root)
    ids = {e["session_id"] for e in listed}
    assert "s-0" not in ids
    assert "s-5" in ids
    assert len([e for e in listed if e["active"]]) == 5


def test_shared_shell_registry_untouched(chat_root):
    s = console_pty.get_or_create_chat_pty(chat_root, "s6", "bash", ["-c", "echo hi; sleep 30"])
    assert s is not None
    assert console_pty._sessions.get(chat_root) is None
