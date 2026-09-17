"""Tests for chat_session_queue — per-run pending message queue (POS-1846).

Run from the ``api/`` directory:

    python3 -m pytest test_chat_session_queue.py -v
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from chat_session_queue import PENDING_QUEUE_FILENAME, SessionQueueManager


@pytest.fixture
def qm():
    return SessionQueueManager()


@pytest.fixture
def session_dir_path(tmp_path):
    """Create the session directory structure and return it."""
    d = tmp_path / ".praxis" / "chat" / "sessions" / "test-session"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_append_persists_and_returns_list(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    result = qm.append("run-1", "hello")
    assert result == ["hello"]

    result = qm.append("run-1", "world")
    assert result == ["hello", "world"]

    # Verify disk
    path = tmp_path / ".praxis" / "chat" / "sessions" / "test-session" / PENDING_QUEUE_FILENAME
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["messages"] == ["hello", "world"]
    assert data["run_id"] == "run-1"
    assert "updated_at" in data


def test_drain_returns_and_clears(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    qm.append("run-1", "msg1")
    qm.append("run-1", "msg2")

    drained = qm.drain("run-1")
    assert drained == ["msg1", "msg2"]

    # In-memory empty
    assert qm.get("run-1") == []

    # Disk file deleted
    path = tmp_path / ".praxis" / "chat" / "sessions" / "test-session" / PENDING_QUEUE_FILENAME
    assert not path.exists()


def test_clear_empties_and_deletes_file(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    qm.append("run-1", "msg")

    qm.clear("run-1")
    assert qm.get("run-1") == []

    path = tmp_path / ".praxis" / "chat" / "sessions" / "test-session" / PENDING_QUEUE_FILENAME
    assert not path.exists()


def test_get_unknown_run_raises_keyerror(qm):
    with pytest.raises(KeyError):
        qm.get("nonexistent")


def test_restore_reads_from_disk(qm, tmp_path):
    # Write a queue file manually
    d = tmp_path / ".praxis" / "chat" / "sessions" / "test-session"
    d.mkdir(parents=True, exist_ok=True)
    queue_file = d / PENDING_QUEUE_FILENAME
    queue_file.write_text(json.dumps({
        "run_id": "run-1",
        "messages": ["restored1", "restored2"],
        "updated_at": 1234567890.0,
    }))

    result = qm.restore("run-1", "test-session", str(tmp_path))
    assert result == ["restored1", "restored2"]
    assert qm.get("run-1") == ["restored1", "restored2"]


def test_restore_missing_file_returns_empty(qm, tmp_path):
    result = qm.restore("run-1", "test-session", str(tmp_path))
    assert result == []
    assert qm.get("run-1") == []


def test_unregister_removes_entry(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    qm.append("run-1", "msg")
    qm.unregister("run-1")

    with pytest.raises(KeyError):
        qm.get("run-1")


def test_unregister_idempotent(qm):
    qm.unregister("nonexistent")  # should not raise


def test_thread_safety(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    errors = []

    def appender(msg):
        try:
            qm.append("run-1", msg)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=appender, args=(f"msg-{i}",)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    result = qm.get("run-1")
    assert len(result) == 20
    assert set(result) == {f"msg-{i}" for i in range(20)}


def test_atomic_write_temp_file_cleaned_on_success(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    qm.append("run-1", "test")

    d = tmp_path / ".praxis" / "chat" / "sessions" / "test-session"
    # Only the queue file should exist (no leftover temp files)
    files = list(d.iterdir())
    assert len(files) == 1
    assert files[0].name == PENDING_QUEUE_FILENAME


def test_empty_queue_no_file_written(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    # No append — queue is empty
    path = tmp_path / ".praxis" / "chat" / "sessions" / "test-session" / PENDING_QUEUE_FILENAME
    assert not path.exists()


def test_remove_by_index(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    qm.append("run-1", "a")
    qm.append("run-1", "b")
    qm.append("run-1", "c")

    result = qm.remove("run-1", 1)
    assert result == ["a", "c"]
    assert qm.get("run-1") == ["a", "c"]

    # Verify disk
    path = tmp_path / ".praxis" / "chat" / "sessions" / "test-session" / PENDING_QUEUE_FILENAME
    data = json.loads(path.read_text())
    assert data["messages"] == ["a", "c"]


def test_remove_out_of_range(qm, tmp_path):
    qm.register("run-1", "test-session", str(tmp_path))
    qm.append("run-1", "only")

    with pytest.raises(IndexError):
        qm.remove("run-1", 5)

    # Queue unchanged
    assert qm.get("run-1") == ["only"]


def test_remove_unknown_run(qm):
    with pytest.raises(KeyError):
        qm.remove("nonexistent", 0)
