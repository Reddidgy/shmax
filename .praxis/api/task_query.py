"""Shared task-read logic for the Praxis Local API (pure, no Flask).

Complements the write-only :mod:`task_ops`: this module owns the *read* half
of task operations — listing tasks across status columns and fetching a
single task by id.

Deliberately free of any Flask import so the logic is unit-testable and
reusable from MCP tools or a future CLI.
"""

from __future__ import annotations

import json
import logging
import os

__all__ = ["list_tasks", "get_task", "get_active_tasks", "find_task_by_keywords", "reconcile_task_statuses"]

logger = logging.getLogger(__name__)

TASK_STATUS_DIRS: tuple[str, ...] = (
    "new",
    "in_progress",
    "to_review",
    "completed",
    "on_hold",
    "archived",
)

_ACTIVE_STATUSES: tuple[str, ...] = ("new", "in_progress")

_SUMMARY_KEYS: tuple[str, ...] = (
    "id",
    "title",
    "status",
    "priority",
    "labels",
    "assignee",
)


def _read_task_file(path: str) -> dict | None:
    """Read and parse a single task JSON file, returning ``None`` on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("skipping unreadable task file %s: %s", path, exc)
        return None


def _summarize(task: dict) -> dict:
    """Extract summary fields from a full task dict."""
    return {k: task.get(k) for k in _SUMMARY_KEYS}


def _scan_status_dir(tasks_dir: str, status: str) -> list[dict]:
    """Read all ``*.json`` task files from a single status directory."""
    status_path = os.path.join(tasks_dir, status)
    if not os.path.isdir(status_path):
        return []
    results: list[dict] = []
    try:
        entries = sorted(os.scandir(status_path), key=lambda e: e.name)
    except OSError:
        return []
    for entry in entries:
        if not entry.name.endswith(".json") or not entry.is_file():
            continue
        task = _read_task_file(entry.path)
        if task is None:
            continue
        # Folder is the source of truth for status — override the JSON field.
        json_status = task.get("status")
        if json_status is not None and json_status != status:
            logger.warning(
                "status desync in %s: JSON says %r but file is in %s/ — "
                "using folder as source of truth",
                entry.name, json_status, status,
            )
        task["status"] = status
        results.append(task)
    return results


def list_tasks(
    project_root: str,
    status: str | None = None,
) -> list[dict]:
    """Return task summaries from the project's ``.praxis/tasks/`` tree.

    When *status* is ``None``, scans all known status directories and returns
    every task.  When *status* is one of the known column names (``new``,
    ``in_progress``, ``completed``, ``on_hold``, ``archived``), scans only
    that directory.

    Each returned dict contains only summary fields: ``id``, ``title``,
    ``status``, ``priority``, ``labels``, ``assignee``.

    Malformed JSON files are silently skipped (logged at WARNING).  A missing
    or empty project returns an empty list — never raises.
    """
    tasks_dir = os.path.join(project_root, ".praxis", "tasks")
    if not os.path.isdir(tasks_dir):
        return []

    if status and status not in TASK_STATUS_DIRS:
        raise ValueError(f"unknown status: {status}")

    statuses = (status,) if status else TASK_STATUS_DIRS
    summaries: list[dict] = []
    for s in statuses:
        for task in _scan_status_dir(tasks_dir, s):
            summaries.append(_summarize(task))
    return summaries


def get_task(project_root: str, task_id: int | str) -> dict | None:
    """Return the full task dict for *task_id*, or ``None`` if not found.

    Accepts both composite string IDs (e.g., ``POS-1295-a7x3mq``) and
    legacy numeric IDs (e.g., ``1295``).
    """
    tasks_dir = os.path.join(project_root, ".praxis", "tasks")

    # Try exact filename match first
    filename = f"{task_id}.json"
    for status in TASK_STATUS_DIRS:
        path = os.path.join(tasks_dir, status, filename)
        if os.path.isfile(path):
            task = _read_task_file(path)
            if task is not None:
                json_status = task.get("status")
                if json_status is not None and json_status != status:
                    logger.warning(
                        "status desync in %s: JSON says %r but file is in %s/ — "
                        "using folder as source of truth",
                        f"{task_id}.json", json_status, status,
                    )
                task["status"] = status
                return task

    # If numeric, scan for composite files containing this numeric ID
    from task_ops import extract_numeric_id
    numeric = extract_numeric_id(task_id)
    if numeric is not None:
        for status in TASK_STATUS_DIRS:
            status_path = os.path.join(tasks_dir, status)
            if not os.path.isdir(status_path):
                continue
            try:
                for entry in os.scandir(status_path):
                    if not entry.name.endswith(".json") or not entry.is_file():
                        continue
                    stem = entry.name[:-len(".json")]
                    entry_numeric = extract_numeric_id(stem)
                    if entry_numeric == numeric:
                        task = _read_task_file(entry.path)
                        if task is not None:
                            json_status = task.get("status")
                            if json_status is not None and json_status != status:
                                logger.warning(
                                    "status desync in %s: JSON says %r but file is in %s/ — "
                                    "using folder as source of truth",
                                    entry.name, json_status, status,
                                )
                            task["status"] = status
                            return task
            except OSError:
                continue
    return None


def get_active_tasks(project_root: str) -> list[dict]:
    """Return minimal summaries (``id``, ``title``) for active tasks only.

    Active tasks live in ``new/`` and ``in_progress/`` — small enough to be
    safe for any MCP payload budget.
    """
    tasks_dir = os.path.join(project_root, ".praxis", "tasks")
    if not os.path.isdir(tasks_dir):
        return []
    results: list[dict] = []
    for status in _ACTIVE_STATUSES:
        for task in _scan_status_dir(tasks_dir, status):
            results.append({"id": task.get("id"), "title": task.get("title")})
    return results


_SEARCH_FIELDS: tuple[str, ...] = ("title", "why_we_need_this", "acceptance_criteria")
_SEARCH_RESULT_KEYS: tuple[str, ...] = ("id", "title", "status")


def find_task_by_keywords(
    project_root: str,
    query: str,
    status: str | None = None,
) -> list[dict]:
    """Search tasks by keyword across title and description fields.

    Returns matches with summary fields (``id``, ``title``, ``status``).
    When *status* is ``None``, searches only active statuses (``new``,
    ``in_progress``).  A specific status can be passed to widen or narrow
    the scope.  All keywords must appear (AND logic, case-insensitive).

    Raises :class:`ValueError` for an unrecognised *status*.
    """
    tasks_dir = os.path.join(project_root, ".praxis", "tasks")
    if not os.path.isdir(tasks_dir):
        return []

    if status and status not in TASK_STATUS_DIRS:
        raise ValueError(f"unknown status: {status}")

    statuses = (status,) if status else _ACTIVE_STATUSES
    keywords = query.lower().split()
    if not keywords:
        return []

    matches: list[dict] = []
    for s in statuses:
        for task in _scan_status_dir(tasks_dir, s):
            searchable = " ".join(str(task.get(f, "")) for f in _SEARCH_FIELDS).lower()
            if all(kw in searchable for kw in keywords):
                matches.append({k: task.get(k) for k in _SEARCH_RESULT_KEYS})
    return matches


def reconcile_task_statuses(project_root: str) -> dict:
    """Scan all task folders and fix JSON files whose ``status`` field
    does not match their folder location.

    Returns ``{"fixed": [...], "errors": [...]}`` where each ``fixed``
    entry is ``{"id": <str>, "file": <str>, "old_status": <str>,
    "new_status": <str>}`` and each ``errors`` entry describes a file
    that could not be repaired.
    """
    tasks_dir = os.path.join(project_root, ".praxis", "tasks")
    if not os.path.isdir(tasks_dir):
        return {"fixed": [], "errors": []}

    fixed: list[dict] = []
    errors: list[dict] = []

    for status in TASK_STATUS_DIRS:
        status_path = os.path.join(tasks_dir, status)
        if not os.path.isdir(status_path):
            continue
        try:
            entries = sorted(os.scandir(status_path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".json") or not entry.is_file():
                continue
            try:
                with open(entry.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                errors.append({"file": entry.name, "folder": status, "error": str(exc)})
                continue

            json_status = data.get("status")
            if json_status == status:
                continue  # already consistent

            old_status = json_status
            data["status"] = status
            try:
                tmp_path = entry.path + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, entry.path)
                fixed.append({
                    "id": data.get("id", entry.name),
                    "file": entry.name,
                    "old_status": old_status,
                    "new_status": status,
                })
                logger.info(
                    "reconciled %s: status %r → %r",
                    entry.name, old_status, status,
                )
            except OSError as exc:
                errors.append({"file": entry.name, "folder": status, "error": str(exc)})

    return {"fixed": fixed, "errors": errors}
