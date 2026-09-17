"""Per-run pending message queue with disk persistence (POS-1846).

Each run's queue is stored at ``.praxis/chat/sessions/<session_id>/pending_queue.json``.
Thread-safe: all mutations are guarded by a per-run lock.

Prior to this module, the pending queue (messages typed while the AI is still
responding) was persisted ONLY on the frontend — written directly to
``full.json``.  This moves queue ownership to the backend so the frontend
becomes a thin client that POSTs/GETs/DELETEs via HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field

from chat_session_paths import session_dir

__all__ = ["SessionQueueManager", "QUEUE"]

logger = logging.getLogger(__name__)

PENDING_QUEUE_FILENAME = "pending_queue.json"


@dataclass
class _QueueState:
    session_id: str
    project_root: str
    messages: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class SessionQueueManager:
    """Per-run pending message queue with disk persistence.

    Each run's queue is stored at:
      .praxis/chat/sessions/<session_id>/pending_queue.json

    Thread-safe: all mutations are guarded by a per-run lock.
    """

    def __init__(self) -> None:
        self._queues: dict[str, _QueueState] = {}

    def register(self, run_id: str, session_id: str, project_root: str) -> None:
        """Register a run's queue. Called from RunManager.start_run().

        Creates the _QueueState entry. No disk write yet (empty queue = no file).
        """
        self._queues[run_id] = _QueueState(
            session_id=session_id, project_root=project_root
        )

    def append(self, run_id: str, message: str) -> list[str]:
        """Append a message, persist to disk, return the full queue.

        Raises KeyError if run_id is not registered.
        """
        state = self._require(run_id)
        with state.lock:
            state.messages.append(message)
            result = list(state.messages)
            self._persist(state, run_id)
            return result

    def get(self, run_id: str) -> list[str]:
        """Return the current queue. Raises KeyError if unknown."""
        state = self._require(run_id)
        with state.lock:
            return list(state.messages)

    def drain(self, run_id: str) -> list[str]:
        """Return the full queue and clear it (atomic).

        Persist the empty state (delete the file). This is what the worker
        calls when the run finishes and is ready to process queued messages
        in the next turn.
        """
        state = self._require(run_id)
        with state.lock:
            result = list(state.messages)
            state.messages.clear()
            self._delete_file(state)
            return result

    def clear(self, run_id: str) -> None:
        """Clear without returning. Delete the disk file."""
        state = self._require(run_id)
        with state.lock:
            state.messages.clear()
            self._delete_file(state)

    def remove(self, run_id: str, index: int) -> list[str]:
        """Remove the message at *index*, persist, return the updated queue.

        Raises KeyError if run_id is not registered.
        Raises IndexError if index is out of range.
        """
        state = self._require(run_id)
        with state.lock:
            del state.messages[index]
            result = list(state.messages)
            self._persist(state, run_id)
            return result

    def unregister(self, run_id: str) -> None:
        """Remove the in-memory entry. Called from RunManager._finalize().

        Best-effort: does not raise if already unregistered.
        """
        self._queues.pop(run_id, None)

    def restore(self, run_id: str, session_id: str, project_root: str) -> list[str]:
        """Read pending_queue.json from disk into memory and return it.

        Used on reattach if the backend restarted and lost in-memory state.
        """
        directory = session_dir(project_root, session_id)
        path = os.path.join(directory, PENDING_QUEUE_FILENAME)
        messages: list[str] = []
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                raw = data.get("messages")
                if isinstance(raw, list):
                    messages = [m for m in raw if isinstance(m, str)]
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("restore queue from %s failed: %s", path, exc)

        state = _QueueState(
            session_id=session_id,
            project_root=project_root,
            messages=messages,
        )
        self._queues[run_id] = state
        return list(messages)

    # -- internals ---------------------------------------------------------

    def _require(self, run_id: str) -> _QueueState:
        try:
            return self._queues[run_id]
        except KeyError:
            raise KeyError(f"run {run_id!r} is not registered") from None

    def _persist(self, state: _QueueState, run_id: str) -> None:
        """Atomically write the queue to disk. Best-effort."""
        if not state.messages:
            self._delete_file(state)
            return
        directory = session_dir(state.project_root, state.session_id)
        path = os.path.join(directory, PENDING_QUEUE_FILENAME)
        tmp_path: str | None = None
        try:
            os.makedirs(directory, exist_ok=True)
            payload = {
                "run_id": run_id,
                "messages": state.messages,
                "updated_at": time.time(),
            }
            with tempfile.NamedTemporaryFile(
                mode="w", dir=directory, delete=False, encoding="utf-8"
            ) as handle:
                tmp_path = handle.name
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except Exception as exc:  # noqa: BLE001 — queue writes must never crash a run
            logger.warning("queue persist failed for run %s: %s", run_id, exc)
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _delete_file(self, state: _QueueState) -> None:
        """Remove the disk file. Best-effort."""
        directory = session_dir(state.project_root, state.session_id)
        path = os.path.join(directory, PENDING_QUEUE_FILENAME)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("queue file delete failed: %s", exc)


# Module-level singleton, same pattern as RUNS = RunManager() in chat_runs.py.
QUEUE = SessionQueueManager()
