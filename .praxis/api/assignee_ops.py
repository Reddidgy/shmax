"""Assignee creation operations for the Praxis Local API.

Creates assignee JSON files in a PraxisOS project's ``.praxis/assignees/``
directory.  Mirrors the frontend filename-sanitisation and uniqueness logic
from ``assignee-parsing.ts``.
"""

from __future__ import annotations

import json
import os
import re


class ValidationError(Exception):
    """Raised when assignee payload validation fails."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


# ---------------------------------------------------------------------------
# Filename helpers (port of frontend sanitizeFileNameBase / buildUniqueFileName)
# ---------------------------------------------------------------------------

def _sanitize_file_name_base(raw_value: str) -> str:
    """Sanitise *raw_value* into a safe, lowercase, hyphen-separated base name.

    Mirrors ``sanitizeFileNameBase`` in ``assignee-parsing.ts``.
    """
    cleaned = raw_value.strip().lower()
    # Strip trailing .json
    cleaned = re.sub(r"\.json$", "", cleaned, flags=re.IGNORECASE)
    # Keep only alphanumeric, hyphens, underscores, whitespace
    cleaned = re.sub(r"[^a-z0-9\-_\s]", "", cleaned)
    # Collapse whitespace into single hyphen
    cleaned = re.sub(r"\s+", "-", cleaned)
    # Collapse multiple hyphens
    cleaned = re.sub(r"-+", "-", cleaned)
    # Strip leading/trailing hyphens and underscores
    cleaned = re.sub(r"^[-_]+|[-_]+$", "", cleaned)
    return cleaned or "assignee"


def _build_unique_file_name(base_name: str, existing_names: set[str]) -> str:
    """Return a unique ``.json`` filename derived from *base_name*.

    Mirrors ``buildUniqueFileName`` in ``assignee-parsing.ts``.
    """
    normalised = _sanitize_file_name_base(base_name)
    candidate = f"{normalised}.json"
    index = 2
    while candidate.lower() in existing_names:
        candidate = f"{normalised}-{index}.json"
        index += 1
    return candidate


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("name", "role", "mission")

_ALL_PROFILE_FIELDS = (
    "name",
    "role",
    "mission",
    "mandatory_constraints",
    "edge_cases_and_fallbacks",
    "workflow_and_response_format",
)


def create_assignee(project_root: str, payload: dict) -> dict[str, str]:
    """Create a new assignee JSON file and return its metadata.

    Parameters
    ----------
    project_root:
        Absolute path to the PraxisOS project directory.
    payload:
        Dict with at least ``name``, ``role``, ``mission``.  May also
        include ``mandatory_constraints``, ``edge_cases_and_fallbacks``,
        ``workflow_and_response_format``, and ``assignee_icon``.

    Returns
    -------
    dict
        ``{"file": "<filename>", "name": "<display name>"}``

    Raises
    ------
    ValidationError
        When a required field is missing or empty.
    """
    # --- validate required fields ---
    for field in _REQUIRED_FIELDS:
        value = payload.get(field)
        if not value or not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"Field '{field}' is required and must be a non-empty string.",
                field=field,
            )

    # --- build profile dict ---
    profile: dict[str, object] = {}
    for field in _ALL_PROFILE_FIELDS:
        raw = payload.get(field, "")
        profile[field] = str(raw).strip() if raw else ""

    # Handle assignee-icon (param uses underscore, JSON key uses hyphen)
    icon = payload.get("assignee_icon", "")
    if icon and isinstance(icon, str) and icon.strip():
        profile["assignee-icon"] = icon.strip()

    # --- ensure assignees directory ---
    assignees_dir = os.path.join(project_root, ".praxis", "assignees")
    os.makedirs(assignees_dir, exist_ok=True)

    # --- compute unique filename ---
    existing_names: set[str] = set()
    if os.path.isdir(assignees_dir):
        for entry in os.listdir(assignees_dir):
            if entry.lower().endswith(".json"):
                existing_names.add(entry.lower())

    name_str = str(profile["name"])
    explicit_file = payload.get("file", "")
    if explicit_file and isinstance(explicit_file, str) and explicit_file.strip():
        file_name = _build_unique_file_name(explicit_file.strip(), existing_names)
    else:
        file_name = _build_unique_file_name(name_str, existing_names)

    # --- write file ---
    file_path = os.path.join(assignees_dir, file_name)
    tmp_path = os.path.join(assignees_dir, f".{file_name}.tmp")

    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, file_path)

    return {"file": file_name, "name": name_str}
