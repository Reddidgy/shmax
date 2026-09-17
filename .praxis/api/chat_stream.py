"""Pure ``claude`` stream-json → SSE-event translator for the Praxis Local API.

This module is deliberately **side-effect-free** and isolated from subprocess
spawning and HTTP. It consumes already-parsed stream-json line objects (the
newline-delimited JSON that ``claude --print --output-format stream-json
--include-partial-messages --verbose`` writes to stdout) and yields stable
``(event_name, data_dict)`` tuples that the endpoint frames as SSE. Keeping it
pure makes it unit-testable against recorded fixtures.

The ``claude`` CLI wraps the Anthropic content-block streaming model
(``message_start → content_block_start → content_block_delta →
content_block_stop → message_stop``) inside ``{"type": "stream_event",
"event": {...}}`` envelopes, interleaved with its own line kinds (``system``,
``assistant``, ``result``, ``rate_limit_event``, ``user`` for tool results).

The stable SSE façade this produces:

* ``thinking``     — ``{text}``               thinking-block delta
* ``text``         — ``{text}``               text-block delta (accumulated)
* ``tool_use``     — ``{id, name, input}``    tool_use block (on its stop)
* ``tool_result``  — ``{id, content, is_error}`` tool-result line
* ``done``         — ``{stop_reason, full_text}`` terminal clean finish
* ``error``        — ``{error}``              the single terminal failure event

Contract: events follow ``claude`` stdout byte order; visible ``text`` deltas
are accumulated so ``done.full_text`` carries the complete answer; **exactly
one** terminal event is emitted (``done`` xor ``error``); **unknown line kinds
are ignored** (allowlist → tolerant of schema drift).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

logger = logging.getLogger(__name__)

# What the translator yields: an SSE event name plus its JSON data payload.
# (Plain alias rather than the 3.12 ``type`` statement, to match the runtime.)
SseEvent = tuple[str, dict[str, Any]]


class _BlockState:
    """Mutable accumulator for one in-flight ``tool_use`` content block.

    ``content_block_start`` carries the tool ``id``/``name`` but often an empty
    or partial ``input``; the arguments stream in as ``input_json_delta``
    fragments. We stitch the partial JSON together and parse it once the block
    stops, falling back to the (possibly complete) start-time input.
    """

    __slots__ = ("tool_id", "name", "partial_json", "start_input")

    def __init__(self, tool_id: str, name: str, start_input: object) -> None:
        self.tool_id = tool_id
        self.name = name
        self.start_input = start_input
        self.partial_json = ""


def _coerce_tool_input(state: _BlockState) -> object:
    """Resolve a tool block's final ``input`` from streamed JSON or start value."""
    import json

    if state.partial_json:
        try:
            return json.loads(state.partial_json)
        except (json.JSONDecodeError, ValueError):
            # Keep the raw fragment rather than dropping the tool call.
            return state.partial_json
    return state.start_input if state.start_input is not None else {}


def _iter_tool_result_events(content_blocks: object) -> Iterator[SseEvent]:
    """Yield ``tool_result`` events for any tool_result blocks in a ``user`` line.

    ``claude`` reports tool execution results as a ``user``-role message whose
    ``content`` is a list of ``tool_result`` blocks (Anthropic's model for
    feeding results back). Anything that is not a well-formed tool_result block
    is skipped (allowlist).
    """
    if not isinstance(content_blocks, list):
        return
    for block in content_blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        yield (
            "tool_result",
            {
                "id": block.get("tool_use_id", ""),
                "content": block.get("content", ""),
                "is_error": bool(block.get("is_error", False)),
            },
        )


def translate_stream(lines: Iterable[dict[str, object]]) -> Iterator[SseEvent]:
    """Translate parsed stream-json line objects into ``(event, data)`` tuples.

    ``lines`` is an iterable of already-``json.loads``-ed objects. The function
    is a pure generator: it never spawns, reads files, or touches HTTP. It
    accumulates visible text so the terminal ``done`` carries ``full_text``.

    Exactly one terminal event is yielded:

    * a ``done`` with ``{stop_reason, full_text, usage?}`` on a clean
      ``message_stop`` / ``result`` line, or
    * an ``error`` with ``{error}`` if a line indicates a non-success result.

    If the iterable ends without an explicit terminal line, a ``done`` is
    synthesized so callers always observe a terminal event. Unknown line kinds
    are ignored.

    Token usage (``input_tokens``, ``output_tokens``, cache metrics) is
    collected from Anthropic stream events (``message_start``, ``message_delta``)
    and from the CLI's ``result`` line. The ``result`` line is authoritative;
    stream-event counts are the fallback when the ``result`` line lacks usage.
    """
    full_text_parts: list[str] = []
    open_blocks: dict[int, _BlockState] = {}
    stop_reason: str | None = None
    terminated = False
    # Token usage accumulated from Anthropic streaming events (fallback).
    stream_usage: dict[str, int] = {}
    # tool_use ids already yielded via the delta path (content_block_stop), so
    # the assistant-snapshot fallback below can dedup instead of re-emitting.
    emitted_tool_ids: set[str] = set()
    # POS-1892: tracks whether any content (text, thinking, tool_use) was
    # yielded before a terminal event, so a late `is_error` on the result
    # line does not override useful output with a red error bubble.
    has_content = False

    for line in lines:
        if terminated:
            break
        if not isinstance(line, dict):
            continue

        kind = line.get("type")

        if kind == "stream_event":
            event = line.get("event")
            if not isinstance(event, dict):
                continue
            for sse_event in _handle_stream_event(
                event, full_text_parts, open_blocks, emitted_tool_ids
            ):
                yield sse_event
                if sse_event[0] in ("text", "thinking", "tool_use"):
                    has_content = True
            # Accumulate token usage from Anthropic streaming events.
            _collect_stream_usage(event, stream_usage)
            # message_stop inside the envelope is the Anthropic terminal marker;
            # we defer the actual `done` to the CLI's own `result` line, which
            # also carries the authoritative stop_reason. Capture stop_reason
            # from message_delta when present.
            if event.get("type") == "message_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])

        elif kind == "assistant":
            # Full assistant-message snapshot. If the delta stream already
            # yielded a tool_use block, we skip it here (dedup by id).
            # Otherwise we recover the tool_use so downstream consumers
            # (e.g. the AskUserQuestion pause) still see it.
            message = line.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        tid = str(block.get("id", ""))
                        if tid and tid not in emitted_tool_ids:
                            emitted_tool_ids.add(tid)
                            logger.warning(
                                "tool_use '%s' recovered from assistant snapshot"
                                " (delta stream did not emit it)",
                                block.get("name", ""),
                            )
                            yield (
                                "tool_use",
                                {
                                    "id": tid,
                                    "name": str(block.get("name", "")),
                                    "input": block.get("input", {}),
                                },
                            )
                            has_content = True

        elif kind == "user":
            message = line.get("message")
            content = (
                message.get("content") if isinstance(message, dict) else None
            )
            yield from _iter_tool_result_events(content)

        elif kind == "result":
            terminated = True
            if line.get("is_error"):
                # POS-1892: when content was already streamed, a late is_error
                # (e.g. a tool returned an error, or the model hit a retryable
                # failure after producing output) should NOT mark the whole
                # turn as an error. Downgrade to a normal completion so the
                # user sees their useful content without a red error bubble.
                if has_content:
                    result_stop = line.get("stop_reason") or stop_reason or "end_turn"
                    done_data: dict[str, Any] = {
                        "stop_reason": str(result_stop),
                        "full_text": "".join(full_text_parts),
                    }
                    usage = _extract_usage(line.get("usage"), stream_usage)
                    if usage:
                        done_data["usage"] = usage
                    cost = line.get("cost_usd")
                    if isinstance(cost, (int, float)) and cost > 0:
                        done_data["cost_usd"] = cost
                    logger.info(
                        "result.is_error downgraded to done — content already"
                        " streamed (POS-1892)"
                    )
                    yield ("done", done_data)
                else:
                    # No content produced → genuine error, surface it.
                    result_text = line.get("result")
                    if isinstance(result_text, str) and result_text.strip():
                        message = result_text.strip()
                    else:
                        message = f"claude reported an error: {line.get('subtype') or 'error'}"
                    yield ("error", {"error": message})
            else:
                # The CLI's `result` line carries the authoritative stop_reason;
                # fall back to one captured from message_delta, then a default.
                result_stop = line.get("stop_reason") or stop_reason or "end_turn"
                done_data: dict[str, Any] = {
                    "stop_reason": str(result_stop),
                    "full_text": "".join(full_text_parts),
                }
                # Attach token usage: prefer the result line's own `usage`
                # (authoritative), fall back to stream-accumulated counts.
                usage = _extract_usage(line.get("usage"), stream_usage)
                if usage:
                    done_data["usage"] = usage
                # Attach cost_usd from the result line when present.
                cost = line.get("cost_usd")
                if isinstance(cost, (int, float)) and cost > 0:
                    done_data["cost_usd"] = cost
                yield ("done", done_data)
        # All other line kinds (system, rate_limit_event, …) are ignored.

    if not terminated:
        done_data = {
            "stop_reason": stop_reason or "end_turn",
            "full_text": "".join(full_text_parts),
        }
        usage = _extract_usage(None, stream_usage)
        if usage:
            done_data["usage"] = usage
        yield ("done", done_data)


def _handle_stream_event(
    event: dict[str, object],
    full_text_parts: list[str],
    open_blocks: dict[int, _BlockState],
    emitted_tool_ids: set[str],
) -> Iterator[SseEvent]:
    """Map one Anthropic ``stream_event`` envelope to zero or more SSE events."""
    event_type = event.get("type")

    if event_type == "content_block_start":
        index = _as_index(event.get("index"))
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            open_blocks[index] = _BlockState(
                tool_id=str(block.get("id", "")),
                name=str(block.get("name", "")),
                start_input=block.get("input"),
            )
        return

    if event_type == "content_block_delta":
        index = _as_index(event.get("index"))
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return
        delta_type = delta.get("type")
        if delta_type == "thinking_delta":
            text = delta.get("thinking")
            if isinstance(text, str) and text:
                yield ("thinking", {"text": text})
        elif delta_type == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                full_text_parts.append(text)
                yield ("text", {"text": text})
        elif delta_type == "input_json_delta":
            block_state = open_blocks.get(index)
            if block_state is not None:
                fragment = delta.get("partial_json")
                if isinstance(fragment, str):
                    block_state.partial_json += fragment
        # signature_delta and any other delta kinds are ignored.
        return

    if event_type == "content_block_stop":
        index = _as_index(event.get("index"))
        block_state = open_blocks.pop(index, None)
        if block_state is not None and block_state.tool_id not in emitted_tool_ids:
            emitted_tool_ids.add(block_state.tool_id)
            yield (
                "tool_use",
                {
                    "id": block_state.tool_id,
                    "name": block_state.name,
                    "input": _coerce_tool_input(block_state),
                },
            )
        return

    # message_start / message_delta / message_stop carry no directly-emitted
    # SSE payload here (stop_reason is read by the caller); ignore the rest.


def _as_index(value: object) -> int:
    """Coerce a content-block index to int, defaulting to 0 on bad input."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# ── Token usage helpers ─────────────────────────────────────────────────────

# Keys we recognize in usage objects (Anthropic + Claude CLI).
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _collect_stream_usage(
    event: dict[str, object], acc: dict[str, int]
) -> None:
    """Accumulate token counts from Anthropic streaming events.

    * ``message_start.message.usage`` → ``input_tokens`` (and optionally
      ``cache_creation_input_tokens`` / ``cache_read_input_tokens``).
    * ``message_delta.usage`` → ``output_tokens``.

    Counts are **max-merged**: if the same key appears in multiple events the
    larger value wins, since the API can report cumulative counts.
    """
    event_type = event.get("type")

    if event_type == "message_start":
        msg = event.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
            if isinstance(usage, dict):
                _merge_usage(acc, usage)

    elif event_type == "message_delta":
        usage = event.get("usage")
        if isinstance(usage, dict):
            _merge_usage(acc, usage)


def _merge_usage(acc: dict[str, int], source: dict[str, object]) -> None:
    """Max-merge recognized integer usage keys from *source* into *acc*."""
    for key in _USAGE_KEYS:
        value = source.get(key)
        if isinstance(value, int) and value > 0:
            acc[key] = max(acc.get(key, 0), value)


def _extract_usage(
    result_usage: object,
    stream_usage: dict[str, int],
) -> dict[str, int] | None:
    """Return a clean usage dict, preferring *result_usage* over *stream_usage*.

    Returns ``None`` when no meaningful usage data is available.
    """
    # Prefer the authoritative result-line usage when present and non-empty.
    if isinstance(result_usage, dict):
        clean: dict[str, int] = {}
        for key in _USAGE_KEYS:
            value = result_usage.get(key)
            if isinstance(value, int) and value > 0:
                clean[key] = value
        if clean:
            return clean

    # Fall back to stream-accumulated counts.
    return stream_usage if stream_usage else None
