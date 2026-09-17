#!/usr/bin/env python3
"""Praxis API Wrapper — manages the API process with auto-restart on crash.

Starts praxis_local_api.py as a managed subprocess, monitors it, and restarts
automatically on non-zero exit or signal death.  A clean exit (code 0) stops
the wrapper.  SIGTERM/SIGINT forwarding ensures no orphaned child processes
when the wrapper itself is killed.

The inner API exposes POST /api/restart which exits with a non-zero code,
triggering a fresh subprocess from this wrapper.

Wrapper output goes through the shared rotating praxis.log handler
(``praxis_logging``) rather than raw stdout, so long-running sessions no
longer grow the log without bound (POS-1995).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time

import praxis_logging

_IS_WINDOWS = sys.platform == "win32"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_SCRIPT = os.path.join(SCRIPT_DIR, "praxis_local_api.py")
PID_FILE = os.path.join(SCRIPT_DIR, "praxis.pid")
RESTART_DELAY = 1.0

logger = logging.getLogger("praxis_wrapper")

_shutting_down = False
_child: subprocess.Popen | None = None


def _write_pidfile():
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _remove_pidfile():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def _pid_is_alive(pid: int) -> bool:
    try:
        if _IS_WINDOWS:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
            )
            return str(pid) in result.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def _kill_stale_process():
    """Kill a stale wrapper process left over from a previous run, if any."""
    try:
        if not os.path.exists(PID_FILE):
            return

        with open(PID_FILE) as f:
            content = f.read().strip()

        if not content:
            _remove_pidfile()
            return

        try:
            pid = int(content)
        except ValueError:
            _remove_pidfile()
            return

        if pid == os.getpid():
            return

        if not _pid_is_alive(pid):
            _remove_pidfile()
            return

        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 5
            while time.time() < deadline and _pid_is_alive(pid):
                time.sleep(0.1)
            if _pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)

        _remove_pidfile()
    except Exception:
        # Never let stale-process cleanup crash the wrapper.
        pass


def _shutdown(signum, _frame):
    global _shutting_down
    _shutting_down = True
    if _child and _child.poll() is None:
        if _IS_WINDOWS:
            _child.terminate()
        else:
            _child.send_signal(signum)
        try:
            _child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _child.kill()
    _remove_pidfile()
    os._exit(0)


def main():
    global _child

    # Rotating praxis.log must be live before any wrapper output (POS-1995).
    praxis_logging.configure_root_logging()

    if _IS_WINDOWS:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGBREAK, _shutdown)
    else:
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

    _kill_stale_process()
    _write_pidfile()

    python = sys.executable

    while not _shutting_down:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        logger.info("[%s] Starting Praxis Local API...", ts)

        _child = subprocess.Popen(
            [python, API_SCRIPT],
            stderr=subprocess.STDOUT,
        )

        exit_code = _child.wait()
        _child = None

        if _shutting_down:
            break

        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        if exit_code == 0:
            logger.info("[%s] API exited cleanly (code 0). Wrapper stopping.", ts)
            break

        logger.info(
            "[%s] API exited (code %s). Restarting in %.0fs...",
            ts,
            exit_code,
            RESTART_DELAY,
        )
        time.sleep(RESTART_DELAY)

    _remove_pidfile()


if __name__ == "__main__":
    main()
