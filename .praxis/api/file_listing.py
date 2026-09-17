"""Project file listing for the Praxis Local API (pure, no Flask).

The "Mention project files with @" feature needs the set of a project's file
paths so the frontend can offer them for @-mention. This module owns that
*read*: it walks the project tree from an absolute root, prunes the usual
heavyweight / generated directories, and returns project-relative POSIX paths.

It is deliberately free of any Flask import so the logic is unit-testable and
reusable. The HTTP layer (``praxis_local_api.py``) is a thin adapter that
resolves + validates the ``project_root`` and serializes the result as JSON.

The traversal does not follow symlinks (``os.walk`` default) and caps the
result at :data:`MAX_FILES` so a pathologically large tree cannot blow up the
response or the browser.
"""

from __future__ import annotations

import os
import re

__all__ = ["MAX_FILES", "PRUNED_DIRS", "list_project_files"]

# Hard ceiling on the number of paths returned. A project larger than this is
# truncated (first ``MAX_FILES`` after sorting) rather than streamed whole — the
# @-mention picker is interactive and never needs an unbounded list.
MAX_FILES = 8000

# Directory *names* pruned from the walk wherever they appear. These hold
# dependencies, build output, VCS internals, caches, and IDE state — never
# project source a user would @-mention.
PRUNED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".idea",
        ".pytest_cache",
        ".mypy_cache",
        ".next",
        "coverage",
    }
)

# Sidecar files the general.yaml backup/recovery layer writes (POS-2296). Never
# useful to @-mention.
_PRUNED_FILE_RE = re.compile(r"^\.?general\.yaml\.(bak2?|lock|corrupted(\.\d+)*|.*\.tmp)$")


def list_project_files(root: str) -> list[str]:
    """Return the project's file paths, relative to ``root`` and POSIX-style.

    Walks ``root`` (an absolute directory) with :func:`os.walk`, pruning the
    directory names in :data:`PRUNED_DIRS` in place so the walk never descends
    into them, and skipping general.yaml's backup/lock/corrupted/tmp sidecar
    files (see :data:`_PRUNED_FILE_RE`). Symlinks are not followed (``os.walk``
    default). Paths are made relative to ``root`` with forward slashes and
    returned sorted; the result is capped at :data:`MAX_FILES` (the first
    ``MAX_FILES`` after sorting).
    """
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk does not descend into excluded dirs.
        dirnames[:] = [name for name in dirnames if name not in PRUNED_DIRS]
        for filename in filenames:
            if _PRUNED_FILE_RE.match(filename):
                continue
            absolute = os.path.join(dirpath, filename)
            relative = os.path.relpath(absolute, root).replace(os.sep, "/")
            files.append(relative)

    files.sort()
    return files[:MAX_FILES]
