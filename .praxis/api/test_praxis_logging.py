"""Tests for praxis_logging — shared rotating-file logging setup (POS-1995).

Run from the ``api/`` directory:

    python3 -m pytest test_praxis_logging.py -v
"""

from __future__ import annotations

import logging
import os

import praxis_logging
from praxis_logging import ReopeningRotatingFileHandler, build_handler


def _make_logger(name: str, log_file: str, max_bytes: int, backup_count: int):
    """Build a fresh logger + rotating handler pair for a test, isolated by name."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    handler = ReopeningRotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, handler


def _cleanup(logger: logging.Logger, *handlers: ReopeningRotatingFileHandler) -> None:
    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()


def test_log_path_is_next_to_module():
    expected = os.path.join(
        os.path.dirname(os.path.abspath(praxis_logging.__file__)), "praxis.log"
    )
    assert praxis_logging.log_path() == expected


def test_rotation_caps_file_count_and_size(tmp_path):
    log_file = tmp_path / "praxis.log"
    logger, handler = _make_logger(
        "praxis_test_rotation", str(log_file), max_bytes=1024, backup_count=2
    )
    try:
        line = "x" * 90  # ~100 bytes per record once formatted with the index
        for i in range(500):
            logger.info("%s %d", line, i)
    finally:
        _cleanup(logger, handler)

    assert not (tmp_path / "praxis.log.3").exists()

    for name in ("praxis.log", "praxis.log.1", "praxis.log.2"):
        path = tmp_path / name
        if path.exists():
            # One-line slack: a record straddling the maxBytes boundary is
            # still written in full before rotation happens.
            assert path.stat().st_size <= 1024 + 200


def test_no_records_lost_across_rotation(tmp_path):
    log_file = tmp_path / "praxis.log"
    logger, handler = _make_logger(
        "praxis_test_no_loss", str(log_file), max_bytes=4096, backup_count=2
    )
    lines = [f"{i:05d}-{'a' * 30}" for i in range(250)]
    total_bytes = sum(len(line) + 1 for line in lines)
    assert total_bytes < 3 * 4096

    try:
        for line in lines:
            logger.info(line)
    finally:
        _cleanup(logger, handler)

    combined = ""
    for suffix in (".2", ".1", ""):
        path = tmp_path / f"praxis.log{suffix}"
        if path.exists():
            combined += path.read_text()

    combined_lines = [line for line in combined.split("\n") if line]
    assert combined_lines == lines


def test_two_handlers_share_one_file(tmp_path):
    log_file = tmp_path / "praxis.log"
    logger_a = logging.getLogger("praxis_test_shared_a")
    logger_b = logging.getLogger("praxis_test_shared_b")
    for logger in (logger_a, logger_b):
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.propagate = False

    handler_a = ReopeningRotatingFileHandler(
        str(log_file), maxBytes=1_000_000, backupCount=2
    )
    handler_a.setFormatter(logging.Formatter("%(message)s"))
    handler_b = ReopeningRotatingFileHandler(
        str(log_file), maxBytes=1_000_000, backupCount=2
    )
    handler_b.setFormatter(logging.Formatter("%(message)s"))
    logger_a.addHandler(handler_a)
    logger_b.addHandler(handler_b)

    try:
        for i in range(20):
            logger_a.info("a-%d", i)
            logger_b.info("b-%d", i)
    finally:
        _cleanup(logger_a, handler_a)
        _cleanup(logger_b, handler_b)

    content = log_file.read_text()
    for i in range(20):
        assert f"a-{i}" in content
        assert f"b-{i}" in content


def test_message_format_is_bare(tmp_path, monkeypatch):
    log_file = tmp_path / "praxis.log"
    monkeypatch.setattr(praxis_logging, "log_path", lambda: str(log_file))
    handler = build_handler()

    logger = logging.getLogger("praxis_test_bare_format")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)

    message = "[2026-01-01T00:00:00+0000] Starting Praxis Local API..."
    try:
        logger.info(message)
    finally:
        _cleanup(logger, handler)

    assert log_file.read_text() == message + "\n"


def test_uvicorn_log_config_shape():
    cfg = praxis_logging.uvicorn_log_config("info")

    assert cfg["disable_existing_loggers"] is False

    for handler_name in ("default", "access"):
        handler = cfg["handlers"][handler_name]
        assert handler["class"] == "praxis_logging.ReopeningRotatingFileHandler"
        assert handler["filename"] == praxis_logging.log_path()
        assert handler["maxBytes"] == praxis_logging.MAX_BYTES
        assert handler["backupCount"] == praxis_logging.BACKUP_COUNT

    assert cfg["formatters"]["default"]["fmt"] == "%(levelprefix)s %(message)s"
    assert (
        cfg["formatters"]["access"]["fmt"]
        == '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    )
    assert cfg["formatters"]["default"]["use_colors"] is False
    assert cfg["formatters"]["access"]["use_colors"] is False

    assert cfg["loggers"]["uvicorn"]["propagate"] is False
    assert cfg["loggers"]["uvicorn.access"]["propagate"] is False


def test_logger_writer_buffers_partial_lines():
    records: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("praxis_test_logger_writer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    capture = _CaptureHandler()
    logger.addHandler(capture)

    try:
        writer = praxis_logging._LoggerWriter(logger, logging.INFO)
        writer.write("partial")
        assert records == []

        writer.write(" line\n")
        assert records == ["partial line"]

        writer.write("trailing, no newline")
        assert records == ["partial line"]

        writer.flush()
        assert records == ["partial line", "trailing, no newline"]
    finally:
        logger.removeHandler(capture)
