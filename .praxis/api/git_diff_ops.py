"""Line-level ``git diff HEAD -- <file>`` parsing for the Project Files diff gutter (POS-2308).

The endpoint ``GET /api/git/diff-file`` in ``praxis_local_api.py`` calls
:func:`diff_file_against_head`.  Output is consumed by
``src/components/ProjectFiles/git-diff-line-data.ts``.
"""

from __future__ import annotations

import os
import re
import subprocess

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_GIT_TIMEOUT_SECONDS = 10


def is_safe_relative_path(file_path: object) -> bool:
    """True when ``file_path`` is a non-empty relative path with no ``..`` segments."""
    if not isinstance(file_path, str):
        return False
    candidate = file_path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or os.path.isabs(candidate):
        return False
    return all(segment not in ("", "..") for segment in candidate.split("/"))


def parse_unified_diff(text: str) -> tuple[list[dict], bool]:
    """Parse unified diff text into ``(hunks, is_binary)``.

    Each hunk: ``{old_start, old_lines, new_start, new_lines, lines}``.
    Each line: ``{type: 'context'|'added'|'removed', old_line, new_line, content}``;
    removed lines additionally carry ``anchor_new_line`` — the 1-based line number
    in the NEW file *before which* the removed content originally sat (may be
    ``last_line + 1`` for deletions at end of file).  Zero-length hunk ranges
    (``+4,0`` / ``-12,0``) mean "after that line", so counters start at ``start + 1``.
    """
    hunks: list[dict] = []
    current: dict | None = None
    old_line = 0
    new_line = 0
    is_binary = False

    for raw in text.splitlines():
        if raw.startswith("Binary files") and raw.endswith("differ"):
            is_binary = True
            current = None
            continue
        if raw.startswith("@@"):
            match = HUNK_HEADER_RE.match(raw)
            if not match:
                current = None
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            current = {
                "old_start": old_start,
                "old_lines": old_count,
                "new_start": new_start,
                "new_lines": new_count,
                "lines": [],
            }
            hunks.append(current)
            old_line = old_start if old_count > 0 else old_start + 1
            new_line = new_start if new_count > 0 else new_start + 1
            continue
        if current is None:
            continue  # diff --git / index / --- / +++ headers
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        if raw.startswith("+"):
            current["lines"].append({"type": "added", "old_line": None, "new_line": new_line, "content": raw[1:]})
            new_line += 1
        elif raw.startswith("-"):
            current["lines"].append({
                "type": "removed",
                "old_line": old_line,
                "new_line": None,
                "anchor_new_line": new_line,
                "content": raw[1:],
            })
            old_line += 1
        elif raw.startswith(" ") or raw == "":
            current["lines"].append({"type": "context", "old_line": old_line, "new_line": new_line, "content": raw[1:]})
            old_line += 1
            new_line += 1
        else:
            current = None  # unexpected header line — stop attributing lines to this hunk

    return hunks, is_binary


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _run_git_bytes(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=_GIT_TIMEOUT_SECONDS)


def read_head_content(project_root: str, file_path: str) -> tuple[str | None, bool]:
    """Return ``(text, is_binary)`` for ``HEAD:<file_path>``; ``(None, False)`` when unreadable."""
    result = _run_git_bytes(["show", f"HEAD:{file_path}"], project_root)
    if result.returncode != 0:
        return None, False
    if b"\x00" in result.stdout:
        return None, True
    return result.stdout.decode("utf-8", errors="replace"), False


def diff_file_against_head(project_root: str, file_path: str) -> dict:
    """Return ``{tracked, binary, hunks, head_content}`` (plus ``error`` on git failure).

    * File absent from HEAD (untracked, staged-new, no commits, not a repo) →
      ``tracked=False`` with an empty hunk list — never an error.
    * Binary file → ``binary=True`` with an empty hunk list.
    ``head_content`` is the full HEAD text so the frontend can diff the live
    editor buffer against it while typing.
    May raise ``OSError`` / ``subprocess.SubprocessError`` (caller maps to HTTP 500).
    """
    head_check = _run_git(["cat-file", "-e", f"HEAD:{file_path}"], project_root)
    if head_check.returncode != 0:
        return {"tracked": False, "binary": False, "hunks": [], "head_content": None}

    head_text, head_binary = read_head_content(project_root, file_path)
    if head_binary:
        return {"tracked": True, "binary": True, "hunks": [], "head_content": None}

    diff = _run_git(
        ["diff", "HEAD", "--no-color", "--no-ext-diff", "--no-renames", "-U0", "--", file_path],
        project_root,
    )
    if diff.returncode != 0:
        return {
            "tracked": True,
            "binary": False,
            "hunks": [],
            "head_content": None,
            "error": diff.stderr.strip() or "git diff failed",
        }

    hunks, is_binary = parse_unified_diff(diff.stdout)
    if is_binary:
        return {"tracked": True, "binary": True, "hunks": [], "head_content": None}
    return {"tracked": True, "binary": False, "hunks": hunks, "head_content": head_text}


def rollback_hunk(
    project_root: str,
    file_path: str,
    old_start: int,
    old_lines_count: int,
    new_start: int,
    new_lines_count: int,
) -> dict:
    """Rollback a single diff hunk, restoring the HEAD content for that range.

    Runs a fresh diff to verify the hunk coordinates still match,
    then replaces the working-tree lines with the HEAD lines for that hunk.
    """
    # Verify the hunk still exists in a fresh diff
    diff_result = diff_file_against_head(project_root, file_path)
    if diff_result.get("error"):
        return {"ok": False, "error": diff_result["error"]}
    if not diff_result.get("tracked"):
        return {"ok": False, "error": "File is not tracked by git"}
    if diff_result.get("binary"):
        return {"ok": False, "error": "Cannot rollback hunks in binary files"}

    hunks = diff_result.get("hunks", [])
    matching = None
    for h in hunks:
        if (
            h["old_start"] == old_start
            and h["old_lines"] == old_lines_count
            and h["new_start"] == new_start
            and h["new_lines"] == new_lines_count
        ):
            matching = h
            break

    if matching is None:
        return {"ok": False, "error": "Hunk not found — file may have changed since diff was fetched"}

    # Read current file from disk
    abs_path = os.path.join(project_root, file_path)
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            current_content = f.read()
    except OSError as exc:
        return {"ok": False, "error": f"Cannot read file: {exc}"}

    current_lines = current_content.splitlines(True)

    # Get HEAD content
    head_content = diff_result.get("head_content") or ""
    head_lines = head_content.splitlines(True)

    # Extract old content to restore
    if old_lines_count > 0:
        old_content = head_lines[old_start - 1 : old_start - 1 + old_lines_count]
    else:
        old_content = []

    # Apply rollback
    if new_lines_count > 0:
        start_idx = new_start - 1
        end_idx = start_idx + new_lines_count
        current_lines[start_idx:end_idx] = old_content
    else:
        # Pure deletion (new_lines=0 means "after new_start line")
        insert_idx = new_start
        current_lines[insert_idx:insert_idx] = old_content

    # Write back
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.writelines(current_lines)
    except OSError as exc:
        return {"ok": False, "error": f"Cannot write file: {exc}"}

    return {"ok": True}
