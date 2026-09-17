"""Tests for the pure runtime helpers in ``chat_runtime``.

Run from the ``api/`` directory:

    python3 -m pytest test_chat_runtime.py -v

These cover the image-input additions (Spec 018, Slice 1):

* ``validate_chat_body`` accepts a well-formed ``images`` list on a user
  message and rejects every malformed shape with a precise
  ``messages[i].images[j]…`` 400-style error string;
* ``messages_have_images`` reflects image presence;
* ``build_user_stream_json`` emits the text block first, then one image block
  per image in conversation order, wrapped as a ``user`` envelope;
* ``build_claude_argv(with_image_input=...)`` toggles the
  ``--input-format stream-json`` flag (default keeps callers identical).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import subprocess as _subprocess
from unittest import mock

import chat_runtime
from chat_runtime import (
    COMPACTION_LINE_THRESHOLD,
    COMPACTION_MODEL,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    MAX_IMAGES_PER_REQUEST,
    MAX_TOTAL_IMAGE_BYTES,
    VERIFIED_CLAUDE_CLI_VERSION,
    build_claude_argv,
    build_stream_json_line,
    build_transcript,
    build_user_stream_json,
    check_claude_version,
    collect_request_images,
    compact_stdin_payload,
    messages_have_images,
    validate_chat_body,
)

_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
_JPEG_B64 = base64.b64encode(b"\xff\xd8\xff-jpeg-bytes").decode("ascii")


def _body(messages: list[dict[str, object]]) -> dict[str, object]:
    return {"messages": messages, "system_prompt": "You are terse."}


def _b64_of_size(n: int) -> str:
    return base64.b64encode(b"x" * n).decode("ascii")


# ---------------------------------------------------------------------------
# validate_chat_body — images accepted
# ---------------------------------------------------------------------------
def test_valid_images_on_user_message_accepted():
    body = _body(
        [
            {
                "role": "user",
                "content": "what is this?",
                "images": [
                    {"media_type": "image/png", "data": _PNG_B64},
                    {"media_type": "image/jpeg", "data": _JPEG_B64},
                ],
            }
        ]
    )
    assert validate_chat_body(body) is None


def test_empty_images_list_accepted():
    body = _body([{"role": "user", "content": "hi", "images": []}])
    assert validate_chat_body(body) is None


def test_images_none_is_accepted_like_absent():
    body = _body([{"role": "user", "content": "hi", "images": None}])
    assert validate_chat_body(body) is None


# ---------------------------------------------------------------------------
# validate_chat_body — images rejected
# ---------------------------------------------------------------------------
def test_images_not_a_list_rejected():
    body = _body([{"role": "user", "content": "hi", "images": {"x": 1}}])
    error = validate_chat_body(body)
    assert error == "messages[0].images must be an array"


def test_images_on_assistant_message_rejected():
    body = _body(
        [
            {
                "role": "assistant",
                "content": "hi",
                "images": [{"media_type": "image/png", "data": _PNG_B64}],
            },
            {"role": "user", "content": "ok"},
        ]
    )
    error = validate_chat_body(body)
    assert error == "messages[0].images is only allowed on a 'user' message"


def test_unsupported_media_type_rejected():
    body = _body(
        [
            {
                "role": "user",
                "content": "hi",
                "images": [{"media_type": "image/tiff", "data": _PNG_B64}],
            }
        ]
    )
    error = validate_chat_body(body)
    assert error is not None
    assert error.startswith("messages[0].images[0].media_type")


def test_missing_data_rejected():
    body = _body(
        [
            {
                "role": "user",
                "content": "hi",
                "images": [{"media_type": "image/png"}],
            }
        ]
    )
    error = validate_chat_body(body)
    assert error == "messages[0].images[0].data must be a non-empty base64 string"


def test_empty_data_rejected():
    body = _body(
        [
            {
                "role": "user",
                "content": "hi",
                "images": [{"media_type": "image/png", "data": ""}],
            }
        ]
    )
    error = validate_chat_body(body)
    assert error == "messages[0].images[0].data must be a non-empty base64 string"


def test_non_base64_data_rejected():
    body = _body(
        [
            {
                "role": "user",
                "content": "hi",
                "images": [{"media_type": "image/png", "data": "not base64!!!"}],
            }
        ]
    )
    error = validate_chat_body(body)
    assert error == "messages[0].images[0].data must be valid base64"


def test_image_object_not_dict_rejected():
    body = _body(
        [{"role": "user", "content": "hi", "images": ["just-a-string"]}]
    )
    error = validate_chat_body(body)
    assert error == "messages[0].images[0] must be an object"


def test_per_message_count_cap_rejected():
    images = [
        {"media_type": "image/png", "data": _PNG_B64}
        for _ in range(MAX_IMAGES_PER_MESSAGE + 1)
    ]
    body = _body([{"role": "user", "content": "hi", "images": images}])
    error = validate_chat_body(body)
    assert error is not None
    assert "must not exceed" in error
    assert str(MAX_IMAGES_PER_MESSAGE) in error


def test_per_request_total_count_cap_rejected():
    # Split across enough user turns to exceed the per-request total without
    # tripping the per-message cap.
    messages: list[dict[str, object]] = []
    remaining = MAX_IMAGES_PER_REQUEST + 1
    while remaining > 0:
        take = min(MAX_IMAGES_PER_MESSAGE, remaining)
        messages.append(
            {
                "role": "user",
                "content": "hi",
                "images": [
                    {"media_type": "image/png", "data": _PNG_B64}
                    for _ in range(take)
                ],
            }
        )
        remaining -= take
    error = validate_chat_body(_body(messages))
    assert error is not None
    assert "images per request must not exceed" in error
    assert str(MAX_IMAGES_PER_REQUEST) in error


def test_per_image_decoded_bytes_cap_rejected():
    big = _b64_of_size(MAX_IMAGE_BYTES + 1)
    body = _body(
        [
            {
                "role": "user",
                "content": "hi",
                "images": [{"media_type": "image/png", "data": big}],
            }
        ]
    )
    error = validate_chat_body(body)
    assert error is not None
    assert "per-image" in error
    assert "messages[0].images[0].data" in error


def test_total_decoded_bytes_cap_rejected():
    # Each image is under the per-image cap, but enough of them together exceed
    # the total. Spread across user turns so neither the per-message nor the
    # per-request count cap fires first.
    per_image = MAX_IMAGE_BYTES - 1  # just under the per-image cap
    count = (MAX_TOTAL_IMAGE_BYTES // per_image) + 1
    assert count <= MAX_IMAGES_PER_REQUEST  # guard: count cap must not fire first
    chunk = _b64_of_size(per_image)
    image = {"media_type": "image/png", "data": chunk}
    messages: list[dict[str, object]] = []
    remaining = count
    while remaining > 0:
        take = min(MAX_IMAGES_PER_MESSAGE, remaining)
        messages.append(
            {"role": "user", "content": "x", "images": [dict(image) for _ in range(take)]}
        )
        remaining -= take
    error = validate_chat_body(_body(messages))
    assert error == (
        f"total decoded image bytes must not exceed {MAX_TOTAL_IMAGE_BYTES}"
    )


# ---------------------------------------------------------------------------
# messages_have_images
# ---------------------------------------------------------------------------
def test_messages_have_images_true_when_present():
    messages = [
        {"role": "user", "content": "hi", "images": [{"media_type": "image/png", "data": _PNG_B64}]}
    ]
    assert messages_have_images(messages) is True


def test_messages_have_images_false_when_absent_or_empty():
    assert messages_have_images([{"role": "user", "content": "hi"}]) is False
    assert messages_have_images([{"role": "user", "content": "hi", "images": []}]) is False


# ---------------------------------------------------------------------------
# build_user_stream_json — shape & ordering
# ---------------------------------------------------------------------------
def test_build_user_stream_json_text_block_then_images_in_order():
    messages = [
        {
            "role": "user",
            "content": "what is this?",
            "images": [
                {"media_type": "image/png", "data": _PNG_B64},
                {"media_type": "image/jpeg", "data": _JPEG_B64},
            ],
        }
    ]
    line = build_user_stream_json(messages)
    # No trailing newline — the worker appends it.
    assert not line.endswith("\n")
    envelope = json.loads(line)
    assert envelope["type"] == "user"
    assert envelope["message"]["role"] == "user"
    content = envelope["message"]["content"]
    # First block is the flattened transcript text.
    assert content[0] == {"type": "text", "text": build_transcript(messages)}
    # Then one image block per image, in order, with correct media_type/data.
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
    }
    assert content[2] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": _JPEG_B64},
    }
    assert len(content) == 3


def test_build_user_stream_json_collects_images_across_user_turns():
    messages = [
        {
            "role": "user",
            "content": "first",
            "images": [{"media_type": "image/png", "data": _PNG_B64}],
        },
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": "second",
            "images": [{"media_type": "image/jpeg", "data": _JPEG_B64}],
        },
    ]
    content = json.loads(build_user_stream_json(messages))["message"]["content"]
    media_types = [b["source"]["media_type"] for b in content if b["type"] == "image"]
    assert media_types == ["image/png", "image/jpeg"]
    # The text block still flattens the whole conversation.
    assert content[0]["text"] == build_transcript(messages)


# ---------------------------------------------------------------------------
# collect_request_images / build_stream_json_line (POS-1992)
# ---------------------------------------------------------------------------
def test_collect_request_images_gathers_user_turns_in_order():
    messages = [
        {"role": "user", "content": "a", "images": [
            {"media_type": "image/png", "data": "AAA"}
        ]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "b", "images": [
            {"media_type": "image/jpeg", "data": "BBB"}
        ]},
    ]
    blocks = collect_request_images(messages)
    assert [b["source"]["data"] for b in blocks] == ["AAA", "BBB"]
    assert blocks[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAA"},
    }


def test_collect_request_images_ignores_assistant_and_malformed():
    messages = [
        {"role": "assistant", "content": "x", "images": [
            {"media_type": "image/png", "data": "NOPE"}
        ]},
        {"role": "user", "content": "y", "images": "not-a-list"},
        {"role": "user", "content": "z", "images": ["not-a-dict"]},
        "not-a-dict",
    ]
    assert collect_request_images(messages) == []


def test_collect_request_images_returns_empty_for_text_only():
    assert collect_request_images([{"role": "user", "content": "hi"}]) == []


def test_build_stream_json_line_text_block_first_then_images():
    blocks = [
        {"type": "image", "source": {"type": "base64",
                                     "media_type": "image/png", "data": "AAA"}}
    ]
    line = build_stream_json_line("session payload", blocks)
    assert "\n" not in line
    envelope = json.loads(line)
    assert envelope["type"] == "user"
    assert envelope["message"]["role"] == "user"
    content = envelope["message"]["content"]
    assert content[0] == {"type": "text", "text": "session payload"}
    assert content[1] == blocks[0]


def test_build_stream_json_line_without_images_is_text_only():
    content = json.loads(build_stream_json_line("just text", []))["message"]["content"]
    assert content == [{"type": "text", "text": "just text"}]


# ---------------------------------------------------------------------------
# validate_chat_body — tool_summary
# ---------------------------------------------------------------------------
def test_tool_summary_string_accepted():
    body = _body(
        [
            {"role": "assistant", "content": "done", "tool_summary": "called x"},
            {"role": "user", "content": "ok"},
        ]
    )
    assert validate_chat_body(body) is None


def test_tool_summary_none_accepted_like_absent():
    body = _body(
        [
            {"role": "assistant", "content": "done", "tool_summary": None},
            {"role": "user", "content": "ok"},
        ]
    )
    assert validate_chat_body(body) is None


def test_tool_summary_non_string_rejected():
    body = _body(
        [
            {"role": "assistant", "content": "done", "tool_summary": ["x"]},
            {"role": "user", "content": "ok"},
        ]
    )
    assert validate_chat_body(body) == "messages[0].tool_summary must be a string"


# ---------------------------------------------------------------------------
# build_transcript — plain + inline tool activity (POS-1570)
# ---------------------------------------------------------------------------
def test_build_transcript_plain_history_unchanged():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
    ]
    assert build_transcript(messages) == (
        "[User]: hi\n[Assistant]: hello\n[User]: more"
    )


def test_build_transcript_renders_tool_summary_inline_before_text():
    messages = [
        {"role": "user", "content": "what is task 42?"},
        {
            "role": "assistant",
            "content": "It's the auth fix.",
            "tool_summary": 'called get_task id=42 → {title:"Fix auth"}',
        },
        {"role": "user", "content": "thanks"},
    ]
    transcript = build_transcript(messages)
    assert (
        '[Assistant]: (called get_task id=42 → {title:"Fix auth"}) '
        "It's the auth fix." in transcript
    )


def test_build_transcript_tool_summary_with_empty_content():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_summary": "called list_files"},
        {"role": "user", "content": "ok"},
    ]
    assert "[Assistant]: (called list_files)" in build_transcript(messages)
    # No trailing space / empty content fragment left dangling.
    assert "(called list_files) \n" not in build_transcript(messages) + "\n"


def test_build_transcript_tool_summary_only_on_assistant():
    # A stray tool_summary on a user turn is ignored (defensive).
    messages = [
        {"role": "user", "content": "hi", "tool_summary": "called x"},
    ]
    assert build_transcript(messages) == "[User]: hi"


# ---------------------------------------------------------------------------
# build_claude_argv — input-format flag
# ---------------------------------------------------------------------------
def test_build_claude_argv_default_has_no_input_format():
    argv = build_claude_argv("claude", "sys", "sonnet")
    assert "--input-format" not in argv


def test_build_claude_argv_with_image_input_adds_stream_json():
    argv = build_claude_argv("claude", "sys", "sonnet", with_image_input=True)
    assert "--input-format" in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    # The output-format pairing the CLI requires is still present.
    assert "--output-format" in argv


def test_build_claude_argv_default_has_no_mcp_config():
    argv = build_claude_argv("claude", "sys", "sonnet")
    assert "--mcp-config" not in argv


def test_build_claude_argv_with_mcp_port():
    import json
    argv = build_claude_argv("claude", "sys", "sonnet", mcp_port=7865)
    # POS-1645: --strict-mcp-config removed; inline config is the full set
    assert "--strict-mcp-config" not in argv
    assert "--mcp-config" in argv
    config_str = argv[argv.index("--mcp-config") + 1]
    config = json.loads(config_str)
    assert "mcpServers" in config
    assert "praxis" in config["mcpServers"]
    assert config["mcpServers"]["praxis"]["url"] == "http://127.0.0.1:7865/stream"
    assert config["mcpServers"]["praxis"]["type"] == "streamable-http"


def test_build_claude_argv_extra_mcp_servers_merged():
    """Extra MCP servers are merged and praxis always wins."""
    import json
    extra = {
        "barley": {"type": "http", "url": "https://barley.example.com/mcp"},
        "praxis": {"type": "http", "url": "https://evil.example.com/override"},
    }
    argv = build_claude_argv(
        "claude", "sys", "sonnet", mcp_port=7865, extra_mcp_servers=extra
    )
    config_str = argv[argv.index("--mcp-config") + 1]
    config = json.loads(config_str)
    servers = config["mcpServers"]
    # barley is merged in
    assert "barley" in servers
    assert servers["barley"]["url"] == "https://barley.example.com/mcp"
    # praxis key cannot be overridden — it always points to local MCP
    assert servers["praxis"]["url"] == "http://127.0.0.1:7865/stream"
    assert servers["praxis"]["type"] == "streamable-http"


def test_build_claude_argv_extra_mcp_servers_none_no_change():
    """When extra_mcp_servers is None or empty, only praxis is in the config."""
    import json
    argv = build_claude_argv(
        "claude", "sys", "sonnet", mcp_port=7865, extra_mcp_servers=None
    )
    config_str = argv[argv.index("--mcp-config") + 1]
    config = json.loads(config_str)
    assert list(config["mcpServers"].keys()) == ["praxis"]


# ---------------------------------------------------------------------------
# build_claude_argv — resume flag
# ---------------------------------------------------------------------------
def test_build_claude_argv_without_resume():
    argv = build_claude_argv("claude", "sys", "sonnet")
    assert "--resume" not in argv


def test_build_claude_argv_with_resume():
    argv = build_claude_argv("claude", "sys", "sonnet", resume_session_id="abc-123")
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "abc-123"
    # system-prompt is still present
    assert "--system-prompt" in argv


def test_build_claude_argv_resume_none_no_flag():
    argv = build_claude_argv("claude", "sys", "sonnet", resume_session_id=None)
    assert "--resume" not in argv


# ---------------------------------------------------------------------------
# validate_chat_body — context_flags (POS-1596)
# ---------------------------------------------------------------------------
def test_context_flags_absent_accepted():
    assert validate_chat_body(_body([{"role": "user", "content": "hi"}])) is None


def test_context_flags_well_formed_accepted():
    body = _body([{"role": "user", "content": "hi"}])
    body["context_flags"] = {"context_router_requested": True, "prd_requested": False}
    assert validate_chat_body(body) is None


def test_context_flags_not_object_rejected():
    body = _body([{"role": "user", "content": "hi"}])
    body["context_flags"] = "yes"
    assert validate_chat_body(body) == "'context_flags' must be an object"


def test_context_flags_non_bool_value_rejected():
    body = _body([{"role": "user", "content": "hi"}])
    body["context_flags"] = {"context_router_requested": "true"}
    assert (
        validate_chat_body(body)
        == "'context_flags.context_router_requested' must be a boolean"
    )


# ---------------------------------------------------------------------------
# check_claude_version — CLI drift warning (POS-1756)
# ---------------------------------------------------------------------------
def _write_version_stub(tmp_path, *, output: str) -> str:
    """An executable stub that prints ``output`` and exits 0, like ``claude --version``."""
    stub = tmp_path / "claude"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({output!r} + '\\n')\n"
        "sys.exit(0)\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub)


def test_check_claude_version_matching_verified_logs_nothing(tmp_path, caplog):
    fake = _write_version_stub(
        tmp_path, output=f"{VERIFIED_CLAUDE_CLI_VERSION} (Claude Code)"
    )
    with caplog.at_level(logging.WARNING, logger="chat_runtime"):
        version = check_claude_version(fake)
    assert version == f"{VERIFIED_CLAUDE_CLI_VERSION} (Claude Code)"
    assert caplog.records == []


def test_check_claude_version_mismatch_logs_warning(tmp_path, caplog):
    fake = _write_version_stub(tmp_path, output="9.9.9 (Claude Code)")
    with caplog.at_level(logging.WARNING, logger="chat_runtime"):
        version = check_claude_version(fake)
    assert version == "9.9.9 (Claude Code)"
    assert len(caplog.records) == 1
    assert "9.9.9" in caplog.records[0].message
    assert VERIFIED_CLAUDE_CLI_VERSION in caplog.records[0].message


def test_check_claude_version_missing_binary_returns_none(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    assert check_claude_version(missing) is None


def test_check_claude_version_defaults_to_claude_on_path(monkeypatch):
    """A ``None`` claude_bin falls back to the bare ``"claude"`` command name."""
    captured: dict[str, list[str]] = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        raise FileNotFoundError("no such file")

    import subprocess

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert check_claude_version(None) is None
    assert captured["argv"] == ["claude", "--version"]


# ---------------------------------------------------------------------------
# Stdin payload compaction (POS-1830)
# ---------------------------------------------------------------------------
class _FakePopen:
    """Minimal stand-in for subprocess.Popen used by compaction tests.

    Mirrors the ``Popen`` + ``communicate()`` shape used by
    :func:`compact_stdin_payload` since POS-1918 (was ``subprocess.run``,
    switched to ``Popen`` so the spawned process can be captured via
    ``proc_hook`` and killed by a concurrent ``stop()``).
    """

    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.communicate_kwargs: dict[str, object] | None = None
        self.killed = False
        self.waited = False

    def communicate(self, input=None, timeout=None):  # noqa: A002 - matches stdlib name
        self.communicate_kwargs = {"input": input, "timeout": timeout}
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> None:
        self.waited = True


class _FakeTimeoutPopen(_FakePopen):
    """A fake Popen whose ``communicate()`` always times out."""

    def communicate(self, input=None, timeout=None):  # noqa: A002
        raise _subprocess.TimeoutExpired(cmd="claude", timeout=timeout)


def test_compact_stdin_payload_returns_compacted_on_success():
    fake_proc = _FakePopen(0, "compacted text")
    with mock.patch(
        "subprocess.Popen",
        return_value=fake_proc,
    ) as popen_mock:
        result = compact_stdin_payload("original long payload", "/bin/claude")
    assert result == "compacted text"
    argv = popen_mock.call_args.args[0]
    assert argv[0] == "/bin/claude"
    assert "--print" in argv
    assert "--model" in argv
    assert COMPACTION_MODEL in argv
    assert fake_proc.communicate_kwargs["input"] == "original long payload"


def test_compact_stdin_payload_defaults_binary_when_none():
    with mock.patch(
        "subprocess.Popen",
        return_value=_FakePopen(0, "compacted"),
    ) as popen_mock:
        compact_stdin_payload("payload", None)
    assert popen_mock.call_args.args[0][0] == "claude"


def test_compact_stdin_payload_falls_back_on_nonzero_exit():
    with mock.patch("subprocess.Popen", return_value=_FakePopen(1, "")):
        assert compact_stdin_payload("original", None) == "original"


def test_compact_stdin_payload_falls_back_on_empty_output():
    with mock.patch("subprocess.Popen", return_value=_FakePopen(0, "   ")):
        assert compact_stdin_payload("original", None) == "original"


def test_compact_stdin_payload_falls_back_on_timeout():
    fake_proc = _FakeTimeoutPopen(0, "")
    with mock.patch("subprocess.Popen", return_value=fake_proc):
        assert compact_stdin_payload("original", None) == "original"
    assert fake_proc.killed
    assert fake_proc.waited


def test_compact_stdin_payload_falls_back_on_spawn_error():
    with mock.patch("subprocess.Popen", side_effect=OSError("boom")):
        assert compact_stdin_payload("original", None) == "original"


def test_compact_stdin_payload_invokes_proc_hook():
    """POS-1918: proc_hook receives the spawned Popen for stop() to kill."""
    fake_proc = _FakePopen(0, "compacted")
    captured: list[object] = []
    with mock.patch("subprocess.Popen", return_value=fake_proc):
        compact_stdin_payload(
            "payload", None, proc_hook=lambda p: captured.append(p)
        )
    assert captured == [fake_proc]


def test_compaction_line_threshold_is_positive_int():
    assert isinstance(COMPACTION_LINE_THRESHOLD, int)
    assert COMPACTION_LINE_THRESHOLD >= 1


def test_line_count_threshold_logic():
    # The worker triggers compaction only when the "\n" count strictly exceeds
    # COMPACTION_LINE_THRESHOLD; at/under the threshold it sends as-is.
    at_threshold = "\n" * COMPACTION_LINE_THRESHOLD
    over_threshold = "\n" * (COMPACTION_LINE_THRESHOLD + 1)
    assert at_threshold.count("\n") <= COMPACTION_LINE_THRESHOLD
    assert over_threshold.count("\n") > COMPACTION_LINE_THRESHOLD


# ---------------------------------------------------------------------------
# has_system_prompt_changed (POS-1940)
# ---------------------------------------------------------------------------


def _write_session_system_prompt(tmp_path, session_id, content):
    from chat_session_paths import session_dir

    sdir = session_dir(str(tmp_path), session_id)
    os.makedirs(sdir, exist_ok=True)
    path = os.path.join(sdir, "system_prompt.txt")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return path


def test_system_prompt_changed_false_when_file_missing(tmp_path):
    from chat_runtime import has_system_prompt_changed

    assert has_system_prompt_changed(str(tmp_path), "sess-1", "PROMPT") is False


def test_system_prompt_changed_false_when_identical(tmp_path):
    from chat_runtime import has_system_prompt_changed

    prompt = "line one\n\nline two with assignee\n"
    _write_session_system_prompt(tmp_path, "sess-1", prompt)
    assert has_system_prompt_changed(str(tmp_path), "sess-1", prompt) is False


def test_system_prompt_changed_true_when_different(tmp_path):
    from chat_runtime import has_system_prompt_changed

    _write_session_system_prompt(tmp_path, "sess-1", "old assignee prompt")
    assert (
        has_system_prompt_changed(str(tmp_path), "sess-1", "new assignee prompt")
        is True
    )


def test_system_prompt_changed_detects_assignee_section_swap(tmp_path):
    from chat_runtime import has_system_prompt_changed

    base = "PREAMBLE\n\nCODEX\n\nWorking directory: /x\n\n"
    _write_session_system_prompt(tmp_path, "sess-1", base + "NAME: Beacon")
    assert (
        has_system_prompt_changed(str(tmp_path), "sess-1", base + "NAME: Critic")
        is True
    )
