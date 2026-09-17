"""Praxis Local API — local Flask service wrapping the user's `claude` CLI.

This is a *local-only* sibling of ``praxis_api.py``. It runs on the user's own
machine, binds to loopback (``127.0.0.1``) only, and will later spawn the
``claude`` subprocess under the user's authenticated session — so it must never
be reachable off-host. Core PraxisOS never depends on this process and it is
never deployed server-side.

Endpoints:

* ``GET /health``      — connection + ``claude`` availability check (cached).
* ``GET /api/models``  — static three-tier model catalogue.
* ``POST /api/chat``   — SSE streaming of a ``claude`` subprocess turn.

The stream-json → SSE translation lives in the sibling :mod:`chat_stream`
module (pure + unit-testable); this file owns validation, rate limiting, the
``claude`` availability probe, and routing. Each ``POST /api/chat`` turn is run
as a detached **run** owned by :mod:`chat_runs` (a ``RunManager`` whose worker
thread spawns ``claude`` independent of the HTTP connection) so a browser
refresh never kills an in-progress generation; the endpoint merely tails the
run's event buffer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from threading import Lock

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request


from chat_constants import (
    DEBUG_INPUT_FILENAME,
    DEBUG_STDIN_FILENAME,
    DEBUG_SYSTEM_PROMPT_FILENAME,
    SESSION_DEBUG_COPY_FILES,
)
from chat_runs import (
    RUNS,
    RunNotFoundError,
)
from chat_session_paths import session_dir, sanitize_session_id
from chat_session_queue import QUEUE
from chat_session_memory import (
    delete_claude_code_session_id,
    delete_fork_source_claude_sid,
    load_claude_code_session_id,
    load_claude_code_system_prompt_path,
    load_fork_source_claude_sid,
    save_claude_code_session_id,
    save_claude_code_system_prompt,
    save_fork_source_claude_sid,
)
from chat_runtime import (
    CHAT_RATE_LIMIT_SECONDS,
    CHAT_SESSION_HEADER,
    DEFAULT_MODEL,
    _chat_rate_limits,
    _chat_rate_limits_lock,
    build_stdin_payload,
    chat_session_key,
    check_chat_rate_limit,
    mark_chat_request,
    resolve_model,
    sse_frame,
    validate_chat_body,
)
from file_listing import list_project_files
import task_ops
import praxis_logging
import mcp_connections
import console_pty
import settings_ops
import git_diff_ops
import git_history_ops
import route_activity

load_dotenv()

# Re-exported for callers/tests that import these names from this module.
__all__ = [
    "app",
    "is_claude_available",
    "_resolve_claude_binary",
    "check_chat_rate_limit",
    "mark_chat_request",
    "_chat_rate_limits",
    "_chat_rate_limits_lock",
    "DEFAULT_MODEL",
]

app = Flask(__name__)

# Cap the accepted request body so an oversized payload (e.g. many base64
# images) is rejected with a clean 413 before Flask parses it. Base64 inflates
# decoded bytes by ~33%, so the 20 MB decoded image ceiling is ~27 MB on the
# wire; 32 MB gives headroom for the surrounding JSON. The image byte caps in
# chat_runtime are the authoritative per-image/total ceilings.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

# Service version reported by /health. Independent of the PraxisOS app version.
SERVICE_VERSION = "0.1.0"


def _read_api_file_version() -> str:
    """Read the ``api/version`` sentinel from this script's directory."""
    try:
        version_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "version"
        )
        with open(version_path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


API_FILE_VERSION = _read_api_file_version()


@app.errorhandler(413)
def request_entity_too_large(_error):
    """Return JSON for an over-``MAX_CONTENT_LENGTH`` body (not Flask's HTML)."""
    return jsonify({"error": "request body too large"}), 413

# ---------------------------------------------------------------------------
# Configuration (environment-driven; mirrors praxis_api.py conventions for the
# Praxis Local API)
# ---------------------------------------------------------------------------
# Any loopback origin (any scheme/port) + the deployed PraxisOS frontend. This
# is the default because the PraxisOS dev server / preview / production page can
# legitimately run on many local ports, and this service is bound to loopback
# only and handles no credentials or secrets — so reflecting any localhost
# origin is the safe, Ollama/LM-Studio-style pattern (not a CSRF surface).
# `localhost` AND `127.0.0.1` (and `[::1]`) are matched so the browser's choice
# of loopback address never causes a CORS rejection.
DEFAULT_LOCAL_API_CORS_ORIGINS = [
    re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"),
    re.compile(r"^https://([a-z0-9-]+\.)?praxisos\.dev$"),
]


def _get_allowed_origins() -> list:
    """Return the CORS origin matchers for the Praxis Local API.

    If ``LOCAL_API_CORS_ORIGINS`` is set, it is an explicit comma-separated
    allowlist (literal origins) that fully replaces the default. Otherwise the
    default permissive-loopback matchers are used. Mirrors how the feedback API
    reads ``CORS_ALLOWED_ORIGINS``, but defaults open for loopback since the
    service is unreachable off-host.

    The legacy ``CHAT_CORS_ORIGINS`` env var is deprecated; it is still read as
    a fallback so a user who pinned an allowlist under the old name is not
    silently broken by this rename.
    """
    origins_str = (
        os.environ.get("LOCAL_API_CORS_ORIGINS", "")
        or os.environ.get("CHAT_CORS_ORIGINS", "")
    ).strip()
    if origins_str:
        return [o.strip() for o in origins_str.split(",") if o.strip()]
    return DEFAULT_LOCAL_API_CORS_ORIGINS


# Optional explicit path to the `claude` binary. When set it overrides PATH
# resolution, avoiding a false "claude missing" when the API process's PATH
# differs from the user's interactive shell PATH.
CHAT_CLAUDE_BIN = os.environ.get("CHAT_CLAUDE_BIN", "").strip()

# Port the combined API+MCP server listens on. Set at startup in __main__; used
# to pass --mcp-config to the claude subprocess so it discovers MCP tools.
LOCAL_API_PORT: int = int(
    os.environ.get("LOCAL_API_PORT") or os.environ.get("CHAT_API_PORT") or 7865
)

# Working directory for the spawned `claude` process. Defaults to the project
# root, derived from this file's location — so chat runs with the project as
# its context no matter where the server was launched from.
# Works from both `api/` (repo source) and `.praxis/api/` (user copy).
# Override with CHAT_PROJECT_ROOT.
def _derive_project_root() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(script_dir)
    if os.path.basename(parent) == ".praxis":
        return os.path.dirname(parent)
    return parent

CHAT_PROJECT_ROOT = (
    os.environ.get("CHAT_PROJECT_ROOT", "").strip()
    or _derive_project_root()
)


_startup_logger = logging.getLogger("praxis_local_api")


def _ensure_absolute_project_path():
    """Write ``absoluteProjectPath`` into ``general.yaml`` so the browser has it.

    The Chrome File System Access API cannot expose absolute paths. In dev
    mode the Vite plugin writes the path; in production this is the only
    mechanism. Delegates to ``settings_ops.update_settings`` so general.yaml
    has a single Python write path (backup + atomic + locked, POS-2296).
    Never raises — a config problem must not block API startup.
    """
    project_path = CHAT_PROJECT_ROOT.rstrip("/\\") + os.sep
    try:
        current = settings_ops.read_general_config(CHAT_PROJECT_ROOT).get("absoluteProjectPath")
        if isinstance(current, str) and current.strip():
            return
        result = settings_ops.update_settings(
            CHAT_PROJECT_ROOT, {"absoluteProjectPath": project_path}, allow_internal=True,
        )
        if not result.get("ok"):
            _startup_logger.warning("Could not write absoluteProjectPath: %s", result.get("error"))
    except Exception as exc:  # noqa: BLE001 — startup must never fail on config problems
        _startup_logger.warning("Could not ensure absoluteProjectPath: %s", exc)


_ensure_absolute_project_path()


def _load_authed_external_mcp_servers() -> dict:
    """Read ~/.claude.json and return MCP entries that have an Authorization header.

    These are automatically injected into every AI Chat run so the user's
    Claude Code-registered MCPs (Barley, Hops, etc.) work inside PraxisOS chat
    without any per-project configuration.

    The 'praxis' key is always excluded — it is managed by PraxisOS.
    """
    import pathlib
    claude_json_path = pathlib.Path.home() / ".claude.json"
    try:
        with open(claude_json_path) as f:
            data = json.load(f)
        servers = data.get("mcpServers", {})
        return {
            name: cfg
            for name, cfg in servers.items()
            if name != "praxis"
            and isinstance(cfg.get("headers"), dict)
            and cfg["headers"].get("Authorization")
        }
    except Exception:
        return {}


def _resolve_cwd(requested: object) -> str:
    """Pick the working directory for the spawned ``claude`` process.

    Prefers ``requested`` — the request's ``project_root``, which the frontend
    sources from ``general.yaml``'s ``absoluteProjectPath`` — when it is a
    non-empty string naming an existing absolute directory. Otherwise falls back
    to ``CHAT_PROJECT_ROOT`` (derived from this script's location). This lets the
    user pin the directory claude runs in via their project config.
    """
    if isinstance(requested, str):
        candidate = requested.strip()
        if candidate and os.path.isabs(candidate) and os.path.isdir(candidate):
            return candidate
    return CHAT_PROJECT_ROOT


def _validate_project_root(requested: object) -> str | None:
    """Return ``requested`` as a real PraxisOS project root, or ``None``.

    Unlike :func:`_resolve_cwd`, a create operation must target an *explicit*
    project and never silently falls back to ``CHAT_PROJECT_ROOT``: ``None`` is
    returned (→ ``404`` at the route) unless ``requested`` is a non-empty
    absolute path naming an existing directory that contains a ``.praxis``
    subdirectory (the marker of a real PraxisOS project).
    """
    if not isinstance(requested, str):
        return None
    candidate = requested.strip()
    if not candidate or not os.path.isabs(candidate) or not os.path.isdir(candidate):
        return None
    if not os.path.isdir(os.path.join(candidate, ".praxis")):
        return None
    return candidate


def _last_user_message(messages: object) -> str | None:
    """Return the content of the last ``role == "user"`` message, if any.

    Defensive: tolerates a non-list ``messages`` or malformed entries (returns
    ``None``) so the debug dump never raises on unexpected shapes. The chat
    body validator already guarantees the last message is a user turn on the
    happy path; this is purely for the convenience field in the artifact.
    """
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else None
    return None


# Section headings that mark a context block as already present in an assembled
# system_prompt. They MUST stay byte-for-byte identical to the frontend's
# `CONTEXT_ROUTER_HEADING` / `PRD_HEADING` (src/services/chat-context-service.ts)
# so the sentinel check below never double-injects a block the frontend included.
_CONTEXT_ROUTER_HEADING = "# Context Router"
_PRD_HEADING = "# Product Requirements (PRD)"
# Sentinel substring for the MCP Codex. Matches the codex file's heading
# (present in the inlined content) so the safety net fires only when
# neither the inlined codex nor the read instruction is in the system prompt.
_MCP_CODEX_SENTINEL = "# PraxisOS MCP Instructions"
_MCP_CODEX_READ_INSTRUCTION = (
    "MANDATORY BEFORE ANY WORK: You must Read and follow instructions inside .praxis/prompts/praxis-mcp-codex.md"
)


def _read_root_doc(project_root: str, filename: str) -> str | None:
    """Read ``<project_root>/<filename>`` from disk, or ``None`` if unavailable.

    Plain ``open()`` (no File System Access API): this is the server-side safety
    net that reads the project's context docs directly from disk. Any failure
    (missing file, permissions, decode error) resolves to ``None`` so injection
    is simply skipped and a chat turn is never broken by it.
    """
    try:
        with open(os.path.join(project_root, filename), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None
    except Exception as exc:  # noqa: BLE001 — safety net must never break a turn
        app.logger.warning("context doc read failed for %s: %s", filename, exc)
        return None


def _inject_missing_context(
    system_prompt: str, project_root: str, context_flags: object
) -> str:
    """Inject context-router / PRD into ``system_prompt`` when the frontend omitted them.

    Server-side safety net (POS-1596): the frontend assembles the system prompt,
    but if a requested context block silently dropped (e.g. the file was missing
    at frontend read time) the AI would start blind. Here we read the real file
    from ``project_root`` on disk and append it — but ONLY when all of:

    * the user actually wanted the block (``context_flags.*_requested`` is true —
      a deliberately-disabled toggle is respected and never injected), and
    * its sentinel heading is not already present in ``system_prompt`` (so a block
      the frontend included correctly is never double-injected), and
    * the file exists on disk with non-empty content.

    The MCP Codex is UNCONDITIONAL (no flag gate): it is standing tool guidance
    that must reach the AI on every turn, so it is injected from
    a read instruction whenever the codex sentinel is missing from
    the prompt — independent of the context flags.

    When the frontend already assembled the blocks this is a no-op.
    ``context_flags`` is tolerated in any shape; absent flags fall back to the
    historical on/off defaults (context-router on, PRD off).
    """
    additions: list[str] = []

    # Context-router and PRD are permanently excluded from AI Chat (POS-1651).
    # Only the MCP Codex safety net remains active.

    # MCP Codex: unconditional. Guarantee the standing tool instructions reach the
    # AI even if the frontend assembly somehow omitted them.
    if _MCP_CODEX_SENTINEL not in system_prompt:
        additions.append(_MCP_CODEX_READ_INSTRUCTION)
        app.logger.info("safety net injected codex read instruction into system_prompt")

    if not additions:
        return system_prompt
    return system_prompt + "\n\n" + "\n\n".join(additions)


# The debug artifact file names, shared with chat_constants so the prune
# keep-set and the writers cannot drift apart. The debug directory holds
# EXACTLY these three files: the most recent chat input (JSON), the byte-exact
# system prompt, and the byte-exact stdin payload. Any other file found there
# is pruned on each write so stale snapshots never accumulate.
_DEBUG_KEEP = frozenset(
    {DEBUG_INPUT_FILENAME, DEBUG_SYSTEM_PROMPT_FILENAME, DEBUG_STDIN_FILENAME}
    | set(SESSION_DEBUG_COPY_FILES)
)


def _prune_debug_dir(debug_dir: str) -> None:
    """Remove every file in ``debug_dir`` except the kept debug artifacts.

    Keeps the debug directory to ``last_input.json``,
    ``last_system_prompt.txt``, ``last_stdin.txt``, ``full.json``, and
    ``memory.json``. Best-effort: a failure removing any stray entry is logged
    and ignored. Only regular files directly inside ``debug_dir`` are touched —
    subdirectories are left alone.
    """
    try:
        for name in os.listdir(debug_dir):
            if name in _DEBUG_KEEP:
                continue
            path = os.path.join(debug_dir, name)
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    app.logger.warning("could not prune debug file %s: %s", path, exc)
    except OSError as exc:
        app.logger.warning("could not list debug dir %s: %s", debug_dir, exc)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate via the standard len//4 heuristic.

    Deliberately arithmetic, not a tokenizer call: this is a measurement aid
    for before/after context-optimization comparisons (POS-1827), and it must
    add zero latency and zero dependencies to a chat turn.
    """
    return len(text) // 4


def _write_chat_debug_input(
    project_root: str,
    *,
    messages: object,
    system_prompt: object,
    model: object,
    session_id: object = None,
) -> None:
    """Persist the last input sent to the AI as the single ``last_input.json``.

    The debug directory (``<project_root>/.praxis/chat/debug/``) holds EXACTLY one
    file — overwritten each turn — capturing the FINAL post-injection system
    prompt, the full message history, the resolved model, the last user message,
    and the originating ``session_id`` (so you can tell which conversation the
    last call belonged to). Any other file in that directory (e.g. a stale
    per-session snapshot from an earlier build) is pruned on every write so the
    folder never accumulates more than this one artifact.

    The artifact additionally carries a ``token_stats`` block (POS-1827): an
    estimated input-token count for the turn (stdin payload + system prompt,
    ``len//4`` heuristic) so context-optimization work can be measured turn
    over turn without an extra API call.

    Debug-only and best-effort: this must never break or block a chat turn, so
    every failure (permissions, missing dir, disk) is caught and logged as a
    single short line while the turn proceeds and streams normally. The target
    directory is derived from ``project_root`` and stays inside the project's
    ``.praxis/chat/`` tree.
    """
    try:
        debug_dir = os.path.join(project_root, ".praxis", "chat", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        _prune_debug_dir(debug_dir)
        stdin_payload = (
            build_stdin_payload(messages, work_dir=project_root)
            if isinstance(messages, list)
            else ""
        )
        system_prompt_text = system_prompt if isinstance(system_prompt, str) else ""
        stdin_tokens = _estimate_tokens(stdin_payload)
        system_prompt_tokens = _estimate_tokens(system_prompt_text)
        token_stats = {
            "stdin_chars": len(stdin_payload),
            "stdin_tokens_estimated": stdin_tokens,
            "system_prompt_chars": len(system_prompt_text),
            "system_prompt_tokens_estimated": system_prompt_tokens,
            "total_tokens_estimated": stdin_tokens + system_prompt_tokens,
        }
        artifact = {
            "session_id": session_id if isinstance(session_id, str) else None,
            "system_prompt": system_prompt,
            "messages": messages,
            "model": model,
            "token_stats": token_stats,
            "last_user_message": _last_user_message(messages),
        }
        target = os.path.join(debug_dir, DEBUG_INPUT_FILENAME)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        app.logger.info(
            "chat debug input written to %s (input ~%d tokens: stdin %d + system %d)",
            target,
            token_stats["total_tokens_estimated"],
            stdin_tokens,
            system_prompt_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — debug dump must never break a turn
        app.logger.warning("chat debug input write failed: %s", exc)


def _write_chat_debug_payload(
    project_root: str,
    *,
    messages: object,
    system_prompt: object,
) -> None:
    """Persist the two channels the model actually receives, byte-exact.

    Companion to :func:`_write_chat_debug_input`: that artifact is JSON meant
    for programmatic inspection (and carries model/session attribution), these
    two are BYTE-EXACT copies of the ``--system-prompt`` argv value and the
    ``claude`` child's stdin payload — nothing else.

    NOTE: This is a PRE-WORKER snapshot. When the session-memory path or
    compaction rewrites the payload, the worker overwrites this file with the
    ACTUAL final bytes via ``_overwrite_stdin_debug`` (``chat_runs.py``).
    The authoritative byte-exact record for session-based turns is the
    per-session ``.praxis/chat/<session_id>/last_stdin.txt``.

    No headers, no
    separators, no metadata: the user requires the artifact contain 100% of
    what is sent and nothing else. Model name and session id are deliberately
    NOT recorded here; they already live in ``last_input.json``.

    The stdin payload is built via :func:`chat_runtime.build_stdin_payload` —
    the SAME function :mod:`chat_runs` uses to build the real payload — so this
    artifact cannot drift from what was actually sent. In particular, image
    turns are written verbatim, including full base64, exactly as they were
    sent (never elided or truncated): the artifact must never lie about
    payload size or shape.

    Same best-effort contract as the JSON writer: the whole body is wrapped in
    try/except, a single warning line is logged on failure, and a chat turn is
    never blocked by it.
    """
    try:
        debug_dir = os.path.join(project_root, ".praxis", "chat", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        stdin_payload = (
            build_stdin_payload(messages, work_dir=project_root)
            if isinstance(messages, list)
            else ""
        )
        system_prompt_target = os.path.join(
            debug_dir, DEBUG_SYSTEM_PROMPT_FILENAME
        )
        # newline="" is REQUIRED: without it Python translates "\n" to "\r\n"
        # on Windows and the file stops being byte-exact. No trailing newline
        # is added — both build_transcript and build_user_stream_json (via
        # build_stdin_payload) already end exactly as the model received them.
        with open(
            system_prompt_target, "w", encoding="utf-8", newline=""
        ) as handle:
            handle.write(system_prompt if isinstance(system_prompt, str) else "")
        stdin_target = os.path.join(debug_dir, DEBUG_STDIN_FILENAME)
        with open(stdin_target, "w", encoding="utf-8", newline="") as handle:
            handle.write(stdin_payload)
        app.logger.info(
            "chat debug payload written to %s and %s",
            system_prompt_target,
            stdin_target,
        )
    except Exception as exc:  # noqa: BLE001 — debug dump must never break a turn
        app.logger.warning("chat debug payload write failed: %s", exc)


def _copy_session_debug_files(project_root: str, session_id: str) -> None:
    """Copy session debug files to the shared debug directory (POS-1856).

    Copies ``last_stdin.txt``, ``full.json``, and ``memory.json`` from the
    active session directory to ``.praxis/chat/debug/`` so developers can
    inspect the current session's state without locating the session UUID
    directory. Best-effort: missing source files are silently skipped and a
    copy failure never blocks the chat turn.
    """
    try:
        src_dir = session_dir(project_root, session_id)
        dst_dir = os.path.join(project_root, ".praxis", "chat", "debug")
        os.makedirs(dst_dir, exist_ok=True)
        for filename in SESSION_DEBUG_COPY_FILES:
            src = os.path.join(src_dir, filename)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dst_dir, filename))
    except Exception as exc:  # noqa: BLE001 — debug copy must never break a turn
        app.logger.warning("session debug file copy failed: %s", exc)


# DEFAULT_MODEL, CHAT_RATE_LIMIT_SECONDS, CHAT_SESSION_HEADER are imported from
# chat_runtime (single source of truth).

# How long a successful `claude --version` probe result stays fresh, in seconds.
# The frontend polls /health roughly every 10s; a TTL in the 30-60s range keeps
# us from spawning a process on every poll while still reflecting a newly
# installed/removed `claude` within one TTL.
CLAUDE_PROBE_TTL_HIT_SECONDS = 45

# How long a failed probe result stays fresh, in seconds. A miss should
# re-probe sooner so a transiently-invisible CLI is detected quickly (POS-1641).
CLAUDE_PROBE_TTL_MISS_SECONDS = 5

# How long to wait for `claude --version` before treating it as unavailable.
CLAUDE_PROBE_TIMEOUT_SECONDS = 5

# How long to wait for a login-shell fallback probe (e.g. `zsh -lc 'which
# claude'`) before giving up. Kept shorter than the main probe timeout to
# avoid blocking `/health` when a slow shell profile is sourced.
_LOGIN_SHELL_PROBE_TIMEOUT_SECONDS = 3

# CORS is handled by Starlette CORSMiddleware on the combined ASGI app (see
# __main__).  Flask-CORS is NOT applied here so there are no duplicate
# Access-Control-Allow-Origin headers when requests reach Flask routes through
# the WsgiToAsgi bridge.  Tests that use `app.test_client()` directly run
# without CORS — acceptable since tests never cross origins.

# ---------------------------------------------------------------------------
# Static model catalogue
# ---------------------------------------------------------------------------
# Server-defined, no subprocess. Each ``id`` must be a string that
# ``claude --model`` accepts (alias or full ID). Aliases are used here so the
# catalogue stays valid as Anthropic rolls dated model IDs forward.
MODEL_CATALOG: list[dict[str, str]] = [
    {"id": "haiku", "name": "Claude Haiku", "tier": "fast"},
    {"id": "sonnet", "name": "Claude Sonnet", "tier": "balanced"},
    {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "tier": "powerful"},
    {"id": "opus", "name": "Claude Opus (Latest)", "tier": "powerful"},
    {"id": "fable", "name": "Claude Fable", "tier": "creative"},
]

# ---------------------------------------------------------------------------
# `claude` availability probe (cached, thread-safe)
# ---------------------------------------------------------------------------
# A single Lock-guarded cache entry holds the last probe result plus the time
# it was taken. /health polling reads this; the real probe (which spawns a
# `claude --version` subprocess) only runs when the cache is empty or expired.
_probe_lock = Lock()
_probe_cache: dict[str, float | bool | str | None] = {
    "value": None,
    "timestamp": 0.0,
    # When the CLI was found via a fallback probe (not on the process PATH),
    # its resolved absolute path is cached here so subsequent calls to
    # ``_resolve_claude_binary()`` skip re-probing.
    "resolved_path": None,
}

# Well-known install locations checked when ``shutil.which`` fails to find
# ``claude`` on the process-level ``PATH``.  Checked in order; the first
# existing + executable file wins.
_KNOWN_CLAUDE_LOCATIONS: tuple[str, ...] = (
    os.path.expanduser("~/.local/bin/claude"),
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
)


def _check_executable(path: str) -> str | None:
    """Return *path* if it exists and is executable, else ``None``."""
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return None


def _probe_known_locations() -> str | None:
    """Scan the well-known install directories for ``claude``.

    Also tries ``$(brew --prefix)/bin/claude`` when *brew* is available and
    none of the hardcoded paths matched.  Returns the first executable hit or
    ``None``.
    """
    for location in _KNOWN_CLAUDE_LOCATIONS:
        found = _check_executable(location)
        if found:
            return found

    # Try brew-prefix as an extra location only when the static list misses.
    try:
        result = subprocess.run(
            ["brew", "--prefix"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            brew_path = os.path.join(result.stdout.strip(), "bin", "claude")
            found = _check_executable(brew_path)
            if found:
                return found
    except Exception:  # noqa: BLE001 — brew lookup is best-effort
        pass

    return None


def _probe_login_shell() -> str | None:
    """Attempt to locate ``claude`` via a login-shell subprocess.

    On macOS / Linux, ``~/.zshrc`` or ``~/.bashrc`` may add directories (e.g.
    ``~/.local/bin``) to ``PATH`` that are *not* inherited by a daemon started
    from a non-interactive context.  A single ``zsh -lc 'which claude'`` (or
    ``bash`` fallback) resolves this — the shell sources the user's profile,
    expanding PATH, and ``which`` prints the absolute path.

    A hard timeout prevents a slow shell profile from blocking ``/health``.
    This is a **one-time diagnostic** probe; once found the resolved path is
    cached and reused — no further shell spawns occur.
    """
    shells_to_try = []
    user_shell = os.environ.get("SHELL", "")
    # Try the user's default shell first, then the other common one.
    if "zsh" in user_shell:
        shells_to_try = ["zsh", "bash"]
    elif "bash" in user_shell:
        shells_to_try = ["bash", "zsh"]
    else:
        shells_to_try = ["zsh", "bash"]

    for shell in shells_to_try:
        try:
            result = subprocess.run(
                [shell, "-lc", "which claude"],
                capture_output=True,
                text=True,
                timeout=_LOGIN_SHELL_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode == 0:
                candidate = result.stdout.strip()
                if candidate and os.path.isabs(candidate):
                    found = _check_executable(candidate)
                    if found:
                        app.logger.info(
                            "claude found via login-shell probe (%s): %s",
                            shell,
                            found,
                        )
                        return found
        except Exception:  # noqa: BLE001 — login-shell probe is best-effort
            pass

    return None


def _resolve_claude_binary() -> str | None:
    """Return the path to the ``claude`` binary, or ``None`` if not found.

    Resolution order (POS-1641):

    1. ``CHAT_CLAUDE_BIN`` env var — explicit override, checked first.
    2. Previously-cached ``resolved_path`` from a prior fallback probe — avoids
       re-running expensive probes on every call.
    3. ``shutil.which("claude")`` — standard ``PATH`` lookup.
    4. Well-known install locations (``~/.local/bin``, ``/usr/local/bin``,
       ``/opt/homebrew/bin``, ``$(brew --prefix)/bin``).
    5. Login-shell subprocess (``zsh -lc 'which claude'`` / ``bash -lc …``) as
       a last-resort probe with a short timeout.

    When a fallback (steps 4–5) succeeds, the resolved absolute path is cached
    in ``_probe_cache["resolved_path"]`` so subsequent calls (including the
    chat-turn subprocess spawn) use it directly with no further shell-spawning
    overhead.  The cache is cleared when a miss is recorded so the next cycle
    re-probes.
    """
    # 1. Explicit env-var override.
    if CHAT_CLAUDE_BIN:
        if os.path.isfile(CHAT_CLAUDE_BIN) and os.access(CHAT_CLAUDE_BIN, os.X_OK):
            return CHAT_CLAUDE_BIN
        return None

    # 2. Previously-resolved fallback path (cached across calls).
    cached_resolved = _probe_cache.get("resolved_path")
    if isinstance(cached_resolved, str) and cached_resolved:
        if _check_executable(cached_resolved):
            return cached_resolved
        # Stale cache entry (file removed since last probe); clear it so we
        # fall through and re-probe.
        _probe_cache["resolved_path"] = None

    # 3. Standard PATH lookup.
    found = shutil.which("claude")
    if found:
        return found

    # 4. Well-known install locations.
    found = _probe_known_locations()
    if found:
        _probe_cache["resolved_path"] = found
        app.logger.info("claude found at known location: %s", found)
        return found

    # 5. Login-shell probe (last resort; ~2-3s timeout).
    found = _probe_login_shell()
    if found:
        _probe_cache["resolved_path"] = found
        return found

    return None


def _probe_claude_available() -> bool:
    """Run a fast ``claude --version`` probe; ``True`` only on a clean exit.

    Any failure mode — missing binary, non-zero exit, timeout, or any other
    exception — resolves to ``False``. This function never raises, so /health
    can never return a 500 because of it.
    """
    claude_bin = _resolve_claude_binary()
    if not claude_bin:
        return False
    try:
        result = subprocess.run(
            [claude_bin, "--version"],
            capture_output=True,
            timeout=CLAUDE_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — any probe failure → unavailable
        app.logger.warning("claude availability probe failed: %s", exc)
        return False
    return result.returncode == 0


def is_claude_available() -> bool:
    """Return cached ``claude`` availability, re-probing once past the TTL.

    The cache is guarded by ``_probe_lock`` so concurrent /health polls share a
    single probe per TTL window rather than each spawning a subprocess.

    Asymmetric TTL (POS-1641): a confirmed-available CLI uses the longer TTL
    (``CLAUDE_PROBE_TTL_HIT_SECONDS``); a miss re-probes sooner
    (``CLAUDE_PROBE_TTL_MISS_SECONDS``) so a transiently-invisible CLI is
    detected within a few seconds rather than staying cached as false for the
    full window.
    """
    now = time.time()
    with _probe_lock:
        cached_value = _probe_cache["value"]
        cached_at = _probe_cache["timestamp"] or 0.0

        if cached_value is not None:
            ttl = (
                CLAUDE_PROBE_TTL_HIT_SECONDS
                if cached_value
                else CLAUDE_PROBE_TTL_MISS_SECONDS
            )
            if (now - cached_at) < ttl:
                return bool(cached_value)

        available = _probe_claude_available()
        _probe_cache["value"] = available
        _probe_cache["timestamp"] = now
        # Clear the cached fallback path on a miss so the next probe cycle
        # re-runs the full resolution chain.
        if not available:
            _probe_cache["resolved_path"] = None
        return available


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
# Rate limiting, validation, transcript/model/argv assembly, and SSE framing
# live in chat_runtime; detached run spawning + teardown live in chat_runs.
# This file owns validation, rate limiting, the availability probe, and routing.


def _chat_session_key() -> str:
    """Rate-limit key from this request's remote IP + optional session header."""
    return chat_session_key(
        request.remote_addr, request.headers.get(CHAT_SESSION_HEADER)
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    """Connection + `claude` availability check.

    Frontend state mapping: ``200 + claude_available:true`` → ready;
    ``200 + claude_available:false`` → claude-missing; a failed request →
    not-connected (client-observed; no server state for it).
    """
    return (
        jsonify(
            {
                "status": "ok",
                "service": "praxis-local-api",
                "version": SERVICE_VERSION,
                "api_version": API_FILE_VERSION,
                "claude_available": is_claude_available(),
                "default_model": DEFAULT_MODEL,
                "mcp_available": True,
                "mcp_http": True,
            }
        ),
        200,
    )


@app.route("/api/mcp-connections", methods=["GET"])
def mcp_connections_endpoint():
    """Return MCP server connection status.

    Shells out to ``claude mcp list`` and parses the output.  Falls back to
    direct HTTP probing when the CLI is unavailable or times out.  Results
    are cached for 60 seconds.  Returns an empty server list when
    ``MCPMode`` is ``false`` in ``general.yaml``.
    """
    project_root = _resolve_cwd(request.args.get("project_root"))
    claude_bin = _resolve_claude_binary()
    result = mcp_connections.check_mcp_connections(claude_bin, project_root)
    return jsonify(result), 200


@app.route("/api/models", methods=["GET"])
def models():
    """Return the static model catalogue for the model selector."""
    return (
        jsonify({"default": DEFAULT_MODEL, "available": MODEL_CATALOG}),
        200,
    )


@app.route("/api/resolve-path", methods=["POST"])
def resolve_path():
    """Return the absolute project root after verifying a marker file.

    The browser writes a unique marker to ``.praxis/.resolve-path`` via the
    File System Access API, then POSTs ``{dirName, marker}`` here.  We verify
    the marker matches the file on disk and return ``{path}`` — the same
    contract as the Vite dev-server ``/__praxis/resolve-path`` endpoint that
    is unavailable in production builds.
    """
    body = request.get_json(silent=True) or {}
    dir_name = body.get("dirName")
    marker = body.get("marker")
    if not isinstance(dir_name, str) or not isinstance(marker, str):
        return jsonify({}), 400

    marker_path = os.path.join(CHAT_PROJECT_ROOT, ".praxis", ".resolve-path")
    try:
        with open(marker_path, encoding="utf-8") as fh:
            content = fh.read().strip()
    except OSError:
        return jsonify({}), 404

    if content != marker:
        return jsonify({}), 404

    resolved = CHAT_PROJECT_ROOT.rstrip("/\\") + os.sep
    return jsonify({"path": resolved}), 200


@app.route("/api/files", methods=["GET"])
def files():
    """List the file paths of the project named by ``?project_root=``.

    Path-addressed and tokenless (loopback-only daemon): the query carries the
    target project's absolute ``project_root`` and the daemon returns that
    project's file paths for the frontend's @-mention picker. The root must come
    from the request — this service is shared across projects and may run
    outside any of them — so it is never derived from the process cwd.

    * **404** — ``project_root`` is missing, not absolute, not an existing
      directory, or not a real PraxisOS project (no ``.praxis/``).
    * **200** — returns the project-relative POSIX file paths, sorted, capped at
      ``file_listing.MAX_FILES``.
    """
    project_root = _validate_project_root(request.args.get("project_root"))
    if project_root is None:
        return jsonify({"ok": False, "error": "project not found"}), 404

    return jsonify({"ok": True, "files": list_project_files(project_root)}), 200


@app.route("/api/git/status", methods=["GET"])
def git_status():
    """Return changed, added (staged-new), untracked, and deleted files."""
    project_root = _resolve_cwd(request.args.get("project_root"))

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if result.returncode != 0:
        return jsonify({"ok": False, "error": result.stderr.strip() or "git status failed"}), 500

    modified = []
    added = []
    untracked = []
    deleted = []
    staged = []

    # Patterns to exclude from git status (like IntelliJ IDEA)
    _IGNORED_SEGMENTS = {"__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", ".DS_Store"}
    _IGNORED_EXTENSIONS = {".pyc", ".pyo"}

    for line in result.stdout.splitlines():
        if len(line) < 3:
            continue
        status_code = line[:2]
        file_path = line[3:].rstrip("/")  # strip trailing slash from directory entries
        # Handle renamed files: "R  old -> new"
        if " -> " in file_path:
            file_path = file_path.split(" -> ")[-1]

        # Skip empty paths and ignored patterns
        if not file_path:
            continue
        path_parts = file_path.replace("\\", "/").split("/")
        if any(seg in _IGNORED_SEGMENTS for seg in path_parts):
            continue
        if any(file_path.endswith(ext) for ext in _IGNORED_EXTENSIONS):
            continue

        if status_code.strip() == "??":
            untracked.append(file_path)
        elif "D" in status_code:
            deleted.append(file_path)
        elif status_code[0] == "A":
            added.append(file_path)
        else:
            modified.append(file_path)

        # First character = staging area (index) status.
        index_status = status_code[0]
        if index_status not in (" ", "?"):
            staged.append(file_path)

    return jsonify({
        "ok": True,
        "modified": sorted(modified),
        "added": sorted(added),
        "untracked": sorted(untracked),
        "deleted": sorted(deleted),
        "staged": sorted(staged),
    }), 200


@app.route("/api/git/diff-stats", methods=["GET"])
def git_diff_stats():
    """Return aggregated file count (all uncommitted) and line-change statistics."""
    project_root = _resolve_cwd(request.args.get("project_root"))

    # Detect whether the requested path was actually valid (not silently
    # fallen back to CHAT_PROJECT_ROOT).  The frontend uses this to warn
    # the user when absoluteProjectPath is wrong.
    _requested_root = request.args.get("project_root")
    _path_valid = True
    if isinstance(_requested_root, str) and _requested_root.strip():
        _req = _requested_root.strip()
        _path_valid = os.path.isabs(_req) and os.path.isdir(_req)

    # --- File count from git status (matches /api/git/status logic) ---
    _IGNORED_SEGMENTS = {"__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", ".DS_Store"}
    _IGNORED_EXTENSIONS = {".pyc", ".pyo"}
    files_changed = 0

    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if status_result.returncode != 0:
        return jsonify({"ok": False, "error": status_result.stderr.strip() or "git status failed"}), 500

    for line in status_result.stdout.splitlines():
        if len(line) < 3:
            continue
        file_path = line[3:].rstrip("/")
        if " -> " in file_path:
            file_path = file_path.split(" -> ")[-1]
        if not file_path:
            continue
        path_parts = file_path.replace("\\", "/").split("/")
        if any(seg in _IGNORED_SEGMENTS for seg in path_parts):
            continue
        if any(file_path.endswith(ext) for ext in _IGNORED_EXTENSIONS):
            continue
        files_changed += 1

    # --- Line counts from git diff --numstat (staged + unstaged) ---
    total_insertions = 0
    total_deletions = 0

    for diff_cmd in [
        ["git", "diff", "--numstat"],
        ["git", "diff", "--cached", "--numstat"],
    ]:
        try:
            result = subprocess.run(
                diff_cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip() or "git diff failed"}), 500

        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, removed = parts[0], parts[1]
            if added == "-" or removed == "-":
                continue
            try:
                total_insertions += int(added)
                total_deletions += int(removed)
            except ValueError:
                continue

    return jsonify({
        "ok": True,
        "path_valid": _path_valid,
        "files_changed": files_changed,
        "lines_changed": total_insertions + total_deletions,
        "insertions": total_insertions,
        "deletions": total_deletions,
    }), 200


@app.route("/api/git/diff-file", methods=["GET"])
def git_diff_file():
    """Return line-level ``git diff HEAD`` hunks for one file (POS-2308).

    Query: ``project_root`` (absolute path) and ``file`` (project-relative path).
    Files absent from HEAD return ``tracked: false`` with no hunks; binary files
    return ``binary: true`` with no hunks.
    """
    project_root = _resolve_cwd(request.args.get("project_root"))
    file_arg = request.args.get("file")
    if not git_diff_ops.is_safe_relative_path(file_arg):
        return jsonify({"ok": False, "error": "invalid file path"}), 400
    file_path = file_arg.strip().replace("\\", "/")

    try:
        payload = git_diff_ops.diff_file_against_head(project_root, file_path)
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if payload.get("error"):
        return jsonify({"ok": False, "error": payload["error"]}), 500
    return jsonify({"ok": True, **payload}), 200


@app.route("/api/git/stage", methods=["POST"])
def git_stage():
    """Stage (git add) the specified files."""
    body = request.get_json(silent=True) or {}
    project_root = _resolve_cwd(body.get("project_root"))
    files = body.get("files")

    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "error": "files must be a non-empty list"}), 400

    try:
        result = subprocess.run(
            ["git", "add", "--"] + files,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            # Batch failed — likely a deleted file. Try each individually.
            if "pathspec" in result.stderr and "did not match" in result.stderr:
                skipped = []
                for f in files:
                    individual = subprocess.run(
                        ["git", "add", "--", f],
                        cwd=project_root,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if individual.returncode != 0:
                        # File not on disk — stage the deletion via git rm
                        abs_path = os.path.join(project_root, f)
                        if not os.path.exists(abs_path):
                            rm_result = subprocess.run(
                                ["git", "rm", "--cached", "--ignore-unmatch", "--", f],
                                cwd=project_root,
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            if rm_result.returncode != 0:
                                skipped.append(f)
                        else:
                            skipped.append(f)
                if len(skipped) == len(files):
                    return jsonify({"ok": False, "error": "All selected files are missing or unrecognized by git"}), 400
            else:
                return jsonify({"ok": False, "error": result.stderr.strip() or "git add failed"}), 500
        return jsonify({"ok": True}), 200
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/git/unstage", methods=["POST"])
def git_unstage():
    """Unstage (git reset HEAD) the specified files."""
    body = request.get_json(silent=True) or {}
    project_root = _resolve_cwd(body.get("project_root"))
    files = body.get("files")

    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "error": "files must be a non-empty list"}), 400

    try:
        result = subprocess.run(
            ["git", "reset", "HEAD", "--"] + files,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return jsonify({"ok": False, "error": result.stderr.strip() or "git reset failed"}), 500
        return jsonify({"ok": True}), 200
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/git/rollback", methods=["POST"])
def git_rollback():
    """Rollback (discard changes in) a single file."""
    body = request.get_json(silent=True) or {}
    project_root = _resolve_cwd(body.get("project_root"))
    file_path = body.get("file", "").strip()
    file_status = body.get("status", "").strip()

    if not file_path:
        return jsonify({"ok": False, "error": "file is required"}), 400

    if not git_diff_ops.is_safe_relative_path(file_path):
        return jsonify({"ok": False, "error": "Invalid file path"}), 400

    try:
        if file_status == "untracked":
            # Untracked files — just delete
            abs_path = os.path.join(project_root, file_path)
            if os.path.exists(abs_path):
                os.remove(abs_path)
            return jsonify({"ok": True}), 200
        else:
            # Tracked files (modified, added, deleted) — restore from HEAD
            result = subprocess.run(
                ["git", "checkout", "HEAD", "--", file_path],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return jsonify({"ok": False, "error": result.stderr.strip() or "git checkout failed"}), 500
            return jsonify({"ok": True}), 200
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/git/rollback-lines", methods=["POST"])
def git_rollback_lines():
    """Rollback a single diff hunk within a file."""
    body = request.get_json(silent=True) or {}
    project_root = _resolve_cwd(body.get("project_root"))
    file_path = body.get("file", "").strip()

    if not file_path:
        return jsonify({"ok": False, "error": "file is required"}), 400
    if not git_diff_ops.is_safe_relative_path(file_path):
        return jsonify({"ok": False, "error": "Invalid file path"}), 400

    try:
        old_start = int(body.get("old_start", 0))
        old_lines = int(body.get("old_lines", 0))
        new_start = int(body.get("new_start", 0))
        new_lines = int(body.get("new_lines", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid hunk coordinates"}), 400

    if old_start < 0 or old_lines < 0 or new_start < 0 or new_lines < 0:
        return jsonify({"ok": False, "error": "Hunk coordinates must be non-negative"}), 400

    try:
        result = git_diff_ops.rollback_hunk(
            project_root, file_path, old_start, old_lines, new_start, new_lines
        )
        status_code = 200 if result.get("ok") else 400
        return jsonify(result), status_code
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/git/last-commit", methods=["GET"])
def git_last_commit():
    """Return HEAD commit message, files, and merge/initial flags for amend support."""
    project_root = _resolve_cwd(request.args.get("project_root"))

    empty = {
        "ok": True,
        "has_commits": False,
        "message": "",
        "files": [],
        "is_merge": False,
        "is_initial": False,
    }

    try:
        # Parent count: "<sha> <parent1> <parent2> ..." — no commits => non-zero exit
        parents_result = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if parents_result.returncode != 0:
            # Unborn HEAD (no commits yet) — not an error for the UI
            return jsonify(empty), 200

        parts = parents_result.stdout.strip().split()
        parent_count = max(0, len(parts) - 1)

        message_result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if message_result.returncode != 0:
            return jsonify({"ok": False, "error": message_result.stderr.strip() or "git log failed"}), 500

        files_result = subprocess.run(
            ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        files = []
        if files_result.returncode == 0:
            for line in files_result.stdout.splitlines():
                path = line.strip()
                if path and path not in files:
                    files.append(path)

        return jsonify({
            "ok": True,
            "has_commits": True,
            "message": message_result.stdout.strip("\n"),
            "files": sorted(files),
            "is_merge": parent_count > 1,
            "is_initial": parent_count == 0,
        }), 200
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/git/commit", methods=["POST"])
def git_commit():
    """Stage selected files and commit with the given message. Supports amending
    the last commit (amend=true) to rewrite HEAD instead of creating a new commit."""
    body = request.get_json(silent=True) or {}
    project_root = _resolve_cwd(body.get("project_root"))
    files = body.get("files")
    message = body.get("message", "").strip()
    amend = body.get("amend") is True

    # files is optional — if provided, stage them first; otherwise commit what's already staged
    if not message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    try:
        if amend:
            head_check = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if head_check.returncode != 0:
                return jsonify({"ok": False, "error": "No commits to amend"}), 400

        # Stage selected files (backward compat — staging is normally done via /api/git/stage)
        if files:
            add_result = subprocess.run(
                ["git", "add", "--"] + files,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if add_result.returncode != 0:
                # If some files no longer exist (moved/deleted externally), add individually and skip missing
                if "pathspec" in add_result.stderr and "did not match" in add_result.stderr:
                    skipped = []
                    for f in files:
                        individual = subprocess.run(
                            ["git", "add", "--", f],
                            cwd=project_root,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if individual.returncode != 0:
                            # File not on disk — stage the deletion via git rm
                            abs_path = os.path.join(project_root, f)
                            if not os.path.exists(abs_path):
                                rm_result = subprocess.run(
                                    ["git", "rm", "--cached", "--ignore-unmatch", "--", f],
                                    cwd=project_root,
                                    capture_output=True,
                                    text=True,
                                    timeout=10,
                                )
                                if rm_result.returncode != 0:
                                    skipped.append(f)
                            else:
                                skipped.append(f)
                    if len(skipped) == len(files):
                        return jsonify({"ok": False, "error": "All selected files are missing or already moved"}), 400
                else:
                    return jsonify({"ok": False, "error": add_result.stderr.strip() or "git add failed"}), 500

        # Commit
        commit_cmd = ["git", "commit", "--amend", "-m", message] if amend else ["git", "commit", "-m", message]
        commit_result = subprocess.run(
            commit_cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if commit_result.returncode != 0:
            return jsonify({"ok": False, "error": commit_result.stderr.strip() or ("git commit --amend failed" if amend else "git commit failed")}), 500

        # Get the commit hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        commit_hash = hash_result.stdout.strip() if hash_result.returncode == 0 else ""

        return jsonify({"ok": True, "commit_hash": commit_hash}), 200

    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/git/commits", methods=["GET"])
def git_commits():
    """Return a paginated slice of commit history for the Commits History Panel (POS-2337).

    Query: ``project_root``, ``offset``/``limit`` (pagination), and an optional
    ``search`` that matches commits whose hash or author name starts with it
    (case-insensitive).
    """
    project_root = _resolve_cwd(request.args.get("project_root"))
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    search = request.args.get("search", "")

    try:
        payload = git_history_ops.list_commits(project_root, offset, limit, search)
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if "error" in payload:
        return jsonify({"ok": False, "error": payload["error"]}), 500
    return jsonify({"ok": True, **payload}), 200


@app.route("/api/git/branches", methods=["GET"])
def git_branches():
    """Return local/remote branch listings and the current branch (POS-2337)."""
    project_root = _resolve_cwd(request.args.get("project_root"))

    try:
        payload = git_history_ops.list_branches(project_root)
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if "error" in payload:
        return jsonify({"ok": False, "error": payload["error"]}), 500
    return jsonify({"ok": True, **payload}), 200


@app.route("/api/blueprint/heatmap", methods=["GET"])
def blueprint_heatmap():
    """Return per-route activity metrics for the Blueprint heatmap (POS-2348).

    Query: ``project_root``, ``period`` (day|week|month, default week).
    """
    project_root = _resolve_cwd(request.args.get("project_root"))
    period = request.args.get("period", "week")
    if period not in ("day", "week", "month"):
        period = "week"

    try:
        data = route_activity.compute_route_activity(project_root, period)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, **data}), 200


@app.route("/api/git/checkout", methods=["POST"])
def git_checkout():
    """Check out a branch, tag, or commit hash (POS-2337)."""
    body = request.get_json(silent=True) or {}
    project_root = _resolve_cwd(body.get("project_root"))
    ref = body.get("ref")

    try:
        result = git_history_ops.checkout(project_root, ref)
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if "error" in result:
        status_code = 400 if result["error"] == "invalid ref" else 500
        return jsonify({"ok": False, "error": result["error"]}), status_code
    return jsonify({"ok": True}), 200


@app.route("/api/git/file-at-commit", methods=["GET"])
def git_file_at_commit():
    """Return the content of a file as it existed at a given commit (POS-2337)."""
    project_root = _resolve_cwd(request.args.get("project_root"))
    commit_hash = request.args.get("hash")
    file_arg = request.args.get("file")

    try:
        payload = git_history_ops.file_at_commit(project_root, commit_hash, file_arg)
    except (OSError, subprocess.SubprocessError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if "error" in payload:
        error = payload["error"]
        if error.startswith("invalid "):
            status_code = 400
        elif error == "file not found in commit":
            status_code = 404
        else:
            status_code = 500
        return jsonify({"ok": False, "error": error}), status_code
    return jsonify({"ok": True, **payload}), 200


@app.route("/api/mcp-servers", methods=["GET"])
def mcp_servers_status():
    """Return all globally configured MCPs from ~/.claude.json with auth status.

    Never returns token values. 'fix_instructions' is populated when auth is absent
    so the UI can surface actionable text directly to the user.
    """
    import pathlib
    MISSING_AUTH_INSTRUCTIONS = {
        "http": (
            'Add to ~/.claude.json under mcpServers.{name}:\n'
            '  "headers": {{"Authorization": "Bearer YOUR_TOKEN"}}'
        ),
        "streamable-http": (
            'Add to ~/.claude.json under mcpServers.{name}:\n'
            '  "headers": {{"Authorization": "Bearer YOUR_TOKEN"}}'
        ),
    }
    claude_json_path = pathlib.Path.home() / ".claude.json"
    try:
        with open(claude_json_path) as f:
            data = json.load(f)
        servers = data.get("mcpServers", {})
    except Exception:
        servers = {}

    result = {}
    for name, cfg in servers.items():
        has_auth = bool(
            isinstance(cfg.get("headers"), dict)
            and cfg["headers"].get("Authorization")
        )
        server_type = cfg.get("type", "http")
        result[name] = {
            "type": server_type,
            "url": cfg.get("url"),
            "has_auth": has_auth,
            "managed_by_praxis": name == "praxis",
            "fix_instructions": (
                None if has_auth or name == "praxis"
                else MISSING_AUTH_INSTRUCTIONS.get(
                    server_type,
                    "Add auth headers to ~/.claude.json"
                ).format(name=name)
            ),
        }
    return jsonify({"servers": result})


@app.route("/api/console/state", methods=["GET"])
def console_state():
    """Return a snapshot of the shared terminal session for ``project_root``.

    Query params: ``project_root``.
    Response: the dict from :func:`console_pty.snapshot`, including ``ready``,
    ``alive``, ``commands`` (ring of recent command records) and ``history``
    (recent command strings).
    """
    cwd = _resolve_cwd(request.args.get("project_root"))
    return jsonify(console_pty.snapshot(cwd)), 200


@app.route("/api/console/run", methods=["POST"])
def console_run():
    """Run a command in the shared terminal session for ``project_root``.

    Body: ``{"project_root": str, "command": str}``.
    Response: ``{"ok": true, "command": {...}}`` (200); ``409`` if a command is
    already running; ``400`` if ``command`` is missing/blank; ``503`` if PTY
    support is unavailable on this platform; ``500`` for any other failure.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))
    command = body.get("command", "")

    result = console_pty.run_command(cwd, command)
    if result.get("ok"):
        return jsonify(result), 200
    if result.get("busy"):
        return jsonify(result), 409
    if result.get("error") == "command is required":
        return jsonify(result), 400
    if result.get("supported") is False:
        return jsonify(result), 503
    return jsonify(result), 500


@app.route("/api/console/interrupt", methods=["POST"])
def console_interrupt():
    """Send Ctrl+C to the shared terminal session for ``project_root``.

    Body: ``{"project_root": str}``.
    Response: ``{"ok": true}`` (200); ``503`` if PTY support is unavailable;
    ``500`` for any other failure.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))

    result = console_pty.interrupt(cwd)
    if result.get("ok"):
        return jsonify(result), 200
    if result.get("supported") is False:
        return jsonify(result), 503
    return jsonify(result), 500


@app.route("/api/console/stdin", methods=["POST"])
def console_stdin():
    """Write stdin text to the running interactive process in the shared terminal.

    Body: ``{"project_root": str, "text": str}``.
    Response: ``{"ok": true}`` (200); ``503`` if PTY support is unavailable;
    ``500`` for any other failure.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))
    text = body.get("text", "")

    result = console_pty.write_stdin(cwd, text)
    if result.get("ok"):
        return jsonify(result), 200
    if result.get("supported") is False:
        return jsonify(result), 503
    return jsonify(result), 500


@app.route("/api/console/resize", methods=["POST"])
def console_resize():
    """Resize the shared terminal's PTY to match the frontend terminal dimensions.

    Body: ``{"project_root": str, "cols": int, "rows": int}``.
    Response: ``{"ok": true}`` (200); ``503`` if PTY support is unavailable;
    ``500`` for any other failure.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))
    cols = body.get("cols", 120)
    rows = body.get("rows", 40)
    if not isinstance(cols, int) or not isinstance(rows, int):
        return jsonify({"ok": False, "error": "cols and rows must be integers"}), 400
    cols = max(1, min(cols, 500))
    rows = max(1, min(rows, 200))

    result = console_pty.resize(cwd, cols, rows)
    if result.get("ok"):
        return jsonify(result), 200
    if result.get("supported") is False:
        return jsonify(result), 503
    return jsonify(result), 500


@app.route("/api/console/reset", methods=["POST"])
def console_reset():
    """Close and recreate the shared terminal session for ``project_root``.

    Body: ``{"project_root": str}``.
    Response: a fresh :func:`console_pty.snapshot` (200); ``503`` if PTY
    support is unavailable; ``500`` for any other failure.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))

    result = console_pty.reset(cwd)
    if result.get("ok"):
        return jsonify(result), 200
    if result.get("supported") is False:
        return jsonify(result), 503
    return jsonify(result), 500


@app.route("/api/console/complete", methods=["POST"])
def console_complete():
    """Return tab-completion candidates for the current command line.

    Body: ``{"project_root": str, "line": str}``.
    Response: ``{"ok": true, "completions": [...], "prefix": str, "type": str}``
    (200); ``503`` if PTY support is unavailable; ``500`` for any other failure.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))
    line = body.get("line", "")

    result = console_pty.complete(cwd, line)
    if result.get("ok"):
        return jsonify(result), 200
    if "supported" in result and not result.get("supported", True):
        return jsonify(result), 503
    return jsonify(result), 500


@app.route("/api/console/info", methods=["GET"])
def console_info():
    """Return shell type and live working directory for the terminal toolbar."""
    cwd = _resolve_cwd(request.args.get("project_root"))
    return jsonify(console_pty.get_session_info(cwd)), 200


@app.route("/api/chat-console/prepare", methods=["POST"])
def chat_console_prepare():
    """Store the system prompt for a Claude Code chat session (POS-2219).

    The prompt (MCP codex + assignee) can be tens of KB — too big for a
    WebSocket query string — so the frontend POSTs it here first and the
    PTY spawn passes it via ``--append-system-prompt-file``.
    Body: ``{project_root, session_id, system_prompt}``.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"ok": False, "error": "session_id is required"}), 400
    session_id = sanitize_session_id(session_id)
    prompt = body.get("system_prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    path = save_claude_code_system_prompt(cwd, session_id, prompt)
    return jsonify({"ok": True, "path": path}), 200


@app.route("/api/chat-console/session-id", methods=["GET"])
def chat_console_session_id():
    """Return the persisted Claude Code CLI session id for this chat session."""
    cwd = _resolve_cwd(request.args.get("project_root"))
    session_id = request.args.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"ok": False, "error": "session_id is required"}), 400
    session_id = sanitize_session_id(session_id)
    return jsonify(
        {"ok": True, "claude_session_id": load_claude_code_session_id(cwd, session_id)}
    ), 200


@app.route("/api/chat-console/status", methods=["GET"])
def chat_console_status():
    """Return whether a Claude Code PTY is active/resumable for this session."""
    cwd = _resolve_cwd(request.args.get("project_root"))
    session_id = request.args.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"ok": False, "error": "session_id is required"}), 400
    session_id = sanitize_session_id(session_id)
    status = console_pty.chat_pty_status(cwd, session_id)
    claude_sid = load_claude_code_session_id(cwd, session_id)
    fork_source = load_fork_source_claude_sid(cwd, session_id)
    return jsonify(
        {
            "ok": True,
            "supported": console_pty.is_chat_supported(),
            "active": status["active"],
            "resumable": (not status["active"]) and bool(claude_sid),
            "forkable": (not status["active"]) and bool(fork_source),
            "exit_code": status["exit_code"],
            "claude_session_id": claude_sid,
        }
    ), 200


@app.route("/api/chat-console/session", methods=["DELETE"])
def chat_console_session():
    """Destroy the Claude Code PTY for this chat session, if any."""
    cwd = _resolve_cwd(request.args.get("project_root"))
    session_id = request.args.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return jsonify({"ok": False, "error": "session_id is required"}), 400
    session_id = sanitize_session_id(session_id)
    console_pty.destroy_chat_pty(cwd, session_id)
    return jsonify({"ok": True}), 200


@app.route("/api/chat-console/fork", methods=["POST"])
def chat_console_fork():
    """Set up a Claude Code session fork for a duplicated chat session (POS-2240).

    Body: ``{project_root, source_session_id, new_session_id}``.
    Writes a ``fork_source_claude_sid.txt`` marker in the *new* session
    directory so the WebSocket handler forks on first connect.
    """
    body = request.get_json(silent=True) or {}
    cwd = _resolve_cwd(body.get("project_root"))
    source_sid = body.get("source_session_id")
    new_sid = body.get("new_session_id")
    if not isinstance(source_sid, str) or not source_sid.strip():
        return jsonify({"ok": False, "error": "source_session_id is required"}), 400
    if not isinstance(new_sid, str) or not new_sid.strip():
        return jsonify({"ok": False, "error": "new_session_id is required"}), 400
    source_sid = sanitize_session_id(source_sid)
    new_sid = sanitize_session_id(new_sid)

    source_claude_sid = load_claude_code_session_id(cwd, source_sid)
    if not source_claude_sid:
        return jsonify({"ok": True, "forked": False}), 200

    save_fork_source_claude_sid(cwd, new_sid, source_claude_sid)
    return jsonify({"ok": True, "forked": True}), 200


@app.route("/api/chat-console/list", methods=["GET"])
def chat_console_list():
    """List every Claude Code PTY session registered for ``project_root``."""
    cwd = _resolve_cwd(request.args.get("project_root"))
    return jsonify(
        {
            "ok": True,
            "supported": console_pty.is_chat_supported(),
            "sessions": console_pty.list_chat_ptys(cwd),
        }
    ), 200


@app.route("/api/chat-console/mark-idle-seen", methods=["POST"])
def chat_console_mark_idle_seen():
    """Create the idle_seen marker for a Claude Code PTY session (POS-2268).

    Writes through the ChatPtySession when one is alive; falls back to writing
    directly to the session directory so the marker survives even when the PTY
    has been reaped or the server has restarted (POS-2272).
    """
    try:
        body = request.get_json(silent=True) or {}
        cwd = _resolve_cwd(body.get("project_root"))
        sid = body.get("session_id")
        if not isinstance(sid, str) or not sid.strip():
            return jsonify({"ok": False, "error": "session_id is required"}), 400
        sid = sanitize_session_id(sid)
        session = console_pty.get_chat_pty(cwd, sid)
        if session is not None:
            session.mark_idle_seen()
        else:
            # No live PTY — write the marker file directly (POS-2272).
            sess_dir = session_dir(cwd, sid)
            os.makedirs(sess_dir, exist_ok=True)
            marker_path = os.path.join(sess_dir, "idle_seen")
            with open(marker_path, "w"):
                pass
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/chat-console/agent-busy-tasks", methods=["GET"])
def chat_console_agent_busy_tasks():
    """Return task IDs where the Claude Code agent is still working (POS-2284).

    A task is "agent busy" when its linked chat session has ``mode:
    'claude-code'``, a persisted ``claude_code_session_id.txt`` (the CLI
    was actually started), **no** ``idle`` marker file, **and** a live
    PTY process in memory.

    The live-PTY check is critical: without it, sessions left behind
    after an unclean server shutdown (no ``idle`` file, no running
    process) would be falsely reported as "busy" indefinitely, showing
    the dashed spinning border on the task card even though no agent is
    working.  A session with no ``idle`` file *and* no live PTY is
    treated as idle — the marker was simply lost.
    """
    cwd = _resolve_cwd(request.args.get("project_root"))
    sessions_root = os.path.join(cwd, ".praxis", "chat", "sessions")
    busy_task_ids: list[int] = []
    try:
        if os.path.isdir(sessions_root):
            for entry in os.scandir(sessions_root):
                if not entry.is_dir():
                    continue
                meta_path = os.path.join(entry.path, "full.json")
                if not os.path.isfile(meta_path):
                    continue
                idle_path = os.path.join(entry.path, "idle")
                if os.path.exists(idle_path):
                    continue  # agent is idle — not busy
                session_id_path = os.path.join(entry.path, "claude_code_session_id.txt")
                if not os.path.isfile(session_id_path):
                    continue  # CLI was never started for this session
                # No idle file — but is anything actually running?
                # Check for a live PTY.  If the PTY is dead (server
                # restarted, process crashed), the agent is done and the
                # missing idle file is just a lost marker.
                live_pty = console_pty.get_chat_pty(cwd, entry.name)
                if live_pty is None or not live_pty.is_alive():
                    # No running process → agent is not actually busy.
                    # Heal the missing marker so subsequent polls skip
                    # the is_alive() check and go straight to the
                    # os.path.exists(idle_path) fast path above.
                    try:
                        with open(idle_path, "w"):
                            pass
                    except Exception:
                        pass
                    continue
                try:
                    import json as _json_inner
                    with open(meta_path, encoding="utf-8") as fh:
                        meta = _json_inner.load(fh)
                    if meta.get("mode") != "claude-code":
                        continue
                    task = meta.get("task")
                    if isinstance(task, dict):
                        tid = task.get("id")
                        if isinstance(tid, int):
                            busy_task_ids.append(tid)
                except Exception:
                    continue
    except Exception:
        pass
    return jsonify({"ok": True, "task_ids": busy_task_ids}), 200


@app.route("/api/chat-console/touch", methods=["POST"])
def chat_console_touch():
    """Update the UI-visibility heartbeat for a Claude Code PTY session."""
    try:
        body = request.get_json(silent=True) or {}
        cwd = _resolve_cwd(body.get("project_root"))
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"ok": False, "error": "session_id is required"}), 400
        session_id = sanitize_session_id(session_id)
        console_pty.touch_chat_pty(cwd, session_id)
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200


@app.route("/api/tasks", methods=["POST"])
def create_task():
    """Create a single task in the project named by the request's ``project_root``.

    Path-addressed and tokenless (loopback-only daemon): the body carries the
    target project's absolute ``project_root`` plus the task fields, and the
    daemon writes the same ``.praxis/tasks/new/{id}.json`` the browser UI would.

    * **400** — body is not a JSON object, or a required task field
      (``title`` / ``why_we_need_this`` / ``acceptance_criteria``) is
      missing or blank (``field`` names the offender).
    * **404** — ``project_root`` is missing, not absolute, not an existing
      directory, or not a real PraxisOS project (no ``.praxis/``).
    * **201** — created; returns the new ``id`` and project-relative ``path``.

    The id is allocated under an ``fcntl`` lock with a filesystem-max fallback
    (see :mod:`task_ops`), so concurrent creates never collide or reuse a
    number. ``/health`` and the loopback bind are unchanged; no auth header.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "request body must be a JSON object"}), 400

    project_root = _validate_project_root(body.get("project_root"))
    if project_root is None:
        return jsonify({"ok": False, "error": "project not found"}), 404

    try:
        result = task_ops.create_task(project_root, body)
    except task_ops.ValidationError as exc:
        return jsonify({"ok": False, "error": str(exc), "field": exc.field}), 400

    return jsonify({"ok": True, "id": result["id"], "path": result["path"]}), 201


@app.route("/api/chat", methods=["POST"])
def chat():
    """Stream a single ``claude`` turn back to the client as SSE.

    Pre-stream checks return plain JSON (never an in-stream event) so the
    client can distinguish a 4xx from a streamed ``error``:

    * **400** — body not an object; ``messages`` missing/empty/malformed; a
      role outside ``{user, assistant}``; ``system_prompt`` missing/empty; the
      last message not role ``user``.
    * **429** — per-session cooldown (``Retry-After`` + ``retry_after``).
    * **503** — ``claude`` not available (reuses the cached probe).

    On success a detached **run** is started (owned by the helper, not this
    HTTP connection) and a ``text/event-stream`` response tails it. The first
    SSE frame is a ``run`` event carrying the ``run_id`` so a later page reload
    can reattach (Slice 2); subsequent frames replay the run's events. A client
    disconnect stops only this tail generator — the run keeps generating.
    """
    session_key = _chat_session_key()

    # Rate limit first (fail-open: never block a turn if the limiter itself errors).
    try:
        is_limited, retry_after = check_chat_rate_limit(session_key)
        if is_limited:
            response = jsonify(
                {
                    "error": f"Rate limit exceeded. Please wait {retry_after} seconds.",
                    "retry_after": retry_after,
                }
            )
            response.headers["Retry-After"] = str(retry_after)
            return response, 429
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        app.logger.warning("chat rate limiter check failed: %s", exc)

    data = request.get_json(silent=True)
    error = validate_chat_body(data)
    if error is not None:
        return jsonify({"error": error}), 400

    if not is_claude_available():
        return jsonify({"error": "claude CLI is not available"}), 503

    assert isinstance(data, dict)
    messages = data["messages"]
    system_prompt = data["system_prompt"]
    model = resolve_model(data.get("model"))
    cwd = _resolve_cwd(data.get("project_root"))
    current_date = data.get("current_date", "") if isinstance(data.get("current_date"), str) else ""

    # Server-side safety net: guarantee the requested context-router / PRD reach
    # the AI even if the frontend assembled an incomplete system prompt (POS-1596).
    # No-op when the frontend already included them.
    system_prompt = _inject_missing_context(system_prompt, cwd, data.get("context_flags"))

    # Persist the last input sent to the AI for offline inspection. Best-effort
    # and isolated: a failure here is logged but never blocks the turn. Writes the
    # FINAL (post-injection) system prompt so the artifact mirrors what Claude saw.
    _write_chat_debug_input(
        cwd,
        messages=messages,
        system_prompt=system_prompt,
        model=model,
        session_id=data.get("session_id"),
    )
    _write_chat_debug_payload(cwd, messages=messages, system_prompt=system_prompt)
    if data.get("session_id"):
        _copy_session_debug_files(cwd, data["session_id"])

    try:
        mark_chat_request(session_key)
    except Exception as exc:  # noqa: BLE001 — fail-open
        app.logger.warning("chat rate limiter update failed: %s", exc)

    extra_mcps = _load_authed_external_mcp_servers()

    run_id = RUNS.start_run(
        messages,
        system_prompt,
        model,
        cwd,
        claude_binary=_resolve_claude_binary(),
        mcp_port=LOCAL_API_PORT,
        extra_mcp_servers=extra_mcps or None,
        session_id=data.get("session_id"),
        current_date=current_date,
    )

    def _tail_run() -> Iterator[str]:
        """Frame the run id, then each buffered run event, as SSE.

        A client disconnect raises ``GeneratorExit`` here, which only stops this
        tail — the detached run/worker is untouched and keeps generating.
        """
        yield sse_frame("run", {"run_id": run_id})
        for event_name, data in RUNS.tail(run_id, 0):
            yield sse_frame(event_name, data)

    return Response(
        _tail_run(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _parse_from_index(raw: object) -> int:
    """Coerce a ``?from=`` query value to a non-negative int (default 0).

    Tolerant by design: a missing, non-integer, or negative value all resolve
    to ``0`` so a malformed reattach query replays the run from the start rather
    than erroring.
    """
    try:
        index = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, index)


@app.route("/api/chat/runs/<run_id>", methods=["GET"])
def reattach_run(run_id: str):
    """Reattach to a detached run and replay its events from ``?from=`` as SSE.

    Reads an optional ``?from=<int>`` (default 0; negatives clamp to 0,
    non-integers tolerated → 0). An unknown/expired run is reported as **404
    JSON** (not an in-stream ``error`` frame) so the client can distinguish an
    unrecoverable run (helper restarted, or run aged out of retention) from a
    mid-stream error — we probe ``get_status`` eagerly before opening the
    stream. A client disconnect stops only this tail; the detached run/worker is
    untouched and keeps generating.
    """
    if RUNS.get_status(run_id) is None:
        return jsonify({"error": "run not found or expired"}), 404

    from_index = _parse_from_index(request.args.get("from"))

    def _tail_run() -> Iterator[str]:
        try:
            for event_name, data in RUNS.tail(run_id, from_index):
                yield sse_frame(event_name, data)
        except RunNotFoundError:
            # Raced with retention pruning after the status probe; nothing left
            # to replay. End the stream cleanly (the 404 path covers the common
            # unknown-id case before streaming starts).
            return

    return Response(
        _tail_run(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/chat/runs/<run_id>/stop", methods=["POST"])
def stop_run(run_id: str):
    """Stop a detached run. ``200`` JSON on success, ``404`` if unknown/expired.

    Used by Stop after a reattach (the local ``AbortController`` only tears down
    the fetch) and for cross-session stop.
    """
    try:
        RUNS.stop(run_id)
    except RunNotFoundError:
        return jsonify({"error": "run not found or expired"}), 404
    return jsonify({"status": "stopping"}), 200


@app.route("/api/chat/runs/<run_id>/queue", methods=["POST"])
def append_to_run_queue(run_id: str):
    """Append a message to the run's pending queue."""
    if RUNS.get_status(run_id) is None:
        return jsonify({"error": "run not found or expired"}), 404
    body = request.get_json(silent=True) or {}
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "missing message field"}), 400
    try:
        queue = QUEUE.append(run_id, message)
    except KeyError:
        return jsonify({"error": "run not found or expired"}), 404
    return jsonify({"queue": queue, "length": len(queue)}), 200


@app.route("/api/chat/runs/<run_id>/queue", methods=["GET"])
def get_run_queue(run_id: str):
    """Get the current pending queue for a run."""
    if RUNS.get_status(run_id) is None:
        return jsonify({"error": "run not found or expired"}), 404
    try:
        queue = QUEUE.get(run_id)
    except KeyError:
        return jsonify({"error": "run not found or expired"}), 404
    return jsonify({"queue": queue, "length": len(queue)}), 200


@app.route("/api/chat/runs/<run_id>/queue", methods=["DELETE"])
def clear_run_queue(run_id: str):
    """Clear the pending queue for a run."""
    if RUNS.get_status(run_id) is None:
        return jsonify({"error": "run not found or expired"}), 404
    try:
        QUEUE.clear(run_id)
    except KeyError:
        return jsonify({"error": "run not found or expired"}), 404
    return jsonify({"status": "cleared"}), 200


@app.route("/api/chat/runs/<run_id>/queue/<int:index>", methods=["DELETE"])
def remove_from_run_queue(run_id: str, index: int):
    """Remove a single message from the run's pending queue by index."""
    if RUNS.get_status(run_id) is None:
        return jsonify({"error": "run not found or expired"}), 404
    try:
        queue = QUEUE.remove(run_id, index)
    except KeyError:
        return jsonify({"error": "run not found or expired"}), 404
    except IndexError:
        return jsonify({"error": "index out of range"}), 400
    return jsonify({"queue": queue, "length": len(queue)}), 200


@app.route("/api/chat/runs/<run_id>/queue/drain", methods=["POST"])
def drain_run_queue(run_id: str):
    """Drain the pending queue (return + clear)."""
    if RUNS.get_status(run_id) is None:
        return jsonify({"error": "run not found or expired"}), 404
    try:
        drained = QUEUE.drain(run_id)
    except KeyError:
        return jsonify({"error": "run not found or expired"}), 404
    return jsonify({"drained": drained, "length": len(drained)}), 200


@app.route("/api/chat/runs", methods=["GET"])
def list_runs():
    """Return the ids of currently-running detached runs for reconciliation."""
    return jsonify({"active": RUNS.active_run_ids()}), 200


@app.route("/api/restart", methods=["POST"])
def restart_api():
    """Trigger a graceful restart of the API subprocess.

    Returns 200 immediately, then terminates the process after a short delay so
    the wrapper (praxis_wrapper.py) spawns a fresh instance.  When running
    without the wrapper the process simply exits.
    """
    import threading

    def _delayed_exit():
        time.sleep(0.3)
        try:
            RUNS.shutdown_all()
        except Exception:
            pass
        os._exit(42)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return jsonify({"status": "restarting"}), 200


def _write_hooks_debug_log(lines: list[str]) -> None:
    """Append timestamped debug info to ``.praxis/api/debug_log_hooks.txt``."""
    import datetime
    debug_path = os.path.join(CHAT_PROJECT_ROOT, ".praxis", "api", "debug_log_hooks.txt")
    try:
        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(debug_path, "w", encoding="utf-8") as fh:
            fh.write(f"=== run-enable-hooks {stamp} ===\n")
            for line in lines:
                fh.write(line + "\n")
            fh.write("\n")
    except Exception as exc:
        app.logger.warning("hooks debug log write failed: %s", exc)


@app.route("/api/run-enable-hooks", methods=["POST"])
def run_enable_hooks():
    """Execute the enable-hooks.sh script to set hook permissions.

    Runs ``<project_root>/.praxis/scripts/enable-hooks.sh`` which makes all
    ``.git/hooks/*`` files executable and sets ``core.hooksPath``.

    The target project is identified by ``project_root`` in the JSON body
    (sourced from ``general.yaml``'s ``absoluteProjectPath`` on the frontend).
    Falls back to ``CHAT_PROJECT_ROOT`` when omitted.
    """
    debug_lines: list[str] = []

    # --- resolve target project root from request body ---
    body = request.get_json(silent=True) or {}
    requested_root = body.get("project_root")
    project_root = _validate_project_root(requested_root)
    if project_root is None:
        # fallback: accept via _resolve_cwd (allows non-.praxis dirs)
        project_root = _resolve_cwd(requested_root)

    script_path = os.path.join(
        project_root, ".praxis", "scripts", "enable-hooks.sh"
    )
    debug_lines.append(f"requested_project_root: {requested_root}")
    debug_lines.append(f"resolved_project_root: {project_root}")
    debug_lines.append(f"script_path: {script_path}")
    debug_lines.append(f"script_exists: {os.path.isfile(script_path)}")

    # List what's in .praxis/scripts/
    scripts_dir = os.path.join(project_root, ".praxis", "scripts")
    if os.path.isdir(scripts_dir):
        debug_lines.append(f"scripts_dir contents: {os.listdir(scripts_dir)}")
    else:
        debug_lines.append(f"scripts_dir MISSING: {scripts_dir}")

    # Show script content for verification
    if os.path.isfile(script_path):
        try:
            with open(script_path, encoding="utf-8") as fh:
                debug_lines.append(f"script_content:\n{fh.read()}")
        except Exception as exc:
            debug_lines.append(f"script_read_error: {exc}")

    # Check .git/hooks state before running
    git_dir = os.path.join(project_root, ".git")
    hooks_dir = os.path.join(git_dir, "hooks")
    if os.path.isdir(hooks_dir):
        hook_files = os.listdir(hooks_dir)
        debug_lines.append(f"hooks_dir contents BEFORE: {hook_files}")
        for hf in sorted(hook_files):
            hp = os.path.join(hooks_dir, hf)
            if os.path.isfile(hp):
                debug_lines.append(f"  {hf}: executable={os.access(hp, os.X_OK)}, size={os.path.getsize(hp)}")
    else:
        debug_lines.append(f"hooks_dir MISSING: {hooks_dir}")

    # Check current core.hooksPath
    try:
        hp_result = subprocess.run(
            ["git", "-C", project_root, "config", "--local", "core.hooksPath"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        debug_lines.append(f"core.hooksPath BEFORE: '{hp_result.stdout.strip()}' (rc={hp_result.returncode})")
    except Exception as exc:
        debug_lines.append(f"core.hooksPath check error: {exc}")

    if not os.path.isfile(script_path):
        debug_lines.append("RESULT: 404 — script not found")
        _write_hooks_debug_log(debug_lines)
        return jsonify({"error": "enable-hooks.sh not found"}), 404

    try:
        result = subprocess.run(
            ["sh", script_path],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        debug_lines.append(f"returncode: {result.returncode}")
        debug_lines.append(f"stdout: {result.stdout}")
        debug_lines.append(f"stderr: {result.stderr}")

        # Check hooks state AFTER running
        if os.path.isdir(hooks_dir):
            hook_files = os.listdir(hooks_dir)
            debug_lines.append(f"hooks_dir contents AFTER: {hook_files}")
            for hf in sorted(hook_files):
                hp = os.path.join(hooks_dir, hf)
                if os.path.isfile(hp):
                    debug_lines.append(f"  {hf}: executable={os.access(hp, os.X_OK)}, size={os.path.getsize(hp)}")

        # Check core.hooksPath AFTER
        try:
            hp_result2 = subprocess.run(
                ["git", "-C", project_root, "config", "--local", "core.hooksPath"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            debug_lines.append(f"core.hooksPath AFTER: '{hp_result2.stdout.strip()}' (rc={hp_result2.returncode})")
        except Exception as exc:
            debug_lines.append(f"core.hooksPath check error AFTER: {exc}")

        _write_hooks_debug_log(debug_lines)

        if result.returncode == 0:
            return jsonify({"status": "ok", "output": result.stdout.strip()}), 200
        return jsonify({
            "status": "error",
            "output": result.stdout.strip(),
            "error": result.stderr.strip(),
        }), 500
    except subprocess.TimeoutExpired:
        debug_lines.append("RESULT: 504 — timeout")
        _write_hooks_debug_log(debug_lines)
        return jsonify({"error": "Script execution timed out"}), 504
    except Exception as e:
        debug_lines.append(f"RESULT: 500 — exception: {e}")
        _write_hooks_debug_log(debug_lines)
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def stop_api():
    """Shut down the API server and its wrapper process.

    Returns 200 immediately, then terminates. The wrapper sees a clean exit
    (code 0) and stops instead of restarting.
    """
    import threading

    def _delayed_stop():
        time.sleep(0.3)
        try:
            RUNS.shutdown_all()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_delayed_stop, daemon=True).start()
    return jsonify({"status": "stopping"}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # POS-1995: the rotating praxis.log handler must be live before ANY output —
    # including the dependency-error prints below and uvicorn's startup banner.
    praxis_logging.configure_root_logging()
    praxis_logging.redirect_stdio()

    # ``LOCAL_API_PORT`` is the current name; the legacy ``CHAT_API_PORT`` is
    # deprecated but still honoured as a fallback so an existing override keeps
    # working through this rename. Default port 7865 is unchanged.
    port = int(
        os.environ.get("LOCAL_API_PORT") or os.environ.get("CHAT_API_PORT") or 7865
    )
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    try:
        import uvicorn
        from asgiref.wsgi import WsgiToAsgi  # noqa: F811
        from starlette.applications import Starlette  # noqa: F811
        from starlette.middleware.cors import CORSMiddleware  # noqa: F811
        from starlette.routing import Mount, Route, WebSocketRoute  # noqa: F811
        from mcp_tools import create_mcp_server  # noqa: F811
    except ImportError as exc:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        req_path = os.path.join(script_dir, "requirements.txt")
        print("ERROR: Missing or incompatible dependencies.")
        print(f"  {type(exc).__name__}: {exc}")
        print()
        print("Install them with:")
        print(f"  pip3 install -r {req_path}")
        print()
        print("If the module exists but the import still fails, the installed")
        print("version is likely incompatible — recreate the venv:")
        print(f"  rm -rf {os.path.join(script_dir, '.venv')} && bash {os.path.join(script_dir, 'start.sh')}")
        raise SystemExit(1)

    from contextlib import asynccontextmanager
    import asyncio
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import (
        JSONResponse as StarletteJSONResponse,
        StreamingResponse as StarletteStreamingResponse,
    )

    # -- Async SSE handlers (bypass WsgiToAsgi executor threads) -----------
    # CORS is handled uniformly by Starlette CORSMiddleware (see below).
    # The handlers no longer set Access-Control-* headers manually.

    _TERMINAL_SSE_EVENTS = frozenset({"done", "error"})

    async def _async_tail_sse(run_id: str, from_index: int):
        """Poll run events with asyncio.sleep instead of Condition.wait."""
        run = RUNS._require(run_id)
        index = max(0, from_index)
        while True:
            with run.cond:
                snapshot_end = len(run.events)
                finished = run.is_finished
            while index < snapshot_end:
                event_name, payload = run.events[index]
                index += 1
                yield sse_frame(event_name, payload)
                if event_name in _TERMINAL_SSE_EVENTS:
                    return
                await asyncio.sleep(0)
            if finished:
                return
            await asyncio.sleep(0.03)

    async def _async_chat_handler(request: StarletteRequest):
        client_ip = request.client.host if request.client else "unknown"
        session_header = request.headers.get(CHAT_SESSION_HEADER)
        session_key = chat_session_key(client_ip, session_header)

        try:
            is_limited, retry_after = check_chat_rate_limit(session_key)
            if is_limited:
                return StarletteJSONResponse(
                    {
                        "error": f"Rate limit exceeded. Please wait {retry_after} seconds.",
                        "retry_after": retry_after,
                    },
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
        except Exception:
            pass

        try:
            body_bytes = await request.body()
            if len(body_bytes) > app.config["MAX_CONTENT_LENGTH"]:
                return StarletteJSONResponse(
                    {"error": "request body too large"},
                    status_code=413,
                )
            data = json.loads(body_bytes)
        except Exception:
            data = None

        error = validate_chat_body(data)
        if error is not None:
            return StarletteJSONResponse(
                {"error": error}, status_code=400,
            )

        if not is_claude_available():
            return StarletteJSONResponse(
                {"error": "claude CLI is not available"},
                status_code=503,
            )

        assert isinstance(data, dict)
        messages = data["messages"]
        system_prompt = data["system_prompt"]
        model = resolve_model(data.get("model"))
        cwd = _resolve_cwd(data.get("project_root"))

        system_prompt = _inject_missing_context(
            system_prompt, cwd, data.get("context_flags")
        )

        _write_chat_debug_input(
            cwd,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            session_id=data.get("session_id"),
        )
        _write_chat_debug_payload(cwd, messages=messages, system_prompt=system_prompt)
        if data.get("session_id"):
            _copy_session_debug_files(cwd, data["session_id"])

        try:
            mark_chat_request(session_key)
        except Exception:
            pass

        extra_mcps = _load_authed_external_mcp_servers()

        current_date = data.get("current_date", "") if isinstance(data.get("current_date"), str) else ""

        run_id = RUNS.start_run(
            messages,
            system_prompt,
            model,
            cwd,
            claude_binary=_resolve_claude_binary(),
            mcp_port=LOCAL_API_PORT,
            extra_mcp_servers=extra_mcps or None,
            session_id=data.get("session_id"),
            current_date=current_date,
        )

        return StarletteJSONResponse({"run_id": run_id})

    async def _async_reattach_handler(request: StarletteRequest):
        run_id = request.path_params["run_id"]

        if RUNS.get_status(run_id) is None:
            return StarletteJSONResponse(
                {"error": "run not found or expired"},
                status_code=404,
            )

        from_index = _parse_from_index(request.query_params.get("from"))

        async def _stream():
            try:
                async for frame in _async_tail_sse(run_id, from_index):
                    yield frame
            except RunNotFoundError:
                return

        return StarletteStreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # -- WebSocket handler for interactive PTY (POS-2168) ---------------------

    _SENTINEL_STRIP_RE = re.compile("\x1ePRXP:-?\\d+\x1f")

    async def _async_console_ws_handler(websocket):
        """Bidirectional WebSocket: raw PTY stdout → client, client stdin → PTY."""
        from starlette.websockets import WebSocketDisconnect
        cwd = websocket.query_params.get("project_root", "")
        cwd = _resolve_cwd(cwd)

        await websocket.accept()

        session = console_pty.get_session(cwd)
        if not session.wait_ready(timeout=10.0):
            await websocket.close(1011, "Terminal not ready")
            return

        # Send initial info
        snap = session.snapshot()
        await websocket.send_json({
            "type": "info",
            "shell": snap.get("shell", "bash"),
            "cwd": snap.get("live_cwd", cwd),
        })

        # Send buffered raw output (sentinel-stripped) for replay
        raw_buffer = session.get_raw_buffer()
        if raw_buffer:
            cleaned = _SENTINEL_STRIP_RE.sub("", raw_buffer)
            if cleaned:
                await websocket.send_bytes(cleaned.encode("utf-8"))

        # Subscribe to live raw output
        raw_q = session.subscribe_raw()

        try:
            async def forward_output():
                loop = asyncio.get_event_loop()
                while True:
                    try:
                        text = await asyncio.wait_for(
                            loop.run_in_executor(None, raw_q.get, True, 30.0),
                            timeout=35.0,
                        )
                    except (asyncio.TimeoutError, Exception):
                        if session._closed:
                            break
                        continue
                    if text is None:
                        break
                    cleaned = _SENTINEL_STRIP_RE.sub("", text)
                    if cleaned:
                        try:
                            await websocket.send_bytes(cleaned.encode("utf-8"))
                        except Exception:
                            break

            async def forward_input():
                import json as _json
                while True:
                    try:
                        message = await websocket.receive()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    if message.get("type") == "websocket.disconnect":
                        break
                    raw_text = message.get("text") or message.get("bytes", b"").decode("utf-8", errors="replace")
                    if not raw_text:
                        continue
                    try:
                        msg = _json.loads(raw_text)
                    except (ValueError, TypeError):
                        session.write_stdin(raw_text)
                        continue
                    msg_type = msg.get("type", "")
                    if msg_type == "stdin":
                        session.write_stdin(msg.get("data", ""))
                    elif msg_type == "resize":
                        cols = int(msg.get("cols", 120))
                        rows = int(msg.get("rows", 40))
                        session.resize(cols, rows)
                    elif msg_type == "interrupt":
                        session.interrupt()

            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_output()),
                 asyncio.create_task(forward_input())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
        finally:
            session.unsubscribe_raw(raw_q)
            try:
                await websocket.close()
            except Exception:
                pass

    # POS-2307: a `--resume` attempt against a stale stored session ID makes
    # Claude Code exit almost immediately with a non-zero code. These bound how
    # long the handler waits before deciding the spawn survived.
    _STALE_RESUME_WINDOW_S = 5.0
    _STALE_RESUME_SETTLED_S = 1.0
    _STALE_RESUME_GRACE_S = 1.5
    _STALE_RESUME_POLL_S = 0.1

    async def _detect_stale_resume(session) -> bool:
        """True when a ``--resume`` PTY died with a non-zero code right after spawn.

        Returns as soon as the process is known dead, or as soon as it has
        rendered output and survived ``_STALE_RESUME_SETTLED_S`` — so a healthy
        resume costs ~1s, not the full window.
        """
        waited = 0.0
        while waited < _STALE_RESUME_WINDOW_S:
            if not session.is_alive():
                # The reader thread assigns `_exit_code` just before it flips
                # `_closed`, so a bare `is_alive()` can win that race; wait a
                # moment rather than misread a real exit code as unknown.
                grace = 0.0
                while session.exit_code is None and grace < _STALE_RESUME_GRACE_S:
                    await asyncio.sleep(_STALE_RESUME_POLL_S)
                    grace += _STALE_RESUME_POLL_S
                return session.exit_code not in (0, None)
            if waited >= _STALE_RESUME_SETTLED_S and session.get_raw_buffer():
                return False
            await asyncio.sleep(_STALE_RESUME_POLL_S)
            waited += _STALE_RESUME_POLL_S
        return False

    async def _async_chat_console_ws_handler(websocket):
        """Per-chat-session Claude Code terminal (POS-2219).

        Query params: project_root, session_id (required), model,
        claude_session_id (optional → ``--resume``), system_prompt (optional,
        small prompts only; large prompts go through POST /api/chat-console/prepare).
        Protocol matches /api/console/ws: binary frames = PTY output, JSON text
        frames = stdin/resize/interrupt/first_message from the client, plus
        {"type":"info"} on connect and {"type":"process_exit","exit_code":N} when
        Claude Code exits.
        """
        from starlette.websockets import WebSocketDisconnect
        import json as _json

        cwd = _resolve_cwd(websocket.query_params.get("project_root", ""))
        raw_sid = websocket.query_params.get("session_id", "")
        model = resolve_model(websocket.query_params.get("model"))
        claude_sid_param = (websocket.query_params.get("claude_session_id") or "").strip() or None
        system_prompt_param = websocket.query_params.get("system_prompt") or ""

        await websocket.accept()

        if not raw_sid.strip():
            await websocket.send_json({"type": "error", "message": "session_id is required"})
            await websocket.close(1008)
            return
        sid = sanitize_session_id(raw_sid)

        if not console_pty.is_chat_supported():
            await websocket.send_json({"type": "error", "message": "PTY not supported"})
            await websocket.close(1011, "PTY not supported")
            return

        if system_prompt_param.strip():
            save_claude_code_system_prompt(cwd, sid, system_prompt_param)

        existing = console_pty.get_chat_pty(cwd, sid)
        attached = existing is not None and existing.is_alive()

        resumed = False
        resumed_from_stored = False
        if not attached:
            claude_bin = _resolve_claude_binary()
            if claude_bin is None:
                await websocket.send_json(
                    {"type": "error", "message": "claude CLI is not available"}
                )
                await websocket.close(1011, "claude CLI is not available")
                return

            args = ["--model", model]
            prompt_path = load_claude_code_system_prompt_path(cwd, sid)
            if prompt_path:
                args += ["--append-system-prompt-file", prompt_path]

            fork_source = load_fork_source_claude_sid(cwd, sid)
            if fork_source:
                # First connect after a fork: resume from source with --fork-session (POS-2240).
                effective_sid = str(uuid.uuid4())
                args += ["--resume", fork_source, "--fork-session", "--session-id", effective_sid]
                save_claude_code_session_id(cwd, sid, effective_sid)
                delete_fork_source_claude_sid(cwd, sid)
                resumed = False
            elif claude_sid_param:
                args += ["--resume", claude_sid_param]
                effective_sid = claude_sid_param
                resumed = True
            else:
                # POS-2307: a session that already stored a Claude Code session
                # ID must resume it. Minting a fresh UUID here overwrote the
                # still-valid ID whenever the PTY died between the status probe
                # and this connect, orphaning the user's conversation.
                existing_sid = load_claude_code_session_id(cwd, sid)
                if existing_sid:
                    args += ["--resume", existing_sid]
                    effective_sid = existing_sid
                    resumed = True
                    resumed_from_stored = True
                else:
                    effective_sid = str(uuid.uuid4())
                    args += ["--session-id", effective_sid]
                    save_claude_code_session_id(cwd, sid, effective_sid)
                    resumed = False

            sess_dir = session_dir(cwd, sid)
            session = await asyncio.to_thread(
                console_pty.get_or_create_chat_pty, cwd, sid, claude_bin, args,
                session_dir=sess_dir,
            )
            if session is None:
                await websocket.send_json(
                    {"type": "error", "message": "failed to spawn Claude Code"}
                )
                await websocket.close(1011, "failed to spawn Claude Code")
                return

            if resumed_from_stored and await _detect_stale_resume(session):
                # POS-2307: the stored ID no longer resumes. Clean it up and let
                # the user consciously re-open — auto-retrying with a new UUID
                # would silently discard the old conversation.
                _startup_logger.warning(
                    "--resume failed for stored session ID, deleted stale "
                    "claude_code_session_id.txt (session %s, exit %s)",
                    sid,
                    session.exit_code,
                )
                delete_claude_code_session_id(cwd, sid)
                console_pty.destroy_chat_pty(cwd, sid)
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Previous session not found. Please re-open the session to start a new conversation.",
                    }
                )
                await websocket.close(1011, "stale claude session id")
                return
        else:
            session = existing
            effective_sid = load_claude_code_session_id(cwd, sid)
            resumed = False

        await websocket.send_json(
            {
                "type": "info",
                "session_id": sid,
                "claude_session_id": effective_sid,
                "model": model,
                "attached": attached,
                "resumed": resumed,
            }
        )

        # Replay ring-buffer output for reconnect.
        buf = session.get_raw_buffer()
        if buf:
            await websocket.send_bytes(buf.encode("utf-8"))

        if not session.is_alive():
            await websocket.send_json(
                {"type": "process_exit", "exit_code": session.exit_code}
            )
            try:
                await websocket.close()
            except Exception:
                pass
            return

        raw_q = session.subscribe_raw()

        try:
            async def forward_output():
                loop = asyncio.get_event_loop()
                while True:
                    try:
                        text = await asyncio.wait_for(
                            loop.run_in_executor(None, raw_q.get, True, 30.0),
                            timeout=35.0,
                        )
                    except (asyncio.TimeoutError, Exception):
                        if session._closed:
                            break
                        continue
                    if text is None:
                        try:
                            await websocket.send_json(
                                {"type": "process_exit", "exit_code": session.exit_code}
                            )
                        except Exception:
                            pass
                        break
                    try:
                        await websocket.send_bytes(text.encode("utf-8"))
                    except Exception:
                        break

            async def forward_input():
                while True:
                    try:
                        message = await websocket.receive()
                    except WebSocketDisconnect:
                        break
                    except Exception:
                        break
                    if message.get("type") == "websocket.disconnect":
                        break
                    raw_text = message.get("text") or message.get("bytes", b"").decode("utf-8", errors="replace")
                    if not raw_text:
                        continue
                    try:
                        msg = _json.loads(raw_text)
                    except (ValueError, TypeError):
                        session.write_stdin(raw_text)
                        continue
                    msg_type = msg.get("type", "")
                    if msg_type == "stdin":
                        session.write_stdin(msg.get("data", ""))
                    elif msg_type == "resize":
                        cols = int(msg.get("cols", 120))
                        rows = int(msg.get("rows", 40))
                        session.resize(cols, rows)
                    elif msg_type == "interrupt":
                        session.interrupt()
                    elif msg_type == "first_message":
                        fm_images = msg.get("images")  # POS-2242
                        session.queue_first_message(str(msg.get("data", "")), images=fm_images)
                    elif msg_type == "image_paste":
                        import base64 as _b64
                        import tempfile as _tf
                        img_data = msg.get("data", "")
                        img_mime = msg.get("mimeType", "image/png")
                        _ext_map = {
                            "image/png": ".png",
                            "image/jpeg": ".jpg",
                            "image/webp": ".webp",
                            "image/gif": ".gif",
                        }
                        ext = _ext_map.get(img_mime, ".png")
                        try:
                            raw = _b64.b64decode(img_data)
                            fd, path = _tf.mkstemp(suffix=ext, prefix="claude-paste-")
                            os.write(fd, raw)
                            os.close(fd)
                            # Inject the saved file path into the PTY stdin
                            # using bracketed paste so it appears in Claude
                            # Code's input buffer without triggering a submit.
                            session.write_stdin(
                                "\x1b[200~" + path + "\x1b[201~"
                            )
                        except Exception as exc:
                            console_pty._chat_logger.warning(
                                "image_paste decode/save failed: %s", exc
                            )

            # POS-2429: notify the frontend once the first message has been
            # written to PTY stdin (after the settle delay).  Runs as a
            # background task so it does not block the main I/O loop.
            async def notify_first_message_delivered():
                loop = asyncio.get_event_loop()
                was_set = await loop.run_in_executor(
                    None, session._first_message_delivered.wait, 60.0
                )
                if was_set:
                    try:
                        await websocket.send_json({"type": "first_message_delivered"})
                    except Exception:
                        pass

            notify_task = asyncio.create_task(notify_first_message_delivered())

            done, pending = await asyncio.wait(
                [asyncio.create_task(forward_output()),
                 asyncio.create_task(forward_input())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            notify_task.cancel()
        finally:
            session.unsubscribe_raw(raw_q)
            try:
                await websocket.close()
            except Exception:
                pass

    async def _async_console_stream_handler(request: StarletteRequest):
        cwd = _resolve_cwd(request.query_params.get("project_root"))
        from_index = _parse_from_index(request.query_params.get("from"))

        async def _stream():
            snap = console_pty.snapshot(cwd)
            yield sse_frame("snapshot", snap)
            if not snap.get("ok"):
                return
            index = from_index if from_index > 0 else int(snap.get("next_event") or 0)
            last_ping = 0.0
            while True:
                batch = console_pty.events_since(cwd, index)
                if batch.get("resync"):
                    yield sse_frame("resync", {})
                    snap = console_pty.snapshot(cwd)
                    yield sse_frame("snapshot", snap)
                    index = int(snap.get("next_event") or 0)
                    continue
                for item in batch.get("events", []):
                    yield sse_frame(str(item["name"]), item["data"])
                index = int(batch.get("next_event") or index)
                await asyncio.sleep(0.05)

        return StarletteStreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- Async handlers for polled endpoints (bypass WsgiToAsgi executor) --

    async def _async_health_handler(request: StarletteRequest):
        claude_available = await asyncio.to_thread(is_claude_available)

        return StarletteJSONResponse(
            {
                "status": "ok",
                "service": "praxis-local-api",
                "version": SERVICE_VERSION,
                "api_version": API_FILE_VERSION,
                "claude_available": claude_available,
                "default_model": DEFAULT_MODEL,
                "mcp_available": True,
                "mcp_http": True,
            },
        )

    async def _async_models_handler(request: StarletteRequest):
        return StarletteJSONResponse(
            {"default": DEFAULT_MODEL, "available": MODEL_CATALOG},
        )

    async def _async_mcp_connections_handler(request: StarletteRequest):
        project_root = _resolve_cwd(
            request.query_params.get("project_root")
        )
        claude_bin = _resolve_claude_binary()
        result = await asyncio.to_thread(
            mcp_connections.check_mcp_connections, claude_bin, project_root,
        )
        return StarletteJSONResponse(result)

    # -- End async handlers ------------------------------------------------

    mcp_server = create_mcp_server(
        claude_checker=is_claude_available,
        default_project_root=CHAT_PROJECT_ROOT,
    )

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.fastmcp.server import StreamableHTTPASGIApp
    _mcp_session_mgr = StreamableHTTPSessionManager(app=mcp_server._mcp_server)
    _mcp_http_handler = StreamableHTTPASGIApp(_mcp_session_mgr)

    flask_asgi = WsgiToAsgi(app)

    @asynccontextmanager
    async def _lifespan(_app):
        async with _mcp_session_mgr.run():
            yield
        RUNS.shutdown_all()

    # -- CORS middleware (single source of truth for all routes) ------------
    # Starlette CORSMiddleware handles preflight OPTIONS and adds
    # Access-Control-* headers on every response — covering async handlers,
    # MCP mounts, and Flask routes uniformly. This replaces the previous
    # per-handler manual CORS + per-route Flask-CORS setup that silently
    # missed new routes (e.g. /api/resolve-path) and all MCP paths.
    _origins = _get_allowed_origins()
    _cors_kwargs: dict = {
        "allow_methods": ["GET", "POST", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-Chat-Session-Id"],
    }
    if _origins and isinstance(_origins[0], str):
        _cors_kwargs["allow_origins"] = _origins
    else:
        _cors_kwargs["allow_origin_regex"] = (
            r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
            r"|^https://([a-z0-9-]+\.)?praxisos\.dev$"
        )

    combined = Starlette(
        routes=[
            Route("/api/chat", _async_chat_handler, methods=["POST"]),
            Route(
                "/api/chat/runs/{run_id}",
                _async_reattach_handler,
                methods=["GET"],
            ),
            Route("/health", _async_health_handler, methods=["GET"]),
            Route("/api/models", _async_models_handler, methods=["GET"]),
            Route("/api/mcp-connections", _async_mcp_connections_handler, methods=["GET"]),
            Route("/api/console/stream", _async_console_stream_handler, methods=["GET"]),
            WebSocketRoute("/api/console/ws", _async_console_ws_handler),
            WebSocketRoute("/api/chat-console/ws", _async_chat_console_ws_handler),
            Route("/stream", endpoint=_mcp_http_handler),
            Mount("/", app=flask_asgi),
        ],
        lifespan=_lifespan,
    )
    combined.add_middleware(CORSMiddleware, **_cors_kwargs)

    print(f"Praxis Local API + MCP  http://127.0.0.1:{port}/")
    print(f"  MCP Streamable HTTP transport:  /stream")
    uvicorn.run(
        combined,
        host="127.0.0.1",
        port=port,
        log_level="debug" if debug else "info",
        log_config=praxis_logging.uvicorn_log_config("debug" if debug else "info"),
        timeout_graceful_shutdown=0,
    )
