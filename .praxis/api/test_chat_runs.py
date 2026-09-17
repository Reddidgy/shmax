"""Tests for the detached run registry (``chat_runs.RunManager``).

Run from the ``api/`` directory:

    python3 -m pytest test_chat_runs.py -v

These cover the helper-owned "run" model that decouples a ``claude`` generation
from the HTTP connection (spec 011, Slice 1):

* ``start_run`` allocates a ``run_id`` and a background worker that buffers SSE
  events independent of any tailer;
* ``tail`` replays buffered events and blocks until the single terminal, and a
  second tailer from index 0 replays the full run (multiple concurrent tabs);
* ``stop`` terminates the process group and yields a ``stopped``/terminal frame;
* the worker is NOT tied to any consumer — dropping a tailer never stops it;
* unknown ids raise ``RunNotFoundError``; finished runs are retained then pruned.

The subprocess is faked with an executable stub exactly as ``test_praxis_local_api``
does, so these run in CI without a real ``claude``.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

import base64

import chat_runs
from unittest import mock

from chat_constants import is_ask_user_question
from chat_runs import RunManager, RunNotFoundError
from chat_runtime import COMPACTION_CHAR_THRESHOLD

# A tiny but real base64 payload (the bytes don't matter to the stub).
_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-png-bytes").decode("ascii")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _write_streaming_claude(
    tmp_path,
    *,
    lines: list[str],
    exit_code: int = 0,
    hang: bool = False,
    stdin_sidecar: str | None = None,
) -> str:
    """Write an executable stub that emits ``lines`` of stream-json then exits.

    With ``hang=True`` the stub sleeps after emitting its lines, simulating a
    long-running turn for stop/worker-independence tests. When ``stdin_sidecar``
    is given, the stub reads all of its stdin and writes it verbatim to that path
    before exiting, so a test can assert on the exact bytes the worker fed the
    CLI (e.g. the stream-json envelope for an image turn).
    """
    stub = tmp_path / "claude"
    body = ["#!/usr/bin/env python3", "import sys, time"]
    if stdin_sidecar is not None:
        body.append("_stdin = sys.stdin.read()")
        body.append(
            f"open({stdin_sidecar!r}, 'w', encoding='utf-8').write(_stdin)"
        )
    for line in lines:
        body.append(f"sys.stdout.write({line!r} + '\\n')")
    body.append("sys.stdout.flush()")
    if hang:
        body.append("time.sleep(3600)")
    body.append(f"sys.exit({exit_code})")
    stub.write_text("\n".join(body) + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub)


def _messages() -> list[dict[str, object]]:
    return [{"role": "user", "content": "hello"}]


def _result_line(*, is_error: bool = False, stop_reason: str = "end_turn", session_id: str | None = None) -> str:
    d = {
        "type": "result",
        "subtype": "success" if not is_error else "error_during_execution",
        "is_error": is_error,
        "stop_reason": stop_reason,
    }
    if session_id is not None:
        d["session_id"] = session_id
    return json.dumps(d)


def _text_event(text: str) -> str:
    return json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        }
    )


def _ask_user_question_lines(
    *, tool_id: str = "toolu_q1", name: str = "AskUserQuestion"
) -> list[str]:
    """stream-json lines that emit one ``AskUserQuestion`` tool_use block.

    A ``content_block_start`` carrying the tool id/name/input followed by its
    ``content_block_stop`` — the point at which the translator yields the
    ``tool_use`` event.
    """
    start = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {"questions": [{"question": "Pick", "header": "H", "options": []}]},
                },
            },
        }
    )
    stop = json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_stop", "index": 0},
        }
    )
    return [start, stop]


def _drain(manager: RunManager, run_id: str, *, from_index: int = 0, timeout: float = 5.0):
    """Tail a run to its terminal with a wall-clock safety timeout."""
    events: list[tuple[str, dict]] = []
    deadline = time.time() + timeout
    for event in manager.tail(run_id, from_index):
        events.append(event)
        if time.time() > deadline:  # pragma: no cover - safety net
            raise AssertionError("tail did not terminate in time")
    return events


@pytest.fixture
def manager() -> RunManager:
    return RunManager()


# ---------------------------------------------------------------------------
# start_run + tail
# ---------------------------------------------------------------------------
def test_start_run_returns_hex_run_id(manager, tmp_path):
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    assert isinstance(run_id, str) and len(run_id) == 32
    int(run_id, 16)  # valid hex
    _drain(manager, run_id)  # let the worker finish/reap


def test_tail_replays_text_then_single_terminal_done(manager, tmp_path):
    fake = _write_streaming_claude(
        tmp_path, lines=[_text_event("Hi!"), _result_line()]
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)
    names = [name for name, _ in events]
    assert names == ["text", "done"]
    assert events[0][1] == {"text": "Hi!"}
    assert events[-1][1]["full_text"] == "Hi!"
    assert names.count("done") + names.count("error") == 1
    assert manager.get_status(run_id) == "done"


def test_tail_blocks_until_terminal(manager, tmp_path):
    """A tailer attached before the stub finishes still ends on the terminal."""
    fake = _write_streaming_claude(
        tmp_path, lines=[_text_event("part"), _result_line()]
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    # Tail immediately — may attach before the worker has appended everything.
    events = _drain(manager, run_id)
    assert events[-1][0] == "done"


def test_second_tailer_from_zero_replays_whole_run(manager, tmp_path):
    """A reattaching tab tails the finished run from index 0 and replays all."""
    fake = _write_streaming_claude(
        tmp_path, lines=[_text_event("Hi!"), _result_line()]
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    first = _drain(manager, run_id)
    second = _drain(manager, run_id, from_index=0)
    assert [n for n, _ in second] == [n for n, _ in first] == ["text", "done"]


def test_tail_from_nonzero_index_skips_replayed_events(manager, tmp_path):
    fake = _write_streaming_claude(
        tmp_path, lines=[_text_event("a"), _text_event("b"), _result_line()]
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    full = _drain(manager, run_id)
    assert [n for n, _ in full] == ["text", "text", "done"]
    # Reattach skipping the first event.
    tail = _drain(manager, run_id, from_index=1)
    assert [n for n, _ in tail] == ["text", "done"]


def test_nonzero_exit_yields_single_error_no_done(manager, tmp_path):
    fake = _write_streaming_claude(
        tmp_path, lines=["garbage-not-json"], exit_code=2
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)
    names = [n for n, _ in events]
    assert names.count("error") == 1
    assert "done" not in names
    assert manager.get_status(run_id) == "error"


def test_nonzero_exit_with_content_yields_done_not_error(manager, tmp_path):
    """POS-1892: when content events were streamed, a non-zero exit code
    should NOT produce an error terminal — the turn completes normally."""
    fake = _write_streaming_claude(
        tmp_path, lines=[_text_event("useful output"), _result_line()], exit_code=1
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)
    names = [n for n, _ in events]
    assert "error" not in names
    assert names == ["text", "done"]
    assert manager.get_status(run_id) == "done"


def test_claude_reported_error_surfaces_message(manager, tmp_path):
    fake = _write_streaming_claude(
        tmp_path,
        lines=[json.dumps({"type": "result", "is_error": True, "result": "model not found"})],
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)
    assert events[-1][0] == "error"
    assert events[-1][1]["error"] == "model not found"


def test_spawn_failure_yields_single_error(manager, tmp_path):
    """A non-resolvable binary path → one error terminal, no crash."""
    run_id = manager.start_run(
        _messages(),
        "sys",
        "sonnet",
        str(tmp_path),
        claude_binary=str(tmp_path / "does-not-exist"),
    )
    events = _drain(manager, run_id)
    assert [n for n, _ in events] == ["error"]
    assert manager.get_status(run_id) == "error"


# ---------------------------------------------------------------------------
# is_ask_user_question — name matcher (plain + mcp__praxis__ prefixed)
# ---------------------------------------------------------------------------
def test_is_ask_user_question_matches_plain_name():
    assert is_ask_user_question("AskUserQuestion") is True


def test_is_ask_user_question_matches_mcp_prefixed():
    assert is_ask_user_question("mcp__praxis__AskUserQuestion") is True


def test_is_ask_user_question_rejects_other_tools():
    assert is_ask_user_question("Read") is False
    assert is_ask_user_question("") is False
    assert is_ask_user_question("NotAskUserQuestion") is False


# ---------------------------------------------------------------------------
# AskUserQuestion pauses the turn (so the user can actually answer)
# ---------------------------------------------------------------------------
def test_ask_user_question_pauses_turn_with_clean_done(manager, tmp_path):
    """An AskUserQuestion ends the turn cleanly the instant it is emitted.

    The stub hangs after emitting the question, simulating the CLI's would-be
    self-resolution + continuation. The worker must finalize a ``done`` at the
    question (not wait for the hung child) so the frontend can collect the
    user's answer as the next turn.
    """
    fake = _write_streaming_claude(
        tmp_path, lines=_ask_user_question_lines(), hang=True
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)
    names = [n for n, _ in events]
    assert names == ["tool_use", "done"]
    assert events[0][1]["name"] == "AskUserQuestion"
    assert events[-1][1]["stop_reason"] == "awaiting_user_input"
    assert manager.get_status(run_id) == "done"
    assert run_id not in manager.active_run_ids()


def test_ask_user_question_mcp_prefixed_name_pauses_turn(manager, tmp_path):
    """AskUserQuestion with mcp__praxis__ prefix still pauses the turn."""
    lines = _ask_user_question_lines(name="mcp__praxis__AskUserQuestion")
    result = json.dumps({
        "type": "result", "stop_reason": "end_turn",
        "result": "", "subtype": "success",
    })
    lines.append(result)
    fake = _write_streaming_claude(tmp_path, lines=lines)

    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)

    tool_events = [(n, d) for n, d in events if n == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0][1]["name"] == "mcp__praxis__AskUserQuestion"

    done_events = [(n, d) for n, d in events if n == "done"]
    assert len(done_events) == 1
    assert done_events[0][1]["stop_reason"] == "awaiting_user_input"


def test_ask_user_question_drops_self_resolved_continuation(manager, tmp_path):
    """Anything the CLI emits after the question is discarded (turn paused).

    The stub emits the question, then a self-resolved continuation (text +
    result). The worker must stop at the question and never surface the
    "nevermind"-style continuation.
    """
    fake = _write_streaming_claude(
        tmp_path,
        lines=[
            *_ask_user_question_lines(),
            _text_event("Well, you didn't answer, nevermind."),
            _result_line(),
        ],
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)
    names = [n for n, _ in events]
    assert names == ["tool_use", "done"]
    assert all(n != "text" for n in names)
    assert manager.get_status(run_id) == "done"


def test_non_question_tool_use_does_not_pause(manager, tmp_path):
    """A normal (non-AskUserQuestion) tool_use streams through without pausing."""
    fake = _write_streaming_claude(
        tmp_path,
        lines=[
            *_ask_user_question_lines(tool_id="toolu_r1", name="Read"),
            _text_event("done reading"),
            _result_line(),
        ],
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)
    names = [n for n, _ in events]
    assert names == ["tool_use", "text", "done"]
    assert events[-1][1]["stop_reason"] != "awaiting_user_input"


def _assistant_snapshot_line(
    *, tool_id: str = "toolu_recovered", name: str = "AskUserQuestion"
) -> str:
    """A full ``assistant`` snapshot line carrying one ``tool_use`` block.

    Mirrors what the CLI emits as a whole-message snapshot (distinct from the
    ``content_block_start``/``content_block_stop`` delta pair in
    ``_ask_user_question_lines``) — used to exercise the assistant-snapshot
    recovery path in ``chat_stream.translate_stream`` (POS-1756).
    """
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me ask you something."},
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": {
                            "questions": [
                                {"question": "Pick one", "header": "H", "options": []}
                            ]
                        },
                    },
                ],
            },
        }
    )


def test_ask_user_question_recovered_from_assistant_snapshot(manager, tmp_path):
    """AskUserQuestion detected even when only present in an assistant snapshot.

    Simulates a CLI wire-format drift where the delta stream (content_block_
    start/stop) never carries the tool_use — only the full assistant-message
    snapshot does. The recovery path in ``translate_stream`` must still surface
    it as a ``tool_use`` event so the pause-for-AskUserQuestion logic fires.
    """
    fake = _write_streaming_claude(
        tmp_path,
        lines=[_assistant_snapshot_line(), _result_line()],
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)

    tool_events = [(n, d) for n, d in events if n == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0][1]["name"] == "AskUserQuestion"
    assert tool_events[0][1]["id"] == "toolu_recovered"

    done_events = [(n, d) for n, d in events if n == "done"]
    assert len(done_events) == 1
    assert done_events[0][1]["stop_reason"] == "awaiting_user_input"


def test_assistant_snapshot_does_not_duplicate_delta_tool_use(manager, tmp_path):
    """tool_use seen via both the delta stream and an assistant snapshot is emitted once.

    Uses a non-``AskUserQuestion`` tool name (``Read``) deliberately: an
    AskUserQuestion tool_use ends the worker's turn immediately (see the
    ``_finalize`` + ``return`` in ``chat_runs._worker``), which would abandon
    the underlying generator before it ever reaches the trailing assistant
    snapshot line — masking whether dedup actually happened. A non-pausing
    tool lets the full line sequence (delta pair, then snapshot, then result)
    actually flow through ``translate_stream``, so this test genuinely
    exercises the ``emitted_tool_ids`` dedup guard end-to-end.
    """
    fake = _write_streaming_claude(
        tmp_path,
        lines=[
            # Normal delta path: content_block_start + content_block_stop.
            *_ask_user_question_lines(tool_id="toolu_dedup", name="Read"),
            # Same tool_use also present in a trailing assistant snapshot.
            _assistant_snapshot_line(tool_id="toolu_dedup", name="Read"),
            _text_event("done reading"),
            _result_line(),
        ],
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    events = _drain(manager, run_id)

    names = [n for n, _ in events]
    assert names == ["tool_use", "text", "done"]
    tool_events = [(n, d) for n, d in events if n == "tool_use"]
    assert len(tool_events) == 1, f"Expected 1 tool_use, got {len(tool_events)}"
    assert tool_events[0][1]["id"] == "toolu_dedup"


# ---------------------------------------------------------------------------
# worker independence
# ---------------------------------------------------------------------------
def test_worker_runs_to_completion_without_a_tailer(manager, tmp_path):
    """The worker buffers and finalizes even if nobody ever tails the run."""
    fake = _write_streaming_claude(
        tmp_path, lines=[_text_event("Hi!"), _result_line()]
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    for _ in range(100):
        if manager.get_status(run_id) == "done":
            break
        time.sleep(0.05)
    assert manager.get_status(run_id) == "done"
    # And a late tailer still replays the full buffered run.
    assert [n for n, _ in _drain(manager, run_id)] == ["text", "done"]


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------
def test_stop_yields_terminal_and_reaps_process(manager, tmp_path):
    fake = _write_streaming_claude(
        tmp_path, lines=[_text_event("streaming...")], hang=True
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    # Wait until the run is active and has produced its first event.
    for _ in range(100):
        if run_id in manager.active_run_ids():
            break
        time.sleep(0.05)
    assert run_id in manager.active_run_ids()

    manager.stop(run_id)
    events = _drain(manager, run_id)
    assert events[-1][0] in ("done", "error")  # a terminal was appended
    assert manager.get_status(run_id) == "stopped"
    assert run_id not in manager.active_run_ids()


def test_stop_unknown_run_raises(manager):
    with pytest.raises(RunNotFoundError):
        manager.stop("nope")


def test_stop_finished_run_is_noop(manager, tmp_path):
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    _drain(manager, run_id)
    manager.stop(run_id)  # must not raise or change a finished status
    assert manager.get_status(run_id) == "done"


# ---------------------------------------------------------------------------
# active_run_ids / unknown ids
# ---------------------------------------------------------------------------
def test_active_run_ids_reflects_running_only(manager, tmp_path):
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    _drain(manager, run_id)
    assert run_id not in manager.active_run_ids()


def test_tail_unknown_run_raises(manager):
    with pytest.raises(RunNotFoundError):
        list(manager.tail("nope", 0))


def test_get_status_unknown_run_is_none(manager):
    assert manager.get_status("nope") is None


# ---------------------------------------------------------------------------
# retention / pruning
# ---------------------------------------------------------------------------
def test_finished_run_retained_then_pruned(tmp_path):
    mgr = RunManager(retention_seconds=0)  # immediate expiry on next access
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    run_id = mgr.start_run(
        _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
    )
    _drain(mgr, run_id)
    # finished_at is set; with retention_seconds=0 the next access prunes it.
    time.sleep(0.01)
    assert mgr.get_status(run_id) == "done"  # still in registry until a sweep
    mgr.active_run_ids()  # triggers a lazy prune
    with pytest.raises(RunNotFoundError):
        list(mgr.tail(run_id, 0))


# ---------------------------------------------------------------------------
# image input — worker stdin envelope + argv flag (stdin captured to a sidecar)
# ---------------------------------------------------------------------------
def test_image_turn_writes_stream_json_envelope_and_flag(manager, tmp_path):
    """An image turn feeds a single stream-json user line and uses the flag."""
    sidecar = str(tmp_path / "stdin.txt")
    fake = _write_streaming_claude(
        tmp_path, lines=[_result_line()], stdin_sidecar=sidecar
    )

    captured_argv: dict[str, list[str]] = {}
    real_popen = chat_runs.subprocess.Popen

    def _capturing_popen(argv, *args, **kwargs):
        captured_argv["argv"] = argv
        return real_popen(argv, *args, **kwargs)

    chat_runs.subprocess.Popen = _capturing_popen
    try:
        messages = [
            {
                "role": "user",
                "content": "what is this?",
                "images": [{"media_type": "image/png", "data": _PNG_B64}],
            }
        ]
        run_id = manager.start_run(
            messages, "sys", "sonnet", str(tmp_path), claude_binary=fake
        )
        _drain(manager, run_id)
    finally:
        chat_runs.subprocess.Popen = real_popen

    # argv carries the stream-json input flag.
    argv = captured_argv["argv"]
    assert "--input-format" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"

    # stdin is exactly one JSON line that parses to the user/image envelope.
    raw = open(sidecar, encoding="utf-8").read()
    assert raw.endswith("\n")
    json_lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(json_lines) == 1
    envelope = json.loads(json_lines[0])
    assert envelope["type"] == "user"
    content = envelope["message"]["content"]
    assert content[0]["type"] == "text"
    assert "what is this?" in content[0]["text"]
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    }


def test_session_memory_image_turn_wraps_payload_with_image_blocks(
    manager, tmp_path
):
    """POS-1992: a session image turn keeps session-memory stdin AND images.

    Before POS-1992 an image turn was forced onto the legacy path (session
    memory was skipped) and its images were stripped after a separate Sonnet
    description pass. Now the session-memory text payload is wrapped into the
    stream-json envelope with the image blocks appended, so the main model
    sees both the memory context and the raw images in one call.
    """
    sidecar = str(tmp_path / "stdin.txt")
    fake = _write_streaming_claude(
        tmp_path, lines=[_result_line()], stdin_sidecar=sidecar
    )

    captured_argv: dict[str, list[str]] = {}
    real_popen = chat_runs.subprocess.Popen

    def _capturing_popen(argv, *args, **kwargs):
        captured_argv["argv"] = list(argv)
        return real_popen(argv, *args, **kwargs)

    chat_runs.subprocess.Popen = _capturing_popen
    try:
        messages = [
            {
                "role": "user",
                "content": "what is on this screenshot?",
                "images": [{"media_type": "image/png", "data": _PNG_B64}],
            }
        ]
        run_id = manager.start_run(
            messages, "SYSTEM PROMPT", "sonnet", str(tmp_path),
            claude_binary=fake, session_id="img-session",
        )
        _drain(manager, run_id)
    finally:
        chat_runs.subprocess.Popen = real_popen

    argv = captured_argv["argv"]
    assert "--input-format" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"

    raw = open(sidecar, encoding="utf-8").read()
    assert raw.endswith("\n")
    json_lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(json_lines) == 1
    content = json.loads(json_lines[0])["message"]["content"]
    # The text block is the session-memory payload (system prompt + current
    # turn), not the legacy [User]: transcript.
    assert content[0]["type"] == "text"
    assert "SYSTEM PROMPT" in content[0]["text"]
    assert "### Current Turn" in content[0]["text"]
    assert "what is on this screenshot?" in content[0]["text"]
    # The image rides in-band, unstripped.
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    }


def test_resume_image_turn_skips_resume_uses_session_memory(
    manager, tmp_path
):
    """POS-2000: an image turn on a resumed session drops --resume.

    The CLI's --resume mode does not relay stream-json image content blocks
    to the API, so the model never sees attached images. When images are
    present the worker must fall back to the session-memory path (no
    --resume, but with --input-format stream-json and the image envelope).
    """
    from chat_session_memory import save_claude_session_id

    save_claude_session_id(str(tmp_path), "img-resume", "existing-sid")

    sidecar = str(tmp_path / "stdin.txt")
    fake = _write_streaming_claude(
        tmp_path, lines=[_result_line()], stdin_sidecar=sidecar
    )

    captured_argv: dict[str, list[str]] = {}
    real_popen = chat_runs.subprocess.Popen

    def _capturing_popen(argv, *args, **kwargs):
        captured_argv["argv"] = list(argv)
        return real_popen(argv, *args, **kwargs)

    chat_runs.subprocess.Popen = _capturing_popen
    try:
        messages = [
            {
                "role": "user",
                "content": "what is on this screenshot?",
                "images": [{"media_type": "image/png", "data": _PNG_B64}],
            }
        ]
        run_id = manager.start_run(
            messages, "SYSTEM PROMPT", "sonnet", str(tmp_path),
            claude_binary=fake, session_id="img-resume",
        )
        _drain(manager, run_id)
    finally:
        chat_runs.subprocess.Popen = real_popen

    argv = captured_argv["argv"]
    # --resume must NOT be present for image turns.
    assert "--resume" not in argv
    # --input-format stream-json must still be present for images.
    assert "--input-format" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"

    # stdin is the session-memory payload (not a resume-mode plain text).
    raw = open(sidecar, encoding="utf-8").read()
    assert raw.endswith("\n")
    json_lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(json_lines) == 1
    content = json.loads(json_lines[0])["message"]["content"]
    # Session-memory path includes system prompt and current turn marker.
    assert content[0]["type"] == "text"
    assert "SYSTEM PROMPT" in content[0]["text"]
    assert "### Current Turn" in content[0]["text"]
    assert "what is on this screenshot?" in content[0]["text"]
    # Image rides in-band.
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    }


def test_text_only_turn_writes_plain_transcript_no_flag(manager, tmp_path):
    """A text-only turn keeps the plain transcript and never adds the flag."""
    sidecar = str(tmp_path / "stdin.txt")
    fake = _write_streaming_claude(
        tmp_path, lines=[_result_line()], stdin_sidecar=sidecar
    )

    captured_argv: dict[str, list[str]] = {}
    real_popen = chat_runs.subprocess.Popen

    def _capturing_popen(argv, *args, **kwargs):
        captured_argv["argv"] = argv
        return real_popen(argv, *args, **kwargs)

    chat_runs.subprocess.Popen = _capturing_popen
    try:
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        run_id = manager.start_run(
            messages, "sys", "sonnet", str(tmp_path), claude_binary=fake
        )
        _drain(manager, run_id)
    finally:
        chat_runs.subprocess.Popen = real_popen

    assert "--input-format" not in captured_argv["argv"]

    raw = open(sidecar, encoding="utf-8").read()
    assert raw == "[User]: first\n[Assistant]: reply\n[User]: second"


def test_cap_evicts_oldest_finished_runs(tmp_path):
    mgr = RunManager(retention_seconds=3600, max_retained=2)
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    ids = []
    for _ in range(4):
        rid = mgr.start_run(
            _messages(), "sys", "sonnet", str(tmp_path), claude_binary=fake
        )
        _drain(mgr, rid)
        ids.append(rid)
    # At most max_retained survive; the newest run is always retained.
    survivors = [rid for rid in ids if mgr.get_status(rid) is not None]
    assert len(survivors) <= 2
    assert ids[-1] in survivors


# ---------------------------------------------------------------------------
# Compaction threshold-trigger integration (POS-1847)
# ---------------------------------------------------------------------------


def test_compaction_fires_on_session_memory_path(manager, tmp_path):
    """When the session-memory stdin exceeds COMPACTION_CHAR_THRESHOLD,
    compact_stdin_payload is invoked on the memory portion and compacting
    SSE events bracket it."""
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    # Build history messages large enough to push memory_text past the
    # character threshold when rendered.
    big_content = "x" * (COMPACTION_CHAR_THRESHOLD + 1000)
    messages = [
        {"role": "user", "content": big_content},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "next"},
    ]
    compacted_text = "compacted memory"

    with mock.patch("chat_runs.compact_stdin_payload", return_value=compacted_text) as mock_compact, \
         mock.patch("chat_runs.save_memory_messages"):
        run_id = manager.start_run(
            messages, "sys", "sonnet", str(tmp_path),
            claude_binary=fake, session_id="test-session-compact",
        )
        events = _drain(manager, run_id)

    mock_compact.assert_called_once()
    # compact_stdin_payload should receive memory_text (not full stdin)
    call_args = mock_compact.call_args
    compacted_input = call_args[0][0]
    assert "### Previous Turns" in compacted_input
    # System prompt must NOT be in the compacted input
    assert compacted_input.startswith("### Previous Turns")

    names = [n for n, _ in events]
    assert "compacting" in names

    compacting_events = [(n, d) for n, d in events if n == "compacting"]
    assert len(compacting_events) == 2
    start_ev = compacting_events[0][1]
    done_ev = compacting_events[1][1]
    assert start_ev["status"] == "start"
    assert start_ev["stdin_path"] == "session-memory"
    assert done_ev["status"] == "done"
    assert done_ev["stdin_path"] == "session-memory"
    assert done_ev["original_chars"] > COMPACTION_CHAR_THRESHOLD
    assert "compacted_chars" in done_ev


def test_compaction_fires_on_legacy_path(manager, tmp_path):
    """When the legacy (no session_id) stdin exceeds COMPACTION_CHAR_THRESHOLD,
    compact_stdin_payload is invoked and compacting SSE events bracket it."""
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    huge_content = "x" * (COMPACTION_CHAR_THRESHOLD + 1000)
    messages = [{"role": "user", "content": huge_content}]
    compacted_text = "compacted payload"

    with mock.patch("chat_runs.compact_stdin_payload", return_value=compacted_text) as mock_compact:
        run_id = manager.start_run(
            messages, "sys", "sonnet", str(tmp_path),
            claude_binary=fake,
        )
        events = _drain(manager, run_id)

    mock_compact.assert_called_once()

    compacting_events = [(n, d) for n, d in events if n == "compacting"]
    assert len(compacting_events) == 2
    start_ev = compacting_events[0][1]
    done_ev = compacting_events[1][1]
    assert start_ev["status"] == "start"
    assert start_ev["stdin_path"] == "legacy"
    assert done_ev["status"] == "done"
    assert done_ev["stdin_path"] == "legacy"
    assert done_ev["original_chars"] > COMPACTION_CHAR_THRESHOLD
    assert "compacted_chars" in done_ev


def test_compaction_not_triggered_below_threshold(manager, tmp_path):
    """When stdin char count is at or below COMPACTION_CHAR_THRESHOLD,
    compact_stdin_payload is NOT invoked and no compacting events appear."""
    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    messages = [{"role": "user", "content": "short message"}]

    with mock.patch("chat_runs.compact_stdin_payload") as mock_compact:
        run_id = manager.start_run(
            messages, "sys", "sonnet", str(tmp_path),
            claude_binary=fake,
        )
        events = _drain(manager, run_id)

    mock_compact.assert_not_called()
    assert all(n != "compacting" for n, _ in events)


def test_session_memory_compaction_threshold_is_char_based(tmp_path):
    """Compaction triggers on character count, not line count.

    A payload with very few lines but many characters (typical of session
    memory) must trigger compaction when it exceeds COMPACTION_CHAR_THRESHOLD.
    """
    from chat_session_memory import build_session_stdin

    system_prompt = "You are a helpful assistant."
    # Few lines but many characters — exactly the session-memory pattern.
    memory_text = "x" * (COMPACTION_CHAR_THRESHOLD - 100)
    # Short user message that pushes total past the threshold.
    user_msg = "y" * 200

    payload = build_session_stdin(
        str(tmp_path), "char-test", system_prompt, memory_text, user_msg
    )
    # The payload has very few lines (~5) but lots of characters.
    assert payload.count("\n") < 20
    assert len(payload) > COMPACTION_CHAR_THRESHOLD


# ---------------------------------------------------------------------------
# --resume: session_id capture and persistence (POS-1938)
# ---------------------------------------------------------------------------
def test_claude_session_id_captured_and_saved(manager, tmp_path):
    """When the result event carries session_id, it is persisted to disk."""
    fake = _write_streaming_claude(
        tmp_path,
        lines=[_text_event("Hi!"), _result_line(session_id="claude-sid-abc")],
    )
    run_id = manager.start_run(
        _messages(), "sys", "sonnet", str(tmp_path),
        claude_binary=fake, session_id="test-capture",
    )
    _drain(manager, run_id)

    from chat_session_memory import load_claude_session_id
    saved = load_claude_session_id(str(tmp_path), "test-capture")
    assert saved == "claude-sid-abc"


def test_resume_argv_contains_resume_flag(manager, tmp_path):
    """When a claude_session_id exists on disk, argv includes --resume."""
    from chat_session_memory import save_claude_session_id

    save_claude_session_id(str(tmp_path), "test-resume", "existing-sid")

    fake = _write_streaming_claude(
        tmp_path, lines=[_result_line(session_id="existing-sid")]
    )

    captured_argv: dict[str, list[str]] = {}
    real_popen = chat_runs.subprocess.Popen

    def _capturing_popen(argv, *args, **kwargs):
        captured_argv["argv"] = list(argv)
        return real_popen(argv, *args, **kwargs)

    chat_runs.subprocess.Popen = _capturing_popen
    try:
        run_id = manager.start_run(
            _messages(), "sys", "sonnet", str(tmp_path),
            claude_binary=fake, session_id="test-resume",
        )
        _drain(manager, run_id)
    finally:
        chat_runs.subprocess.Popen = real_popen

    argv = captured_argv["argv"]
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "existing-sid"


def test_resume_stdin_is_user_message_only(manager, tmp_path):
    """In resume mode, stdin is only the current user message text."""
    from chat_session_memory import save_claude_session_id

    save_claude_session_id(str(tmp_path), "test-stdin", "existing-sid")

    sidecar = str(tmp_path / "stdin.txt")
    fake = _write_streaming_claude(
        tmp_path, lines=[_result_line(session_id="existing-sid")],
        stdin_sidecar=sidecar,
    )

    run_id = manager.start_run(
        [{"role": "user", "content": "hello world"}],
        "sys", "sonnet", str(tmp_path),
        claude_binary=fake, session_id="test-stdin",
    )
    _drain(manager, run_id)

    raw = open(sidecar, encoding="utf-8").read()
    assert raw == "hello world"


def test_resume_fallback_on_failure(manager, tmp_path):
    """When --resume fails (non-zero exit, no content), retries without resume."""
    from chat_session_memory import save_claude_session_id, load_claude_session_id

    save_claude_session_id(str(tmp_path), "test-fallback", "bad-sid")

    call_count = {"n": 0}

    # First call: exit 1 (resume fails), second call: normal success
    (tmp_path / "fail").mkdir()
    (tmp_path / "ok").mkdir()
    fake_fail = _write_streaming_claude(
        tmp_path / "fail", lines=[], exit_code=1
    )
    fake_ok = _write_streaming_claude(
        tmp_path / "ok", lines=[_text_event("recovered"), _result_line()]
    )

    real_popen = chat_runs.subprocess.Popen
    captured_argvs: list[list[str]] = []

    def _routing_popen(argv, *args, **kwargs):
        call_count["n"] += 1
        captured_argvs.append(list(argv))
        if call_count["n"] == 1:
            # Replace binary with the failing one
            argv = [fake_fail] + list(argv[1:])
        else:
            # Replace binary with the succeeding one
            argv = [fake_ok] + list(argv[1:])
        return real_popen(argv, *args, **kwargs)

    chat_runs.subprocess.Popen = _routing_popen
    try:
        run_id = manager.start_run(
            _messages(), "sys", "sonnet", str(tmp_path),
            claude_binary="dummy", session_id="test-fallback",
        )
        events = _drain(manager, run_id)
    finally:
        chat_runs.subprocess.Popen = real_popen

    # Two Popen calls: resume attempt + fallback
    assert call_count["n"] == 2
    # First call had --resume, second did not
    assert "--resume" in captured_argvs[0]
    assert "--resume" not in captured_argvs[1]
    # Run completed successfully
    assert events[-1][0] == "done"
    # claude_session_id.txt was deleted on failure
    assert load_claude_session_id(str(tmp_path), "test-fallback") is None


# ---------------------------------------------------------------------------
# Session rotation memory duplication (POS-1952)
# ---------------------------------------------------------------------------


def _session_dir_for(tmp_path, session_id: str):
    """Create and return the per-session directory under ``tmp_path``."""
    import pathlib

    from chat_session_paths import session_dir

    directory = pathlib.Path(session_dir(str(tmp_path), session_id))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_rotation_compaction_does_not_reappend_pre_rotation_history(manager, tmp_path):
    """POS-1952: after Session Rotation compacts the session, memory.json holds
    ONLY the compacted summary — not the pre-rotation turns it already covers."""
    sid = "test-rotation-dup"
    sdir = _session_dir_for(tmp_path, sid)
    # Previous turn's realtime log — the rotation compaction input.
    (sdir / "realtime_log.txt").write_text(
        "### Realtime log (previous turns + realtime answers from AI)\n\n"
        "[User]: first question\n[Assistant]: first answer\n",
        encoding="utf-8",
    )
    # A differing system_prompt.txt triggers rotation (has_system_prompt_changed).
    (sdir / "system_prompt.txt").write_text("OLD PROMPT", encoding="utf-8")

    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]

    with mock.patch("chat_runs.compact_stdin_payload", return_value="SUMMARY"):
        run_id = manager.start_run(
            messages, "NEW PROMPT", "sonnet", str(tmp_path),
            claude_binary=fake, session_id=sid,
        )
        _drain(manager, run_id)

    stored = json.loads((sdir / "memory.json").read_text(encoding="utf-8"))["messages"]
    assert len(stored) == 1, stored
    assert stored[0].get("compacted") is True
    assert stored[0]["content"] == "SUMMARY"
    contents = [m.get("content") for m in stored]
    assert "first question" not in contents
    assert "first answer" not in contents


def test_memory_append_latest_turn_unchanged_without_rotation(manager, tmp_path):
    """POS-1952 regression guard: without rotation this turn, the existing
    append-latest-turn behaviour is unchanged (backwards compatible)."""
    from chat_session_memory import save_memory_messages

    sid = "test-no-rotation"
    sdir = _session_dir_for(tmp_path, sid)
    save_memory_messages(
        str(tmp_path),
        sid,
        [{"role": "assistant", "content": "SUMMARY", "compacted": True}],
    )

    fake = _write_streaming_claude(tmp_path, lines=[_result_line()])
    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]

    run_id = manager.start_run(
        messages, "sys", "sonnet", str(tmp_path),
        claude_binary=fake, session_id=sid,
    )
    _drain(manager, run_id)

    stored = json.loads((sdir / "memory.json").read_text(encoding="utf-8"))["messages"]
    assert [m.get("content") for m in stored] == [
        "SUMMARY",
        "first question",
        "first answer",
    ]
