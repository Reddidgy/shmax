"""Provision PraxisOS auto-add-tasks-to-commits git hooks.

Mirrors the hook generation logic implemented in TypeScript
(``src/services/git-hook-scripts.ts`` and ``src/services/git-hook-service.ts``)
so that the MCP ``update_settings`` tool can provision the same hook files the
frontend writes via ``syncAutoAddTasksGitHooks`` + the ``/api/run-enable-hooks``
endpoint — without depending on the browser File System Access API.

The four managed hooks (pre-commit, post-commit, post-rewrite,
prepare-commit-msg) each get a fenced ``# PRAXIS auto-add start`` /
``# PRAXIS auto-add end`` block appended to (or removed from) whatever
content already exists in ``.git/hooks/<name>``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Callable

__all__ = [
    "sync_auto_add_tasks_git_hooks",
    "save_enable_hooks_script",
]

# ---------------------------------------------------------------------------
# Markers and regexes — must match git-hook-scripts.ts exactly.
# ---------------------------------------------------------------------------

PRAXIS_AUTO_ADD_START = "# PRAXIS auto-add start"
PRAXIS_AUTO_ADD_END = "# PRAXIS auto-add end"

PRAXIS_HOOK_BLOCK_RE = re.compile(r"# PRAXIS auto-add start[\s\S]*?# PRAXIS auto-add end\s*")
LEGACY_AIBAN_HOOK_BLOCK_RE = re.compile(r"# AIBAN auto-add start[\s\S]*?# AIBAN auto-add end\s*")

_PRE_COMMIT_HOOK_FILE_NAME = "pre-commit"
_POST_COMMIT_HOOK_FILE_NAME = "post-commit"
_POST_REWRITE_HOOK_FILE_NAME = "post-rewrite"
_PREPARE_COMMIT_MSG_HOOK_FILE_NAME = "prepare-commit-msg"
_ENABLE_HOOKS_SCRIPT_FILE_NAME = "enable-hooks.sh"

# ---------------------------------------------------------------------------
# Hook script bodies (without the PRAXIS fence markers).
# ---------------------------------------------------------------------------


def _pre_commit_block() -> str:
    """Content for the pre-commit hook (minimal — real work is elsewhere)."""
    return (
        "# PRAXIS pre-commit staging\n"
        "# Task completion is handled via prepare-commit-msg (signal) + post-commit (action).\n"
        "# This block is intentionally minimal.\n"
        "exit 0"
    )


def _post_commit_block() -> str:
    """Content for the post-commit hook — moves completed tasks into place."""
    return r"""# PRAXIS post-commit task auto-complete
PRAXIS_ADD_SKIP=".git/.praxis-skip-post-add"

if [ -f "$PRAXIS_ADD_SKIP" ]; then
  rm -f "$PRAXIS_ADD_SKIP" 2>/dev/null || true
else
  GIT_DIR_PATH=$(git rev-parse --git-dir 2>/dev/null || echo ".git")
  PENDING_FILE="$GIT_DIR_PATH/.praxis-pending-complete"

  if [ -f "$PENDING_FILE" ]; then
    TASK_IDS=$(cat "$PENDING_FILE" 2>/dev/null || true)
    rm -f "$PENDING_FILE" 2>/dev/null || true

    if [ -n "$TASK_IDS" ]; then
      COMPLETED_DIR=".praxis/tasks/completed"
      mkdir -p "$COMPLETED_DIR" 2>/dev/null || true

      for task_id in $TASK_IDS; do
        # Search for both legacy numeric and composite filenames
        source_path=$(find .praxis/tasks -type f \( -name "$task_id.json" -o -name "*-$task_id-*.json" \) ! -path "*/completed/*" 2>/dev/null | head -n 1)

        if [ -n "$source_path" ]; then
          task_file_name=$(basename "$source_path")
          target_path="$COMPLETED_DIR/$task_file_name"

          if [ "$source_path" != "$target_path" ]; then
            mv "$source_path" "$target_path" || continue
            git add -- "$target_path" >/dev/null 2>&1
            git add -u -- "$source_path" >/dev/null 2>&1
          else
            git add -- "$source_path" >/dev/null 2>&1
          fi
          continue
        fi

        existing_completed_path=$(find "$COMPLETED_DIR" -type f \( -name "$task_id.json" -o -name "*-$task_id-*.json" \) 2>/dev/null | head -n 1)
        if [ -n "$existing_completed_path" ]; then
          git add -- "$existing_completed_path" >/dev/null 2>&1
        fi
      done

      if ! git diff --cached --quiet -- .praxis/tasks 2>/dev/null; then
        touch "$PRAXIS_ADD_SKIP" 2>/dev/null || true
        git commit --amend --no-edit --no-verify >/dev/null 2>&1 || true
        rm -f "$PRAXIS_ADD_SKIP" 2>/dev/null || true
        exit 0
      fi
    fi
  fi
fi"""


def _post_rewrite_block() -> str:
    """Content for the post-rewrite hook (no-op, kept for hook detection)."""
    return (
        "# PRAXIS post-rewrite task auto-complete\n"
        "# Task completion has moved to the prepare-commit-msg hook.\n"
        "# During rebase, prepare-commit-msg fires for each replayed commit,\n"
        "# so task files are staged atomically  -  no amend needed.\n"
        "# This block is intentionally a no-op (preserved for hook detection compatibility).\n"
        "exit 0"
    )


def _prepare_commit_msg_block() -> str:
    """Content for the prepare-commit-msg hook — records task IDs to complete."""
    return r"""# PRAXIS prepare-commit-msg task auto-complete
COMMIT_MSG_FILE="$1"
COMMIT_SOURCE="${2:-}"

# Skip amend, merge, and squash  -  task files were already handled in the original commit.
if [ "$COMMIT_SOURCE" = "commit" ] || [ "$COMMIT_SOURCE" = "merge" ] || [ "$COMMIT_SOURCE" = "squash" ]; then
  exit 0
fi

COMMIT_MESSAGE=$(cat "$COMMIT_MSG_FILE" 2>/dev/null || true)
if [ -z "$COMMIT_MESSAGE" ]; then
  exit 0
fi

TASK_IDS=$(printf '%s\n' "$COMMIT_MESSAGE" | grep -Eo '\[([A-Za-z0-9]{1,3}-)?[0-9]+\]' | sed -E 's/^\[([A-Za-z0-9]{1,3}-)?([0-9]+)\]$/\2/' | awk '!seen[$0]++')
if [ -z "$TASK_IDS" ]; then
  exit 0
fi

# Store task IDs for post-commit to pick up after the commit is finalized.
GIT_DIR_PATH=$(git rev-parse --git-dir 2>/dev/null || echo ".git")
printf '%s\n' "$TASK_IDS" > "$GIT_DIR_PATH/.praxis-pending-complete" 2>/dev/null || true

exit 0"""


def _enable_hooks_script_content() -> str:
    """Content for ``.praxis/scripts/enable-hooks.sh`` (installs + chmods hooks)."""
    return """#!/usr/bin/env sh
set -eu

if ! command -v git >/dev/null 2>&1; then
  echo "git is required to configure hooks."
  exit 1
fi

SCRIPT_DIR=$(dirname -- "$0")
REPO_ROOT_CANDIDATE=$(dirname -- "$(dirname -- "$SCRIPT_DIR")")

REPO_ROOT=""
if [ -d "$REPO_ROOT_CANDIDATE/.git" ]; then
  REPO_ROOT="$REPO_ROOT_CANDIDATE"
else
  REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)
fi

if [ -z "$REPO_ROOT" ]; then
  echo "Unable to resolve repository root."
  exit 1
fi

if ! git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Not inside a Git work tree: $REPO_ROOT"
  exit 1
fi

GIT_DIR=$(git -C "$REPO_ROOT" rev-parse --absolute-git-dir)
HOOKS_DIR="$GIT_DIR/hooks"

mkdir -p "$HOOKS_DIR"

installed_count=0
HOOKS_SOURCE_DIR="$REPO_ROOT/scripts/hooks"
if [ -d "$HOOKS_SOURCE_DIR" ]; then
  for hook_path in "$HOOKS_SOURCE_DIR"/*; do
    [ -f "$hook_path" ] || continue
    hook_name=$(basename "$hook_path")
    target_path="$HOOKS_DIR/$hook_name"

    if [ -f "$target_path" ] && cmp -s "$hook_path" "$target_path"; then
      :
    else
      cp "$hook_path" "$target_path"
    fi

    chmod +x "$target_path"
    installed_count=$((installed_count + 1))
  done
fi

chmod_count=0
for existing_hook in "$HOOKS_DIR"/*; do
  [ -f "$existing_hook" ] || continue
  chmod +x "$existing_hook" || true
  chmod_count=$((chmod_count + 1))
done

git -C "$REPO_ROOT" config --local core.hooksPath "$HOOKS_DIR"

echo "Configured $installed_count hook(s) and verified executable permissions on $chmod_count hook file(s) in $HOOKS_DIR"

"""


# ---------------------------------------------------------------------------
# Generic block insertion/removal — mirrors buildPreCommitContent et al.
# ---------------------------------------------------------------------------


def _strip_legacy_aiban_blocks(content: str) -> str:
    """Remove legacy ``# AIBAN auto-add`` blocks, matching TS ``stripLegacyAibanHookBlocks``."""
    if not content:
        return content
    return LEGACY_AIBAN_HOOK_BLOCK_RE.sub("", content).rstrip()


def _build_hook_content(current_content: str, block_content: str, should_enable: bool) -> str:
    """Insert, replace, or remove the fenced PRAXIS block in a hook file's content.

    Mirrors the shared shape of ``buildPreCommitContent`` / ``buildPostCommitContent`` /
    ``buildPostRewriteContent`` / ``buildPrepareCommitMsgContent`` in git-hook-scripts.ts.
    """
    sanitized = _strip_legacy_aiban_blocks(current_content)
    if not sanitized:
        if should_enable:
            return f"#!/bin/sh\n{PRAXIS_AUTO_ADD_START}\n{block_content}\n{PRAXIS_AUTO_ADD_END}\n"
        return ""

    start_index = sanitized.find(PRAXIS_AUTO_ADD_START)
    end_index = sanitized.find(PRAXIS_AUTO_ADD_END)

    if start_index != -1 and end_index != -1 and end_index > start_index:
        before_block = sanitized[:start_index]
        after_block = sanitized[end_index + len(PRAXIS_AUTO_ADD_END):]
        content_without_praxis = before_block + after_block

        if should_enable:
            praxis_block = f"\n{PRAXIS_AUTO_ADD_START}\n{block_content}\n{PRAXIS_AUTO_ADD_END}\n"
            return content_without_praxis.rstrip() + praxis_block

        return content_without_praxis.rstrip() + "\n"

    if should_enable:
        praxis_block = f"\n{PRAXIS_AUTO_ADD_START}\n{block_content}\n{PRAXIS_AUTO_ADD_END}\n"
        return sanitized.rstrip() + praxis_block

    return sanitized


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    """Read a text file, returning an empty string if it does not exist."""
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, content: str) -> None:
    """Write text content to a file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_executable(path: str) -> None:
    """Set the executable bit (owner/group/other) on a file, best-effort."""
    try:
        current_mode = os.stat(path).st_mode
        os.chmod(path, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

_HOOK_SPECS: list[tuple[str, Callable[[], str]]] = [
    (_PRE_COMMIT_HOOK_FILE_NAME, _pre_commit_block),
    (_POST_COMMIT_HOOK_FILE_NAME, _post_commit_block),
    (_POST_REWRITE_HOOK_FILE_NAME, _post_rewrite_block),
    (_PREPARE_COMMIT_MSG_HOOK_FILE_NAME, _prepare_commit_msg_block),
]


def save_enable_hooks_script(project_root: str) -> None:
    """Write ``.praxis/scripts/enable-hooks.sh`` and make it executable."""
    scripts_dir = os.path.join(project_root, ".praxis", "scripts")
    script_path = os.path.join(scripts_dir, _ENABLE_HOOKS_SCRIPT_FILE_NAME)
    _write_text(script_path, _enable_hooks_script_content())
    _make_executable(script_path)


def sync_auto_add_tasks_git_hooks(project_root: str, should_enable: bool) -> dict:
    """Provision (or remove) the PRAXIS auto-add-tasks git hooks for a project.

    Mirrors ``GitHookService.syncAutoAddTasksGitHooks`` from
    ``src/services/git-hook-service.ts``. Errors are returned as a result dict
    rather than raised — hook provisioning failures should never prevent a
    settings update from succeeding.
    """
    git_dir = os.path.join(project_root, ".git")
    if not os.path.isdir(git_dir):
        return {"ok": False, "error": "Not a git repository (no .git directory found)"}

    hooks_dir = os.path.join(git_dir, "hooks")
    try:
        os.makedirs(hooks_dir, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"Failed to create hooks directory: {exc}"}

    try:
        for hook_name, block_fn in _HOOK_SPECS:
            hook_path = os.path.join(hooks_dir, hook_name)
            hook_exists = os.path.isfile(hook_path)
            current_content = _read_text(hook_path)
            next_content = _build_hook_content(current_content, block_fn(), should_enable)

            if next_content and next_content != current_content:
                _write_text(hook_path, next_content)
                _make_executable(hook_path)
            elif not should_enable and hook_exists:
                stripped = PRAXIS_HOOK_BLOCK_RE.sub("", current_content)
                stripped = LEGACY_AIBAN_HOOK_BLOCK_RE.sub("", stripped)
                stripped = stripped.rstrip() + "\n"
                _write_text(hook_path, stripped)

        if should_enable:
            try:
                subprocess.run(
                    ["git", "-C", project_root, "config", "--local", "core.hooksPath", hooks_dir],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                pass

            save_enable_hooks_script(project_root)
    except OSError as exc:
        return {"ok": False, "error": f"Failed to provision git hooks: {exc}"}

    return {"ok": True, "hooks_provisioned": True}
