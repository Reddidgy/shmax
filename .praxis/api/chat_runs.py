"""Detached ``claude`` run registry for the Praxis Local API.

A *run* is a single ``claude`` generation that is owned by the local Flask
helper, NOT by the HTTP connection that started it. Each run owns a background
worker thread that spawns ``claude``, translates its stdout into SSE events
(via :func:`chat_stream.translate_stream`), and appends every event to an
append-only buffer guarded by a :class:`threading.Condition`. HTTP requests
merely *tail* that buffer — so a browser refresh (which drops the SSE
connection) never kills the generation, and a reloaded page can reattach to a
still-running run and replay its events (Slice 2).

This module deliberately owns the subprocess-touching code (spawn + teardown)
so :mod:`praxis_local_api` can import it without a circular dependency. The pure
helpers (:func:`build_claude_argv`, :func:`build_transcript`) and the translator
(:func:`translate_stream`) are reused unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

_IS_WINDOWS = sys.platform == "win32"

from chat_constants import DEBUG_STDIN_FILENAME, is_ask_user_question
from chat_runtime import (
    COMPACTION_CHAR_THRESHOLD,
    COMPACTION_LINE_THRESHOLD,
    COMPACTION_SYSTEM_PROMPT,
    SESSION_ROTATION_BYTE_THRESHOLD,
    build_claude_argv,
    build_stdin_payload,
    build_stream_json_line,
    collect_request_images,
    compact_stdin_payload,
    has_system_prompt_changed,
    messages_have_images,
    should_rotate_session,
)
from chat_session_memory import (
    build_session_stdin,
    load_claude_session_id,
    load_memory_messages,
    persist_compaction,
    render_memory_messages,
    save_claude_session_id,
    save_memory_messages,
)
from chat_session_paths import session_dir, claude_session_id_path, enforce_session_limit
from chat_saved_items import (
    add_saved_item,
    load_saved_items,
    render_saved_items,
)
from chat_tool_actions import (
    build_tool_note,
    clear_pending_actions,
    load_pending_actions,
    merge_tool_actions,
    normalize_tool_name,
    save_pending_actions,
    with_status,
)
from chat_realtime_log import (
    build_realtime_log_base,
    build_realtime_log_content,
    write_realtime_log,
)
from chat_session_queue import QUEUE
from chat_stream import translate_stream
import console_pty

__all__ = [
    "RunManager",
    "RunNotFoundError",
    "RUNS",
    "RUN_RETENTION_SECONDS",
    "CLAUDE_TERM_GRACE_SECONDS",
    "terminate_process_group",
]

logger = logging.getLogger("praxis_local_api.runs")

# What the run buffer stores / tailers yield: an SSE event name + its payload.
# (Plain alias rather than the 3.12 ``type`` statement, to match the runtime —
# the helper runs under the user's Python, which may be 3.9, like chat_stream.)
RunEvent = tuple[str, dict[str, Any]]

# Terminal event names — exactly one of these ends every run's event buffer.
_TERMINAL_EVENTS = frozenset({"done", "error"})

# How long a spawned `claude` process is given to exit after SIGTERM before we
# escalate to SIGKILL during cancellation/cleanup. Mirrors the historical
# constant from praxis_local_api (single source of truth now lives here).
CLAUDE_TERM_GRACE_SECONDS = 3

# How long a finished run's events are retained so a tab reattaching right
# after completion can still replay the terminal frame. Override with
# CHAT_RUN_RETENTION_SECONDS.
RUN_RETENTION_SECONDS = max(
    1, int(os.environ.get("CHAT_RUN_RETENTION_SECONDS", 600))
)

# Defensive cap on total retained runs (running + finished). When exceeded we
# drop the oldest finished runs first. Bounds helper memory.
MAX_RETAINED_RUNS = max(16, int(os.environ.get("CHAT_MAX_RETAINED_RUNS", 256)))


class RunNotFoundError(KeyError):
    """Raised when a ``run_id`` is unknown or has been pruned after retention."""


def terminate_process_group(
    proc: subprocess.Popen[str], *, force: bool = False
) -> None:
    """Terminate the child process tree, escalate to force-kill, always reap.

    On Unix, spawned with ``start_new_session=True`` so the child leads its own
    process group; ``os.killpg`` takes down any descendants ``claude`` spawned.
    On Windows, ``taskkill /F /T`` kills the process tree. The final ``wait()``
    guarantees no zombie/orphan survives. Never raises.

    When *force* is True, skip graceful termination and go straight to
    force-kill — used during server shutdown where speed matters more than
    graceful child exit.
    """
    try:
        if proc.poll() is None:
            if _IS_WINDOWS:
                _terminate_windows(proc, force=force)
            else:
                _terminate_unix(proc, force=force)
    except Exception as exc:  # noqa: BLE001 — cleanup must never raise out
        logger.warning("claude process-group teardown failed: %s", exc)
    finally:
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, Exception):  # noqa: BLE001
            pass


def _terminate_unix(
    proc: subprocess.Popen[str], *, force: bool = False
) -> None:
    if force:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=CLAUDE_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _terminate_windows(
    proc: subprocess.Popen[str], *, force: bool = False
) -> None:
    if force:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=CLAUDE_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )


def _overwrite_stdin_debug(cwd: str, payload: str) -> None:
    """Overwrite ``last_stdin.txt`` with the final (compacted) payload.

    Byte-exact debug contract (POS-1830): after in-worker compaction the payload
    piped to ``claude`` differs from the pre-compaction snapshot the HTTP handler
    already wrote, so the worker rewrites the artifact with the ACTUAL bytes.
    Mirrors ``praxis_local_api._write_chat_debug_payload``: ``newline=""`` (no OS
    newline translation), no trailing newline added. Best-effort — a debug write
    failure must never break a turn.
    """
    try:
        debug_dir = os.path.join(cwd, ".praxis", "chat", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        target = os.path.join(debug_dir, DEBUG_STDIN_FILENAME)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
    except Exception as exc:  # noqa: BLE001 — debug write must never break a turn
        logger.warning("compacted stdin debug write failed: %s", exc)


@dataclass
class _Run:
    """In-memory record for a single detached ``claude`` run.

    All mutation of ``events``/``status``/``proc``/``finished_at`` happens while
    holding ``cond``; workers ``notify_all`` after each append and on finish,
    tailers ``wait`` on it.
    """

    run_id: str
    cond: threading.Condition = field(default_factory=threading.Condition)
    status: str = "running"  # 'running' | 'done' | 'error' | 'stopped'
    events: list[RunEvent] = field(default_factory=list)
    proc: subprocess.Popen[str] | None = None
    # POS-1918: the currently-running pre-generation subprocess (stdin
    # compaction or session-rotation compaction), if any. Set/cleared by the
    # worker via a ``proc_hook`` so ``stop()`` can kill it even before the main
    # ``claude`` process (``proc``) is spawned.
    pre_proc: subprocess.Popen[str] | None = None
    finished_at: float | None = None
    # Set by ``stop()`` BEFORE a terminal exists; the finalizer turns whatever
    # terminal arrives into a ``stopped`` outcome. Kept distinct from ``status``
    # so a stop-in-flight does not make ``is_finished`` true prematurely (which
    # would suppress the terminal and hang tailers).
    stop_requested: bool = False
    # POS-1846: stored so queue endpoints can look up which session a run
    # belongs to and where its project root is.
    session_id: str | None = None
    cwd: str = ""
    claude_session_id: str | None = None

    @property
    def is_finished(self) -> bool:
        return self.status != "running"


class RunManager:
    """Thread-safe registry of detached ``claude`` runs.

    A module-level singleton :data:`RUNS` is the intended instance; the class is
    instantiable for isolated tests.
    """

    def __init__(
        self,
        *,
        retention_seconds: int = RUN_RETENTION_SECONDS,
        max_retained: int = MAX_RETAINED_RUNS,
    ) -> None:
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()  # guards the registry dict only
        self._retention_seconds = retention_seconds
        self._max_retained = max_retained

    # -- lifecycle ---------------------------------------------------------
    def start_run(
        self,
        messages: list[dict[str, object]],
        system_prompt: str,
        model: str,
        cwd: str,
        *,
        claude_binary: str | None,
        mcp_port: int | None = None,
        extra_mcp_servers: dict | None = None,
        session_id: str | None = None,
        current_date: str = "",
    ) -> str:
        """Allocate a run, spawn its background worker, and return the run id.

        The worker is a daemon thread that is NOT tied to any request: a tailing
        consumer disconnecting never stops it. It spawns ``claude``, appends each
        translated event to the run buffer, decides the terminal from exit
        status exactly as the legacy generator did, and always reaps the child.

        ``session_id`` (POS-1838) routes the worker onto the compact per-session
        memory stdin path (:mod:`chat_session_memory`) instead of replaying the
        full message history; ``None``/empty falls back to the legacy
        ``build_stdin_payload`` path unchanged.
        """
        self._prune_expired()
        enforce_session_limit(cwd)

        run_id = uuid.uuid4().hex
        run = _Run(run_id=run_id)
        run.session_id = session_id
        run.cwd = cwd
        if session_id:
            QUEUE.register(run_id, session_id, cwd)
        with self._lock:
            self._runs[run_id] = run
            self._enforce_cap()

        worker = threading.Thread(
            target=self._worker,
            args=(
                run,
                messages,
                system_prompt,
                model,
                cwd,
                claude_binary,
                mcp_port,
                extra_mcp_servers,
                session_id,
                current_date,
            ),
            name=f"chat-run-{run_id[:8]}",
            daemon=True,
        )
        worker.start()
        return run_id

    def _worker(
        self,
        run: _Run,
        messages: list[dict[str, object]],
        system_prompt: str,
        model: str,
        cwd: str,
        claude_binary: str | None,
        mcp_port: int | None = None,
        extra_mcp_servers: dict | None = None,
        session_id: str | None = None,
        current_date: str = "",
    ) -> None:
        """Background body: spawn ``claude``, buffer SSE events, finalize.

        Mirrors the old ``_stream_claude_chat`` subprocess loop, but appends to
        the run buffer instead of yielding and is never interrupted by a client
        disconnect. Always appends exactly one terminal event and always reaps.
        """
        memory_text = ""

        def _set_pre_proc(p: subprocess.Popen) -> None:
            # POS-1918: records the currently-running pre-generation
            # subprocess (stdin / session-rotation compaction) so a
            # concurrent stop() can kill it before the main claude process
            # exists.
            with run.cond:
                run.pre_proc = p

        has_images = messages_have_images(messages)
        # POS-1992: images no longer go through a separate blocking Sonnet
        # vision subprocess. They ride in-band to the MAIN generation model as
        # stream-json ``image`` content blocks, so image understanding costs
        # zero extra latency and the agent can never claim it "sees no images".
        # ``has_images`` therefore stays True for the rest of the turn: it
        # drives ``--input-format stream-json`` in the argv AND the envelope
        # wrapping below.

        def _wrap_images(payload: str) -> str:
            """Wrap a session-path text payload into the stream-json envelope.

            The legacy path (no ``session_id``) already emits the envelope via
            ``build_stdin_payload``; only the session-memory and resume paths
            assemble plain text and need wrapping here. Text-only turns and the
            legacy path pass straight through, byte-for-byte unchanged.
            """
            if not has_images or not session_id:
                return payload
            return (
                build_stream_json_line(
                    payload, collect_request_images(messages)
                )
                + "\n"
            )

        # POS-1939: Session Rotation — proactively reset CLI session when
        # full.json grows large. Compacts realtime_log.txt through Sonnet,
        # persists the compacted result as new memory.json, deletes
        # claude_session_id.txt (forcing the session-memory path which
        # picks up the compacted memory), and records a rotation marker
        # so the check does not re-fire every turn.
        # full.json is NEVER touched — it is the frontend's source of
        # truth for rendering the complete chat session.
        # POS-1940: a changed system prompt (assignee switch mid-session)
        # is a second rotation trigger — a resumed CLI session would keep
        # answering as the previous persona, so previous work is compacted
        # and the CLI session is re-created without --resume. Fires at most
        # once per change: system_prompt.txt is overwritten with the new
        # prompt later this turn.
        # POS-1952: set to True only when this turn's rotation actually
        # replaced memory.json with a compacted summary — the memory-loading
        # block below must then NOT re-append the last completed turn from
        # the POST body, because the summary already covers it.
        _rotation_compacted = False
        if session_id:
            _prompt_changed = has_system_prompt_changed(
                cwd, session_id, system_prompt
            )
            if should_rotate_session(cwd, session_id) or _prompt_changed:
                _sdir = session_dir(cwd, session_id)
                _sid_path = claude_session_id_path(cwd, session_id)

                # Measure full.json for logging + marker
                try:
                    _full_size = os.path.getsize(
                        os.path.join(_sdir, "full.json")
                    )
                except OSError:
                    _full_size = 0

                # Delete claude_session_id.txt (forces session-memory path)
                if os.path.isfile(_sid_path):
                    try:
                        os.unlink(_sid_path)
                    except OSError:
                        pass

                logger.info(
                    "Session rotation triggered (%s), resetting CLI session",
                    "system prompt changed"
                    if _prompt_changed
                    else f"full.json={_full_size} bytes > threshold",
                )

                # Compact from realtime_log.txt — a token-efficient
                # conversation view that is much smaller than memory.json
                # or full.json. The compacted result becomes the new
                # memory.json so subsequent turns start compact.
                _rt_log_path = os.path.join(_sdir, "realtime_log.txt")
                _rt_content = ""
                try:
                    with open(
                        _rt_log_path, "r", encoding="utf-8"
                    ) as _rt_f:
                        _rt_content = _rt_f.read()
                except OSError:
                    logger.warning(
                        "Session rotation: realtime_log.txt not readable, "
                        "skipping compaction"
                    )

                _compacted = ""
                if _rt_content.strip():
                    self._append(
                        run,
                        (
                            "compacting",
                            {
                                "status": "start",
                                "stdin_path": "session-rotation",
                            },
                        ),
                    )
                    # Extract only conversation content for compaction,
                    # stripping metadata (workDir, saved items) that
                    # confuses the compaction model into responding
                    # conversationally instead of summarising.
                    _compaction_input = _rt_content
                    for _marker in (
                        "### Compacted data from previous turns",
                        "### Realtime log (previous turns + realtime answers from AI)",
                        "### Previous Turns",
                    ):
                        _marker_pos = _rt_content.find(_marker)
                        if _marker_pos != -1:
                            _compaction_input = _rt_content[_marker_pos:]
                            break
                    try:
                        _cp_path = os.path.join(
                            _sdir, "compaction_prompt.txt"
                        )
                        with open(
                            _cp_path, "w", encoding="utf-8", newline=""
                        ) as _cp_f:
                            _cp_f.write(
                                f"### System Prompt\n\n"
                                f"{COMPACTION_SYSTEM_PROMPT}\n\n"
                                f"### Input\n\n"
                                f"{_compaction_input}"
                            )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "compaction_prompt.txt debug write failed"
                        )
                    _compacted = compact_stdin_payload(
                        _compaction_input,
                        claude_binary,
                        cwd=cwd,
                        proc_hook=_set_pre_proc,
                    )
                    if _compacted and len(_compacted) < len(_compaction_input):
                        persist_compaction(cwd, session_id, _compacted)
                        _rotation_compacted = True
                        logger.info(
                            "Session rotation compacted realtime_log.txt "
                            "(%d chars -> %d chars)",
                            len(_compaction_input),
                            len(_compacted),
                        )
                    self._append(
                        run,
                        (
                            "compacting",
                            {
                                "status": "done",
                                "stdin_path": "session-rotation",
                                "original_chars": len(_compaction_input),
                                "compacted_chars": len(_compacted)
                                if _compacted
                                else len(_compaction_input),
                            },
                        ),
                    )
                    with run.cond:
                        run.pre_proc = None
                    if run.stop_requested:
                        return

                # Write rotation marker so should_rotate_session knows
                # when the NEXT rotation is due (only after another
                # threshold-worth of growth).
                try:
                    with open(
                        os.path.join(_sdir, "rotation_byte_offset.txt"),
                        "w",
                    ) as _marker_f:
                        _marker_f.write(str(_full_size))
                except OSError:
                    pass

        # Load claude_session_id for potential resume
        claude_sid = None
        if session_id:
            claude_sid = load_claude_session_id(cwd, session_id)
        # POS-2000: Image turns must skip --resume. The CLI's resume mode does
        # not relay stream-json image content blocks to the API — the model
        # receives the text but never sees the attached pixels. Clearing
        # claude_sid forces the session-memory path, which correctly wraps
        # images into the stream-json envelope and sends them in a new CLI
        # session. The next text-only turn will resume from that new session.
        if claude_sid and has_images:
            logger.info(
                "Skipping resume for image turn (session %s), "
                "falling back to session-memory path",
                session_id,
            )
            claude_sid = None
        if claude_sid:
            logger.info("Resuming claude session %s", claude_sid)
        elif session_id:
            logger.info("Starting new claude session")

        argv = build_claude_argv(
            claude_binary, system_prompt, model,
            with_image_input=has_images, mcp_port=mcp_port,
            extra_mcp_servers=extra_mcp_servers,
            resume_session_id=claude_sid if session_id else None,
        )
        # POS-1838: a non-empty session_id routes onto the compact per-session
        # memory stdin path (chat_session_memory) instead of replaying the full
        # message history — this is what keeps the payload small enough that
        # the compaction pass below is almost never needed.
        # POS-1992: image turns now take this path TOO. chat_session_memory
        # still produces plain text; ``_wrap_images`` below folds that text
        # into the stream-json envelope --input-format stream-json requires,
        # so session memory and in-band vision coexist. Wrapping happens AFTER
        # compaction so base64 never inflates the compaction trigger.
        # POS-1938: when claude_sid is set (resume mode), stdin is just the
        # current user message — the CLI manages context via --resume.
        _rt_base = ""  # POS-1890: set below when session-memory path is taken
        if session_id:
            current_user_msg = ""
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    content = message.get("content")
                    current_user_msg = content if isinstance(content, str) else ""
                    # POS-1987: Inject file attachment content into the user message.
                    file_attachments = message.get("file_attachments")
                    if isinstance(file_attachments, list) and file_attachments:
                        file_blocks = []
                        for fa in file_attachments:
                            if isinstance(fa, dict):
                                fname = fa.get("file_name", "unknown")
                                fcontent = fa.get("content", "")
                                if fcontent:
                                    file_blocks.append(
                                        f"[Attached file: {fname}]\n```\n{fcontent}\n```"
                                    )
                        if file_blocks:
                            current_user_msg = (
                                current_user_msg + "\n\n" + "\n\n".join(file_blocks)
                            )
                    break

            def _sub_workdir(text: str) -> str:
                normalized = cwd.rstrip("/\\") if cwd else ""
                return text.replace(normalized + "/", "") if normalized else text

            # Extract assignee name from system prompt for conversational
            # history role labels (POS-1848).
            name_match = re.search(r'^NAME:\s*(.+)$', system_prompt, re.MULTILINE)
            assignee_name = name_match.group(1).strip() if name_match else "Assistant"

            # POS-1852: memory.json now stores the same message format as
            # full.json.  Load existing messages, append only the latest
            # completed turn, and persist — keeping old turns immutable
            # (POS-1848) while enabling compaction persistence. This runs
            # ALWAYS — even in resume mode — so the full-path fallback below
            # always has an up-to-date memory.json to work from.
            # History = all POST body messages except the trailing current
            # user message (which build_session_stdin handles separately).
            history_messages: list[dict] = [
                msg for msg in messages if isinstance(msg, dict)
            ]
            # Strip the trailing user message (the current turn)
            if (
                history_messages
                and history_messages[-1].get("role") == "user"
            ):
                history_messages = history_messages[:-1]

            existing = load_memory_messages(cwd, session_id)
            if existing is None:
                # First turn or migration: initialise from full history
                memory_messages = history_messages
            elif _rotation_compacted:
                # POS-1952: Session Rotation replaced memory.json with a
                # single compacted summary earlier this turn.  That summary
                # was compacted from realtime_log.txt, which already covers
                # every completed turn — appending history_messages[-2:]
                # here would re-introduce messages the summary contains,
                # duplicating them under "### Previous Turns" and inflating
                # the payload every subsequent rotation has to compact.
                # The current turn is picked up on the NEXT turn by the
                # normal append-latest logic below.
                memory_messages = list(existing)
            else:
                # Append only the latest completed turn (last user+assistant
                # pair from history).  Works both pre- and post-compaction.
                if len(history_messages) >= 2:
                    new_turn = list(history_messages[-2:])
                elif history_messages:
                    new_turn = list(history_messages[-1:])
                else:
                    new_turn = []
                memory_messages = existing + new_turn

            # POS-1862: the previous turn's tool-action notes were buffered by
            # the worker AFTER that turn's memory had already been written, so
            # they are folded in here — before the assistant reply they belong
            # to — and the buffer is cleared once the merge is persisted.
            pending_actions = load_pending_actions(cwd, session_id)
            memory_messages = merge_tool_actions(memory_messages, pending_actions)
            save_memory_messages(cwd, session_id, memory_messages)
            if pending_actions:
                clear_pending_actions(cwd, session_id)

            if claude_sid:
                # RESUME PATH: stdin = only the current user message.
                # CLI manages context via --resume; we skip rendering
                # memory/saved-items into stdin.
                # POS-2116: prepend date awareness line when enabled.
                if current_date:
                    stdin_payload = f"You must be aware about current date: {current_date}\n{current_user_msg}"
                else:
                    stdin_payload = current_user_msg
                stdin_path = "resume"
                # POS-1939: Include memory context in realtime_log so
                # the accumulated conversation history (including
                # compacted summaries from previous rotations) is
                # available when the next rotation compacts
                # realtime_log.txt.  Without this, every resume turn
                # overwrites realtime_log.txt with only the current
                # turn, and subsequent rotations compact almost nothing.
                _resume_memory_text = render_memory_messages(
                    memory_messages, assignee_name=assignee_name
                )
                _resume_saved = load_saved_items(cwd, session_id)
                _resume_saved_text = render_saved_items(_resume_saved)
                _rt_base = build_realtime_log_base(
                    cwd,
                    _sub_workdir(_resume_saved_text),
                    _sub_workdir(_resume_memory_text),
                    _sub_workdir(current_user_msg),
                )
                write_realtime_log(cwd, session_id, _rt_base)
            else:
                # FULL SESSION-MEMORY PATH (existing logic)
                memory_text = render_memory_messages(memory_messages, assignee_name=assignee_name)
                saved_items = load_saved_items(cwd, session_id)
                saved_items_text = render_saved_items(saved_items)
                # Substituting each piece before combining (rather than the
                # joined whole) is equivalent — plain substring replacement —
                # and lets build_session_stdin's own last_stdin.txt debug
                # write already reflect the exact bytes about to be piped, no
                # separate overwrite step needed (unlike the legacy path's
                # _overwrite_stdin_debug).
                _cwd_normalized = cwd.rstrip("/\\") if cwd else ""
                _workdir_line = f"workDir: {_cwd_normalized}" if _cwd_normalized else ""
                for _msg in messages:
                    _content = _msg.get("content", "") if isinstance(_msg, dict) else ""
                    if isinstance(_content, str) and _workdir_line and _workdir_line in _content:
                        logger.warning(
                            "system prompt content detected in messages array — "
                            "possible duplication (workDir line found in message)"
                        )
                        break
                stdin_payload = build_session_stdin(
                    cwd,
                    session_id,
                    _sub_workdir(system_prompt),
                    _sub_workdir(memory_text),
                    _sub_workdir(current_user_msg),
                    saved_items_text=_sub_workdir(saved_items_text),
                    current_date=current_date,
                )
                stdin_path = "session-memory"
                # POS-1890: create initial realtime_log.txt — token-efficient
                # session view without system prompt, updated during streaming.
                _rt_base = build_realtime_log_base(
                    cwd,
                    _sub_workdir(saved_items_text),
                    _sub_workdir(memory_text),
                    _sub_workdir(current_user_msg),
                )
                write_realtime_log(cwd, session_id, _rt_base)
        else:
            # POS-1987: Inject file attachment content into messages (legacy path).
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "user":
                    file_attachments = msg.get("file_attachments")
                    if isinstance(file_attachments, list) and file_attachments:
                        file_blocks = []
                        for fa in file_attachments:
                            if isinstance(fa, dict):
                                fname = fa.get("file_name", "unknown")
                                fcontent = fa.get("content", "")
                                if fcontent:
                                    file_blocks.append(
                                        f"[Attached file: {fname}]\n```\n{fcontent}\n```"
                                    )
                        if file_blocks:
                            orig = msg.get("content", "")
                            msg["content"] = (
                                orig + "\n\n" + "\n\n".join(file_blocks)
                            )
            # Payload comes from the shared builder (chat_runtime.build_stdin_payload)
            # that the debug artifact writer also uses, so the two can never drift:
            # image turns feed a single stream-json `user` line (text + image
            # blocks); text-only turns keep the plain role-marked transcript,
            # byte-for-byte unchanged.  work_dir triggers the relative-path
            # replacement (POS-1854, formerly ${workDir} substitution in POS-1831).
            stdin_payload = build_stdin_payload(messages, work_dir=cwd)
            stdin_path = "legacy"
        # POS-1830: when the assembled payload is large, compact it through a
        # blocking Sonnet CLI call BEFORE spawning the main generation. The
        # `compacting` SSE events bracket the call so the frontend can show an
        # inline status; on any failure compact_stdin_payload returns the
        # original text, so the turn is never blocked. Trigger: character
        # count exceeds COMPACTION_CHAR_THRESHOLD (100 000 chars by default).
        # POS-1938: skipped entirely in resume mode — the CLI manages context,
        # and stdin is just the current user message (never large enough to
        # need compaction; compacting the tiny resume stdin would be wasted
        # work and would corrupt the "just the user message" contract).
        if not claude_sid:
            original_chars = len(stdin_payload)
            if original_chars > COMPACTION_CHAR_THRESHOLD:
                original_lines = stdin_payload.count("\n")
                self._append(run, ("compacting", {"status": "start", "stdin_path": stdin_path}))
                if stdin_path == "session-memory" and memory_text:
                    compacted_memory = compact_stdin_payload(
                        memory_text, claude_binary, cwd=cwd,
                        proc_hook=_set_pre_proc,
                    )
                    if len(compacted_memory) < len(memory_text):
                        # POS-1952: unlike the Session Rotation path above,
                        # no skip-append flag is needed here.  This pass
                        # compacts memory_text, which excludes the current
                        # user message and the not-yet-generated assistant
                        # reply, so the next turn's history_messages[-2:]
                        # append adds exactly the turn the summary does not
                        # cover — correct, not duplicated.
                        persist_compaction(cwd, session_id, compacted_memory)
                        stdin_payload = build_session_stdin(
                            cwd,
                            session_id,
                            _sub_workdir(system_prompt),
                            _sub_workdir(compacted_memory),
                            _sub_workdir(current_user_msg),
                            saved_items_text=_sub_workdir(saved_items_text),
                            current_date=current_date,
                        )
                        # POS-1890: rebuild realtime log base with compacted memory
                        _rt_base = build_realtime_log_base(
                            cwd,
                            _sub_workdir(saved_items_text),
                            _sub_workdir(compacted_memory),
                            _sub_workdir(current_user_msg),
                        )
                        write_realtime_log(cwd, session_id, _rt_base)
                else:
                    stdin_payload = compact_stdin_payload(
                        stdin_payload, claude_binary, cwd=cwd,
                        proc_hook=_set_pre_proc,
                    )
                compacted_chars = len(stdin_payload)
                compacted_lines = stdin_payload.count("\n")
                self._append(
                    run,
                    (
                        "compacting",
                        {
                            "status": "done",
                            "stdin_path": stdin_path,
                            "original_lines": original_lines,
                            "compacted_lines": compacted_lines,
                            "original_chars": original_chars,
                            "compacted_chars": compacted_chars,
                        },
                    ),
                )
                with run.cond:
                    run.pre_proc = None
                if run.stop_requested:
                    return
        # POS-1992: fold images into the stream-json envelope AFTER compaction
        # so the base64 payload never counts toward COMPACTION_CHAR_THRESHOLD
        # and the debug artifacts below record the exact bytes claude received.
        stdin_payload = _wrap_images(stdin_payload)
        _overwrite_stdin_debug(cwd, stdin_payload)

        # POS-1942: always write the FINAL stdin payload to the
        # per-session last_stdin.txt so it reflects exactly what was
        # piped to claude — including after any compaction step.
        # build_session_stdin writes an earlier snapshot, but the
        # COMPACTION_CHAR_THRESHOLD pass or the resume path may have
        # changed stdin_payload after that.
        if session_id:
            try:
                _ls_path = os.path.join(
                    session_dir(cwd, session_id), "last_stdin.txt"
                )
                with open(
                    _ls_path, "w", encoding="utf-8", newline=""
                ) as _ls_f:
                    _ls_f.write(stdin_payload)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "per-session last_stdin.txt final write failed"
                )

        # POS-1939: persist the system prompt to the per-session directory
        # so the exact text sent via --system-prompt is inspectable per
        # session. Byte-exact, same newline="" pattern as last_stdin.txt.
        if session_id:
            try:
                _sp_path = os.path.join(
                    session_dir(cwd, session_id), "system_prompt.txt"
                )
                with open(_sp_path, "w", encoding="utf-8", newline="") as _sp_f:
                    _sp_f.write(system_prompt)
            except Exception:  # noqa: BLE001 — diagnostic write must never break a turn
                logger.warning("per-session system_prompt.txt write failed")

        # Spawn + stream + finalize, with retry support for resume fallback.
        _resume_retried = False

        while True:
            if _resume_retried:
                # FALLBACK: resume failed — rebuild for full session-memory path.
                argv = build_claude_argv(
                    claude_binary, system_prompt, model,
                    with_image_input=has_images, mcp_port=mcp_port,
                    extra_mcp_servers=extra_mcp_servers,
                )
                fallback_memory = load_memory_messages(cwd, session_id) or []
                memory_text = render_memory_messages(fallback_memory, assignee_name=assignee_name)
                saved_items = load_saved_items(cwd, session_id)
                saved_items_text = render_saved_items(saved_items)
                stdin_payload = build_session_stdin(
                    cwd,
                    session_id,
                    _sub_workdir(system_prompt),
                    _sub_workdir(memory_text),
                    _sub_workdir(current_user_msg),
                    saved_items_text=_sub_workdir(saved_items_text),
                )
                stdin_path = "session-memory"
                _rt_base = build_realtime_log_base(
                    cwd,
                    _sub_workdir(saved_items_text),
                    _sub_workdir(memory_text),
                    _sub_workdir(current_user_msg),
                )
                write_realtime_log(cwd, session_id, _rt_base)
                # POS-1992: the fallback rebuild produced plain text again —
                # re-wrap so images survive the retry.
                stdin_payload = _wrap_images(stdin_payload)
                _overwrite_stdin_debug(cwd, stdin_payload)

            proc: subprocess.Popen[str] | None = None
            try:
                try:
                    popen_kwargs: dict[str, Any] = {}
                    if _IS_WINDOWS:
                        popen_kwargs["creationflags"] = (
                            subprocess.CREATE_NEW_PROCESS_GROUP
                        )
                    else:
                        popen_kwargs["start_new_session"] = True
                    proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        cwd=cwd,
                        **popen_kwargs,
                    )
                except Exception as exc:  # noqa: BLE001 — spawn failure → single error
                    logger.warning("claude spawn failed: %s", exc)
                    self._finalize(run, ("error", {"error": "Failed to start claude"}))
                    return

                with run.cond:
                    run.proc = proc

                # Write the input payload, then EOF so claude starts producing.
                if proc.stdin is not None:
                    try:
                        proc.stdin.write(stdin_payload)
                    except BrokenPipeError:
                        pass
                    finally:
                        proc.stdin.close()

                _claude_sid_captured: list[str | None] = [None]

                def _parsed_lines() -> Iterator[dict[str, object]]:
                    """Yield json.loads of each stdout line; a bad line raises."""
                    assert proc is not None and proc.stdout is not None
                    for raw in proc.stdout:
                        raw = raw.strip()
                        if not raw:
                            continue
                        obj = json.loads(raw)
                        if isinstance(obj, dict) and obj.get("type") == "result":
                            sid = obj.get("session_id")
                            if isinstance(sid, str) and sid:
                                _claude_sid_captured[0] = sid
                        yield obj

                # The translator yields exactly one terminal (done xor error). We
                # suppress its synthesized terminal and decide it from the real exit
                # status: non-zero → a single `error`, clean → a single `done`.
                terminal: RunEvent | None = None
                _tracked_tool_uses: dict[str, dict] = {}
                _turn_tool_notes: list[str] = []
                _bash_note_index: dict[str, int] = {}
                _rt_ai_text = ""           # POS-1890: AI text accumulator for realtime log
                _rt_chars_since_flush = 0  # POS-1890: chars since last realtime log flush
                try:
                    for event_name, data in translate_stream(_parsed_lines()):
                        if event_name in _TERMINAL_EVENTS:
                            terminal = (event_name, data)
                            continue
                        self._append(run, (event_name, data))
                        if session_id and event_name == "tool_use" and isinstance(data, dict):
                            _tool_name = data.get("name", "")
                            _tool_id = data.get("id", "")
                            _tool_input = data.get("input", {})
                            if _tool_name == "get_task" or _tool_name.endswith("__get_task"):
                                _tracked_tool_uses[_tool_id] = {"type": "task", "input": _tool_input}
                            # POS-1862: record file ops / CLI commands as memory
                            # notes. Persisted per event so a stopped or failed
                            # turn still leaves its notes for the next turn.
                            _note = build_tool_note(_tool_name, _tool_input, cwd)
                            if _note:
                                _turn_tool_notes.append(_note)
                                if normalize_tool_name(_tool_name) == "Bash" and _tool_id:
                                    _bash_note_index[_tool_id] = len(_turn_tool_notes) - 1
                                save_pending_actions(cwd, session_id, _turn_tool_notes)
                            # POS-1890: flush realtime log on tool boundary
                            if _rt_base:
                                write_realtime_log(cwd, session_id, build_realtime_log_content(
                                    _rt_base, _turn_tool_notes, _rt_ai_text, assignee_name
                                ))
                                _rt_chars_since_flush = 0
                        elif session_id and event_name == "tool_result" and isinstance(data, dict):
                            _result_id = data.get("id", "")
                            # POS-1862: only the ok/error outcome is stored for a
                            # Bash note — never the command's output.
                            _bash_idx = _bash_note_index.pop(_result_id, None)
                            if _bash_idx is not None and 0 <= _bash_idx < len(_turn_tool_notes):
                                _turn_tool_notes[_bash_idx] = with_status(
                                    _turn_tool_notes[_bash_idx], bool(data.get("is_error"))
                                )
                                save_pending_actions(cwd, session_id, _turn_tool_notes)
                                # POS-1890: flush realtime log after tool status update
                                if _rt_base:
                                    write_realtime_log(cwd, session_id, build_realtime_log_content(
                                        _rt_base, _turn_tool_notes, _rt_ai_text, assignee_name
                                    ))
                                    _rt_chars_since_flush = 0
                            _tracked = _tracked_tool_uses.pop(_result_id, None)
                            if _tracked and not data.get("is_error"):
                                _content = data.get("content", "")
                                if isinstance(_content, list):
                                    _content = "\n".join(
                                        b.get("text", "") for b in _content
                                        if isinstance(b, dict)
                                    )
                                _content = str(_content)
                                if _content and _tracked["type"] == "task":
                                    _tinput = _tracked.get("input", {})
                                    _tid = _tinput.get("task_id", "") if isinstance(_tinput, dict) else ""
                                    _ap_idx = _content.find("\n## Assignee Profile\n")
                                    if _ap_idx != -1:
                                        _content = _content[:_ap_idx].rstrip()
                                    add_saved_item(cwd, session_id, f"Task #{_tid}", _content)
                        # Pause the turn at an interactive question. Running under
                        # `claude --print` there is no TTY for AskUserQuestion, so the
                        # CLI self-resolves the question with an empty answer and keeps
                        # generating in the SAME turn ("you didn't answer, nevermind").
                        # Instead we end the turn cleanly the instant the question is
                        # emitted: it becomes the assistant's terminal segment, the
                        # frontend renders its interactive card, and the user's
                        # selection is sent as the NEXT turn — which is how the answer
                        # actually reaches the model. The `finally` below kills the
                        # still-running child so it cannot keep producing into a void.
                        if (
                            event_name == "tool_use"
                            and isinstance(data, dict)
                            and is_ask_user_question(data.get("name", ""))
                        ):
                            # POS-1890: flush realtime log before pausing turn
                            if _rt_base:
                                write_realtime_log(cwd, session_id, build_realtime_log_content(
                                    _rt_base, _turn_tool_notes, _rt_ai_text, assignee_name
                                ))
                            self._finalize(
                                run,
                                (
                                    "done",
                                    {
                                        "stop_reason": "awaiting_user_input",
                                        "full_text": "",
                                    },
                                ),
                            )
                            return
                        # POS-1890: accumulate AI text for realtime log
                        if _rt_base and event_name == "text" and isinstance(data, dict):
                            _rt_ai_text += data.get("text", "")
                            _rt_chars_since_flush += len(data.get("text", ""))
                            if _rt_chars_since_flush >= 500:
                                write_realtime_log(cwd, session_id, build_realtime_log_content(
                                    _rt_base, _turn_tool_notes, _rt_ai_text, assignee_name
                                ))
                                _rt_chars_since_flush = 0
                    # POS-1890: final realtime log flush after streaming completes
                    if _rt_base and session_id:
                        write_realtime_log(cwd, session_id, build_realtime_log_content(
                            _rt_base, _turn_tool_notes, _rt_ai_text, assignee_name
                        ))
                except json.JSONDecodeError:
                    self._finalize(
                        run, ("error", {"error": "Malformed output from claude"})
                    )
                    return
                except Exception as exc:  # noqa: BLE001 — any mid-run failure → error
                    logger.warning("chat run failed: %s", exc)
                    self._finalize(run, ("error", {"error": "Internal streaming error"}))
                    return

                proc.wait()

                # POS-1938: persist the captured claude session id for the
                # NEXT turn's --resume, when the result event carried one.
                if session_id and _claude_sid_captured[0]:
                    run.claude_session_id = _claude_sid_captured[0]
                    save_claude_session_id(cwd, session_id, _claude_sid_captured[0])

                # POS-1892: check whether the run already streamed content
                # events. When it did, a non-zero exit code is NOT a user-
                # facing error — the model used tools that may have returned
                # errors, but the conversation progressed with useful output.
                has_content_events = any(
                    name in ("text", "tool_use", "tool_result", "thinking")
                    for name, _ in run.events
                )

                # POS-1938: --resume failure detection — a non-zero exit with
                # no content produced (before we have committed this turn's
                # own terminal) most likely means the CLI could not resume the
                # saved session (e.g. it expired or was invalidated). Delete
                # the stale claude_session_id and retry once with the full
                # session-memory stdin path so the turn still succeeds.
                if (
                    not _resume_retried
                    and claude_sid
                    and proc.returncode not in (0, None)
                    and not has_content_events
                    and not run.stop_requested
                ):
                    logger.warning(
                        "--resume failed (exit %d), deleting claude_session_id "
                        "and retrying with full stdin path",
                        proc.returncode,
                    )
                    try:
                        os.unlink(claude_session_id_path(cwd, session_id))
                    except OSError:
                        pass
                    _resume_retried = True
                    continue

                if proc.returncode not in (0, None):
                    if has_content_events:
                        # Content was produced; honour the translator's
                        # terminal (done or downgraded-error-to-done) or
                        # synthesise a clean done.
                        if terminal is not None:
                            self._finalize(run, terminal)
                        else:
                            logger.info(
                                "claude exited with status %d but content was "
                                "already streamed; treating as done (POS-1892)",
                                proc.returncode,
                            )
                            self._finalize(
                                run,
                                ("done", {"stop_reason": "end_turn", "full_text": ""}),
                            )
                    elif terminal is not None and terminal[0] == "error":
                        self._finalize(run, terminal)
                    else:
                        self._finalize(
                            run,
                            (
                                "error",
                                {"error": f"claude exited with status {proc.returncode}"},
                            ),
                        )
                elif terminal is not None:
                    self._finalize(run, terminal)
                else:
                    self._finalize(
                        run, ("done", {"stop_reason": "end_turn", "full_text": ""})
                    )
                break
            finally:
                if proc is not None:
                    terminate_process_group(proc)

    def _append(self, run: _Run, event: RunEvent) -> None:
        """Append a non-terminal event and wake tailers."""
        with run.cond:
            run.events.append(event)
            run.cond.notify_all()

    def _finalize(
        self, run: _Run, terminal: RunEvent, *, status: str | None = None
    ) -> None:
        """Append the single terminal event, set status + finished_at, notify.

        Idempotent: if a terminal has already been recorded (e.g. by a racing
        :meth:`stop`), this is a no-op so a run never ends with two terminals.
        ``status`` overrides the status derived from the terminal event name
        (used so a stopped run reports ``stopped`` rather than ``done``).
        """
        name, _ = terminal
        with run.cond:
            if run.is_finished or (run.events and run.events[-1][0] in _TERMINAL_EVENTS):
                return
            run.events.append(terminal)
            if status is not None:
                run.status = status
            elif run.stop_requested:
                run.status = "stopped"
            else:
                run.status = name if name in ("done", "error") else "done"
            run.finished_at = time.time()
            try:
                QUEUE.unregister(run.run_id)
            except Exception:  # noqa: BLE001 — never break finalization
                pass
            run.cond.notify_all()

    # -- consumption -------------------------------------------------------
    def tail(self, run_id: str, from_index: int = 0) -> Iterator[RunEvent]:
        """Yield ``events[from_index:]``, blocking for more until the terminal.

        Supports multiple concurrent tailers on the same run. Returns once the
        terminal event has been yielded. Unknown/expired id raises
        :class:`RunNotFoundError`.
        """
        run = self._require(run_id)
        index = max(0, from_index)
        with run.cond:
            while True:
                while index < len(run.events):
                    event = run.events[index]
                    index += 1
                    yield event
                    if event[0] in _TERMINAL_EVENTS:
                        return
                # Defensive: a finished run with no further events (terminal
                # already yielded above) means we are done.
                if run.is_finished and index >= len(run.events):
                    return
                run.cond.wait()

    def stop(self, run_id: str) -> None:
        """Terminate the run's process group; the worker appends a stopped terminal.

        Idempotent and tolerant of an already-finished run. Unknown/expired id
        raises :class:`RunNotFoundError`.
        """
        run = self._require(run_id)
        with run.cond:
            if run.is_finished:
                return
            run.stop_requested = True
            proc = run.proc
            pre_proc = run.pre_proc
        if pre_proc is not None:
            terminate_process_group(pre_proc)
        if proc is not None:
            terminate_process_group(proc)
        # Killing the group ends the worker's stdout loop, which finalizes the
        # run; ``stop_requested`` makes that terminal a ``stopped`` outcome. We
        # also finalize here (idempotent) so a stop wins even if the worker has
        # not yet been scheduled — whichever lands first records the terminal.
        self._finalize(
            run, ("done", {"stop_reason": "stopped", "full_text": ""}), status="stopped"
        )

    def active_run_ids(self) -> list[str]:
        """Return the ids of runs whose status is still ``running``."""
        self._prune_expired()
        with self._lock:
            return [rid for rid, run in self._runs.items() if run.status == "running"]

    def get_status(self, run_id: str) -> str | None:
        """Return a run's status, or ``None`` if unknown/expired (no raise)."""
        with self._lock:
            run = self._runs.get(run_id)
        return run.status if run is not None else None

    # -- internals ---------------------------------------------------------
    def _require(self, run_id: str) -> _Run:
        self._prune_expired()
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def _prune_expired(self) -> None:
        """Drop finished runs whose retention window has elapsed (lazy sweep)."""
        now = time.time()
        with self._lock:
            expired = [
                rid
                for rid, run in self._runs.items()
                if run.finished_at is not None
                and (now - run.finished_at) > self._retention_seconds
            ]
            for rid in expired:
                del self._runs[rid]

    def shutdown_all(self) -> None:
        """Terminate every running ``claude`` subprocess for a clean server exit.

        Called during process shutdown (Starlette ``on_shutdown``). Uses
        force-kill (SIGKILL, no grace period) so the server exits immediately.
        Also destroys every per-session Claude Code PTY (POS-2219). Best-effort
        and never raises.
        """
        with self._lock:
            running = [
                run for run in self._runs.values() if not run.is_finished
            ]
        for run in running:
            try:
                with run.cond:
                    run.stop_requested = True
                    proc = run.proc
                    pre_proc = run.pre_proc
                if pre_proc is not None:
                    terminate_process_group(pre_proc, force=True)
                if proc is not None:
                    terminate_process_group(proc, force=True)
                self._finalize(
                    run,
                    ("done", {"stop_reason": "stopped", "full_text": ""}),
                    status="stopped",
                )
            except Exception as exc:  # noqa: BLE001 — cleanup must never raise
                logger.warning("shutdown_all: failed to stop run %s: %s", run.run_id, exc)

        try:
            console_pty.destroy_all_chat_ptys()
        except Exception as exc:  # noqa: BLE001 — cleanup must never raise
            logger.warning("shutdown_all: failed to destroy chat PTYs: %s", exc)

    def _enforce_cap(self) -> None:
        """Drop oldest finished runs when the registry exceeds the cap.

        Caller must hold ``self._lock``. Only finished runs are evictable —
        running runs are never dropped from under their worker/tailers.
        """
        if len(self._runs) <= self._max_retained:
            return
        finished = sorted(
            (
                (run.finished_at or 0.0, rid)
                for rid, run in self._runs.items()
                if run.is_finished
            ),
        )
        overflow = len(self._runs) - self._max_retained
        for _, rid in finished[:overflow]:
            del self._runs[rid]


# Module-level singleton used by praxis_local_api.
RUNS = RunManager()
