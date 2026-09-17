"""Assignee query operations for the Praxis Local API.

Reads assignee JSON files from a PraxisOS project's ``.praxis/assignees/``
directory and returns minimal summaries (name + role only), with optional
keyword-based filtering.
"""

from __future__ import annotations

import json
import math
import os


def get_assignees(project_root: str) -> list[dict[str, str]]:
    """Return a list of assignees with ``file``, ``name``, and ``role`` fields.

    Reads all ``.json`` files under ``<project_root>/.praxis/assignees/``.
    Each assignee file may store name/role either at the top level or nested
    inside a ``role_and_mission`` dict.  Only assignees with a non-empty name
    are included in the result.

    The ``file`` field contains the JSON filename (e.g. ``"jarvis.json"``).
    This is the value that must be passed as the ``assignee`` parameter in
    ``create_task`` — the frontend stores assignees by filename, not by
    display name.

    Parameters
    ----------
    project_root:
        Absolute path to the PraxisOS project directory.

    Returns
    -------
    list[dict[str, str]]
        Each dict contains exactly ``{"file": ..., "name": ..., "role": ...}``.
    """
    assignees_dir = os.path.join(project_root, ".praxis", "assignees")
    if not os.path.isdir(assignees_dir):
        return []

    result: list[dict[str, str]] = []
    for filename in sorted(os.listdir(assignees_dir)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(assignees_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        name, role = _extract_name_role(data)
        if name:
            result.append({"file": filename, "name": name, "role": role})

    return result


# Minimum keyword count before partial matching activates.
# Mirrors ``project_context_search.MIN_KEYWORDS_FOR_PARTIAL``.
_MIN_KEYWORDS_FOR_PARTIAL = 3


def find_assignee_by_keywords(
    project_root: str,
    query: str,
) -> list[dict[str, str]]:
    """Search assignees by keyword across ``name``, ``role``, and ``mission``.

    Uses coverage-matching logic identical to
    :func:`project_context_search.search_project_context`:

    - 1-2 keywords: all must appear (AND logic).
    - 3+ keywords: at least ``ceil(n / 2)`` must match.

    Matching is case-insensitive with partial word matching (substring).
    Returns the same ``{ file, name, role }`` shape as :func:`get_assignees`,
    filtered to matches. An empty query or no matches returns ``[]``.

    Parameters
    ----------
    project_root:
        Absolute path to the PraxisOS project directory.
    query:
        Space-separated keywords to search for.
    """
    keywords = query.lower().split()
    if not keywords:
        return []

    min_required = (
        len(keywords)
        if len(keywords) < _MIN_KEYWORDS_FOR_PARTIAL
        else math.ceil(len(keywords) / 2)
    )

    assignees_dir = os.path.join(project_root, ".praxis", "assignees")
    if not os.path.isdir(assignees_dir):
        return []

    result: list[dict[str, str]] = []
    for filename in sorted(os.listdir(assignees_dir)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(assignees_dir, filename)
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

        name, role = _extract_name_role(data)
        if not name:
            continue

        mission = _extract_mission(data)

        # Build a single searchable text blob from name + role + mission.
        searchable = f"{name} {role} {mission}".lower()

        matched_count = sum(1 for kw in keywords if kw in searchable)
        if matched_count >= min_required:
            result.append({"file": filename, "name": name, "role": role})

    return result


_PROFILE_FIELDS = (
    "name",
    "role",
    "mission",
    "mandatory_constraints",
    "edge_cases_and_fallbacks",
    "self_validation_rules",
    "workflow_and_response_format",
)


def get_assignee_profile(
    filename: str,
    project_root: str,
) -> dict[str, str] | None:
    """Read a single assignee profile by filename and return its profile fields.

    Returns a dict with the profile fields (name, role, mission, etc.)
    or ``None`` if the file is missing, unreadable, or lacks a name.
    """
    assignees_dir = os.path.join(project_root, ".praxis", "assignees")
    filepath = os.path.join(assignees_dir, filename)
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    name, _ = _extract_name_role(data)
    if not name:
        return None

    result: dict[str, str] = {}
    for field in _PROFILE_FIELDS:
        if field in ("name", "role"):
            n, r = _extract_name_role(data)
            result["name"] = n
            result["role"] = r
        elif field == "mission":
            result["mission"] = _extract_mission(data)
        else:
            result[field] = str(data.get(field, "")).strip()
    return result


def _extract_mission(data: dict) -> str:
    """Extract the mission field from an assignee JSON structure.

    Supports both flat and nested ``role_and_mission`` layouts.
    """
    rm = data.get("role_and_mission")
    if isinstance(rm, dict):
        mission = rm.get("mission", "")
    else:
        mission = data.get("mission", "")
    return str(mission).strip()


def _extract_name_role(data: dict) -> tuple[str, str]:
    """Extract name and role from an assignee JSON structure.

    Supports two layouts:
    - Flat: top-level ``name`` and ``role`` keys.
    - Nested: ``role_and_mission`` dict containing ``name`` and ``role``.
    """
    rm = data.get("role_and_mission")
    if isinstance(rm, dict):
        name = rm.get("name", "")
        role = rm.get("role", "")
    else:
        name = data.get("name", "")
        role = data.get("role", "")
    return str(name).strip(), str(role).strip()
