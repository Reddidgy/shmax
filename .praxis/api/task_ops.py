"""Shared task-write logic for the Praxis Local API (pure, no Flask).

The Praxis board keeps every task as a JSON file on disk under
``<project_root>/.praxis/tasks/<status>/<id>.json``. This module owns the
*write* half of "create a task remotely": it validates the caller's payload,
allocates a collision-free id, assembles the full task dict with the
server-set fields, and atomically writes the new file into
``.praxis/tasks/new/``.

It is deliberately free of any Flask import so the logic is unit-testable and
reusable from a future CLI. The HTTP layer (``praxis_local_api.py``) is a thin
adapter that maps :class:`ValidationError` to ``400`` and a missing project to
``404``.

Two correctness guarantees mirror the browser create path in
``src/components/MainApp/main-app-task-io.ts``:

* **No id collisions.** :func:`_allocate_id` takes an ``fcntl`` exclusive lock
  over the read-compute-write of ``.praxis/config/task_counter``, and computes
  ``next = max(counter_value, filesystem_max) + 1`` — so even a stale or
  over-written counter cannot hand out an id that already exists on disk, and
  concurrent API calls are serialized by the lock.
* **No half-written files.** :func:`create_task` writes a temp file in the
  destination directory and ``os.replace``\\ s it into place, so the board's
  reader never observes a partial JSON.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl

__all__ = ["ValidationError", "TaskNotFoundError", "allocate_task_id", "create_task", "update_task", "extract_numeric_id"]

# Required, non-empty string fields the caller MUST supply.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "title",
    "why_we_need_this",
    "acceptance_criteria",
)

# Server-controlled keys: even if the caller sends these, they are ignored and
# recomputed here so a remote create is indistinguishable from a UI create.
_SERVER_CONTROLLED_FIELDS: frozenset[str] = frozenset(
    {"id", "status", "created_at", "column_entered_at"}
)

# Fields that ``update_task`` is allowed to write (allowlist — not denylist).
_UPDATABLE_FIELDS: frozenset[str] = frozenset({
    "title", "why_we_need_this", "acceptance_criteria",
    "important_constraints", "labels", "priority",
    "assignee", "on_hold_reason",
})

_VALID_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "critical")

# All status directories to scan when locating a task on disk.
_STATUS_DIRECTORIES: tuple[str, ...] = (
    "new", "in_progress", "to_review", "completed", "on_hold", "archived",
)


class ValidationError(Exception):
    """A required task field is missing or blank.

    Carries the offending field name on ``.field`` so the HTTP layer can echo
    it back to the caller in the ``400`` body.
    """

    def __init__(self, field: str, message: str | None = None) -> None:
        self.field = field
        super().__init__(message or f"missing or empty required field: {field}")


class TaskNotFoundError(Exception):
    """Raised when a task ID does not correspond to any on-disk JSON file."""

    def __init__(self, task_id: int | str) -> None:
        self.task_id = task_id
        super().__init__(f"Task {task_id} not found")


def _now_ms() -> int:
    """Current wall-clock time as epoch **milliseconds** (matches the UI)."""
    return int(time.time() * 1000)


def _require_non_empty_str(payload: dict, field: str) -> str:
    """Return ``payload[field]`` as a non-empty trimmed string, or raise.

    Raises :class:`ValidationError` (carrying ``field``) when the key is
    missing, not a string, or blank after stripping whitespace.
    """
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(field)
    return value


# ---------------------------------------------------------------------------
# Title formatting — mirrors src/services/task-formatters.ts
# ---------------------------------------------------------------------------

_TASK_TITLE_TAG_MAX_LENGTH = 3

# Matches existing ID prefixes such as "[123] Title", "[POS-123]. Title",
# or "123. Title".  Same pattern as TASK_ID_PREFIX_REGEX on the TS side.
_TASK_ID_PREFIX_RE = re.compile(
    r"^(?:\[\s*(?:[A-Z0-9]{1,3}\s*-\s*)?(\d+)\s*\]\s*\.?\s*|\s*(\d+)\s*\.\s+)",
    re.IGNORECASE,
)


# First Unicode letter (same as /\p{L}/u in JS).
_FIRST_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


_SUFFIX_LENGTH = 6
_SUFFIX_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'

# Matches composite IDs: "POS-1295-a7x3mq", "1295-a7x3mq", or plain "1295"
_COMPOSITE_ID_RE = re.compile(
    r'^(?:([A-Z0-9]{1,3})-)?(\d+)(?:-([a-z0-9]+))?$',
    re.IGNORECASE,
)


def _generate_suffix() -> str:
    """Generate a 6-char cryptographically random alphanumeric suffix."""
    return ''.join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH))


def _compose_composite_id(tag: str | None, numeric_id: int, suffix: str) -> str:
    """Compose a composite task ID from tag, numeric ID, and suffix."""
    normalized_tag = _normalize_task_title_tag(tag)
    if normalized_tag:
        return f"{normalized_tag}-{numeric_id}-{suffix}"
    return f"{numeric_id}-{suffix}"


def extract_numeric_id(task_id: int | str) -> int | None:
    """Extract the numeric part from a composite or plain task ID.

    Handles: "POS-1295-a7x3mq" → 1295, "1295-a7x3mq" → 1295,
    "1295" → 1295, 1295 → 1295.
    """
    if isinstance(task_id, int):
        return task_id
    if isinstance(task_id, str):
        stripped = task_id.strip()
        m = _COMPOSITE_ID_RE.match(stripped)
        if m:
            return int(m.group(2))
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _is_composite_id(task_id: str) -> bool:
    """Return True if task_id contains a suffix (is composite format)."""
    m = _COMPOSITE_ID_RE.match(task_id.strip())
    return m is not None and m.group(3) is not None


def _normalize_task_title_tag(raw_tag: str | None) -> str | None:
    """Normalize a raw tag string to ≤3 uppercase alphanumeric chars, or None.

    Mirrors ``normalizeTaskTitleTag`` in ``task-formatters.ts``.
    """
    if not raw_tag:
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", raw_tag.strip().upper())[:_TASK_TITLE_TAG_MAX_LENGTH]
    return normalized or None


def _read_task_title_tag(project_root: str) -> str | None:
    """Read ``taskTitleTag`` from ``<project>/.praxis/config/general.yaml``.

    Does a simple line-based scan (no PyYAML dependency) — the value we need
    always sits on a top-level ``taskTitleTag: <value>`` line in this flat YAML
    file.  Returns ``None`` when the file is missing, unreadable, or the key is
    absent/blank/null.
    """
    config_path = os.path.join(project_root, ".praxis", "config", "general.yaml")
    try:
        with open(config_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("taskTitleTag:"):
                    value = stripped[len("taskTitleTag:"):].strip()
                    if not value or value.lower() == "null":
                        return None
                    # Strip optional surrounding quotes
                    if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                        value = value[1:-1]
                    return value
    except (FileNotFoundError, OSError):
        pass
    return None


def _read_context_router_default(project_root: str) -> bool:
    """Read ``contextRouterIncludedByDefault`` from ``general.yaml``.

    Same line-based scan as :func:`_read_task_title_tag`.  Returns ``True``
    when the file is missing, unreadable, or the key is absent (the UI
    default).
    """
    config_path = os.path.join(project_root, ".praxis", "config", "general.yaml")
    try:
        with open(config_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("contextRouterIncludedByDefault:"):
                    value = stripped[len("contextRouterIncludedByDefault:"):].strip().lower()
                    return value not in ("false", "no", "0", "null", "")
    except (FileNotFoundError, OSError):
        pass
    return True


def _format_title(task_id: int | str, raw_title: str, task_title_tag: str | None) -> str:
    """Format a task title with the ``[TAG-id]`` or ``[id]`` prefix.

    Replicates the logic of ``formatTaskTitle`` in ``task-formatters.ts`` so
    that tasks created via the API are indistinguishable from those created in
    the UI.

    Steps (matching the TS implementation):
    1. Strip leading/trailing whitespace.
    2. Default empty titles to "Task {id}".
    3. Remove any existing ID prefix patterns (prevents duplication).
    4. Capitalize the first alphabetical character (Unicode-aware).
    5. Prepend ``[TAG-id]`` (or ``[id]``) prefix.
    """
    numeric_id = extract_numeric_id(task_id) if isinstance(task_id, str) else task_id
    if numeric_id is None:
        numeric_id = 0
    normalized_tag = _normalize_task_title_tag(task_title_tag)
    prefix = f"[{normalized_tag}-{numeric_id}]" if normalized_tag else f"[{numeric_id}]"

    # Step 1
    cleaned = raw_title.strip()

    # Step 2
    if not cleaned:
        return f"{prefix} Task {numeric_id}"

    # Step 3
    m = _TASK_ID_PREFIX_RE.match(cleaned)
    if m:
        cleaned = cleaned[m.end():].strip()

    # Step 4
    if not cleaned:
        return f"{prefix} Task {numeric_id}"

    # Step 5 — capitalize first Unicode letter
    letter_match = _FIRST_LETTER_RE.search(cleaned)
    if letter_match:
        idx = letter_match.start()
        cleaned = cleaned[:idx] + letter_match.group().upper() + cleaned[idx + 1:]

    return f"{prefix} {cleaned}"


def _resolve_assignee(project_root: str, raw_value: str | None) -> str | None:
    """Resolve a raw assignee reference to an existing filename in .praxis/assignees/.

    Resolution order:
    1. Exact filename match (e.g., ``"jarvis.json"``).
    2. Append ``.json`` and match case-insensitively (e.g., ``"jarvis"`` → ``"jarvis.json"``).
    3. Match against the ``name`` field inside each assignee JSON (case-insensitive,
       e.g., ``"Jarvis"`` → ``"jarvis.json"``).

    Returns ``None`` when *raw_value* is ``None`` or blank (no assignee assigned).

    Raises :class:`ValidationError` when the value is non-empty but cannot be
    resolved to an existing assignee file.
    """
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return None
    if not isinstance(raw_value, str):
        raise ValidationError("assignee", "assignee must be a string")

    value = raw_value.strip()
    assignees_dir = os.path.join(project_root, ".praxis", "assignees")

    # 1. Exact filename match
    if os.path.isfile(os.path.join(assignees_dir, value)):
        return value

    # Build lowered-filename → actual-filename mapping for remaining checks
    existing: dict[str, str] = {}
    if os.path.isdir(assignees_dir):
        for entry in os.listdir(assignees_dir):
            if entry.endswith(".json"):
                existing[entry.lower()] = entry

    # 2. Append .json, case-insensitive
    candidate = value.lower() if value.lower().endswith(".json") else f"{value.lower()}.json"
    if candidate in existing:
        return existing[candidate]

    # 3. Match by name field inside assignee files
    for filename in sorted(existing.values()):
        filepath = os.path.join(assignees_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        rm = data.get("role_and_mission")
        name = rm.get("name", "") if isinstance(rm, dict) else data.get("name", "")
        if isinstance(name, str) and name.strip().lower() == value.lower():
            return filename

    raise ValidationError(
        "assignee",
        f"assignee '{raw_value}' not found in .praxis/assignees/",
    )


def _allocate_id(project_root: str) -> tuple[str, int]:
    """Allocate the next task id for ``project_root`` under an exclusive lock.

    Ensures ``.praxis/config/`` exists, then holds an exclusive lock on
    ``.praxis/config/task_counter.lock`` (``fcntl.flock`` on Unix,
    ``msvcrt.locking`` on Windows) around the whole read-compute-write so two
    concurrent API calls can never read the same counter value. Inside the lock
    it reads ``.praxis/config/task_counter`` (a missing, blank, or non-integer
    value is treated as ``0``), scans every subdirectory of
    ``.praxis/tasks/`` for the largest integer id embedded in a ``*.json``
    stem (legacy ``<int>.json`` or composite ``<tag>-<int>-<suffix>.json``),
    computes ``next = max(counter_value, filesystem_max) + 1``, writes
    ``next`` back to the counter. The ``max(..., filesystem_max)`` rule
    mirrors the browser's stale-counter recovery in ``main-app-task-io.ts``
    so the app and the API never disagree.

    Also ensures ``.praxis/tasks/new/`` exists so the subsequent write lands.

    Returns ``(composite_id, numeric_id)`` where composite_id is the full
    string like ``POS-1295-a7x3mq`` and numeric_id is the sequential number.
    """
    praxis_dir = os.path.join(project_root, ".praxis")
    config_dir = os.path.join(praxis_dir, "config")
    tasks_dir = os.path.join(praxis_dir, "tasks")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(os.path.join(tasks_dir, "new"), exist_ok=True)

    counter_path = os.path.join(config_dir, "task_counter")
    lock_path = os.path.join(config_dir, "task_counter.lock")

    if _IS_WINDOWS:
        lock_fh = open(lock_path, "a+b")
        try:
            msvcrt.locking(lock_fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                counter_value = _read_counter(counter_path)
                filesystem_max = _scan_max_task_id(tasks_dir)
                next_id = max(counter_value, filesystem_max) + 1
                _write_counter(counter_path, next_id)
            finally:
                lock_fh.seek(0)
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_fh.close()
    else:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            counter_value = _read_counter(counter_path)
            filesystem_max = _scan_max_task_id(tasks_dir)
            next_id = max(counter_value, filesystem_max) + 1
            _write_counter(counter_path, next_id)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    tag = _read_task_title_tag(project_root)
    suffix = _generate_suffix()
    composite_id = _compose_composite_id(tag, next_id, suffix)
    return composite_id, next_id


def _read_counter(counter_path: str) -> int:
    """Read the integer in ``counter_path``; missing/blank/non-int → ``0``."""
    try:
        with open(counter_path, encoding="utf-8") as handle:
            raw = handle.read().strip()
    except FileNotFoundError:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _write_counter(counter_path: str, value: int) -> None:
    """Persist ``value`` as the new task counter (plain integer text)."""
    with open(counter_path, "w", encoding="utf-8") as handle:
        handle.write(str(value))


def _scan_max_task_id(tasks_dir: str) -> int:
    """Return the largest numeric task ID across ``tasks_dir`` subdirs.

    Scans every immediate subdirectory of ``.praxis/tasks/`` (each a status
    column such as ``new`` / ``in_progress``) for ``*.json`` files and
    returns the maximum numeric id embedded in the filename, or ``0`` when
    none are found. Handles both legacy ``<int>.json`` and composite
    ``<tag>-<int>-<suffix>.json`` filenames. Non-matching stems and
    non-directory entries (e.g. a legacy ``task_counter`` file living directly
    under ``tasks/``) are ignored.
    """
    max_id = 0
    try:
        status_dirs = os.scandir(tasks_dir)
    except FileNotFoundError:
        return 0
    with status_dirs:
        for status_entry in status_dirs:
            if not status_entry.is_dir():
                continue
            with os.scandir(status_entry.path) as task_entries:
                for task_entry in task_entries:
                    if not task_entry.name.endswith(".json"):
                        continue
                    stem = task_entry.name[: -len(".json")]
                    numeric = extract_numeric_id(stem)
                    if numeric is not None and numeric > max_id:
                        max_id = numeric
    return max_id


def allocate_task_id(project_root: str) -> str:
    """Reserve and return the next collision-free composite task ID.

    Public wrapper around :func:`_allocate_id` for use by MCP tools
    that need to reserve an ID without creating a full task.
    """
    composite_id, _numeric_id = _allocate_id(project_root)
    return composite_id


def _build_task(composite_id: str, numeric_id: int, payload: dict, project_root: str) -> dict:
    """Assemble the full on-disk task dict from a validated ``payload``.

    Required fields are already validated by the caller. Optional fields fall
    back to their UI defaults (``important_constraints=""``, ``labels=[]``,
    ``assignee=None``); ``priority`` is included only when the caller provides
    a non-empty string. Every server-controlled field (``id``, ``status``,
    timestamps, ``on_hold_reason``, ``contextRouterIncluded``)
    is set here and any caller-supplied value for them is ignored. Key order
    mirrors a real task file (verified against
    ``.praxis/tasks/in_progress/1503.json``).

    The title is formatted with the ``[TAG-id]`` prefix (or ``[id]`` when no
    tag is configured) to match the frontend create path in
    ``src/services/task-formatters.ts``.
    """
    tag = _read_task_title_tag(project_root)
    formatted_title = _format_title(numeric_id, payload["title"], tag)

    now = _now_ms()
    task: dict = {
        "id": composite_id,
        "assignee": payload.get("assignee"),
        "priority": None,  # placeholder; removed or set just below
        "status": "new",
        "title": formatted_title,
        "why_we_need_this": payload["why_we_need_this"],
        "acceptance_criteria": payload["acceptance_criteria"],
        "important_constraints": payload.get("important_constraints") or "",
        "labels": payload.get("labels") if isinstance(payload.get("labels"), list) else [],
        "on_hold_reason": "",
        "contextRouterIncluded": _read_context_router_default(project_root),
        "created_at": now,
        "column_entered_at": now,
    }

    # `priority` is optional: include it (in its real-file slot, right after
    # `assignee`) only when the caller supplied a non-empty string.
    priority = payload.get("priority")
    if isinstance(priority, str) and priority.strip():
        task["priority"] = priority
    else:
        del task["priority"]

    return task


def create_task(project_root: str, payload: dict) -> dict:
    """Create a new task file under ``<project_root>/.praxis/tasks/new/``.

    Validates that ``title``, ``why_we_need_this``, and ``acceptance_criteria``
    are present and non-blank (raising :class:`ValidationError` carrying the
    first offending field otherwise), allocates a collision-free id under an
    exclusive lock, assembles the full task dict with server-set fields, and
    atomically writes ``{id}.json`` (temp file in the same directory then
    ``os.replace``) with the repository's JSON style (``indent=2``,
    ``ensure_ascii=False``, trailing newline).

    Returns ``{"id": <str>, "path": ".praxis/tasks/new/<id>.json"}`` — the
    project-relative path the HTTP layer echoes back to the caller. ``id`` is
    a composite string like ``POS-1295-a7x3mq``.

    Raises:
        ValidationError: when a required field is missing or blank.
    """
    for field in _REQUIRED_FIELDS:
        _require_non_empty_str(payload, field)

    # Resolve assignee reference → validated filename (or None)
    raw_assignee = payload.get("assignee")
    if raw_assignee:
        payload["assignee"] = _resolve_assignee(project_root, raw_assignee)

    composite_id, numeric_id = _allocate_id(project_root)
    task = _build_task(composite_id, numeric_id, payload, project_root)

    new_dir = os.path.join(project_root, ".praxis", "tasks", "new")
    os.makedirs(new_dir, exist_ok=True)
    final_path = os.path.join(new_dir, f"{composite_id}.json")
    tmp_path = os.path.join(new_dir, f".{composite_id}.json.tmp")

    # Atomic write: serialize to a sibling temp file, fsync, then os.replace so
    # the board's reader never sees a half-written JSON.
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(task, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, final_path)

    return {"id": composite_id, "path": f".praxis/tasks/new/{composite_id}.json"}


# ---------------------------------------------------------------------------
# update_task — partial field updates on existing tasks
# ---------------------------------------------------------------------------


def _find_task_file(project_root: str, task_id: int | str) -> tuple[str, str] | None:
    """Locate a task JSON file by scanning all status directories.

    Accepts both composite string IDs (e.g., ``POS-1295-a7x3mq``) and
    legacy numeric IDs (e.g., ``1295``).

    Returns ``(absolute_path, status_dir_name)`` or ``None``.
    """
    tasks_root = os.path.join(project_root, ".praxis", "tasks")

    # Try exact filename match first (composite or numeric)
    filename = f"{task_id}.json"
    for status_dir in _STATUS_DIRECTORIES:
        candidate = os.path.join(tasks_root, status_dir, filename)
        if os.path.isfile(candidate):
            return candidate, status_dir

    # If numeric, scan for composite files containing this numeric ID
    numeric = extract_numeric_id(task_id) if isinstance(task_id, str) else task_id
    if numeric is not None:
        for status_dir in _STATUS_DIRECTORIES:
            status_path = os.path.join(tasks_root, status_dir)
            if not os.path.isdir(status_path):
                continue
            try:
                for entry in os.scandir(status_path):
                    if not entry.name.endswith(".json") or not entry.is_file():
                        continue
                    stem = entry.name[:-len(".json")]
                    entry_numeric = extract_numeric_id(stem)
                    if entry_numeric == numeric:
                        return entry.path, status_dir
            except OSError:
                continue
    return None


def _validate_update_fields(
    project_root: str, fields: dict,
) -> dict:
    """Filter *fields* through the allowlist and validate each value.

    Returns a dict containing only the valid, allowed fields.  Raises
    :class:`ValidationError` when a field's value fails validation.
    Unsupported keys are silently dropped.
    """
    validated: dict = {}
    for key, value in fields.items():
        if key not in _UPDATABLE_FIELDS:
            continue  # silently ignore unsupported fields

        if key == "priority":
            if not isinstance(value, str) or value.strip() not in _VALID_PRIORITIES:
                raise ValidationError(
                    "priority",
                    f"invalid priority '{value}'. Must be one of: {', '.join(_VALID_PRIORITIES)}",
                )
            validated[key] = value.strip()

        elif key == "assignee":
            validated[key] = _resolve_assignee(project_root, value)

        elif key == "labels":
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValidationError("labels", "labels must be an array of strings")
            validated[key] = value

        else:
            # String fields: title, why_we_need_this, acceptance_criteria,
            # important_constraints, on_hold_reason — type-check only.
            if not isinstance(value, str):
                raise ValidationError(key, f"{key} must be a string")
            validated[key] = value

    return validated


def update_task(project_root: str, task_id: int | str, fields: dict) -> dict:
    """Apply a partial update to an existing task's JSON file on disk.

    Only fields listed in :data:`_UPDATABLE_FIELDS` are written; all others
    are silently ignored.  The file is **not** moved between status
    directories — use ``update_task_status`` to change the task's lifecycle
    column.  If the located task file still uses the legacy numeric-only
    filename (e.g. ``1295.json``), it is migrated in-place to the composite
    format (e.g. ``POS-1295-a7x3mq.json``) as part of this update.

    Accepts both composite string IDs and legacy numeric IDs.

    Returns ``{"ok": True, "task_id": <str>, "updated_fields": [<str>, ...]}``
    where ``task_id`` is the (possibly newly-migrated) composite/legacy stem.

    Raises:
        TaskNotFoundError: when no task with *task_id* exists on disk.
        ValidationError: when a provided field value fails validation.
    """
    numeric = extract_numeric_id(task_id)
    if numeric is None or numeric <= 0:
        raise TaskNotFoundError(task_id)

    result = _find_task_file(project_root, task_id)
    if result is None:
        raise TaskNotFoundError(task_id)

    source_path, _status = result

    validated = _validate_update_fields(project_root, fields)

    # --- Migration: if file has numeric-only name, migrate to composite ---
    current_filename = os.path.basename(source_path)
    current_stem = current_filename[:-len(".json")]
    if not _is_composite_id(current_stem):
        tag = _read_task_title_tag(project_root)
        suffix = _generate_suffix()
        new_composite_id = _compose_composite_id(tag, numeric, suffix)
        new_filename = f"{new_composite_id}.json"
        new_path = os.path.join(os.path.dirname(source_path), new_filename)

        # Read, update id, write to new path, remove old
        with open(source_path, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        data["id"] = new_composite_id
        # Apply validated fields
        updated_fields: list[str] = []
        if "title" in validated:
            ttag = _read_task_title_tag(project_root)
            validated["title"] = _format_title(numeric, validated["title"], ttag)
        for key, value in validated.items():
            data[key] = value
            updated_fields.append(key)

        dir_path = os.path.dirname(source_path)
        tmp_path = os.path.join(dir_path, f".{new_composite_id}.json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, new_path)
        if os.path.isfile(source_path) and source_path != new_path:
            os.remove(source_path)
        return {"ok": True, "task_id": new_composite_id, "updated_fields": sorted(updated_fields)}

    # --- Normal update (already composite) ---
    if not validated:
        return {"ok": True, "task_id": current_stem, "updated_fields": []}

    with open(source_path, "r", encoding="utf-8") as f:
        data = json.loads(f.read())

    if "title" in validated:
        tag = _read_task_title_tag(project_root)
        validated["title"] = _format_title(numeric, validated["title"], tag)

    updated_fields = []
    for key, value in validated.items():
        data[key] = value
        updated_fields.append(key)

    dir_path = os.path.dirname(source_path)
    tmp_path = os.path.join(dir_path, f".{current_stem}.json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, source_path)

    return {"ok": True, "task_id": current_stem, "updated_fields": sorted(updated_fields)}
