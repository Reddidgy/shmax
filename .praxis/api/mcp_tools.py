"""MCP tool definitions for the Praxis Local API.

Exposes PraxisOS capabilities (task CRUD, health) as MCP tools
using the ``mcp`` Python SDK's ``FastMCP`` class.  Each tool reuses existing
backend modules — no business logic is duplicated here.

The module exports a single factory :func:`create_mcp_server` that accepts
external dependencies via arguments (to avoid circular imports with the main
Flask module) and returns a configured ``FastMCP`` instance ready to be
mounted as an ASGI app.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from urllib.parse import unquote, urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import assignee_ops
import assignee_query
import console_pty
import git_hooks_ops
import project_context_reader
import project_context_search
import route_activity
import settings_ops
import task_ops
import task_query

__all__ = ["create_mcp_server"]

SERVICE_VERSION = "0.1.0"

# Fallback project root injected by create_mcp_server from the API's
# CHAT_PROJECT_ROOT.  Used when no explicit project_root is provided and
# the MCP client's workspace roots are unavailable — replacing os.getcwd()
# which may point to a directory unrelated to the open project.
_default_project_root: str = ""

_ANNOTATIONS_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_ANNOTATIONS_WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_ANNOTATIONS_WRITE_NON_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _validate_project_root(project_root: str) -> str | None:
    """Validate that *project_root* is an absolute path to a PraxisOS project.

    Returns the validated path, or ``None`` if invalid.  Same rules as
    ``praxis_local_api._validate_project_root``.
    """
    if not isinstance(project_root, str):
        return None
    candidate = project_root.strip()
    if not candidate or not os.path.isabs(candidate) or not os.path.isdir(candidate):
        return None
    if not os.path.isdir(os.path.join(candidate, ".praxis")):
        return None
    return candidate


def _resolve_project_root(project_root: str) -> str | None:
    """Resolve and validate a project root, falling back to the configured default.

    1. Strip whitespace from *project_root*.
    2. If non-empty, delegate to :func:`_validate_project_root` (explicit value
       takes priority).
    3. If empty, try ``_default_project_root`` (injected by :func:`create_mcp_server`
       from the API's ``CHAT_PROJECT_ROOT`` / ``absoluteProjectPath``).
    4. Last resort: try ``os.getcwd()``.
    5. Return the validated path, or ``None`` if none resolves.
    """
    candidate = project_root.strip() if isinstance(project_root, str) else ""
    if candidate:
        return _validate_project_root(candidate)
    if _default_project_root:
        validated = _validate_project_root(_default_project_root)
        if validated is not None:
            return validated
    cwd = os.getcwd()
    return _validate_project_root(cwd)


def _file_uri_to_path(uri: str) -> str | None:
    """Convert a ``file://`` URI to a local filesystem path, or ``None``."""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return unquote(parsed.path) or None


async def _get_client_project_root(ctx: Context) -> str | None:
    """Ask the MCP client for its workspace roots and return the first valid project."""
    try:
        result = await ctx.session.list_roots()
        for root in result.roots:
            path = _file_uri_to_path(str(root.uri))
            if path is not None:
                validated = _validate_project_root(path)
                if validated is not None:
                    return validated
    except Exception:
        pass
    return None


async def _resolve_project_root_with_ctx(project_root: str, ctx: Context) -> str | None:
    """Resolve project root: explicit value → client roots → configured default → cwd."""
    candidate = project_root.strip() if isinstance(project_root, str) else ""
    if candidate:
        return _validate_project_root(candidate)
    root = await _get_client_project_root(ctx)
    if root is not None:
        return root
    if _default_project_root:
        validated = _validate_project_root(_default_project_root)
        if validated is not None:
            return validated
    cwd = os.getcwd()
    return _validate_project_root(cwd)


_PROJECT_ROOT_ERROR = {
    "error": "Invalid project_root: must be an absolute path to a directory containing .praxis/"
}

_PROJECT_ROOT_ERROR_MSG = "Error: Invalid project_root: must be an absolute path to a directory containing .praxis/"


def create_mcp_server(
    claude_checker: Callable[[], bool] | None = None,
    default_project_root: str = "",
) -> FastMCP:
    """Create and return a configured ``FastMCP`` instance with all tools.

    Parameters
    ----------
    claude_checker:
        A callable that returns ``True`` when the ``claude`` CLI is available.
        Injected by the main module to avoid circular imports.  When ``None``,
        the health tool reports ``claude_available: false``.
    default_project_root:
        Absolute path to the project root (from ``CHAT_PROJECT_ROOT`` /
        ``absoluteProjectPath``).  Used as fallback when MCP tool calls omit
        ``project_root`` and the client's workspace roots are unavailable —
        replacing ``os.getcwd()`` which may not point to the open project.
    """
    global _default_project_root
    if default_project_root:
        _default_project_root = default_project_root
    mcp = FastMCP(
        "praxis",
        host="127.0.0.1",
        port=7865,
    )

    # create_task implementation notes (not sent to LLM):
    # Required: title, why_we_need_this, acceptance_criteria (non-empty).
    # Optional: important_constraints, labels, priority, assignee (filename like 'jarvis.json').
    # Returns 'Task #{id} created ({path})' or 'Error: {message} (field: {name})'.
    # Server assigns id, status, timestamps.
    @mcp.tool(
        name="create_task",
        description=(
            "Create a new task on the kanban board. "
            "Only create on explicit user request. "
            "Server assigns id, status, and timestamps — never send these."
        ),
        annotations=_ANNOTATIONS_WRITE_NON_IDEMPOTENT,
    )
    async def create_task(
        project_root: str = "",
        title: str = "",
        why_we_need_this: str = "",
        acceptance_criteria: str = "",
        important_constraints: str = "",
        labels: list[str] | None = None,
        priority: str = "",
        assignee: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return "Error: project not found"

        payload: dict = {
            "title": title,
            "why_we_need_this": why_we_need_this,
            "acceptance_criteria": acceptance_criteria,
        }
        if important_constraints:
            payload["important_constraints"] = important_constraints
        if labels:
            payload["labels"] = labels
        if priority:
            payload["priority"] = priority
        if assignee:
            payload["assignee"] = assignee

        try:
            result = task_ops.create_task(root, payload)
        except task_ops.ValidationError as exc:
            return f"Error: {exc} (field: {exc.field})"

        return f"Task #{result['id']} created ({result['path']})"

    # get_next_task_id implementation notes (not sent to LLM):
    # Reserves the next collision-free task ID by calling the shared allocation
    # logic (file-locked counter at .praxis/config/task_counter).
    # Each call increments the counter — never call speculatively.
    # Returns plain text: "Next reserved task ID: {N}".
    @mcp.tool(
        name="get_next_task_id",
        description=(
            "Reserve and return the next safe task ID. "
            "Increments the counter atomically — each call consumes one ID. "
            "Use only when writing task files directly; create_task handles allocation internally."
        ),
        annotations=_ANNOTATIONS_WRITE_NON_IDEMPOTENT,
    )
    async def get_next_task_id(
        project_root: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        try:
            next_id = task_ops.allocate_task_id(root)
        except OSError as exc:
            return f"Error: failed to allocate task ID: {exc}"

        return f"Next reserved task ID: {next_id}"

    # list_tasks implementation notes (not sent to LLM):
    # Filter by status: new, in_progress, to_review, completed, on_hold, archived.
    # One line per task: #<id> [<status>] <title> | <priority> | <labels_csv> | <assignee>
    # On Hold tasks with reason append: | ON HOLD: <reason>
    @mcp.tool(
        name="list_tasks",
        description=(
            "List tasks with full metadata (status, priority, labels, assignee). "
            "Filter by status column. "
            "Use get_active_tasks for a lighter id+title-only snapshot."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def list_tasks(
        project_root: str = "",
        status: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        status_filter = status.strip() if status else None
        try:
            tasks = task_query.list_tasks(root, status=status_filter)
        except ValueError as exc:
            return f"Error: {exc}"
        if not tasks:
            if status_filter:
                return f'No tasks with status "{status_filter}"'
            return "No tasks"
        lines: list[str] = []
        for t in tasks:
            labels_csv = ",".join(t.get("labels") or [])
            assignee = t.get("assignee") or ""
            line = f"#{t['id']} [{t['status']}] {t['title']} | {t.get('priority') or ''} | {labels_csv} | {assignee}"
            if t.get("status") == "on_hold":
                full = task_query.get_task(root, t["id"])
                if full and full.get("on_hold_reason"):
                    line += f" | ON HOLD: {full.get('on_hold_reason', '')}"
            lines.append(line)
        return "\n".join(lines)

    # get_task implementation notes (not sent to LLM):
    # Returns plain text: header line, metadata line, then ## sections.
    # The assignee profile is NOT inlined (POS-1940): assignee delivery moved to
    # the chat system prompt and clipboard prompts; only the assignee filename
    # appears on the metadata line.
    @mcp.tool(
        name="get_task",
        description=(
            "Get full details of a single task by ID. "
            "Always call before starting work on a task."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def get_task(
        project_root: str = "",
        task_id: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        # Backward compat: try parsing as int for numeric lookups
        lookup_id: int | str = task_id
        try:
            lookup_id = int(task_id)
        except (ValueError, TypeError):
            pass

        task = task_query.get_task(root, lookup_id)
        if task is None:
            return f"Task {task_id} not found"

        parts: list[str] = [f"Task: ID: {task['id']} [{task['status']}] {task['title']}"]

        meta_fields: list[str] = []
        if task.get("priority"):
            meta_fields.append(f"priority: {task['priority']}")
        labels = task.get("labels")
        if labels:
            meta_fields.append(f"labels: {', '.join(labels)}")
        assignee_filename = task.get("assignee", "")
        if assignee_filename:
            meta_fields.append(f"assignee: {assignee_filename}")
        if meta_fields:
            parts.append(" | ".join(meta_fields))

        if task.get("status") == "on_hold" and task.get("on_hold_reason"):
            parts.append(f"ON HOLD: {task.get('on_hold_reason', '')}")

        parts.append("")
        parts.append("## Why")
        parts.append(task.get("why_we_need_this", ""))

        parts.append("")
        parts.append("## Acceptance Criteria")
        parts.append(task.get("acceptance_criteria", ""))

        constraints = task.get("important_constraints", "")
        if constraints:
            parts.append("")
            parts.append("## Constraints")
            parts.append(constraints)

        screenshots = task.get("screenshots")
        if screenshots and isinstance(screenshots, list):
            parts.append("")
            parts.append("## Screenshots")
            for path in screenshots:
                parts.append(str(path))

        return "\n".join(parts)

    # search_project_context implementation notes (not sent to LLM):
    # BM25-scored, case-insensitive. Field boosting: title > headings > body.
    # Prefix matching (e.g. 'deploy' finds 'deployment') + fuzzy matching for misspellings.
    # 1-2 keywords: all must appear in the same file. 3+ keywords: at least half must match.
    # Returns plain text: one block per matching file, header with path + [exact]/[partial, missing: <terms>], L<N> match lines.
    @mcp.tool(
        name="search_project_context",
        description=(
            "Search the project knowledge base by keyword. "
            "Returns ranked results with relevance scores and context excerpts. "
            "Use before reading source to find relevant docs."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def search_project_context(
        project_root: str = "",
        query: str = "",
        file_pattern: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        pattern = file_pattern.strip() or None
        result = project_context_search.search_project_context(
            root, query, file_pattern=pattern
        )

        # POS-2348: log matched routes for Blueprint heatmap analytics
        try:
            matched_routes: list[str] = []
            for line in result.splitlines():
                if line.startswith("project-context/") and " [" in line:
                    route_part = line[len("project-context/"):]
                    route_id = route_part.split("/")[0]
                    if route_id and route_id not in matched_routes:
                        matched_routes.append(route_id)
            if matched_routes:
                route_activity.log_search_hit(root, matched_routes)
        except Exception:
            pass  # search logging must never break the search tool

        return result

    # read_context_route implementation notes (not sent to LLM):
    # Reads a specific section of a project-context route README.
    # mode="toc" → list sections with line counts.
    # mode="tldr" → ## TL;DR content only (default).
    # mode="section" + section_name → named section content.
    # Non-existent route → error with available routes.
    # Missing section → error with available sections.
    @mcp.tool(
        name="read_context_route",
        description=(
            "Read a specific section of a project-context route README. "
            "Modes: toc (section list with line counts), tldr (TL;DR only, default), "
            "section (named section; section_name accepts comma-separated names for "
            "multiple sections in one call). Use after search_project_context to read "
            "only what you need instead of the entire file."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def read_context_route(
        project_root: str = "",
        route: str = "",
        mode: str = "tldr",
        section_name: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        return project_context_reader.read_context_route(
            root, route, mode=mode, section_name=section_name
        )

    @mcp.tool(
        name="health",
        description=(
            "Check Praxis Local API status and Claude CLI availability."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    def health() -> str:
        claude_available = claude_checker() if claude_checker else False
        claude_status = "available" if claude_available else "not found"
        return f"Praxis Local API v{SERVICE_VERSION} running. Claude CLI: {claude_status}."

    # get_active_tasks implementation notes (not sent to LLM):
    # One line per task: #<id> <title> [<status>]. Empty board → "No active tasks".
    @mcp.tool(
        name="get_active_tasks",
        description=(
            "List active tasks (new + in_progress) with id and title only. "
            "Use list_tasks when you need full metadata."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def get_active_tasks(
        project_root: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        tasks: list[dict] = []
        for s in ("new", "in_progress"):
            tasks.extend(task_query.list_tasks(root, status=s))
        if not tasks:
            return "No active tasks"
        return "\n".join(f"#{t['id']} {t['title']} [{t['status']}]" for t in tasks)

    # find_task_by_keywords implementation notes (not sent to LLM):
    # AND logic, case-insensitive — all keywords must appear.
    # Defaults to active tasks (new, in_progress) when status not specified.
    # One line per match: #<id> <title> [<status>]. Empty → 'No tasks match "<query>"'.
    @mcp.tool(
        name="find_task_by_keywords",
        description=(
            "Search tasks by keywords across title, description, and criteria. "
            "Use to find tasks by topic or check for duplicates before creating."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def find_task_by_keywords(
        project_root: str = "",
        query: str = "",
        status: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        status_filter = status.strip() if status else None
        try:
            tasks = task_query.find_task_by_keywords(root, query, status=status_filter)
        except ValueError as exc:
            return f"Error: {exc}"
        if not tasks:
            return f'No tasks match "{query}"'
        return "\n".join(f"#{t['id']} {t['title']} [{t['status']}]" for t in tasks)

    # get_assignees implementation notes (not sent to LLM):
    # One line per assignee: <file> | <name> | <role>. Empty → "No assignees found".
    @mcp.tool(
        name="get_assignees",
        description=(
            "List all assignees. Pass the 'file' value (not display name) to create_task. "
            "Use find_assignee_by_keywords when the domain is specific."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def get_assignees(
        project_root: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        assignees = assignee_query.get_assignees(root)
        if not assignees:
            return "No assignees found"
        return "\n".join(f"{a['file']} | {a['name']} | {a['role']}" for a in assignees)

    # ------------------------------------------------------------------
    # find_assignee_by_keywords — keyword search over assignee roster
    # ------------------------------------------------------------------

    # find_assignee_by_keywords implementation notes (not sent to LLM):
    # Case-insensitive, partial word matching.
    # 1-2 keywords: all must appear; 3+: at least half must match.
    # Same line format as get_assignees. Empty → 'No assignees match "<query>"'.
    @mcp.tool(
        name="find_assignee_by_keywords",
        description=(
            "Search assignees by keywords in name, role, and mission. "
            "Use get_assignees for general selection — filtered results can hide valid matches."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def find_assignee_by_keywords(
        project_root: str = "",
        query: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        assignees = assignee_query.find_assignee_by_keywords(root, query)
        if not assignees:
            return f'No assignees match "{query}"'
        return "\n".join(f"{a['file']} | {a['name']} | {a['role']}" for a in assignees)

    # ------------------------------------------------------------------
    # create_assignee — create a new AI assignee profile
    # ------------------------------------------------------------------

    # create_assignee implementation notes (not sent to LLM):
    # Required: name, role, mission (non-empty). Optional: mandatory_constraints,
    # edge_cases_and_fallbacks, workflow_and_response_format,
    # assignee_icon (e.g. 'lucide:Code'), file (desired filename, e.g. 'my-agent.json').
    # Returns {ok, file, name} or {error, field}. Filename auto-generated from name
    # (sanitized, lowercased, hyphenated) unless explicit file provided.
    # Duplicates get numeric suffix.
    @mcp.tool(
        name="create_assignee",
        description=(
            "Create a new AI assignee profile. "
            "Only create on explicit user request."
        ),
        annotations=_ANNOTATIONS_WRITE_NON_IDEMPOTENT,
    )
    async def create_assignee(
        project_root: str = "",
        name: str = "",
        role: str = "",
        mission: str = "",
        mandatory_constraints: str = "",
        edge_cases_and_fallbacks: str = "",
        workflow_and_response_format: str = "",
        assignee_icon: str = "",
        file: str = "",
        ctx: Context = None,
    ) -> dict:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR

        payload: dict = {
            "name": name,
            "role": role,
            "mission": mission,
        }
        if mandatory_constraints:
            payload["mandatory_constraints"] = mandatory_constraints
        if edge_cases_and_fallbacks:
            payload["edge_cases_and_fallbacks"] = edge_cases_and_fallbacks
        if workflow_and_response_format:
            payload["workflow_and_response_format"] = workflow_and_response_format
        if assignee_icon:
            payload["assignee_icon"] = assignee_icon
        if file:
            payload["file"] = file

        try:
            result = assignee_ops.create_assignee(root, payload)
        except assignee_ops.ValidationError as exc:
            return {"error": str(exc), "field": exc.field}

        return {"ok": True, "file": result["file"], "name": result["name"]}

    # ------------------------------------------------------------------
    # reconcile_task_statuses — repair status field desync
    # ------------------------------------------------------------------
    @mcp.tool(
        name="reconcile_task_statuses",
        description=(
            "Scan all task folders and fix JSON files whose status field "
            "does not match their folder location. Use this to repair projects "
            "where tasks were moved between folders manually without using "
            "update_task_status. Returns a summary of fixed and errored files."
        ),
        annotations=_ANNOTATIONS_WRITE_IDEMPOTENT,
    )
    async def reconcile_task_statuses(
        project_root: str = "",
        ctx: Context = None,
    ) -> str | dict:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        result = task_query.reconcile_task_statuses(root)
        fixed = result["fixed"]
        errors = result["errors"]

        if not fixed and not errors:
            return "All task statuses are consistent — no repairs needed."

        lines: list[str] = []
        if fixed:
            lines.append(f"Fixed {len(fixed)} task(s):")
            for f in fixed:
                lines.append(f"  {f['file']}: {f['old_status']} → {f['new_status']}")
        if errors:
            lines.append(f"\n{len(errors)} file(s) could not be repaired:")
            for e in errors:
                lines.append(f"  {e['file']}: {e['error']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # update_task_status — mirrors scripts/praxis_task_status.py logic
    # ------------------------------------------------------------------

    _VALID_STATUSES = ("in_progress", "to_review", "error", "completed", "on_hold", "archived")
    _STATUS_DIRECTORIES = (
        "new", "in_progress", "completed", "on_hold", "archived", "to_review",
    )
    # Statuses that physically move the file to a new column directory
    _MOVABLE_STATUSES = ("in_progress", "to_review", "completed", "on_hold", "archived")
    # Statuses that are signals only — JSON updated in-place, file stays in current directory
    _INPLACE_STATUSES = ("error",)
    _AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,9}$")

    # update_task_status implementation notes (not sent to LLM):
    # Column-moving statuses (in_progress, to_review, completed, on_hold, archived)
    # physically relocate the task JSON to the matching directory. Signal-only status
    # (error) updates the status field in-place; file stays in current directory.
    # Returns 'OK' or 'Error: {message}'.
    @mcp.tool(
        name="update_task_status",
        description=(
            "Move a task to a new status column. "
            "Valid statuses: in_progress, to_review, error, completed, on_hold, archived. "
            "agent_name is optional (legacy; the UI shows only a status lamp, never the name). "
            "Include on_hold_reason when setting status to on_hold."
        ),
        annotations=_ANNOTATIONS_WRITE_IDEMPOTENT,
    )
    async def update_task_status(
        project_root: str = "",
        task_id: str = "",
        status: str = "",
        agent_name: str = "",
        on_hold_reason: str = "",
        ctx: Context = None,
    ) -> str | dict:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return "Error: project not found"

        # --- validate task_id (defensively coerce in case a raw int arrives) ---
        task_id = str(task_id).strip() if task_id else ""
        if not task_id:
            return "Error: task_id is required"

        # Backward compat: try parsing as int
        lookup_id: int | str = task_id
        try:
            lookup_id = int(task_id)
        except (ValueError, TypeError):
            pass

        # --- validate status ---
        status = status.strip() if status else ""
        if status not in _VALID_STATUSES:
            return f"Error: invalid status '{status}'. Must be one of: {', '.join(_VALID_STATUSES)}"

        on_hold_reason_val = on_hold_reason.strip() if on_hold_reason else ""

        # --- validate agent_name (optional) ---
        agent_name = agent_name.strip().lower() if agent_name else ""
        if agent_name and not _AGENT_NAME_RE.match(agent_name):
            return f"Error: invalid agent_name '{agent_name}'"

        # --- locate task file ---
        praxis_dir = os.path.join(root, ".praxis")
        tasks_root = os.path.join(praxis_dir, "tasks")
        old_status: str | None = None
        source_path: str | None = None
        current_stem: str | None = None

        # Try exact filename match first
        exact_filename = f"{lookup_id}.json"
        for status_dir in _STATUS_DIRECTORIES:
            candidate = os.path.join(tasks_root, status_dir, exact_filename)
            if os.path.isfile(candidate):
                old_status = status_dir
                source_path = candidate
                current_stem = str(lookup_id)
                break

        # If not found and lookup was numeric, scan for composite match
        if source_path is None:
            numeric = task_ops.extract_numeric_id(lookup_id) if isinstance(lookup_id, str) else lookup_id
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
                            entry_numeric = task_ops.extract_numeric_id(stem)
                            if entry_numeric == numeric:
                                old_status = status_dir
                                source_path = entry.path
                                current_stem = stem
                                break
                    except OSError:
                        continue
                    if source_path is not None:
                        break

        if source_path is None or old_status is None or current_stem is None:
            return f"Error: task {task_id} not found"

        # --- Migration: if numeric-only filename, migrate to composite ---
        numeric_id = task_ops.extract_numeric_id(current_stem)
        if numeric_id is not None and not task_ops._is_composite_id(current_stem):
            tag = task_ops._read_task_title_tag(root)
            suffix = task_ops._generate_suffix()
            new_composite_id = task_ops._compose_composite_id(tag, numeric_id, suffix)
            new_filename = f"{new_composite_id}.json"
            new_path = os.path.join(os.path.dirname(source_path), new_filename)

            try:
                with open(source_path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
                data["id"] = new_composite_id
                with open(new_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                if source_path != new_path:
                    os.remove(source_path)
                source_path = new_path
                current_stem = new_composite_id
            except (json.JSONDecodeError, OSError):
                pass  # Continue with original file if migration fails

        task_filename = f"{current_stem}.json"

        # --- create marker directory and marker file ---
        # Use numeric ID for state directory (backward compat)
        state_id = str(numeric_id) if numeric_id is not None else current_stem
        state_dir = os.path.join(praxis_dir, "config", "state", state_id)
        os.makedirs(state_dir, exist_ok=True)

        # Clean up stale markers when moving to in_progress
        if status == "in_progress":
            stale = os.path.join(state_dir, "to_review")
            if os.path.exists(stale):
                os.remove(stale)

        # Write status marker
        marker_path = os.path.join(state_dir, status)
        with open(marker_path, "a"):
            os.utime(marker_path, None)  # touch

        # Remove agent_not_started marker if present
        not_started = os.path.join(state_dir, "agent_not_started")
        if os.path.exists(not_started):
            os.remove(not_started)

        # --- on_hold_reason_val computed above (only relevant for 'on_hold' status) ---

        # --- move task JSON if status is movable; update in-place for signal statuses ---
        if status in _MOVABLE_STATUSES and old_status != status:
            # Physical move: file goes to the new column directory
            target_dir = os.path.join(tasks_root, status)
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, task_filename)

            try:
                with open(source_path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
                data["status"] = status
                data["column_entered_at"] = int(time.time() * 1000)
                # Write on_hold_reason when transitioning to on_hold
                if status == "on_hold" and on_hold_reason_val:
                    data["on_hold_reason"] = on_hold_reason_val
                with open(target_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                os.remove(source_path)
            except (json.JSONDecodeError, OSError):
                # Fallback: raw rename
                try:
                    os.rename(source_path, target_path)
                except OSError:
                    pass  # best-effort — file stays where it was

        elif status in _INPLACE_STATUSES:
            # Signal-only: update JSON in-place, file stays in its current directory
            try:
                with open(source_path, "r", encoding="utf-8") as f:
                    data = json.loads(f.read())
                data["status"] = status
                data["column_entered_at"] = int(time.time() * 1000)
                with open(source_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            except (json.JSONDecodeError, OSError):
                pass  # best-effort — status field stays as-is

        # --- write agentFeedback.yml if agent_name provided ---
        if agent_name:
            feedback_path = os.path.join(state_dir, "agentFeedback.yml")
            with open(feedback_path, "w", encoding="utf-8") as f:
                f.write(f"agentName: {agent_name}\n")

        if status == "to_review":
            return {
                "ok": "OK",
                "post_transition_guidance": (
                    "REQUIRED NEXT STEP — call update_project_context NOW and follow "
                    "the returned skill to completion. This is not optional. "
                    "Route READMEs must not duplicate project information — instead, "
                    "explain the logic, reasoning, architectural decisions, and "
                    "behavioral rules so that other agents fully understand how "
                    "things work and why. If a route README exceeds ~500 lines, "
                    "split it into child routes."
                ),
            }

        return "OK"

    # ------------------------------------------------------------------
    # update_task — partial field updates on existing tasks
    # ------------------------------------------------------------------

    # update_task implementation notes (not sent to LLM):
    # Only non-null fields are modified; unsupported fields silently ignored.
    # Returns "Updated task #<id>: <fields>" or "Task <id> not found" or "Error: ...".
    @mcp.tool(
        name="update_task",
        description=(
            "Update fields on an existing task (partial update). "
            "Does NOT move between columns — use update_task_status for that."
        ),
        annotations=_ANNOTATIONS_WRITE_IDEMPOTENT,
    )
    async def update_task(
        project_root: str = "",
        task_id: str = "",
        title: str | None = None,
        why_we_need_this: str | None = None,
        acceptance_criteria: str | None = None,
        important_constraints: str | None = None,
        labels: list[str] | None = None,
        priority: str | None = None,
        assignee: str | None = None,
        on_hold_reason: str | None = None,
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        # Backward compat: try parsing as int (defensively coerce non-str input)
        lookup_id: int | str = str(task_id).strip() if task_id else ""
        try:
            lookup_id = int(lookup_id)
        except (ValueError, TypeError):
            pass

        fields: dict = {}
        if title is not None:
            fields["title"] = title
        if why_we_need_this is not None:
            fields["why_we_need_this"] = why_we_need_this
        if acceptance_criteria is not None:
            fields["acceptance_criteria"] = acceptance_criteria
        if important_constraints is not None:
            fields["important_constraints"] = important_constraints
        if labels is not None:
            fields["labels"] = labels
        if priority is not None:
            fields["priority"] = priority
        if assignee is not None:
            fields["assignee"] = assignee
        if on_hold_reason is not None:
            fields["on_hold_reason"] = on_hold_reason

        try:
            result = task_ops.update_task(root, lookup_id, fields)
        except task_ops.TaskNotFoundError:
            return f"Task {task_id} not found"
        except task_ops.ValidationError as exc:
            return f"Error: {exc} (field: {exc.field})"

        updated = ", ".join(result["updated_fields"]) if result["updated_fields"] else ""
        return f"Updated task #{result['task_id']}: {updated}"

    # ------------------------------------------------------------------
    # update_project_context — skill redirect for knowledge-base updates
    # ------------------------------------------------------------------

    # update_project_context implementation notes (not sent to LLM):
    # MANDATORY after every task. Skill found → returns instructions. Skill missing → "Error: ...".
    @mcp.tool(
        name="update_project_context",
        description=(
            "Get instructions for updating the project knowledge base. "
            "MANDATORY after every task — any project change must be documented."
        ),
        annotations=_ANNOTATIONS_WRITE_IDEMPOTENT,
    )
    async def update_project_context(
        project_root: str = "",
        ctx: Context = None,
    ) -> str:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR_MSG

        result = project_context_search.update_project_context(root)
        if not result.get("ok"):
            return f"Error: {result.get('error', 'Unknown error')}"
        return result["instructions"]

    # ------------------------------------------------------------------
    # get_settings — read project settings from general.yaml + ignore configs
    # ------------------------------------------------------------------

    # get_settings implementation notes (not sent to LLM):
    # Returns dict organized by tab — general (taskTitleTag, pythonCommand,
    # contextRouterIncludedByDefault, chatEnabled, markdownDoubleClickSelectionMode),
    # git (addTasksToCommits, autoVersionEnabled, autoVersionFilePath),
    # fileTree (fileTreeIgnorePaths),
    # projectFiles, sound (soundVolume),
    # other (defaultWorker, projectIcon, aiMode).
    @mcp.tool(
        name="get_settings",
        description=(
            "Read all user-configurable project settings. "
            "Always call before update_settings to see current values."
        ),
        annotations=_ANNOTATIONS_READ,
    )
    async def get_settings(
        project_root: str = "",
        ctx: Context = None,
    ) -> dict:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR

        return settings_ops.read_settings(root)

    # ------------------------------------------------------------------
    # execute_command — run a shell command in the shared terminal session
    # ------------------------------------------------------------------

    # execute_command implementation notes (not sent to LLM):
    # Delegates to console_pty.execute_and_wait, which runs the command in the
    # project's persistent PTY shell (one shared session per project root, also
    # used by AI Chat console mode) and blocks until it finishes or times out.
    @mcp.tool(
        name="execute_command",
        description=(
            "Run a shell command in the project's persistent terminal session and return its output. "
            "The terminal is shared and survives between calls, so cd, exports and background "
            "processes persist. Returns the output and the exit code."
        ),
        annotations=_ANNOTATIONS_WRITE_NON_IDEMPOTENT,
    )
    async def execute_command(
        command: str,
        timeout_seconds: int = 120,
        project_root: str = "",
        ctx: Context = None,
    ) -> dict:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR
        timeout = max(1, min(int(timeout_seconds or 120), 600))
        return console_pty.execute_and_wait(root, command, timeout)

    # ------------------------------------------------------------------
    # update_settings — update project settings in general.yaml + ignore configs
    # ------------------------------------------------------------------

    # update_settings implementation notes (not sent to LLM):
    # Supported: task_title_tag, python_command, context_router_included_by_default,
    # chat_enabled, markdown_double_click_selection_mode, add_tasks_to_commits,
    # auto_version_enabled, auto_version_file_path,
    # sound_volume, default_worker, project_icon.
    # Ignore paths (file_tree_ignore_paths, project_files_ignore_paths) replace
    # the full list. Returns {ok, updated} or {ok: false, error}.
    @mcp.tool(
        name="update_settings",
        description=(
            "Update project settings. "
            "Pass only the fields you want to change — unset fields are left unchanged. "
            "Read settings first."
        ),
        annotations=_ANNOTATIONS_WRITE_IDEMPOTENT,
    )
    async def update_settings(
        project_root: str = "",
        # General tab
        task_title_tag: str | None = None,
        python_command: str | None = None,
        context_router_included_by_default: bool | None = None,
        chat_enabled: bool | None = None,
        markdown_double_click_selection_mode: str | None = None,
        # Git tab
        add_tasks_to_commits: bool | None = None,
        auto_version_enabled: bool | None = None,
        auto_version_file_path: str | None = None,
        # File tree tab
        file_tree_ignore_paths: list[str] | None = None,
        # Project files tab
        project_files_ignore_paths: list[str] | None = None,
        # Sound tab
        sound_volume: float | None = None,
        # Other
        default_worker: str | None = None,
        project_icon: str | None = None,
        ctx: Context = None,
    ) -> dict:
        root = await _resolve_project_root_with_ctx(project_root, ctx)
        if root is None:
            return _PROJECT_ROOT_ERROR

        # Build the fields dict for general.yaml updates (snake_case → camelCase)
        param_values = {
            "task_title_tag": task_title_tag,
            "python_command": python_command,
            "context_router_included_by_default": context_router_included_by_default,
            "chat_enabled": chat_enabled,
            "markdown_double_click_selection_mode": markdown_double_click_selection_mode,
            "add_tasks_to_commits": add_tasks_to_commits,
            "auto_version_enabled": auto_version_enabled,
            "auto_version_file_path": auto_version_file_path,
            "sound_volume": sound_volume,
            "default_worker": default_worker,
            "project_icon": project_icon,
        }

        # Only include non-None values → maps to camelCase YAML keys
        fields: dict[str, object] = {}
        for param_name, value in param_values.items():
            if value is not None:
                yaml_key = settings_ops.PARAM_TO_YAML[param_name]
                fields[yaml_key] = value

        results: list[dict] = []

        # 1. Update general.yaml settings if any provided
        if fields:
            result = settings_ops.update_settings(root, fields)
            if not result.get("ok"):
                return result
            results.append(result)

            # Provision git hooks when add_tasks_to_commits changes (MCP mode).
            # The frontend handles this via syncAutoAddTasksGitHooks + run-enable-hooks,
            # but MCP bypasses the frontend — hooks must be provisioned server-side.
            if add_tasks_to_commits is not None:
                hook_result = git_hooks_ops.sync_auto_add_tasks_git_hooks(
                    root, add_tasks_to_commits,
                )
                if hook_result.get("hooks_provisioned"):
                    result.setdefault("hooks_provisioned", True)

        # 2. Update file tree ignore paths if provided
        if file_tree_ignore_paths is not None:
            result = settings_ops.update_ignore_paths(
                root, "prompts", file_tree_ignore_paths,
            )
            if not result.get("ok"):
                return result
            results.append(result)

        # 3. Update project files ignore paths if provided
        if project_files_ignore_paths is not None:
            result = settings_ops.update_ignore_paths(
                root, "projectFiles", project_files_ignore_paths,
            )
            if not result.get("ok"):
                return result
            results.append(result)

        if not results:
            return {"ok": False, "error": "No settings provided to update"}

        # Summarize what was updated
        updated_keys: list[str] = []
        for r in results:
            if "updated" in r:
                updated_keys.extend(r["updated"])
            if "scope" in r:
                scope_label = (
                    "fileTreeIgnorePaths" if r["scope"] == "prompts"
                    else "projectFilesIgnorePaths"
                )
                updated_keys.append(scope_label)

        return {"ok": True, "updated": updated_keys}

    return mcp
