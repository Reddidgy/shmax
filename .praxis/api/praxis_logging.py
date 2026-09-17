#!/usr/bin/env python3
"""Shared rotating-file logging setup for the Praxis Local API (POS-1995).

All API output — wrapper lifecycle messages, uvicorn access/error lines,
library loggers, and stray ``print()`` calls — is funnelled into a single
rotating log file at ``<project>/.praxis/api/praxis.log``.

Sizing: 5 MB per file with 2 backups → a 15 MB worst case, replacing the
previously unbounded log that only ever truncated on restart.

Standard library only; this module deliberately has no third-party imports so
it can be imported before the dependency check in ``praxis_local_api.py``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 2  # praxis.log + .1 + .2 → 15 MB ceiling
LOG_FILE_NAME = "praxis.log"

# Dotted path used by the uvicorn ``dictConfig`` below.  The API directory is
# the main script's directory in both processes, so it is always on sys.path
# and a bare module name resolves.
_HANDLER_CLASS = "praxis_logging.ReopeningRotatingFileHandler"


class ReopeningRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """``RotatingFileHandler`` that closes the log file after every record.

    Two processes write to ``praxis.log``: ``praxis_wrapper.py`` and the
    ``praxis_local_api.py`` child it spawns.  Keeping the file open in both
    makes rotation unsafe — on Windows the rename fails outright while another
    handle is open, and on POSIX the process that did not rotate keeps
    appending to the renamed (and eventually unlinked) inode, which is exactly
    the unbounded growth this handler exists to prevent.

    Closing after each emit costs one open/close per line — negligible at this
    API's log volume — and guarantees every record lands in the *current*
    ``praxis.log`` no matter which process rotated last.
    """

    def __init__(self, filename, **kwargs):
        kwargs.setdefault("maxBytes", MAX_BYTES)
        kwargs.setdefault("backupCount", BACKUP_COUNT)
        kwargs.setdefault("encoding", "utf-8")
        # Always lazy: the stream is (re)opened per record and closed again.
        kwargs["delay"] = True
        super().__init__(filename, **kwargs)

    def emit(self, record):
        try:
            super().emit(record)
        finally:
            # Close the stream directly rather than calling ``self.close()``,
            # which would also unregister the handler from logging's global
            # handler list and break ``logging.shutdown()``.
            try:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
            except Exception:
                pass


class _LoggerWriter:
    """Minimal file-like shim that funnels writes into a logger.

    ``praxis_local_api.py`` prints status lines and tracebacks to stdout and
    stderr.  Under the old shell redirect those bytes went straight into
    praxis.log; with rotation in place they must go through the handler
    instead, otherwise the inherited file descriptor keeps appending to the
    rotated-away inode and the size ceiling is silently defeated.
    """

    def __init__(self, logger: logging.Logger, level: int, fallback=None) -> None:
        self._logger = logger
        self._level = level
        self._fallback = fallback
        self._buffer = ""

    def write(self, message) -> int:
        if not isinstance(message, str):
            message = str(message)
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._logger.log(self._level, line)
        return len(message)

    def flush(self) -> None:
        if self._buffer:
            line, self._buffer = self._buffer, ""
            self._logger.log(self._level, line)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        # Delegate to the real stream so anything handing stdout/stderr to a
        # subprocess still gets a usable descriptor.
        if self._fallback is None:
            raise OSError("praxis logging writer has no file descriptor")
        return self._fallback.fileno()


def log_path() -> str:
    """Absolute path of ``praxis.log`` for the project this instance serves.

    Derived from this module's own location, exactly like ``praxis.pid`` — so
    the copy provisioned into ``<project>/.praxis/api/`` always logs into that
    project and never into the PraxisOS repo root.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILE_NAME)


def build_handler() -> ReopeningRotatingFileHandler:
    """Build a rotating handler that preserves praxis.log's historical format."""
    handler = ReopeningRotatingFileHandler(log_path())
    # Bare message: wrapper "[ts] ..." lines, uvicorn's own "INFO:     ..."
    # prefixes and redirected print() output all render verbatim, so the file
    # format is byte-identical to what it was before rotation was introduced.
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def configure_root_logging(level: int = logging.INFO) -> ReopeningRotatingFileHandler:
    """Point the root logger at the rotating praxis.log handler.

    Idempotent: a second call returns the handler installed by the first.
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, ReopeningRotatingFileHandler):
            return existing
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = build_handler()
    root.addHandler(handler)
    root.setLevel(level)
    # A failing handler must never write to stderr: stderr is redirected back
    # into logging by redirect_stdio(), which would recurse.
    logging.raiseExceptions = False
    return handler


def redirect_stdio() -> None:
    """Route ``print()`` and stderr output through the rotating handler.

    Idempotent.  Call only from a ``__main__`` entry point — never at import
    time — so importing the API modules (tests, tooling) leaves stdio alone.
    """
    if isinstance(sys.stdout, _LoggerWriter):
        return
    logger = logging.getLogger("praxis")
    sys.stdout = _LoggerWriter(logger, logging.INFO, sys.__stdout__)
    sys.stderr = _LoggerWriter(logger, logging.ERROR, sys.__stderr__)


def uvicorn_log_config(level: str = "info") -> dict:
    """uvicorn ``log_config`` that routes uvicorn's loggers into praxis.log.

    Mirrors ``uvicorn.config.LOGGING_CONFIG`` one-for-one, swapping the two
    stderr/stdout StreamHandlers for the rotating file handler and disabling
    colours (ANSI escapes in a file are noise).  The formatters are uvicorn's
    own, so lines such as
    ``INFO:     127.0.0.1:0 - "GET /health HTTP/1.1" 200 OK`` render exactly as
    they always have.
    """
    file_handler = {
        "class": _HANDLER_CLASS,
        "filename": log_path(),
        "maxBytes": MAX_BYTES,
        "backupCount": BACKUP_COUNT,
        "encoding": "utf-8",
    }
    upper = level.upper()
    return {
        "version": 1,
        # Never disable the root handler installed by configure_root_logging().
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": False,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                "use_colors": False,
            },
        },
        "handlers": {
            "default": {**file_handler, "formatter": "default"},
            "access": {**file_handler, "formatter": "access"},
        },
        "loggers": {
            # propagate=False — the root handler writes the same file, so
            # propagation would duplicate every uvicorn line.
            "uvicorn": {"handlers": ["default"], "level": upper, "propagate": False},
            "uvicorn.error": {"level": upper},
            "uvicorn.access": {"handlers": ["access"], "level": upper, "propagate": False},
        },
    }
