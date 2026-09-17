"""Unit tests for the pure ``chat_stream.translate_stream`` translator.

These exercise the stream-json → SSE mapping on recorded-shape fixtures, with
no subprocess or HTTP involved. The fixtures mirror the real ``claude`` CLI
wire format (``{"type": "stream_event", "event": {...}}`` envelopes plus
``system`` / ``assistant`` / ``user`` / ``result`` line kinds).

Run from the ``api/`` directory:

    ./venv/bin/python -m pytest test_chat_stream.py -v
"""

from __future__ import annotations

from chat_stream import translate_stream


# ---------------------------------------------------------------------------
# Fixture builders (match the verified claude 2.x stream-json shapes)
# ---------------------------------------------------------------------------
def _msg_start() -> dict:
    return {"type": "stream_event", "event": {"type": "message_start"}}


def _block_start(index: int, block: dict) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_start", "index": index, "content_block": block},
    }


def _delta(index: int, delta: dict) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": index, "delta": delta},
    }


def _block_stop(index: int) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": index},
    }


def _result(*, is_error: bool = False, stop_reason: str = "end_turn") -> dict:
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "stop_reason": stop_reason,
    }


# ---------------------------------------------------------------------------
# thinking → text → done
# ---------------------------------------------------------------------------
def test_thinking_then_text_then_done_accumulates_only_visible_text():
    lines = [
        {"type": "system"},
        _msg_start(),
        _block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
        _delta(0, {"type": "thinking_delta", "thinking": "Let me "}),
        _delta(0, {"type": "thinking_delta", "thinking": "reason."}),
        _delta(0, {"type": "signature_delta", "signature": "abc"}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": ""}),
        _delta(1, {"type": "text_delta", "text": "Hi"}),
        _delta(1, {"type": "text_delta", "text": " there!"}),
        _block_stop(1),
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "rate_limit_event"},
        _result(),
    ]

    events = list(translate_stream(lines))

    assert events[0] == ("thinking", {"text": "Let me "})
    assert events[1] == ("thinking", {"text": "reason."})
    assert events[2] == ("text", {"text": "Hi"})
    assert events[3] == ("text", {"text": " there!"})

    name, data = events[-1]
    assert name == "done"
    # full_text excludes the thinking text — only visible text deltas.
    assert data["full_text"] == "Hi there!"
    assert data["stop_reason"] == "end_turn"


# ---------------------------------------------------------------------------
# tool_use + tool_result interleaving
# ---------------------------------------------------------------------------
def test_tool_use_and_tool_result_interleaving():
    lines = [
        _msg_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _delta(0, {"type": "text_delta", "text": "Calling a tool. "}),
        _block_stop(0),
        _block_start(
            1, {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}
        ),
        _delta(1, {"type": "input_json_delta", "partial_json": '{"city":'}),
        _delta(1, {"type": "input_json_delta", "partial_json": ' "Paris"}'}),
        _block_stop(1),
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "Sunny, 22C",
                        "is_error": False,
                    }
                ]
            },
        },
        _block_start(2, {"type": "text", "text": ""}),
        _delta(2, {"type": "text_delta", "text": "It is sunny."}),
        _block_stop(2),
        _result(),
    ]

    events = list(translate_stream(lines))
    names = [name for name, _ in events]

    # Ordering: text → tool_use → tool_result → text → done.
    assert names == ["text", "tool_use", "tool_result", "text", "done"]

    tool_use = next(d for n, d in events if n == "tool_use")
    assert tool_use == {"id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}}

    tool_result = next(d for n, d in events if n == "tool_result")
    assert tool_result == {"id": "toolu_1", "content": "Sunny, 22C", "is_error": False}

    done = events[-1][1]
    assert done["full_text"] == "Calling a tool. It is sunny."


def _assistant_snapshot(blocks: list[dict]) -> dict:
    """A full ``assistant``-role message snapshot (distinct from stream_event deltas)."""
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


# ---------------------------------------------------------------------------
# assistant-snapshot tool_use recovery (POS-1756)
#
# The claude CLI occasionally (CLI-version-dependent) omits the delta-path
# content_block_start/content_block_stop pair for a tool_use block and only
# ever reports it via the full ``assistant`` message snapshot. Without
# recovery, that tool_use — including AskUserQuestion — silently vanishes.
# ---------------------------------------------------------------------------
def test_assistant_snapshot_recovers_tool_use_missing_from_delta_stream():
    """A tool_use present only in the assistant snapshot is still surfaced."""
    lines = [
        _assistant_snapshot(
            [
                {"type": "text", "text": "Let me ask you something."},
                {
                    "type": "tool_use",
                    "id": "toolu_recovered",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {"question": "Pick one", "header": "H", "options": []}
                        ]
                    },
                },
            ]
        ),
        _result(),
    ]
    events = list(translate_stream(lines))
    tool_events = [d for n, d in events if n == "tool_use"]
    assert tool_events == [
        {
            "id": "toolu_recovered",
            "name": "AskUserQuestion",
            "input": {
                "questions": [{"question": "Pick one", "header": "H", "options": []}]
            },
        }
    ]
    # Terminal is still exactly one done.
    assert [n for n, _ in events if n in ("done", "error")] == ["done"]


def test_assistant_snapshot_does_not_duplicate_delta_emitted_tool_use():
    """The same tool_use id in BOTH the delta stream and a trailing assistant
    snapshot is emitted only once — recovery dedups by id."""
    lines = [
        _block_start(
            0,
            {
                "type": "tool_use",
                "id": "toolu_dup",
                "name": "get_weather",
                "input": {"city": "Paris"},
            },
        ),
        _block_stop(0),
        _assistant_snapshot(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_dup",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
            ]
        ),
        _result(),
    ]
    events = list(translate_stream(lines))
    tool_event_names = [n for n, _ in events if n == "tool_use"]
    assert tool_event_names == ["tool_use"]  # exactly one, not two


def test_assistant_snapshot_before_block_stop_does_not_duplicate():
    """When an assistant snapshot arrives BEFORE content_block_stop,
    the tool_use must still be emitted only once (snapshot recovery wins)."""
    lines = [
        _block_start(
            0,
            {
                "type": "tool_use",
                "id": "toolu_mid",
                "name": "ToolSearch",
                "input": {},
            },
        ),
        _assistant_snapshot(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_mid",
                    "name": "ToolSearch",
                    "input": {"query": "select:mcp__praxis__search_project_context"},
                },
            ]
        ),
        _block_stop(0),
        _result(),
    ]
    events = list(translate_stream(lines))
    tool_event_names = [n for n, _ in events if n == "tool_use"]
    assert tool_event_names == ["tool_use"]  # exactly one, not two


def test_assistant_snapshot_with_no_content_list_is_ignored():
    """A malformed/contentless assistant snapshot never crashes the translator."""
    lines = [
        {"type": "assistant", "message": {"role": "assistant"}},  # no "content" key
        _result(),
    ]
    events = list(translate_stream(lines))
    assert [n for n, _ in events] == ["done"]


def test_tool_use_falls_back_to_start_input_when_no_delta():
    lines = [
        _block_start(
            0,
            {
                "type": "tool_use",
                "id": "toolu_x",
                "name": "search",
                "input": {"q": "cats"},
            },
        ),
        _block_stop(0),
        _result(),
    ]
    events = list(translate_stream(lines))
    tool_use = next(d for n, d in events if n == "tool_use")
    assert tool_use["input"] == {"q": "cats"}


# ---------------------------------------------------------------------------
# unknown block kinds dropped
# ---------------------------------------------------------------------------
def test_unknown_line_and_block_kinds_are_ignored():
    lines = [
        {"type": "totally_unknown_line"},
        _block_start(0, {"type": "some_future_block", "data": 1}),
        _delta(0, {"type": "mystery_delta", "value": 42}),
        _block_stop(0),
        _block_start(1, {"type": "text", "text": ""}),
        _delta(1, {"type": "text_delta", "text": "ok"}),
        _block_stop(1),
        _result(),
    ]
    events = list(translate_stream(lines))
    names = [name for name, _ in events]
    # Only the real text delta + terminal survive.
    assert names == ["text", "done"]
    assert events[-1][1]["full_text"] == "ok"


# ---------------------------------------------------------------------------
# error result line → downgraded to done when content exists (POS-1892)
# ---------------------------------------------------------------------------
def test_error_result_with_content_yields_done_not_error():
    """POS-1892: when content was already streamed, a result.is_error should
    yield done (not error) so the user sees their content without a red bubble."""
    lines = [
        _block_start(0, {"type": "text", "text": ""}),
        _delta(0, {"type": "text_delta", "text": "partial"}),
        _result(is_error=True),
    ]
    events = list(translate_stream(lines))
    terminals = [name for name, _ in events if name in ("done", "error")]
    assert terminals == ["done"]
    # The visible text delta before the error is still surfaced.
    assert events[0] == ("text", {"text": "partial"})
    # full_text carries the accumulated content.
    assert events[-1][1]["full_text"] == "partial"


def test_error_result_without_content_yields_error():
    """A genuine error (no content produced) still surfaces as an error."""
    lines = [
        _result(is_error=True),
    ]
    events = list(translate_stream(lines))
    terminals = [name for name, _ in events if name in ("done", "error")]
    assert terminals == ["error"]


def test_error_result_with_tool_use_yields_done():
    """POS-1892: tool activity counts as content — error is downgraded."""
    lines = [
        _block_start(
            0, {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
        ),
        _block_stop(0),
        _result(is_error=True),
    ]
    events = list(translate_stream(lines))
    terminals = [name for name, _ in events if name in ("done", "error")]
    assert terminals == ["done"]


# ---------------------------------------------------------------------------
# exactly one terminal event
# ---------------------------------------------------------------------------
def test_exactly_one_terminal_done_when_no_result_line():
    lines = [
        _block_start(0, {"type": "text", "text": ""}),
        _delta(0, {"type": "text_delta", "text": "hi"}),
        _block_stop(0),
        # No `result` line at all — translator must synthesize exactly one done.
    ]
    events = list(translate_stream(lines))
    terminals = [name for name, _ in events if name in ("done", "error")]
    assert terminals == ["done"]


def test_no_events_after_terminal_result():
    lines = [
        _delta(0, {"type": "text_delta", "text": "a"}),
        _result(),
        _delta(0, {"type": "text_delta", "text": "should-not-appear"}),
    ]
    events = list(translate_stream(lines))
    assert events == [("text", {"text": "a"}), ("done", {"stop_reason": "end_turn", "full_text": "a"})]


def test_non_dict_lines_are_skipped():
    lines = ["not-a-dict", 123, None, _delta(0, {"type": "text_delta", "text": "x"}), _result()]
    events = list(translate_stream(lines))
    assert ("text", {"text": "x"}) in events
    assert events[-1][0] == "done"
