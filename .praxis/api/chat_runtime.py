"""Side-effect-light runtime helpers for the Praxis Local API.

Holds the per-session rate-limit state (Lock-guarded dict + fail-open
check/mark), the request-body validation, and the pure request-shaping
helpers (model resolution, transcript assembly, ``claude`` argv vector, SSE
framing). Kept out of :mod:`praxis_local_api` purely to keep that file small;
nothing here spawns a subprocess or touches HTTP routing. The CLI translator
lives in :mod:`chat_stream`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from threading import Lock

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Popen kwargs shared by every pre-generation subprocess (stdin compaction,
# session-rotation compaction) so each leads its own process group/session on Unix (or
# its own process group on Windows) — this is what makes it SAFE for
# ``terminate_process_group`` (``os.killpg`` / ``taskkill /T``) to kill just
# this subprocess tree when "Stop generating" fires mid-phase (POS-1918),
# rather than taking down the server's own process group.
_PRE_PROC_POPEN_KWARGS: dict[str, object] = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if _IS_WINDOWS
    else {"start_new_session": True}
)

# Min seconds between /api/chat requests per session.
CHAT_RATE_LIMIT_SECONDS = max(1, int(os.environ.get("CHAT_RATE_LIMIT_SECONDS", 5)))

# ---------------------------------------------------------------------------
# Image input limits (defensive ceilings — the frontend caps are stricter so
# the user sees an inline message before a request is even built; these are the
# authoritative safety net). All env-overridable, mirroring the
# CHAT_RATE_LIMIT_SECONDS pattern above.
# ---------------------------------------------------------------------------
# Media types accepted for user-supplied chat images. All current Claude models
# support vision; only these four are offered to the user.
SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# Max images attachable to a single user message.
MAX_IMAGES_PER_MESSAGE = max(1, int(os.environ.get("MAX_IMAGES_PER_MESSAGE", 5)))

# Max images across the whole request (every user turn is replayed each turn to
# keep earlier images in context, so this bounds the conversation total).
MAX_IMAGES_PER_REQUEST = max(1, int(os.environ.get("MAX_IMAGES_PER_REQUEST", 10)))

# Max decoded bytes for a single image (5 MB).
MAX_IMAGE_BYTES = max(1, int(os.environ.get("MAX_IMAGE_BYTES", 5 * 1024 * 1024)))

# Max decoded bytes summed across all images in the request (20 MB).
MAX_TOTAL_IMAGE_BYTES = max(
    1, int(os.environ.get("MAX_TOTAL_IMAGE_BYTES", 20 * 1024 * 1024))
)

# Default Claude model alias when /api/chat omits ``model`` (balanced tier).
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "sonnet")

# Optional client-supplied session id header — distinguishes two browser
# sessions behind one loopback IP so they rate-limit independently.
CHAT_SESSION_HEADER = "X-Chat-Session-Id"

# ---------------------------------------------------------------------------
# Per-session rate limiting (Lock-guarded dict + stale cleanup; fail-open)
# ---------------------------------------------------------------------------
# In-memory dict of last-request timestamps keyed by session, guarded by a
# Lock, stale entries pruned on each check. State resets on process restart.
_chat_rate_limits: dict[str, float] = {}
_chat_rate_limits_lock = Lock()


def chat_session_key(client_ip: str | None, session_id_header: str | None) -> str:
    """Derive the rate-limit key: client IP plus an optional session-id header.

    Loopback bind means the IP is usually ``127.0.0.1`` for everyone, so the
    optional header distinguishes concurrent sessions; IP alone is the fallback.
    """
    client_ip = client_ip or "unknown"
    session_id = (session_id_header or "").strip()[:128]
    return f"{client_ip}:{session_id}" if session_id else client_ip


def check_chat_rate_limit(session_key: str) -> tuple[bool, int]:
    """Return ``(is_limited, retry_after_seconds)`` for ``session_key``.

    Limited when a prior request landed within ``CHAT_RATE_LIMIT_SECONDS``.
    Prunes stale entries under the lock so the dict cannot grow unbounded.
    """
    now = time.time()
    stale_threshold = CHAT_RATE_LIMIT_SECONDS * 2
    with _chat_rate_limits_lock:
        stale = [
            key
            for key, ts in _chat_rate_limits.items()
            if now - ts > stale_threshold
        ]
        for key in stale:
            del _chat_rate_limits[key]
        last = _chat_rate_limits.get(session_key)

    if last is None:
        return False, 0
    elapsed = now - last
    if elapsed >= CHAT_RATE_LIMIT_SECONDS:
        return False, 0
    return True, max(1, int(CHAT_RATE_LIMIT_SECONDS - elapsed))


def mark_chat_request(session_key: str) -> None:
    """Record ``session_key``'s request time so the next one is rate-limited."""
    with _chat_rate_limits_lock:
        _chat_rate_limits[session_key] = time.time()


# ---------------------------------------------------------------------------
# Request validation + claude invocation helpers (pure)
# ---------------------------------------------------------------------------
_VALID_ROLES = {"user", "assistant"}


def validate_chat_body(data: object) -> str | None:
    """Return an error string if ``data`` is not a valid /api/chat body, else None.

    Runs before the stream opens so the client can tell a plain 400 from an
    in-stream ``error`` event.
    """
    if not isinstance(data, dict):
        return "Request body must be a JSON object"

    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return "'messages' must be a non-empty array"

    total_images = 0
    total_image_bytes = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"messages[{index}] must be an object"
        role = message.get("role")
        if role not in _VALID_ROLES:
            return f"messages[{index}].role must be 'user' or 'assistant'"
        if not isinstance(message.get("content"), str):
            return f"messages[{index}].content must be a string"

        tool_summary = message.get("tool_summary")
        if tool_summary is not None and not isinstance(tool_summary, str):
            return f"messages[{index}].tool_summary must be a string"

        if "images" in message and message.get("images") is not None:
            error = _validate_message_images(message["images"], index, role)
            if error is not None:
                return error
            images = message["images"]
            total_images += len(images)
            if total_images > MAX_IMAGES_PER_REQUEST:
                return (
                    f"images per request must not exceed {MAX_IMAGES_PER_REQUEST}"
                )
            for image_index, image in enumerate(images):
                decoded_len = len(
                    base64.b64decode(image["data"], validate=True)
                )
                if decoded_len > MAX_IMAGE_BYTES:
                    return (
                        f"messages[{index}].images[{image_index}].data "
                        f"decodes to {decoded_len} bytes, exceeding the "
                        f"{MAX_IMAGE_BYTES}-byte per-image limit"
                    )
                total_image_bytes += decoded_len
            if total_image_bytes > MAX_TOTAL_IMAGE_BYTES:
                return (
                    f"total decoded image bytes must not exceed "
                    f"{MAX_TOTAL_IMAGE_BYTES}"
                )

    if messages[-1].get("role") != "user":
        return "The last message must have role 'user'"

    system_prompt = data.get("system_prompt")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        return "'system_prompt' is required and must be a non-empty string"

    context_flags = data.get("context_flags")
    if context_flags is not None:
        if not isinstance(context_flags, dict):
            return "'context_flags' must be an object"
        for key in ("context_router_requested", "prd_requested"):
            value = context_flags.get(key)
            if value is not None and not isinstance(value, bool):
                return f"'context_flags.{key}' must be a boolean"

    session_id = data.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return "'session_id' must be a string"

    return None


def _validate_message_images(
    images: object, index: int, role: object
) -> str | None:
    """Validate one message's ``images`` field; return an error string or None.

    Enforces: ``images`` is a list; images appear only on ``user`` turns; each
    item is an object with a ``media_type`` in the allow-list and a non-empty
    base64 ``data`` string (decodable with ``validate=True``); and the
    per-message count cap. Per-image / per-request / total decoded-byte caps are
    checked by the caller, which already decodes ``data`` once. Error strings use
    the ``messages[i].images[j]…`` shape to match the existing validation style.
    """
    if not isinstance(images, list):
        return f"messages[{index}].images must be an array"
    if role != "user":
        return f"messages[{index}].images is only allowed on a 'user' message"
    if len(images) > MAX_IMAGES_PER_MESSAGE:
        return (
            f"messages[{index}].images must not exceed "
            f"{MAX_IMAGES_PER_MESSAGE} images"
        )
    for image_index, image in enumerate(images):
        prefix = f"messages[{index}].images[{image_index}]"
        if not isinstance(image, dict):
            return f"{prefix} must be an object"
        media_type = image.get("media_type")
        if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
            return (
                f"{prefix}.media_type must be one of "
                f"{', '.join(sorted(SUPPORTED_IMAGE_MEDIA_TYPES))}"
            )
        data = image.get("data")
        if not isinstance(data, str) or not data:
            return f"{prefix}.data must be a non-empty base64 string"
        try:
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return f"{prefix}.data must be valid base64"
    return None


def resolve_model(requested: object) -> str:
    """Return the requested model when a non-empty string, else ``DEFAULT_MODEL``.

    Unrecognized strings pass through to ``claude --model`` (which falls back
    itself); we never error on the model — degrade, don't block.
    """
    if isinstance(requested, str) and requested.strip():
        return requested.strip()
    return DEFAULT_MODEL


def build_transcript(messages: list[dict[str, object]]) -> str:
    """Flatten multi-turn history into one role-marked transcript string.

    ``claude --print`` is stateless per invocation, so history is rendered as
    ``[User]:`` / ``[Assistant]:`` lines ending on the latest user turn, then
    written to the child's stdin.

    Assistant turns may carry an optional ``tool_summary`` — a compact,
    pre-truncated record of the MCP tools that turn already called and what they
    returned (POS-1570). When present it is rendered inline, parenthesized, before
    the assistant's visible text, e.g.
    ``[Assistant]: (called get_task id=42 → {title:"Fix auth"}) Final answer.``
    This lets the stateless CLI see what it already fetched on prior turns so it
    does not redundantly re-invoke the same tools on a follow-up.

    Assistant turns may also carry an optional ``thinking`` — truncated reasoning
    from that turn, present only for the most recent few assistant turns
    (POS-1777). When present it is rendered inline BEFORE the tool summary, e.g.
    ``[Assistant]: (thinking: considered X, chose Y) (called get_task id=42 → ...) Final answer.``
    This carries the model's own reasoning thread forward across otherwise
    stateless turns. When both ``thinking`` and ``tool_summary`` are absent a
    turn renders exactly as it always has (byte-for-byte unchanged).
    """
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        label = "Assistant" if role == "assistant" else "User"
        thinking = message.get("thinking")
        tool_summary = message.get("tool_summary")

        prefix_parts: list[str] = []
        if role == "assistant" and isinstance(thinking, str) and thinking.strip():
            prefix_parts.append(f"(thinking: {thinking.strip()})")
        if (
            role == "assistant"
            and isinstance(tool_summary, str)
            and tool_summary.strip()
        ):
            prefix_parts.append(f"({tool_summary.strip()})")

        if prefix_parts:
            prefix = " ".join(prefix_parts)
            lines.append(
                f"[{label}]: {prefix} {content}".rstrip()
                if content
                else f"[{label}]: {prefix}"
            )
        else:
            lines.append(f"[{label}]: {content}")
    transcript = "\n".join(lines)
    # Strip bold markdown formatting — stdin sent to the AI model must
    # never contain ** markers (POS-1808).
    return transcript.replace("**", "")


def messages_have_images(messages: list[dict[str, object]]) -> bool:
    """Return True when any message carries a non-empty ``images`` list.

    Drives the input-format switch in the worker: only when this is True do we
    emit the stream-json envelope; text-only turns stay on the plain transcript
    path, byte-for-byte unchanged.
    """
    for message in messages:
        images = message.get("images")
        if isinstance(images, list) and images:
            return True
    return False


def collect_request_images(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return every ``user``-turn image as a stream-json ``image`` content block.

    Blocks are collected in conversation order (earliest user turn first), so a
    turn's own images always trail the images of the turns before it. Malformed
    entries (non-dict messages, non-list ``images``, non-dict image) are skipped
    rather than raising — a bad attachment must never break a turn (POS-1992).
    """
    blocks: list[dict[str, object]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        images = message.get("images")
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict):
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.get("media_type"),
                        "data": image.get("data"),
                    },
                }
            )
    return blocks


def build_stream_json_line(
    text: str, image_blocks: list[dict[str, object]]
) -> str:
    """Wrap *text* + *image_blocks* into ONE stream-json ``user`` line.

    This is the single envelope shape the CLI's ``--input-format stream-json``
    reader accepts: a leading ``text`` block (the verbal anchor — whatever the
    caller assembled, be it a legacy transcript or a session-memory payload)
    followed by the image content blocks. Serialized with :func:`json.dumps`
    and NO trailing newline — the caller appends one (POS-1992).
    """
    content: list[dict[str, object]] = [{"type": "text", "text": text}]
    content.extend(image_blocks)
    return json.dumps(
        {"type": "user", "message": {"role": "user", "content": content}}
    )


def build_user_stream_json(messages: list[dict[str, object]]) -> str:
    """Build the single stream-json ``user`` line carrying text + image blocks.

    The CLI is stateless per invocation, so the full history is flattened by
    :func:`build_transcript` into one leading ``text`` block (the verbal anchor),
    then every image from every ``user`` turn is appended — in conversation order
    — as ``image`` content blocks so earlier-turn images remain in context
    (:func:`collect_request_images`). The whole thing is wrapped as one
    ``{"type":"user","message":{…}}`` object by :func:`build_stream_json_line`
    (no trailing newline — the caller appends one).
    """
    return build_stream_json_line(
        build_transcript(messages), collect_request_images(messages)
    )


def build_stdin_payload(
    messages: list[dict[str, object]],
    *,
    work_dir: str | None = None,
) -> str:
    """Return the exact bytes written to the ``claude`` child's stdin.

    This is the SINGLE source of truth for that payload: image turns get the
    stream-json envelope (:func:`build_user_stream_json`, plus the trailing
    newline the CLI's stream-json reader expects), text-only turns get the
    plain role-marked transcript (:func:`build_transcript`). Both the run
    worker (:mod:`chat_runs`) and the chat debug artifact writer
    (``praxis_local_api._write_chat_debug_payload``) must go through this
    function so the debug file can never drift from what the model actually
    received — that drift (the debug writer always calling ``build_transcript``,
    even on image turns) was the bug this function exists to make structurally
    impossible.

    When *work_dir* is given, every occurrence of the absolute project path
    followed by ``/`` is stripped from the assembled text, producing relative
    paths (e.g. ``src/app.ts`` instead of ``/abs/path/src/app.ts``). The system
    prompt header (``workDir: <path>``) retains the real absolute path because
    it has no trailing ``/`` and therefore survives the replacement unchanged.
    """
    if messages_have_images(messages):
        payload = build_user_stream_json(messages) + "\n"
    else:
        payload = build_transcript(messages)

    if work_dir:
        normalized = work_dir.rstrip("/\\")
        if normalized:
            payload = payload.replace(normalized + "/", "")

    return payload


# ---------------------------------------------------------------------------
# Stdin payload compaction (POS-1830)
# ---------------------------------------------------------------------------
# When build_stdin_payload() output exceeds this many lines, the run worker
# compacts it through a blocking Sonnet CLI call before spawning the main
# generation. "Line count" = number of "\n" in the payload (NOT chars, tokens,
# or message count). Env-overridable so the trigger stays easy to tune.
COMPACTION_LINE_THRESHOLD = max(
    1, int(os.environ.get("CHAT_COMPACTION_LINE_THRESHOLD", 1200))
)

# Character-count threshold for compaction.  When the assembled stdin
# payload exceeds this many characters, the run worker compacts it through
# a blocking Sonnet CLI call before spawning the main generation.
# Env-overridable via CHAT_COMPACTION_CHAR_THRESHOLD.
COMPACTION_CHAR_THRESHOLD = max(
    1, int(os.environ.get("CHAT_COMPACTION_CHAR_THRESHOLD", 100_000))
)

# Sonnet is hardcoded for compaction regardless of the chat's selected model:
# compaction is mechanical summarisation, not reasoning, so it must stay cheap
# and consistent. The "sonnet" alias tracks Anthropic's current Sonnet dated ID.
COMPACTION_MODEL = "sonnet"

# Seconds to wait for the compaction subprocess before killing it and falling
# back to the uncompacted payload. Compaction targets LARGE transcripts, and a
# dense one can take a while for Sonnet to read + rewrite; a too-tight ceiling
# would time out and silently fall back on exactly the payloads the feature
# exists for (the original 30s default did exactly that — see POS-1830). A
# ~1400-line real transcript was measured end-to-end at ~90s, so the default is
# set to 500s to keep meaningful headroom for larger/denser payloads rather than
# racing the observed latency. Env-overridable for tuning.
COMPACTION_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("CHAT_COMPACTION_TIMEOUT_SECONDS", 500))
)

# ---------------------------------------------------------------------------
# Session Rotation (POS-1939)
# ---------------------------------------------------------------------------
# When full.json in a session directory exceeds this byte threshold, the
# worker deletes claude_session_id.txt — forcing the next turn onto the
# session-memory path (with compaction) instead of replaying the growing
# CLI history via --resume. Env-overridable.
SESSION_ROTATION_BYTE_THRESHOLD = max(
    1, int(os.environ.get("CHAT_SESSION_ROTATION_BYTES", 500_000))
)


def should_rotate_session(cwd: str, session_id: str) -> bool:
    """Return True when the session's full.json has grown enough to warrant rotation.

    On the first rotation, fires when ``full.json`` exceeds
    ``SESSION_ROTATION_BYTE_THRESHOLD``.  After a rotation, a marker file
    (``rotation_byte_offset.txt``) records the size at which it fired;
    subsequent rotations only trigger when ``full.json`` has grown by
    another threshold-worth beyond that marker.  This prevents the
    infinite-compaction loop that occurs when the frontend rewrites
    ``full.json`` with its full in-memory history after every turn.

    Never reads/parses ``full.json`` — uses ``os.path.getsize`` only.
    Returns False on any ``OSError``.
    """
    from chat_session_paths import session_dir

    try:
        sdir = session_dir(cwd, session_id)
        full_json_size = os.path.getsize(os.path.join(sdir, "full.json"))
    except OSError:
        return False

    if full_json_size <= SESSION_ROTATION_BYTE_THRESHOLD:
        return False

    # Check rotation marker — only rotate again when full.json has grown
    # by another threshold-worth since the last rotation.
    marker_path = os.path.join(sdir, "rotation_byte_offset.txt")
    try:
        with open(marker_path, "r") as f:
            last_rotation_size = int(f.read().strip())
        return (
            full_json_size > last_rotation_size + SESSION_ROTATION_BYTE_THRESHOLD
        )
    except (OSError, ValueError):
        # No marker or invalid — first rotation for this session
        return True


def has_system_prompt_changed(
    cwd: str, session_id: str, system_prompt: str
) -> bool:
    """Return True when the persisted per-session ``system_prompt.txt``
    exists and differs byte-for-byte from the incoming system prompt.

    A changed system prompt (e.g. an assignee switch mid-session) means a
    resumed CLI session would keep acting as the previous persona — the
    caller must rotate the session: compact previous work and re-create
    the CLI session without ``--resume`` (POS-1940).  A missing file
    (first turn) or any read error returns False.
    """
    from chat_session_paths import session_dir

    try:
        path = os.path.join(
            session_dir(cwd, session_id), "system_prompt.txt"
        )
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read() != system_prompt
    except OSError:
        return False


# System prompt for the Sonnet compaction call. Verbatim from the POS-1830 spec.
COMPACTION_SYSTEM_PROMPT = """\
You are a context compactor. Compress the following conversation transcript into a shorter version that preserves all essential information for continuing the work.

PRESERVE:
- All user instructions, requirements, and questions
- Key decisions made during the conversation
- Acceptance criteria and constraints
- Current state of the work (what is done, what is pending)
- Error messages or issues that are still relevant
- File paths, code references, and technical details that are still relevant

REMOVE:
- Verbose tool outputs (file listings, large code dumps) — summarize what was found
- Process noise (retry requests, tool errors that were resolved, "let me try again")
- Thinking/reasoning segments that led to already-stated conclusions
- Redundant information (if the same thing was stated multiple times, keep only the most complete version)
- Greeting/pleasantry exchanges

OUTPUT: A compressed transcript that a new assistant could read and continue the work without missing any important context. Maintain the [User]: / [Assistant]: format. Be thorough in preserving substance — err on the side of keeping too much rather than too little."""


def compact_stdin_payload(
    payload: str, claude_binary: str | None, cwd: str | None = None,
    proc_hook: Callable[[subprocess.Popen], None] | None = None,
) -> str:
    """Compact *payload* through a blocking Sonnet CLI call; fall back on failure.

    Spawns ``claude --print --model sonnet --system-prompt
    <COMPACTION_SYSTEM_PROMPT>`` (text-only: no MCP, no permission bypass) and
    feeds *payload* on stdin. *cwd* is the project working dir the run worker
    uses to spawn the main generation; passing it here keeps claude resolving the
    same project-scoped config/auth (mirrors ``_run_claude_text``). If *proc_hook*
    is given, it is invoked with the spawned :class:`subprocess.Popen` right
    after launch — this lets the caller record the process so a concurrent
    ``stop()`` can kill it (POS-1918: pre-generation subprocesses must not
    keep running after "Stop generating"). On a clean exit with non-empty
    stdout the compacted transcript is returned (stripped); on ANY failure —
    non-zero exit, timeout, empty output, spawn error — the ORIGINAL *payload*
    is returned unchanged so a broken compaction never blocks a chat turn
    (POS-1830). Never raises.
    """
    argv = [
        claude_binary or "claude",
        "--print",
        "--model",
        COMPACTION_MODEL,
        "--system-prompt",
        COMPACTION_SYSTEM_PROMPT,
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            **_PRE_PROC_POPEN_KWARGS,
        )
        if proc_hook is not None:
            proc_hook(proc)
        try:
            stdout, _stderr = proc.communicate(
                input=payload, timeout=COMPACTION_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            logger.warning("stdin compaction subprocess timed out")
            return payload
    except Exception as exc:  # noqa: BLE001 — compaction must never break a turn
        logger.warning("stdin compaction subprocess failed: %s", exc)
        return payload

    if proc.returncode == 0 and stdout and stdout.strip():
        return stdout.strip()
    logger.warning(
        "stdin compaction produced no usable output (exit=%s); using original "
        "payload",
        proc.returncode,
    )
    return payload


# The claude CLI version the stream-json wire format (flags below, and the
# event shapes chat_stream.py parses) was last verified against. The CLI is an
# external, fast-moving dependency with no compatibility guarantee for this
# unofficial wire format — check_claude_version() logs a warning when the
# installed binary drifts from this so a silent format change surfaces in logs
# instead of as a mysterious feature regression (see POS-1756).
VERIFIED_CLAUDE_CLI_VERSION = "2.1.165"


def check_claude_version(claude_bin: str | None) -> str | None:
    """Run ``claude --version`` and log a warning if it differs from the verified version.

    Best-effort: any failure (binary missing, timeout, unexpected output)
    yields ``None`` rather than raising, since this is a diagnostic aid, not a
    gate — the Flask API must keep working even when ``claude`` itself is
    unavailable or unrecognized.
    """
    import subprocess

    try:
        result = subprocess.run(
            [claude_bin or "claude", "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        version = result.stdout.strip()
        if version and VERIFIED_CLAUDE_CLI_VERSION not in version:
            logger.warning(
                "Claude CLI version '%s' differs from verified '%s'; "
                "stream-json wire format may have changed",
                version,
                VERIFIED_CLAUDE_CLI_VERSION,
            )
        return version
    except Exception:  # noqa: BLE001 — diagnostic helper must never raise
        return None


def build_claude_argv(
    claude_bin: str | None,
    system_prompt: str,
    model: str,
    *,
    with_image_input: bool = False,
    mcp_port: int | None = None,
    extra_mcp_servers: dict | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build the ``claude`` argv vector (never via a shell).

    The fixed flag combination — verified on claude
    ``VERIFIED_CLAUDE_CLI_VERSION`` (see above; :func:`check_claude_version`
    logs when the installed CLI drifts from it) — is the only one that emits
    the partial-message stream-json we translate. When ``with_image_input`` is
    True, ``--input-format stream-json`` is inserted so the worker can feed
    image content blocks on stdin; the default keeps every existing call site
    identical (plain-text transcript input).

    When ``mcp_port`` is given, ``--mcp-config`` is added with an inline JSON
    pointing to the local Praxis MCP server so the subprocess can discover and
    use MCP tools. ``extra_mcp_servers`` (caller-supplied external MCPs from
    ``~/.claude.json``) are merged in; the ``praxis`` key always wins so it
    cannot be overridden by user-registered servers.
    """
    argv = [
        claude_bin or "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if mcp_port is not None:
        import json as _json
        mcp_servers: dict = {
            "praxis": {
                "type": "streamable-http",
                "url": f"http://127.0.0.1:{mcp_port}/stream",
            }
        }
        if extra_mcp_servers:
            # Merge user MCPs; praxis key always wins (defined last, re-applied)
            merged = {**extra_mcp_servers, **{"praxis": mcp_servers["praxis"]}}
            mcp_servers = merged
        mcp_config = _json.dumps({"mcpServers": mcp_servers})
        # --strict-mcp-config REMOVED (POS-1645): the inline config now IS the
        # complete desired set — external MCPs are merged explicitly above.
        argv += ["--mcp-config", mcp_config]
    if with_image_input:
        argv += ["--input-format", "stream-json"]
    if resume_session_id:
        argv += ["--resume", resume_session_id]
    argv += [
        "--system-prompt",
        system_prompt,
        "--model",
        model,
    ]
    return argv


def sse_frame(event_name: str, data: dict[str, object]) -> str:
    """Format one ``(event, data)`` pair as a framed SSE chunk."""
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
