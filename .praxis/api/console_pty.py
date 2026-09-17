"""Persistent, shared PTY shell session per project root.

Backs AI Chat console mode (POS-2153): a real interactive shell (bash) that
lives in the API process itself, independent of any frontend connection. The
session survives page refreshes and dropped SSE streams — the frontend just
reattaches to whatever is already running. There is exactly one session per
resolved project root, shared by every chat session and every MCP
``execute_command`` call, which is what makes it a *single shared terminal*
per project rather than a per-request subprocess.

Stdlib only — no new pip dependencies.
"""

from __future__ import annotations

import codecs
import logging
import os
import re
import shutil
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import queue as _queue

MAX_OUTPUT_CHARS = 400_000  # per command record
MAX_COMMANDS = 200  # ring of command records
MAX_EVENTS = 4000  # ring of stream events
MAX_RAW_RING_CHARS = 500_000  # raw output ring for WebSocket replay
DEFAULT_COLS = 120
DEFAULT_ROWS = 40
READY_TIMEOUT_S = 8.0
# NOTE: raw \x01 / \x02 (as originally specified) do NOT survive bash's PS1
# rendering — bash's decode_prompt_string() treats those two byte values as
# RL_PROMPT_START_IGNORE / RL_PROMPT_END_IGNORE (the internal markers for
# "\[ ... \]" non-printing regions) and strips them from the rendered prompt
# even when inserted as literal bytes and even with --noediting. Verified
# against /bin/bash 3.2 (macOS default) and confirmed \x1e/\x1f (ASCII RS/US)
# pass through untouched, so those are used here instead.
_PROMPT_RE = re.compile("\x1ePRXP:(-?\\d+)\x1f")

# zsh setup, sourced from a throwaway $ZDOTDIR/.zshenv (see `_spawn`) rather
# than typed into the live terminal: .zshenv runs *before* zsh's line editor
# (ZLE) ever starts, so "unsetopt zle" actually takes effect before there is
# any ZLE session to wedge. Typing the same line as injected keystrokes after
# startup (while ZLE is already live) makes zsh echo/redraw them
# character-by-character and the shell never reaches a prompt — verified
# against zsh 5.9 (macOS default). PROMPT_CR/PROMPT_SP are unset too: without
# that, zsh prints its "%" end-of-output indicator before every prompt
# (whether or not the previous line ended in a newline), polluting every
# command's captured output.
_ZSH_SETUP = (
    "unsetopt PROMPT_CR PROMPT_SP 2>/dev/null\n"
    "setopt PROMPT_SUBST 2>/dev/null\n"
    "precmd() { :; }\n"
    "preexec() { :; }\n"
    "precmd_functions=()\n"
    "preexec_functions=()\n"
    "TRAPINT() { return $(( 128 + $1 )); }\n"
    "PS1='%{\x1ePRXP:%?\x1f%}%F{green}%~%f%# '\n"
    "PS2=''\n"
)
# Re-applied after sourcing the user's ~/.zshrc (which may re-enable zle,
# PROMPT_SP, or install its own precmd/preexec hooks e.g. via a framework).
_ZSH_RESET_CMD = (
    "unsetopt PROMPT_CR PROMPT_SP 2>/dev/null; "
    "setopt PROMPT_SUBST 2>/dev/null; "
    "precmd() { :; }; preexec() { :; }; precmd_functions=(); preexec_functions=(); "
    "TRAPINT() { return $(( 128 + $1 )); }; "
)

_PTY_IMPORT_ERROR: str | None = None
try:
    import fcntl
    import pty
    import struct
    import termios
except Exception as exc:  # Windows
    _PTY_IMPORT_ERROR = f"PTY is not available on this platform: {exc}"


def is_supported() -> bool:
    return _PTY_IMPORT_ERROR is None


# Windows ConPTY support for Claude Code terminal (ChatPtySession only).
# The shared shell PtySession (console mode) still requires Unix PTY.
_CHAT_PTY_IMPORT_ERROR: str | None = None
if os.name == "nt":
    try:
        from winpty import PTY as WinPTY
    except Exception as _wpty_exc:
        _CHAT_PTY_IMPORT_ERROR = f"pywinpty is not available: {_wpty_exc}"
else:
    # On Unix, ChatPtySession uses the same PTY primitives as PtySession.
    _CHAT_PTY_IMPORT_ERROR = _PTY_IMPORT_ERROR


def is_chat_supported() -> bool:
    """Whether the Claude Code chat terminal can run on this platform."""
    return _CHAT_PTY_IMPORT_ERROR is None


class CommandRecord:
    """A single command run in the terminal and its (possibly still-growing) output."""

    def __init__(
        self,
        id: str,
        command: str,
        output: str = "",
        exit_code: int | None = None,
        running: bool = True,
        started_at: float = 0.0,
        finished_at: float | None = None,
        truncated: bool = False,
    ) -> None:
        self.id = id
        self.command = command
        self.output = output
        self.exit_code = exit_code
        self.running = running
        self.started_at = started_at
        self.finished_at = finished_at
        self.truncated = truncated

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "output": self.output,
            "exit_code": self.exit_code,
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "truncated": self.truncated,
        }


class PtySession:
    """A single persistent bash shell attached to a PTY, with buffered history."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._events: list[tuple[str, dict]] = []
        self._events_base = 0
        self._commands: list[CommandRecord] = []
        self._current: CommandRecord | None = None
        self._ready = False
        self._closed = False
        self._counter = 0
        self._history: list[str] = []
        self._carry = ""
        self._profile_sourcing = False
        self._shell_name = "bash"
        self._zdotdir: str | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._raw_subscribers: list[_queue.Queue] = []
        self._raw_sub_lock = threading.Lock()
        self._raw_ring: list[str] = []
        self._raw_ring_chars = 0
        self._spawn()

    def subscribe_raw(self) -> _queue.Queue:
        """Subscribe to raw PTY output for WebSocket forwarding."""
        q = _queue.Queue(maxsize=2000)
        with self._raw_sub_lock:
            self._raw_subscribers.append(q)
        return q

    def unsubscribe_raw(self, q: _queue.Queue) -> None:
        with self._raw_sub_lock:
            try:
                self._raw_subscribers.remove(q)
            except ValueError:
                pass

    def get_raw_buffer(self) -> str:
        """Return the accumulated raw output ring (for replay on reconnect)."""
        with self._raw_sub_lock:
            return "".join(self._raw_ring)

    def _push_raw(self, text: str) -> None:
        """Push raw decoded PTY output to the ring buffer and all subscribers."""
        with self._raw_sub_lock:
            self._raw_ring.append(text)
            self._raw_ring_chars += len(text)
            while self._raw_ring_chars > MAX_RAW_RING_CHARS and self._raw_ring:
                removed = self._raw_ring.pop(0)
                self._raw_ring_chars -= len(removed)
            subs = list(self._raw_subscribers)
        for q in subs:
            try:
                q.put_nowait(text)
            except _queue.Full:
                pass

    @staticmethod
    def _child_preexec() -> None:
        """Acquire the PTY slave (fd 0 after dup2) as the controlling terminal.

        Runs in the child process after setsid() (start_new_session=True) and
        after the slave fd has been dup2'd to stdin/stdout/stderr.  Without
        TIOCSCTTY, the child has no controlling terminal, and programs that
        open /dev/tty for interactive prompts (sudo, ssh, gpg) fail with
        "a terminal is required to read the password" (POS-2162).
        """
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    def _spawn(self) -> None:
        user_shell = os.environ.get("SHELL", "")
        shell_basename = os.path.basename(user_shell).lower() if user_shell else ""

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["PAGER"] = "cat"
        env["GIT_PAGER"] = "cat"
        env.pop("PROMPT_COMMAND", None)
        env.pop("BASH_ENV", None)

        setup_via_write: str | None = None

        if shell_basename == "zsh":
            shell = shutil.which("zsh") or user_shell or "/bin/zsh"
            self._shell_name = "zsh"
            # -d: skip only the *global* rc files (/etc/zshenv & co) - the
            # user's own $ZDOTDIR/.zshenv (written below) is still sourced
            # automatically, and crucially *before* zle (zsh's line editor)
            # ever starts. That's the only way to disable it cleanly: writing
            # "unsetopt zle" as injected keystrokes after startup targets an
            # already-live ZLE session, which echoes/redraws the raw bytes
            # character-by-character and the shell never reaches a prompt.
            # Verified against zsh 5.9 (macOS default).
            argv = [shell, "-d", "-i"]
            self._zdotdir = tempfile.mkdtemp(prefix="praxis-zdotdir-")
            with open(os.path.join(self._zdotdir, ".zshenv"), "w") as f:
                f.write(_ZSH_SETUP)
            env["ZDOTDIR"] = self._zdotdir
        else:
            shell = shutil.which("bash") or "/bin/bash"
            self._shell_name = "bash"
            # --noediting: without it, GNU readline mangles the raw control
            # bytes we inject for the PS1 setup line (POS-2153 invariant).
            argv = [shell, "--norc", "--noprofile", "-i"]
            # Use printf to construct sentinel bytes so they are never sent
            # as literal \x1e/\x1f through GNU readline, which binds \x1f
            # to undo and would consume the byte before bash sees it.
            setup_via_write = (
                "unset PROMPT_COMMAND PS0; "
                "PS1=\"\\[$(printf '\\036')PRXP:\\$?$(printf '\\037')\\]"
                "\\[\\e[1;32m\\]\\w\\[\\e[0m\\]\\$ \"; PS2=''\n"
            )

        master, slave = pty.openpty()
        fcntl.ioctl(
            master,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", DEFAULT_ROWS, DEFAULT_COLS, 0, 0),
        )

        self._proc = subprocess.Popen(
            argv,
            cwd=self.cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            start_new_session=True,
            preexec_fn=PtySession._child_preexec,
        )
        os.close(slave)
        self._master = master

        threading.Thread(target=self._reader, daemon=True).start()

        # bash has no startup-file hook that runs early enough to be worth
        # using here, so its sentinel PS1 is still set by writing directly to
        # the pty (safe because --noediting keeps readline out of the way).
        # zsh's equivalent setup already ran via $ZDOTDIR/.zshenv above.
        if setup_via_write is not None:
            os.write(self._master, setup_via_write.encode())

        with self._lock:
            self._append_event("session", {"status": "started", "cwd": self.cwd})

    def _source_profile(self) -> None:
        """Source the user's shell profile after the initial sentinel is set.

        Output from the profile is redirected to /dev/null so it never reaches
        the sentinel parser.  After sourcing, the sentinel PS1 and stty settings
        are re-applied to undo anything the profile overwrote.

        IMPORTANT: the sentinel bytes \x1e (RS) and \x1f (US) must never be
        sent as literal bytes through the interactive terminal input stream.
        ZLE (zsh) binds \x1e to set-mark-command and \x1f to undo; GNU
        readline (bash) binds \x1f to undo.  Both consume the bytes before
        the shell parser sees them, corrupting the PS1 value and preventing
        the second sentinel prompt from ever appearing — which leaves
        ``_ready`` permanently False and blocks all WebSocket connections.

        Fix: for zsh, write the reset commands to a file and ``source`` it
        (file sourcing bypasses ZLE); for bash, use ``printf`` command
        substitution to produce the bytes at shell-evaluation time rather
        than at input-reading time (POS-2168 bugfix).
        """
        if self._shell_name == "zsh":
            # Write profile-sourcing + PS1-reset to a file and source it.
            # File contents are parsed by the shell, not ZLE, so the
            # literal \x1e/\x1f bytes in the PS1 value survive intact.
            reset_file = os.path.join(self._zdotdir, ".praxis-reset")
            with open(reset_file, "w") as f:
                f.write("{ [ -f ~/.zshrc ] && source ~/.zshrc; } >/dev/null 2>&1\n")
                f.write(_ZSH_RESET_CMD + "\n")
                f.write("PS1='%{\x1ePRXP:%?\x1f%}%F{green}%~%f%# '\n")
                f.write("PS2=''\n")
            cmd = "source " + shlex.quote(reset_file) + "\n"
        else:
            profile = "~/.bashrc"
            reset = "unset PROMPT_COMMAND PS0; "
            # Use printf to construct sentinel bytes — same readline
            # bypass rationale as the zsh file-source approach above.
            ps1_line = (
                "PS1=\"\\[$(printf '\\036')PRXP:\\$?$(printf '\\037')\\]"
                "\\[\\e[1;32m\\]\\w\\[\\e[0m\\]\\$ \""
            )
            cmd = (
                f"{{ [ -f {profile} ] && source {profile}; }} >/dev/null 2>&1; "
                f"{reset}"
                f"{ps1_line}; PS2=''\n"
            )
        os.write(self._master, cmd.encode())

    def _reader(self) -> None:
        while True:
            try:
                data = os.read(self._master, 65536)
            except OSError:
                break
            if not data:
                break
            text = self._decoder.decode(data)
            with self._lock:
                was_ready = self._ready
                self._ingest(text)
            # Push to raw subscribers only after session is ready
            # (pre-ready output is shell setup noise)
            if self._ready:
                self._push_raw(text)

        # Notify subscribers of EOF
        with self._raw_sub_lock:
            subs = list(self._raw_subscribers)
        for q in subs:
            try:
                q.put_nowait(None)
            except _queue.Full:
                pass

        with self._lock:
            self._closed = True
            if self._current is not None:
                rec = self._current
                rec.exit_code = -1
                rec.running = False
                rec.finished_at = time.time()
                self._current = None
            self._append_event("session", {"status": "closed"})
            self._cond.notify_all()

    def _ingest(self, text: str) -> None:
        buf = self._carry + text
        self._carry = ""

        pos = 0
        for m in _PROMPT_RE.finditer(buf):
            self._emit_output(buf[pos:m.start()])
            self._on_prompt(int(m.group(1)))
            pos = m.end()

        tail = buf[pos:]
        if "\x1e" in tail:
            idx = tail.rindex("\x1e")
            self._emit_output(tail[:idx])
            self._carry = tail[idx:]
            if len(self._carry) > 64:
                # Not actually a marker — flush it as ordinary output.
                self._emit_output(self._carry)
                self._carry = ""
        else:
            self._emit_output(tail)

    def _on_prompt(self, code: int) -> None:
        if not self._ready:
            if not self._profile_sourcing:
                # First prompt — basic shell ready. Source the user's profile
                # before declaring the session fully ready.
                self._profile_sourcing = True
                self._current = None
                self._source_profile()
                return
            # Second prompt — profile sourcing complete, fully ready.
            self._ready = True
            self._current = None
            self._cond.notify_all()
            return

        if self._current is not None:
            rec = self._current
            rec.exit_code = code
            rec.running = False
            rec.finished_at = time.time()
            self._append_event(
                "exit",
                {
                    "id": rec.id,
                    "code": rec.exit_code,
                    "finished_at": rec.finished_at,
                    "truncated": rec.truncated,
                },
            )
            self._current = None
            self._cond.notify_all()

    def _emit_output(self, text: str) -> None:
        if not text:
            return
        if not self._ready:
            return

        if self._current is None:
            self._current = self._new_record("")

        rec = self._current
        if rec.truncated:
            return

        appended = text
        if len(rec.output) + len(appended) > MAX_OUTPUT_CHARS:
            remaining = MAX_OUTPUT_CHARS - len(rec.output)
            remaining = max(remaining, 0)
            appended = appended[:remaining]
            rec.output += appended
            rec.truncated = True
            marker = "\n… output truncated …\n"
            rec.output += marker
            appended += marker
        else:
            rec.output += appended

        if not appended:
            return

        self._append_event("chunk", {"id": rec.id, "text": appended})

    def _new_record(self, command: str) -> CommandRecord:
        self._counter += 1
        rec = CommandRecord(
            id=f"c{self._counter}",
            command=command,
            output="",
            exit_code=None,
            running=True,
            started_at=time.time(),
            finished_at=None,
            truncated=False,
        )
        self._commands.append(rec)
        if len(self._commands) > MAX_COMMANDS:
            del self._commands[: len(self._commands) - MAX_COMMANDS]
        self._append_event(
            "command", {"id": rec.id, "command": command, "started_at": rec.started_at}
        )
        return rec

    def _append_event(self, name: str, payload: dict) -> None:
        self._events.append((name, payload))
        if len(self._events) > MAX_EVENTS:
            overflow = len(self._events) - MAX_EVENTS
            del self._events[:overflow]
            self._events_base += overflow
        self._cond.notify_all()

    def wait_ready(self, timeout: float = READY_TIMEOUT_S) -> bool:
        with self._cond:
            self._cond.wait_for(lambda: self._ready or self._closed, timeout=timeout)
            return self._ready

    def is_alive(self) -> bool:
        return not self._closed and self._proc.poll() is None

    def run_command(self, command: str) -> dict:
        command = command.rstrip("\n")
        if not command:
            return {"ok": False, "error": "command is required"}

        if not self.wait_ready():
            return {"ok": False, "error": "Terminal session is not ready"}

        with self._lock:
            if self._current is not None and self._current.command:
                return {
                    "ok": False,
                    "busy": True,
                    "error": "A command is already running. Press Ctrl+C to interrupt.",
                }
            # Clear any background (unnamed) record from interactive typing
            if self._current is not None:
                rec = self._current
                rec.running = False
                rec.exit_code = None
                rec.finished_at = time.time()
                self._current = None

            rec = self._new_record(command)
            self._current = rec

            if not self._history or self._history[-1] != command:
                self._history.append(command)
                if len(self._history) > 200:
                    del self._history[: len(self._history) - 200]

            os.write(self._master, (command + "\n").encode())

            return {"ok": True, "command": rec.to_dict()}

    def interrupt(self) -> dict:
        """Send SIGINT to the running command *and* to the shell itself.

        Signalling only the foreground process group (which is what writing
        \\x03 to the pty does, via the line discipline) kills the running
        child but leaves bash 3.2 — the macOS system shell — happily
        executing the remainder of the command list: ``echo a; sleep 30;
        echo b`` interrupted mid-sleep still prints ``b`` and reports exit
        code 0. Signalling the shell process as well makes it abort the rest
        of the list and return to the prompt with a non-zero status, which is
        the behaviour a real terminal shows. Verified against
        /bin/bash 3.2.57 on macOS.
        """
        errors: list[str] = []
        delivered = False

        try:
            fg_pgrp = os.tcgetpgrp(self._master)
        except OSError as exc:
            fg_pgrp = -1
            errors.append(f"tcgetpgrp: {exc}")

        try:
            shell_pgrp = os.getpgid(self._proc.pid)
        except Exception as exc:  # process already gone
            shell_pgrp = -1
            errors.append(f"getpgid: {exc}")

        if fg_pgrp > 0:
            try:
                os.killpg(fg_pgrp, signal.SIGINT)
                delivered = True
            except Exception as exc:
                errors.append(f"killpg({fg_pgrp}): {exc}")

        # Only needed when a command is actually in the foreground; when the
        # shell is idle it already *is* the foreground group and was signalled
        # above.
        if shell_pgrp > 0 and shell_pgrp != fg_pgrp:
            if fg_pgrp <= 0:
                # No valid foreground group — zsh without ZLE/job-control
                # returns 0 from tcgetpgrp.  Children run in the shell's own
                # process group, so killpg on that group reaches them all.
                try:
                    os.killpg(shell_pgrp, signal.SIGINT)
                    delivered = True
                except Exception as exc:
                    errors.append(f"killpg({shell_pgrp}): {exc}")
            else:
                try:
                    os.kill(self._proc.pid, signal.SIGINT)
                    delivered = True
                except Exception as exc:
                    errors.append(f"kill({self._proc.pid}): {exc}")

        # Always write \x03 (ETX) to the PTY as well: the terminal's line
        # discipline converts it to SIGINT through its own path, which is
        # the "natural" signal delivery a real Ctrl+C press would use.
        # For zsh without ZLE this is required — direct signals alone do
        # not trigger the prompt redisplay that the sentinel parser depends
        # on.  For bash it is harmless (redundant with the killpg above).
        try:
            os.write(self._master, b"\x03")
            delivered = True
        except OSError as exc:
            errors.append(f"write ETX: {exc}")

        if delivered:
            return {"ok": True}
        return {"ok": False, "error": "; ".join(errors) or "interrupt failed"}

    def write_stdin(self, text: str) -> dict:
        """Write raw input to the PTY for a running interactive process.

        Used to forward user-typed stdin (e.g. answering a REPL prompt,
        providing a password, responding to a confirmation dialog) to
        whatever process is currently running in the shared terminal.
        Unlike ``run_command``, this does not create a new command record —
        it writes directly to the PTY master fd.
        """
        if not self.wait_ready():
            return {"ok": False, "error": "Terminal session is not ready"}

        if self._closed:
            return {"ok": False, "error": "Terminal session is closed"}

        try:
            os.write(self._master, text.encode())
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def resize(self, cols: int, rows: int) -> dict:
        """Resize the PTY to *cols*×*rows*."""
        with self._lock:
            if not self.is_alive():
                return {"ok": False, "error": "session is dead"}
            try:
                fcntl.ioctl(
                    self._master,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ok": True,
                "supported": True,
                "ready": self._ready,
                "alive": self.is_alive(),
                "cwd": self.cwd,
                "next_event": self._events_base + len(self._events),
                "commands": [r.to_dict() for r in self._commands],
                "history": list(self._history),
                "shell": self._shell_name,
                "live_cwd": self._get_live_cwd(),
            }

    def events_since(self, index: int) -> dict:
        with self._lock:
            if index < self._events_base:
                return {
                    "resync": True,
                    "next_event": self._events_base + len(self._events),
                    "events": [],
                }
            offset = index - self._events_base
            items = self._events[offset:]
            return {
                "resync": False,
                "next_event": self._events_base + len(self._events),
                "events": [{"name": n, "data": d} for n, d in items],
            }

    def wait_for_events(self, index: int, timeout: float) -> None:
        with self._cond:
            self._cond.wait_for(
                lambda: self._events_base + len(self._events) > index or self._closed,
                timeout=timeout,
            )

    def wait_command(self, command_id: str, timeout: float) -> CommandRecord | None:
        with self._cond:
            deadline = time.time() + timeout

            def _find() -> CommandRecord | None:
                for r in self._commands:
                    if r.id == command_id:
                        return r
                return None

            rec = _find()
            if rec is None:
                return None

            while rec.running:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
                rec = _find()
                if rec is None:
                    return None

            return rec

    def close(self) -> None:
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            os.close(self._master)
        except Exception:
            pass
        with self._lock:
            self._closed = True
            self._cond.notify_all()

    def _get_live_cwd(self) -> str:
        """Resolve the shell's current working directory."""
        pid = self._proc.pid
        # Linux: /proc/PID/cwd symlink
        proc_link = f"/proc/{pid}/cwd"
        try:
            return os.readlink(proc_link)
        except (OSError, FileNotFoundError):
            pass
        # macOS: lsof
        try:
            out = subprocess.check_output(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                if line.startswith("n/"):
                    return line[1:]
        except Exception:
            pass
        return self.cwd

    def complete(self, line: str) -> dict:
        """Return tab-completion candidates for the current command line."""
        MAX_COMPLETIONS = 100

        stripped = line.rstrip()
        words = stripped.split()
        completing_first_word = not words or (len(words) == 1 and not line.endswith(" "))

        if completing_first_word:
            prefix = words[0] if words else ""
            comp_flag = "-c"  # commands: builtins + executables + keywords
        else:
            prefix = words[-1] if not line.endswith(" ") else ""
            comp_flag = "-f"  # files

        comp_type = "command" if completing_first_word else "file"
        live_cwd = self._get_live_cwd()
        bash = shutil.which("bash") or "/bin/bash"

        try:
            cmd = f"compgen {comp_flag} -- {shlex.quote(prefix)} 2>/dev/null"
            result = subprocess.run(
                [bash, "-c", cmd],
                capture_output=True,
                text=True,
                timeout=3,
                cwd=live_cwd,
            )
            raw = result.stdout.strip()
            if not raw:
                return {"ok": True, "completions": [], "prefix": prefix, "type": comp_type}

            candidates = sorted(set(raw.splitlines()))

            # For file completions, append '/' to directories.
            if comp_flag == "-f":
                expanded: list[str] = []
                for c in candidates:
                    full = os.path.join(live_cwd, c) if not os.path.isabs(c) else c
                    if os.path.isdir(full):
                        expanded.append(c if c.endswith("/") else c + "/")
                    else:
                        expanded.append(c)
                candidates = expanded

            return {
                "ok": True,
                "completions": candidates[:MAX_COMPLETIONS],
                "prefix": prefix,
                "type": comp_type,
            }
        except Exception as exc:
            return {"ok": False, "completions": [], "error": str(exc)}


_sessions: dict[str, PtySession] = {}
_registry_lock = threading.Lock()


def get_session(cwd: str) -> PtySession:
    with _registry_lock:
        session = _sessions.get(cwd)
        if session is None or not session.is_alive():
            session = PtySession(cwd)
            _sessions[cwd] = session
        return session


def snapshot(cwd: str) -> dict:
    if not is_supported():
        return {"ok": False, "supported": False, "chat_supported": is_chat_supported(), "error": _PTY_IMPORT_ERROR}
    try:
        result = get_session(cwd).snapshot()
        result["chat_supported"] = is_chat_supported()
        return result
    except Exception as exc:
        return {"ok": False, "supported": True, "chat_supported": is_chat_supported(), "error": str(exc)}


def run_command(cwd: str, command: str) -> dict:
    if not is_supported():
        return {"ok": False, "supported": False, "error": _PTY_IMPORT_ERROR}
    try:
        return get_session(cwd).run_command(command)
    except Exception as exc:
        return {"ok": False, "supported": True, "error": str(exc)}


def interrupt(cwd: str) -> dict:
    if not is_supported():
        return {"ok": False, "supported": False, "error": _PTY_IMPORT_ERROR}
    try:
        return get_session(cwd).interrupt()
    except Exception as exc:
        return {"ok": False, "supported": True, "error": str(exc)}


def write_stdin(cwd: str, text: str) -> dict:
    if not is_supported():
        return {"ok": False, "supported": False, "error": _PTY_IMPORT_ERROR}
    try:
        return get_session(cwd).write_stdin(text)
    except Exception as exc:
        return {"ok": False, "supported": True, "error": str(exc)}


def resize(cwd: str, cols: int, rows: int) -> dict:
    if _PTY_IMPORT_ERROR:
        return {"ok": False, "supported": False, "error": _PTY_IMPORT_ERROR}
    return get_session(cwd).resize(cols, rows)


def reset(cwd: str) -> dict:
    if not is_supported():
        return {"ok": False, "supported": False, "error": _PTY_IMPORT_ERROR}
    try:
        with _registry_lock:
            existing = _sessions.get(cwd)
            if existing is not None:
                existing.close()
            session = PtySession(cwd)
            _sessions[cwd] = session
        return session.snapshot()
    except Exception as exc:
        return {"ok": False, "supported": True, "error": str(exc)}


def events_since(cwd: str, index: int) -> dict:
    if not is_supported():
        return {"ok": False, "supported": False, "error": _PTY_IMPORT_ERROR}
    try:
        return get_session(cwd).events_since(index)
    except Exception as exc:
        return {"ok": False, "supported": True, "error": str(exc)}


def wait_for_events(cwd: str, index: int, timeout: float) -> None:
    if not is_supported():
        return
    try:
        get_session(cwd).wait_for_events(index, timeout)
    except Exception:
        return


def execute_and_wait(cwd: str, command: str, timeout_seconds: int = 120) -> dict:
    if not is_supported():
        return {"ok": False, "supported": False, "error": _PTY_IMPORT_ERROR}
    try:
        session = get_session(cwd)
        result = session.run_command(command)
        if not result.get("ok"):
            return result

        command_id = result["command"]["id"]
        rec = session.wait_command(command_id, timeout_seconds)
        if rec is None:
            return {"ok": False, "supported": True, "error": "command record not found"}

        out = {
            "ok": True,
            "id": rec.id,
            "command": rec.command,
            "output": rec.output,
            "exit_code": rec.exit_code,
            "running": rec.running,
            "truncated": rec.truncated,
        }
        if rec.running:
            out["error"] = (
                f"Command still running after {timeout_seconds}s "
                "(it keeps running in the shared terminal)"
            )
        return out
    except Exception as exc:
        return {"ok": False, "supported": True, "error": str(exc)}


def complete(cwd: str, line: str) -> dict:
    if not is_supported():
        return {"ok": False, "completions": [], "error": _PTY_IMPORT_ERROR}
    try:
        return get_session(cwd).complete(line)
    except Exception as exc:
        return {"ok": False, "completions": [], "error": str(exc)}


def get_session_info(cwd: str) -> dict:
    """Return shell name and live working directory for the toolbar."""
    if not is_supported():
        return {"ok": False, "error": _PTY_IMPORT_ERROR}
    try:
        session = get_session(cwd)
        return {
            "ok": True,
            "shell": session._shell_name,
            "cwd": session._get_live_cwd(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Per-chat-session Claude Code PTYs (POS-2219)
# ---------------------------------------------------------------------------
# Independent from the shared shell PtySession registry above (untouched by
# this section): raw PTY, no sentinel prompt, no profile sourcing. One
# ChatPtySession per (project_root, session_id) running `claude` interactively.

MAX_CHAT_PTYS_PER_PROJECT = 10
FIRST_MESSAGE_OUTPUT_TIMEOUT_S = 20.0   # wait for Claude Code's first paint
FIRST_MESSAGE_SETTLE_S = 2.0            # then let the TUI finish rendering
CHAT_PTY_TERM_GRACE_S = 1.5
_IS_WINDOWS = os.name == "nt"
_chat_logger = logging.getLogger(__name__)
CHAT_PTY_IDLE_TIMEOUT_S = 1800  # 30 minutes


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate *proc* and its process group/tree. Platform-aware, never raises."""
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=CHAT_PTY_TERM_GRACE_S)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    finally:
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


_OSC_TITLE_RE = re.compile(r'\x1b\]0;(.*?)(?:\x07|\x1b\\)')


class ChatPtySession:
    """A single ``claude`` interactive TUI attached to a raw PTY.

    Unlike :class:`PtySession`, there is no sentinel prompt parsing and no
    shell profile sourcing — this spawns ``claude`` directly and forwards raw
    bytes both ways, matching what a real terminal emulator would do.
    """

    def __init__(
        self,
        cwd: str,
        argv: list[str],
        env_overrides: dict[str, str] | None = None,
        session_dir: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.argv = argv
        self._session_dir: str | None = session_dir
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.last_ui_touch = self.created_at
        self._closed = False
        self._exit_code: int | None = None
        self._lock = threading.Lock()
        self._raw_subscribers: list[_queue.Queue] = []
        self._raw_sub_lock = threading.Lock()
        self._raw_ring: list[str] = []
        self._raw_ring_chars = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._first_output = threading.Event()
        self._first_message_sent = False
        self._first_message_delivered = threading.Event()  # POS-2429
        self._claude_state: str = 'unknown'  # 'idle' | 'busy' | 'unknown'
        self._spawn(env_overrides)

    def subscribe_raw(self) -> _queue.Queue:
        q = _queue.Queue(maxsize=2000)
        with self._raw_sub_lock:
            self._raw_subscribers.append(q)
        return q

    def unsubscribe_raw(self, q: _queue.Queue) -> None:
        with self._raw_sub_lock:
            try:
                self._raw_subscribers.remove(q)
            except ValueError:
                pass

    def get_raw_buffer(self) -> str:
        with self._raw_sub_lock:
            return "".join(self._raw_ring)

    def _push_raw(self, text: str) -> None:
        with self._raw_sub_lock:
            self._raw_ring.append(text)
            self._raw_ring_chars += len(text)
            while self._raw_ring_chars > MAX_RAW_RING_CHARS and self._raw_ring:
                removed = self._raw_ring.pop(0)
                self._raw_ring_chars -= len(removed)
            subs = list(self._raw_subscribers)
        for q in subs:
            try:
                q.put_nowait(text)
            except _queue.Full:
                pass
        # Parse OSC title sequences to detect Claude's busy/idle state.
        # State transitions create the on-disk ``idle`` marker (POS-2273).
        # The marker is NEVER deleted here — only ``write_stdin`` (real user
        # input) clears it.  This ensures the marker survives PTY recreation
        # on session open so task-card indicators stay correct (POS-2284).
        for m in _OSC_TITLE_RE.finditer(text):
            title = m.group(1)
            if title:
                ch = title[0]
                if ch == '✳':
                    if self._claude_state != 'idle':
                        self._claude_state = 'idle'
                        self._create_idle_marker()
                elif ch in ('◐', '◑'):
                    if self._claude_state != 'busy':
                        self._claude_state = 'busy'

    def _spawn(self, env_overrides: dict[str, str] | None) -> None:
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        # Claude Code refuses to start nested when CLAUDECODE is set (we are
        # ourselves potentially running inside a `claude` session).
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        env.pop("PROMPT_COMMAND", None)
        if env_overrides:
            env.update(env_overrides)

        if _IS_WINDOWS:
            self._winpty = WinPTY(DEFAULT_COLS, DEFAULT_ROWS)
            env_block = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0\0"
            # cmdline is *arguments only* — pywinpty internally prepends appname,
            # so passing argv[0] again would make the exe path appear as a
            # positional arg to claude (shown as the first "message").
            cmdline = subprocess.list2cmdline(self.argv[1:]) if len(self.argv) > 1 else None
            self._winpty.spawn(self.argv[0], cmdline=cmdline, cwd=self.cwd, env=env_block)
            self._master = None  # type: ignore[assignment]
            self._proc = None  # type: ignore[assignment]
        else:
            master, slave = pty.openpty()
            fcntl.ioctl(
                master,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", DEFAULT_ROWS, DEFAULT_COLS, 0, 0),
            )

            self._proc = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                close_fds=True,
                start_new_session=True,
                preexec_fn=PtySession._child_preexec,
            )
            os.close(slave)
            self._master = master
            self._winpty = None  # type: ignore[assignment]

        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        if _IS_WINDOWS:
            self._reader_win()
        else:
            self._reader_unix()

    def _reader_unix(self) -> None:
        while True:
            try:
                data = os.read(self._master, 65536)
            except OSError:
                break
            if not data:
                break
            text = self._decoder.decode(data)
            self.last_activity = time.time()
            self._first_output.set()
            self._push_raw(text)

        try:
            self._exit_code = self._proc.wait(timeout=5)
        except Exception:
            self._exit_code = self._proc.poll()

        self._reader_cleanup()

    def _reader_win(self) -> None:
        while True:
            try:
                text = self._winpty.read()
                if text:
                    self.last_activity = time.time()
                    self._first_output.set()
                    self._push_raw(text)
                else:
                    if not self._winpty.isalive():
                        break
                    time.sleep(0.01)
            except Exception:
                break

        try:
            self._exit_code = self._winpty.get_exitstatus()
        except Exception:
            self._exit_code = None

        self._reader_cleanup()

    def _reader_cleanup(self) -> None:
        self._closed = True

        # When the process exits, ensure the ``idle`` marker exists so the
        # frontend knows the agent is done.  Without this, a process that
        # exits before emitting the idle OSC title (``✳``) — or one killed
        # by the idle reaper — would leave no ``idle`` file, causing the
        # spinning-border indicator to persist indefinitely (POS-2284).
        self._create_idle_marker()

        with self._raw_sub_lock:
            subs = list(self._raw_subscribers)
        for q in subs:
            try:
                q.put_nowait(None)
            except _queue.Full:
                pass

        self._first_output.set()

    def is_alive(self) -> bool:
        if self._closed:
            return False
        if _IS_WINDOWS:
            return self._winpty.isalive()
        return self._proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    def write_stdin(self, text: str) -> dict:
        if self._closed:
            return {"ok": False, "error": "session is closed"}
        try:
            if _IS_WINDOWS:
                self._winpty.write(text)
            else:
                os.write(self._master, text.encode())
            self.last_activity = time.time()
            # User sent input → Claude is about to work; reset idle state
            # so the green indicator disappears immediately (POS-2273).
            self._claude_state = 'unknown'
            self._clear_idle_seen()
            self._clear_idle_marker()
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    # -- Idle-seen marker (POS-2268) -----------------------------------------

    def mark_idle_seen(self) -> None:
        """Create the ``idle_seen`` marker file in the session directory."""
        if self._session_dir:
            try:
                with open(os.path.join(self._session_dir, "idle_seen"), "w"):
                    pass
            except Exception:
                pass

    def _clear_idle_seen(self) -> None:
        """Delete the ``idle_seen`` marker — user is interacting again."""
        if self._session_dir:
            try:
                os.remove(os.path.join(self._session_dir, "idle_seen"))
            except Exception:
                pass

    def _is_idle_seen(self) -> bool:
        if not self._session_dir:
            return False
        return os.path.exists(os.path.join(self._session_dir, "idle_seen"))

    # -- Idle marker (POS-2273) ------------------------------------------------

    def _create_idle_marker(self) -> None:
        """Create the ``idle`` marker file when Claude becomes idle."""
        if self._session_dir:
            try:
                with open(os.path.join(self._session_dir, "idle"), "w"):
                    pass
            except Exception:
                pass

    def _clear_idle_marker(self) -> None:
        """Delete the ``idle`` marker — Claude is busy or user sent input."""
        if self._session_dir:
            try:
                os.remove(os.path.join(self._session_dir, "idle"))
            except Exception:
                pass

    def resize(self, cols: int, rows: int) -> dict:
        if not self.is_alive():
            return {"ok": False, "error": "session is dead"}
        try:
            if _IS_WINDOWS:
                self._winpty.set_size(cols, rows)
            else:
                fcntl.ioctl(
                    self._master,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def interrupt(self) -> dict:
        try:
            if _IS_WINDOWS:
                self._winpty.write("\x03")
            else:
                os.write(self._master, b"\x03")
            return {"ok": True}
        except (OSError, Exception) as exc:
            return {"ok": False, "error": str(exc)}

    def queue_first_message(self, text: str, images=None) -> bool:
        with self._lock:
            if self._first_message_sent:
                return False
            self._first_message_sent = True
        threading.Thread(
            target=self._deliver_first_message, args=(text, images), daemon=True
        ).start()
        return True

    def _deliver_first_message(self, text: str, images=None) -> None:
        try:
            self._first_output.wait(FIRST_MESSAGE_OUTPUT_TIMEOUT_S)
            time.sleep(FIRST_MESSAGE_SETTLE_S)
            if self._closed:
                return
            normalized = text.replace("\r\n", "\n")
            if "\n" in normalized:
                # Bracketed paste so embedded newlines don't submit early.
                payload = "\x1b[200~" + normalized + "\x1b[201~"
            else:
                payload = normalized
            self.write_stdin(payload)
            # POS-2242: inject screenshot file paths into the input buffer so
            # they are part of the same message as the text.
            if images:
                import base64 as _b64
                _ext_map = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }
                for img in images:
                    try:
                        raw = _b64.b64decode(img.get("data", ""))
                        ext = _ext_map.get(img.get("mimeType", "image/png"), ".png")
                        fd, path = tempfile.mkstemp(suffix=ext, prefix="claude-paste-")
                        os.write(fd, raw)
                        os.close(fd)
                        self.write_stdin("\x1b[200~" + path + "\x1b[201~")
                    except Exception:
                        pass
            time.sleep(0.3)
            self.write_stdin("\r")
            self._first_message_delivered.set()  # POS-2429
        except Exception:
            pass

    def close(self) -> None:
        if _IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self._winpty.pid)],
                    capture_output=True,
                )
            except Exception:
                pass
        else:
            _kill_process_tree(self._proc)
            try:
                os.close(self._master)
            except Exception:
                pass
        self._closed = True
        # Ensure the idle marker survives process teardown — the idle
        # reaper or an eviction may close the PTY while it is still busy,
        # and without a marker file the frontend would think the agent is
        # still working (POS-2284).
        self._create_idle_marker()
        with self._raw_sub_lock:
            subs = list(self._raw_subscribers)
        for q in subs:
            try:
                q.put_nowait(None)
            except _queue.Full:
                pass

    def _effective_claude_state(self) -> str:
        """Return ``claude_state`` grounded in the file system (POS-2273).

        The in-memory ``_claude_state`` can drift from the on-disk ``idle``
        marker (e.g. ``write_stdin`` clears the file before the next OSC
        title arrives, or a newly spawned PTY starts as ``'unknown'`` while
        the marker from a previous run still exists on disk).

        Report ``'idle'`` when the marker file exists — even if the in-memory
        state is ``'unknown'`` (just-spawned resume PTY) or ``'busy'``
        (transient phase during resume loading).  The file is the source of
        truth: it is created when Claude settles to idle and deleted only
        when the user sends input (``write_stdin``).
        """
        if self._session_dir and os.path.exists(os.path.join(self._session_dir, "idle")):
            return 'idle'
        if self._claude_state == 'idle':
            # In-memory says idle but the file is gone (write_stdin cleared it
            # before the next OSC title arrived) — don't report a stale idle.
            return 'unknown'
        return self._claude_state

    def snapshot(self) -> dict:
        return {
            "active": self.is_alive(),
            "exit_code": self._exit_code,
            "claude_state": self._effective_claude_state(),
            "idle_seen": self._is_idle_seen(),
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "last_ui_touch": self.last_ui_touch,
        }


_chat_sessions: dict[str, dict[str, ChatPtySession]] = {}
_chat_registry_lock = threading.Lock()


def get_chat_pty(project_root: str, session_id: str) -> ChatPtySession | None:
    """Return the registered chat PTY for (project_root, session_id), if any.

    Returned even if the process has already exited — callers check
    ``is_alive()`` themselves.
    """
    with _chat_registry_lock:
        bucket = _chat_sessions.get(project_root)
        if not bucket:
            return None
        return bucket.get(session_id)


def get_or_create_chat_pty(
    project_root: str,
    session_id: str,
    command: str,
    args: list[str],
    env_overrides: dict[str, str] | None = None,
    session_dir: str | None = None,
) -> ChatPtySession | None:
    """Return the live chat PTY for this session, spawning one if needed.

    Evicts the least-recently-active session in this project once the
    per-project cap is reached. Never raises — returns ``None`` on any
    failure (including PTY being unsupported on this platform).
    """
    if not is_chat_supported():
        return None
    try:
        with _chat_registry_lock:
            bucket = _chat_sessions.setdefault(project_root, {})
            existing = bucket.get(session_id)
            if existing is not None:
                if existing.is_alive():
                    return existing
                existing.close()
                del bucket[session_id]

            # Evict dead entries first.
            for sid in [sid for sid, s in bucket.items() if not s.is_alive()]:
                bucket.pop(sid, None)

            while len(bucket) >= MAX_CHAT_PTYS_PER_PROJECT:
                oldest_sid = min(bucket, key=lambda sid: bucket[sid].last_activity)
                _chat_logger.info(
                    "Evicting chat PTY %s (project %s): per-project cap reached",
                    oldest_sid,
                    project_root,
                )
                bucket.pop(oldest_sid).close()

            session = ChatPtySession(project_root, [command, *args], env_overrides, session_dir)
            bucket[session_id] = session
        _ensure_idle_reaper()
        return session
    except Exception as exc:  # noqa: BLE001 — spawn failures must never raise
        _chat_logger.warning(
            "get_or_create_chat_pty failed for %s/%s: %s", project_root, session_id, exc
        )
        return None


def destroy_chat_pty(project_root: str, session_id: str) -> None:
    """Close and forget the chat PTY for this session, if any. Never raises."""
    try:
        with _chat_registry_lock:
            bucket = _chat_sessions.get(project_root)
            if not bucket:
                return
            session = bucket.pop(session_id, None)
        if session is not None:
            session.close()
    except Exception:
        pass


def scan_idle_chat_sessions(project_root: str) -> list[dict]:
    """Scan session directories for file-based ``idle`` markers (POS-2273).

    Returns synthetic session entries for sessions that have an ``idle``
    marker file on disk but are NOT in the live PTY registry — so the
    frontend can show the green indicator even after the PTY exits.
    """
    sessions_dir = os.path.join(project_root, ".praxis", "chat", "sessions")
    if not os.path.isdir(sessions_dir):
        return []
    result: list[dict] = []
    try:
        for entry in os.scandir(sessions_dir):
            if not entry.is_dir():
                continue
            idle_path = os.path.join(entry.path, "idle")
            if os.path.exists(idle_path):
                idle_seen = os.path.exists(os.path.join(entry.path, "idle_seen"))
                result.append({
                    "session_id": entry.name,
                    "active": False,
                    "exit_code": None,
                    "claude_state": "idle",
                    "idle_seen": idle_seen,
                    "created_at": None,
                    "last_activity": None,
                    "last_ui_touch": None,
                })
    except Exception:
        pass
    return result


def list_chat_ptys(project_root: str) -> list[dict]:
    """Return a snapshot list of every chat PTY registered for *project_root*.

    Also includes synthetic entries for sessions with an on-disk ``idle``
    marker but no live PTY, so the green indicator survives PTY eviction
    and server restarts (POS-2273).
    """
    with _chat_registry_lock:
        bucket = _chat_sessions.get(project_root, {})
        live = [{"session_id": sid, **s.snapshot()} for sid, s in bucket.items()]
    live_ids = {s["session_id"] for s in live}
    for entry in scan_idle_chat_sessions(project_root):
        if entry["session_id"] not in live_ids:
            live.append(entry)
    return live


def chat_pty_status(project_root: str, session_id: str) -> dict:
    """Return ``{"active": bool, "exit_code": int | None}`` for this session."""
    session = get_chat_pty(project_root, session_id)
    if session is None:
        return {"active": False, "exit_code": None}
    return {"active": session.is_alive(), "exit_code": session.exit_code}


def destroy_all_chat_ptys(project_root: str | None = None) -> None:
    """Close every chat PTY for *project_root*, or every project if ``None``.

    Never raises — used on server shutdown for a clean exit.
    """
    try:
        with _chat_registry_lock:
            if project_root is None:
                buckets = list(_chat_sessions.values())
                _chat_sessions.clear()
            else:
                bucket = _chat_sessions.pop(project_root, {})
                buckets = [bucket]
        for bucket in buckets:
            for session in bucket.values():
                try:
                    session.close()
                except Exception:
                    pass
    except Exception:
        pass


def touch_chat_pty(project_root: str, session_id: str) -> None:
    """Update ``last_ui_touch`` on the matching ChatPtySession. Never raises."""
    try:
        with _chat_registry_lock:
            bucket = _chat_sessions.get(project_root)
            if not bucket:
                return
            session = bucket.get(session_id)
            if session is not None:
                session.last_ui_touch = time.time()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Idle reaper — background daemon that closes chat PTYs the UI has stopped
# looking at (POS-2238).
# ---------------------------------------------------------------------------

_idle_reaper_started = False
_idle_reaper_start_lock = threading.Lock()


def _idle_reaper_loop() -> None:
    """Run every 60 s, closing live chat PTYs idle longer than the timeout."""
    while True:
        time.sleep(60)
        try:
            with _chat_registry_lock:
                now = time.time()
                for _project_root, bucket in list(_chat_sessions.items()):
                    to_reap = [
                        sid
                        for sid, s in bucket.items()
                        if s.is_alive()
                        and now - s.last_ui_touch > CHAT_PTY_IDLE_TIMEOUT_S
                    ]
                    for sid in to_reap:
                        _chat_logger.info(
                            "Idle-reaping chat PTY %s (project %s)",
                            sid,
                            _project_root,
                        )
                        try:
                            bucket.pop(sid).close()
                        except Exception:
                            pass
        except Exception:
            pass


def _ensure_idle_reaper() -> None:
    """Start the reaper thread once (idempotent, thread-safe)."""
    global _idle_reaper_started
    if _idle_reaper_started:
        return
    with _idle_reaper_start_lock:
        if _idle_reaper_started:
            return
        t = threading.Thread(
            target=_idle_reaper_loop,
            daemon=True,
            name="chat-pty-idle-reaper",
        )
        t.start()
        _idle_reaper_started = True
