"""Read and update PraxisOS project settings (.praxis/config/general.yaml).

Handles the project configuration file (general.yaml) and the two file-tree
ignore configs (file_tree_ignore.yaml, docs_file_tree_ignore.yaml) without
requiring PyYAML — uses focused, line-based parsing tuned to the predictable
YAML dialect emitted by the frontend's ``js-yaml`` library.

Writes to general.yaml (POS-2296) are hardened: a ``.bak`` copy of the
previous content is made before every write, writes land via a temp file +
``os.replace`` (atomic, never leaves a partial file), reads recover from
``.bak`` when the primary file is corrupted, a corrupted primary is
quarantined as ``.corrupted`` on the next write, and a ``general.yaml.lock``
``flock`` serializes concurrent writers. Updates are block-level: only the
top-level keys being changed are replaced, so unknown content (block
scalars, nested lists) elsewhere in the file survives byte-for-byte.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # Windows: no flock; writes proceed unlocked (degraded mode)
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".bak"
CORRUPTED_SUFFIX = ".corrupted"
LOCK_SUFFIX = ".lock"
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05

# Keys the API may write itself but that are NOT exposed to the MCP update_settings tool.
_INTERNAL_SETTINGS: set[str] = {"absoluteProjectPath"}

__all__ = [
    "read_settings",
    "read_general_config",
    "update_settings",
    "update_ignore_paths",
    "read_linked_projects",
    "resolve_project_roots",
]

# ---------------------------------------------------------------------------
# YAML value headers
# ---------------------------------------------------------------------------

_GENERAL_YAML_HEADER = (
    "# PraxisOS Configuration\n"
    "# These settings are project-specific and should be version-controlled.\n"
    "# Modifications via the Settings modal will update this file.\n"
    "#\n"
)

_IGNORE_YAML_HEADER = (
    "# PRAXIS file tree ignore configuration\n"
    "# Paths are root-relative and applied in addition to built-in system exclusions.\n"
    "#\n"
)

# ---------------------------------------------------------------------------
# Settings exposed via the Settings modal — these are writable by the agent.
# Keys use camelCase to match the YAML file exactly.
# ---------------------------------------------------------------------------

_WRITABLE_SETTINGS: set[str] = {
    # General tab
    "taskTitleTag",
    "markdownDoubleClickSelectionMode",
    "pythonCommand",
    "contextRouterIncludedByDefault",
    "chatEnabled",
    "dateAwareness",
    "linkedProjects",
    # Git tab
    "addTasksToCommits",
    "autoVersionEnabled",
    "autoVersionFilePath",
    # Sound tab
    "soundVolume",
    # Other (not in Settings modal but agent-relevant)
    "defaultWorker",
    "projectIcon",
}

_BOOLEAN_KEYS: set[str] = {
    "contextRouterIncludedByDefault",
    "chatEnabled",
    "dateAwareness",
    "addTasksToCommits",
    "autoVersionEnabled",
}

_STRING_KEYS: set[str] = {
    "markdownDoubleClickSelectionMode",
    "pythonCommand",
    "autoVersionFilePath",
    "defaultWorker",
    "linkedProjects",
    "absoluteProjectPath",
}

# Mapping from MCP tool snake_case parameter names → camelCase YAML keys.
PARAM_TO_YAML: dict[str, str] = {
    "task_title_tag": "taskTitleTag",
    "markdown_double_click_selection_mode": "markdownDoubleClickSelectionMode",
    "python_command": "pythonCommand",
    "context_router_included_by_default": "contextRouterIncludedByDefault",
    "chat_enabled": "chatEnabled",
    "add_tasks_to_commits": "addTasksToCommits",
    "auto_version_enabled": "autoVersionEnabled",
    "auto_version_file_path": "autoVersionFilePath",
    "sound_volume": "soundVolume",
    "default_worker": "defaultWorker",
    "project_icon": "projectIcon",
    "date_awareness": "dateAwareness",
}

# Defaults matching DEFAULT_PRAXIS_CONFIG in config-types.ts.
_DEFAULTS: dict[str, object] = {
    "absoluteProjectPath": "",
    "defaultWorker": "assistant.json",
    "contextRouterIncludedByDefault": True,
    "projectIcon": None,
    "addTasksToCommits": False,
    "taskTitleTag": None,
    "autoVersionEnabled": False,
    "chatEnabled": False,
    "dateAwareness": False,
    "autoVersionFilePath": "/version",
    "markdownDoubleClickSelectionMode": "default",
    "soundVolume": 1,
    "pythonCommand": "python3",
    "linkedProjects": "",
    "aiMode": "agent",
}

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _config_path(project_root: str) -> str:
    return os.path.join(project_root, ".praxis", "config", "general.yaml")


def _ignore_path(project_root: str, scope: str) -> str:
    filename = (
        "file_tree_ignore.yaml" if scope == "prompts"
        else "docs_file_tree_ignore.yaml"
    )
    return os.path.join(project_root, ".praxis", "config", filename)


# ---------------------------------------------------------------------------
# Lightweight YAML parser — handles the exact dialect emitted by js-yaml.
# Supports flat key-value pairs, one nesting level, and list items.
# ---------------------------------------------------------------------------

_TASK_TITLE_TAG_RE = re.compile(r"^[A-Z0-9]{1,3}$")


def _parse_yaml_value(raw: str) -> object:
    """Convert a raw YAML value string to a Python object."""
    if not raw or raw.lower() == "null" or raw == "~":
        return None
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    # Quoted string
    if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
        return raw[1:-1]
    # Try numeric
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _format_yaml_value(value: object) -> str:
    """Serialize a Python value to YAML scalar representation."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # repr-based formatting survives a round trip (e.g. 54.953125), unlike
        # %g which would round it; trailing ".0" is trimmed so 1.0 stays "1".
        text = repr(value)
        return text[:-2] if text.endswith(".0") else text
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # Quote if the value contains YAML-special characters
        if not value or any(c in value for c in ":{}[]&*#?|->!%@`,\n"):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value
    return str(value)


def _parse_yaml_file(content: str) -> dict:
    """Parse a simple YAML file into a nested dict.

    Handles:
    - ``key: value`` at the top level
    - One level of indented ``key: value`` (nested dicts)
    - Indented ``- item`` (lists)
    - Comment lines and blank lines (skipped)
    """
    result: dict = {}
    current_parent: str | None = None
    current_parent_is_list = False

    for line in content.split("\n"):
        # Skip blank and comment lines
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        if indent == 0:
            # Top-level key
            current_parent = None
            current_parent_is_list = False
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                # Nested block or empty list — peek ahead handled by indent>0
                current_parent = key
                result[key] = {}  # placeholder, replaced if list items follow
            else:
                result[key] = _parse_yaml_value(value)
        elif indent > 0 and current_parent is not None:
            if stripped.startswith("- "):
                # List item
                item_value = stripped[2:].strip()
                if not current_parent_is_list:
                    result[current_parent] = []
                    current_parent_is_list = True
                result[current_parent].append(_parse_yaml_value(item_value))
            elif ":" in stripped:
                # Nested key-value
                if current_parent_is_list:
                    continue  # Shouldn't happen in our format
                key, _, value = stripped.partition(":")
                key = key.strip()
                value = value.strip()
                if not isinstance(result.get(current_parent), dict):
                    result[current_parent] = {}
                result[current_parent][key] = _parse_yaml_value(value)

    return result


def _serialize_yaml(data: dict) -> str:
    """Serialize a dict back to YAML format matching js-yaml output style."""
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {_format_yaml_value(sub_value)}")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_format_yaml_value(item)}")
        else:
            lines.append(f"{key}: {_format_yaml_value(value)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Backup / atomic-write / lock / corruption-recovery helpers (POS-2296)
# ---------------------------------------------------------------------------

_TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*:")


def _backup_path(config_file: str) -> str:
    return config_file + BACKUP_SUFFIX


def _has_top_level_content(content: str) -> bool:
    """True when the text has at least one non-blank, non-comment line."""
    return any(line.strip() and not line.strip().startswith("#") for line in content.split("\n"))


def _read_config_text(path: str) -> str | None:
    """Return file text, ``None`` when missing/unreadable, ``""`` is a valid empty file.
    A UnicodeDecodeError is reported by raising ValueError so callers treat it as corruption."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8: {exc}") from None
    except OSError as exc:
        _logger.warning("Cannot read %s: %s", path, exc)
        return None


class _LoadedConfig:
    """Result of loading general.yaml with backup recovery.

    ``raw`` is the text to use as the base for block-level edits (primary text when
    valid, backup text when recovered, ``""`` when nothing valid exists).
    """
    __slots__ = ("config", "raw", "primary_corrupted", "restored_from_backup")

    def __init__(self, config: dict, raw: str, primary_corrupted: bool, restored_from_backup: bool) -> None:
        self.config = config
        self.raw = raw
        self.primary_corrupted = primary_corrupted
        self.restored_from_backup = restored_from_backup


def _parse_or_none(content: str) -> dict | None:
    """Parse text; return ``None`` when the text has content but yields no keys (corrupted)."""
    parsed = _parse_yaml_file(content)
    if not parsed and _has_top_level_content(content):
        return None
    return parsed


def _load_config_with_recovery(project_root: str) -> _LoadedConfig:
    config_file = _config_path(project_root)
    primary_corrupted = False
    try:
        text = _read_config_text(config_file)
    except ValueError as exc:
        _logger.warning("%s", exc)
        text = None
        primary_corrupted = True
    if text is not None:
        parsed = _parse_or_none(text)
        if parsed is not None:
            return _LoadedConfig(parsed, text, False, False)
        primary_corrupted = True
        _logger.warning("general.yaml at %s is corrupted; trying backup", config_file)
    # primary missing or corrupted → try .bak
    try:
        bak_text = _read_config_text(_backup_path(config_file))
    except ValueError:
        bak_text = None
    if bak_text is not None:
        bak_parsed = _parse_or_none(bak_text)
        if bak_parsed:
            _logger.warning("Using general.yaml.bak for %s", project_root)
            return _LoadedConfig(bak_parsed, bak_text, primary_corrupted, True)
    if primary_corrupted:
        _logger.error("general.yaml at %s is corrupted and no valid general.yaml.bak exists; using defaults", config_file)
    return _LoadedConfig({}, "", primary_corrupted, False)


def read_general_config(project_root: str) -> dict:
    """Public: parsed general.yaml dict with backup recovery (never raises)."""
    return _load_config_with_recovery(project_root).config


def _backup_config(config_file: str) -> None:
    """Copy general.yaml → general.yaml.bak. Failure is logged, never raised."""
    try:
        if os.path.isfile(config_file):
            shutil.copyfile(config_file, _backup_path(config_file))
    except OSError as exc:
        _logger.warning("general.yaml backup failed, continuing without backup: %s", exc)


def _quarantine_corrupted(config_file: str) -> str | None:
    """Move a corrupted general.yaml aside. Never overwrites an existing .corrupted file."""
    if not os.path.exists(config_file):
        return None
    target = config_file + CORRUPTED_SUFFIX
    if os.path.exists(target):
        target = f"{target}.{int(time.time())}"
        counter = 1
        while os.path.exists(target):  # same-second collision
            target = f"{config_file}{CORRUPTED_SUFFIX}.{int(time.time())}.{counter}"
            counter += 1
    try:
        os.replace(config_file, target)
        _logger.error("Quarantined corrupted general.yaml as %s", target)
        return target
    except OSError as exc:
        _logger.warning("Could not quarantine corrupted general.yaml: %s", exc)
        return None


def _atomic_write_text(path: str, content: str) -> None:
    """Write via temp file in the same directory + os.replace (never leaves a partial file)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def _config_lock(config_file: str):
    """Best-effort exclusive lock on general.yaml.lock (fcntl.flock, Unix only).
    Yields True when held, False in degraded mode. Never raises."""
    fd = None
    acquired = False
    if fcntl is not None:
        lock_path = config_file + LOCK_SUFFIX
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        _logger.warning("Timed out acquiring %s; writing without lock", lock_path)
                        break
                    time.sleep(_LOCK_POLL_SECONDS)
        except OSError as exc:
            _logger.warning("Cannot acquire config lock %s; writing without lock: %s", config_file + LOCK_SUFFIX, exc)
    try:
        yield acquired
    finally:
        if fd is not None:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)


def _serialize_entry(key: str, value: object) -> list[str]:
    return _serialize_yaml({key: value}).rstrip("\n").split("\n")


def _apply_top_level_updates(content: str, updates: dict[str, object]) -> str:
    """Replace/append top-level keys, preserving every other line verbatim.

    A key's block = its `key:` line plus all following lines that start with
    whitespace, plus blank lines that are followed by more indented lines
    (blank lines inside `|-` block scalars). Keys absent from *content* are
    appended at the end. Output always ends with exactly one newline.
    """
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    pending = dict(updates)
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = _TOP_LEVEL_KEY_RE.match(lines[i])
        if not match or match.group(1) not in pending:
            out.append(lines[i])
            i += 1
            continue
        key = match.group(1)
        out.extend(_serialize_entry(key, pending.pop(key)))
        i += 1
        while i < len(lines):
            if lines[i][:1] in (" ", "\t"):
                i += 1
                continue
            if lines[i].strip() == "":
                j = i
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j < len(lines) and lines[j][:1] in (" ", "\t"):
                    i = j
                    continue
            break
    for key, value in pending.items():
        out.extend(_serialize_entry(key, value))
    return "\n".join(out).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_settings(project_root: str) -> dict:
    """Read all settings from general.yaml and both ignore configs.

    Returns a structured dict organized by Settings tab for clarity.
    """
    # Read general.yaml (with .bak recovery — never raises)
    config = read_general_config(project_root)

    def _get(key: str, default: object = None) -> object:
        val = config.get(key)
        return val if val is not None else _DEFAULTS.get(key, default)

    chat_block = config.get("chat", {})
    if not isinstance(chat_block, dict):
        chat_block = {}

    settings = {
        "general": {
            "taskTitleTag": config.get("taskTitleTag"),
            "markdownDoubleClickSelectionMode": _get(
                "markdownDoubleClickSelectionMode", "default",
            ),
            "pythonCommand": _get("pythonCommand", "python3"),
            "contextRouterIncludedByDefault": _get(
                "contextRouterIncludedByDefault", True,
            ),
            "chatEnabled": _get("chatEnabled", False),
            "dateAwareness": _get("dateAwareness", False),
            "linkedProjects": _get("linkedProjects", ""),
        },
        "git": {
            "addTasksToCommits": _get("addTasksToCommits", False),
            "autoVersionEnabled": _get("autoVersionEnabled", False),
            "autoVersionFilePath": _get("autoVersionFilePath", "/version"),
        },
        "fileTree": {
            "fileTreeIgnorePaths": _read_ignore_paths(project_root, "prompts"),
        },
        "projectFiles": {
            "projectFilesIgnorePaths": _read_ignore_paths(
                project_root, "projectFiles",
            ),
        },
        "sound": {
            "soundVolume": _get("soundVolume", 1),
        },
        "other": {
            "defaultWorker": _get("defaultWorker", "assistant.json"),
            "projectIcon": config.get("projectIcon"),
            "aiMode": _get("aiMode", "agent"),
            "absoluteProjectPath": config.get("absoluteProjectPath", ""),
        },
    }

    return {"ok": True, "settings": settings}


def _read_ignore_paths(project_root: str, scope: str) -> list[str]:
    """Read ignore paths from a YAML ignore config file."""
    path = _ignore_path(project_root, scope)
    try:
        with open(path, encoding="utf-8") as fh:
            data = _parse_yaml_file(fh.read())
        paths = data.get("ignoredPaths", [])
        if isinstance(paths, list):
            return [str(p) for p in paths]
    except (FileNotFoundError, OSError):
        pass
    return []


LINKED_PROJECTS_KEY = "linkedProjects"


def read_linked_projects(project_root: str) -> list[str]:
    """Return the validated linked-project roots declared in general.yaml.

    ``linkedProjects`` is a comma-separated list of absolute project paths.  A
    linked project widens knowledge-base reads (``search_project_context``,
    ``read_context_route``) beyond the current project, so the value is
    validated defensively: an entry is kept only when it is an absolute path to
    an existing directory.  Configuration order is preserved; duplicates and
    *project_root* itself are dropped so a project can never search itself
    twice.  A missing or unreadable config yields an empty list — a linked
    project is an enhancement, never a hard dependency.
    """
    config = read_general_config(project_root)

    raw = config.get(LINKED_PROJECTS_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return []

    self_real = os.path.realpath(project_root)
    seen: set[str] = {self_real}
    resolved: list[str] = []

    for entry in raw.split(","):
        candidate = entry.strip()
        if len(candidate) > 1:
            candidate = candidate.rstrip("/\\")
        if not candidate or not os.path.isabs(candidate):
            continue
        if not os.path.isdir(candidate):
            continue
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        resolved.append(candidate)

    return resolved


def resolve_project_roots(project_root: str) -> list[str]:
    """Return the current project root followed by its linked project roots.

    The current project is always first so it wins every precedence decision
    (route name collisions, ranking ties).
    """
    return [project_root, *read_linked_projects(project_root)]


def update_settings(
    project_root: str, fields: dict[str, object], *, allow_internal: bool = False,
) -> dict:
    """Update specific settings in general.yaml.

    Only keys listed in ``_WRITABLE_SETTINGS`` are accepted, plus
    ``_INTERNAL_SETTINGS`` when ``allow_internal=True`` (used by the API's own
    startup code, never exposed to the MCP ``update_settings`` tool). Unknown
    or read-only keys are rejected.

    The write is block-level (see :func:`_apply_top_level_updates`): only the
    keys in *fields* are replaced in the file text, so unrelated content
    (block scalars, nested lists) is preserved byte-for-byte. A ``.bak`` copy
    of the previous content is made first, the write itself is atomic
    (temp file + ``os.replace``), and a ``general.yaml.lock`` flock
    serializes concurrent writers.

    Parameters
    ----------
    project_root:
        Absolute path to the project root directory.
    fields:
        Mapping of camelCase YAML key → new value.

    Returns
    -------
    dict
        ``{"ok": True, "updated": [...]}`` on success, or
        ``{"ok": False, "error": "..."}`` on failure.
    """
    # Validate field names
    allowed = _WRITABLE_SETTINGS | (_INTERNAL_SETTINGS if allow_internal else set())
    invalid_keys = set(fields.keys()) - allowed
    if invalid_keys:
        return {
            "ok": False,
            "error": (
                f"Unknown or read-only settings: {', '.join(sorted(invalid_keys))}. "
                f"Writable settings: {', '.join(sorted(_WRITABLE_SETTINGS))}"
            ),
        }

    if not fields:
        return {"ok": False, "error": "No settings provided to update"}

    # Validate field values
    for key, value in fields.items():
        error = _validate_setting(key, value)
        if error:
            return {"ok": False, "error": error, "field": key}

    # Normalize values
    normalized: dict[str, object] = {}
    for key, value in fields.items():
        if key == "taskTitleTag":
            value = _normalize_task_title_tag(value)
        elif key == "soundVolume":
            if isinstance(value, (int, float)):
                value = max(0.0, min(1.0, float(value)))
        elif key == "projectIcon":
            # projectIcon can be a string or null
            if isinstance(value, str) and not value.strip():
                value = None
        normalized[key] = value

    config_file = _config_path(project_root)
    with _config_lock(config_file):
        loaded = _load_config_with_recovery(project_root)
        base = loaded.raw if loaded.raw.strip() else _GENERAL_YAML_HEADER
        new_content = _apply_top_level_updates(base, normalized)
        try:
            if loaded.primary_corrupted:
                _quarantine_corrupted(config_file)   # keep the good .bak intact
            else:
                _backup_config(config_file)
            _atomic_write_text(config_file, new_content)
        except OSError as exc:
            return {"ok": False, "error": f"Cannot write config: {exc}"}
    return {"ok": True, "updated": list(fields.keys())}


def update_ignore_paths(
    project_root: str,
    scope: str,
    paths: list[str],
) -> dict:
    """Update ignore paths in a file tree ignore config.

    Parameters
    ----------
    project_root:
        Absolute path to the project root directory.
    scope:
        ``"prompts"`` for file_tree_ignore.yaml (prompt file tree),
        ``"projectFiles"`` for docs_file_tree_ignore.yaml (Project files tree).
    paths:
        Full replacement list of root-relative paths to ignore.

    Returns
    -------
    dict
        ``{"ok": True, "scope": ..., "paths": [...], "count": N}``
    """
    if scope not in ("prompts", "projectFiles"):
        return {
            "ok": False,
            "error": "scope must be 'prompts' or 'projectFiles'",
        }

    # Sanitize paths — reject traversal, strip whitespace
    sanitized: list[str] = []
    for p in paths:
        p = str(p).strip()
        if p and ".." not in p:
            # Normalize: remove leading slashes
            p = p.lstrip("/").lstrip("\\")
            if p:
                sanitized.append(p)

    target = _ignore_path(project_root, scope)
    data = {"ignoredPaths": sanitized}

    try:
        yaml_content = _serialize_yaml(data)
        _atomic_write_text(target, _IGNORE_YAML_HEADER + yaml_content)
    except OSError as exc:
        return {"ok": False, "error": f"Cannot write ignore config: {exc}"}

    return {
        "ok": True,
        "scope": scope,
        "paths": sanitized,
        "count": len(sanitized),
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _normalize_task_title_tag(value: object) -> str | None:
    """Normalize a task title tag the same way as the frontend.

    Uppercase, alphanumeric only, max 3 characters. Empty → None.
    """
    if value is None:
        return None
    raw = str(value).upper()
    normalized = re.sub(r"[^A-Z0-9]", "", raw)[:3]
    return normalized if normalized else None


def _validate_setting(key: str, value: object) -> str | None:
    """Validate a single setting value. Returns error message or ``None``."""
    if key in _BOOLEAN_KEYS:
        if not isinstance(value, bool):
            return f"'{key}' must be a boolean (true/false)"
    elif key == "soundVolume":
        if not isinstance(value, (int, float)):
            return f"'{key}' must be a number between 0 and 1"
        if value < 0 or value > 1:
            return f"'{key}' must be between 0 and 1"
    elif key == "markdownDoubleClickSelectionMode":
        if value not in ("default", "fullPath"):
            return f"'{key}' must be 'default' or 'fullPath'"
    elif key == "taskTitleTag":
        if value is not None and not isinstance(value, str):
            return f"'{key}' must be a string or null"
    elif key == "projectIcon":
        if value is not None and not isinstance(value, str):
            return f"'{key}' must be a string or null"
    elif key in _STRING_KEYS:
        if not isinstance(value, str):
            return f"'{key}' must be a string"
    return None
