#!/usr/bin/env sh
set -eu

if ! command -v git >/dev/null 2>&1; then
  echo "git is required to configure hooks."
  exit 1
fi

SCRIPT_DIR=$(dirname -- "$0")
REPO_ROOT_CANDIDATE=$(dirname -- "$SCRIPT_DIR")

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
