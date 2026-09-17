"""Tests for :mod:`project_context_reader` — section-based route README reader.

Run from the ``api/`` directory:

    ./venv/bin/python -m pytest test_project_context_reader.py -v

These build a throwaway ``project-context/`` tree with ``tmp_path`` and assert
that :func:`read_context_route` honours toc, tldr, section modes, and error
cases (missing route, missing section, invalid mode).
"""

from __future__ import annotations

from project_context_reader import read_context_route


def _make_route(tmp_path, route_name: str, content: str):
    """Create a route directory with a README.md under project-context/."""
    route_dir = tmp_path / "project-context" / route_name
    route_dir.mkdir(parents=True, exist_ok=True)
    (route_dir / "README.md").write_text(content, encoding="utf-8")
    return route_dir


_SAMPLE_README = """\
# Tasks Domain

## TL;DR
- Tasks are JSON files.
- Required fields: id, status, title.
- Task IDs are composite strings.

## Purpose
This domain manages tasks on the kanban board.
It provides CRUD operations and status transitions.

## Architecture Decisions
- Files stored in .praxis/tasks/{status}/ directories.
- Moving a task physically relocates the JSON file.
- Composite IDs prevent merge conflicts.

## Route-Specific Constraints
- Only update_task_status moves tasks between columns.
"""


# ---------------------------------------------------------------------------
# TOC mode
# ---------------------------------------------------------------------------


def test_toc_mode_lists_sections_with_line_counts(tmp_path):
    """mode=toc returns one line per section with heading and line count."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(str(tmp_path), "tasks", mode="toc")

    assert "## TL;DR (5 lines)" in result
    assert "## Purpose (4 lines)" in result
    assert "## Architecture Decisions (5 lines)" in result
    assert "## Route-Specific Constraints (2 lines)" in result


def test_toc_mode_no_sections(tmp_path):
    """mode=toc on a file with no ## headings returns a clear message."""
    _make_route(tmp_path, "empty", "# Just a title\nSome content\n")

    result = read_context_route(str(tmp_path), "empty", mode="toc")

    assert "No ## sections" in result


# ---------------------------------------------------------------------------
# TL;DR mode (default)
# ---------------------------------------------------------------------------


def test_tldr_mode_returns_tldr_section(tmp_path):
    """mode=tldr returns the TL;DR section content."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(str(tmp_path), "tasks", mode="tldr")

    assert "## TL;DR" in result
    assert "Tasks are JSON files" in result
    assert "composite strings" in result
    # Should NOT include content from other sections
    assert "Architecture Decisions" not in result
    assert "Purpose" not in result


def test_default_mode_is_tldr(tmp_path):
    """Omitting mode defaults to tldr."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(str(tmp_path), "tasks")

    assert "## TL;DR" in result
    assert "Tasks are JSON files" in result


def test_tldr_missing_returns_available_sections(tmp_path):
    """When no TL;DR section exists, list available sections."""
    _make_route(tmp_path, "misc", "# Misc\n\n## Purpose\nSome purpose.\n\n## Notes\nSome notes.\n")

    result = read_context_route(str(tmp_path), "misc", mode="tldr")

    assert "No ## TL;DR section" in result
    assert "Purpose" in result
    assert "Notes" in result


# ---------------------------------------------------------------------------
# Section mode
# ---------------------------------------------------------------------------


def test_section_mode_returns_named_section(tmp_path):
    """mode=section returns the content of the named section."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(str(tmp_path), "tasks", mode="section", section_name="Purpose")

    assert "## Purpose" in result
    assert "kanban board" in result
    # Should NOT include other sections
    assert "TL;DR" not in result
    assert "Architecture Decisions" not in result


def test_section_mode_returns_last_section_to_eof(tmp_path):
    """The last section includes content up to end of file."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(
        str(tmp_path), "tasks", mode="section", section_name="Route-Specific Constraints"
    )

    assert "## Route-Specific Constraints" in result
    assert "update_task_status" in result


def test_section_mode_missing_section_lists_available(tmp_path):
    """When the named section doesn't exist, list available sections."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(
        str(tmp_path), "tasks", mode="section", section_name="Nonexistent"
    )

    assert "not found" in result
    assert "TL;DR" in result
    assert "Purpose" in result


def test_section_mode_requires_section_name(tmp_path):
    """mode=section without section_name returns an error."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(str(tmp_path), "tasks", mode="section")

    assert "section_name is required" in result


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_missing_route_returns_error_with_available(tmp_path):
    """A non-existent route returns an error listing available routes."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)
    _make_route(tmp_path, "mcp", "# MCP\n\n## TL;DR\nMCP stuff.\n")

    result = read_context_route(str(tmp_path), "nonexistent")

    assert "not found" in result
    assert "tasks" in result
    assert "mcp" in result


def test_missing_route_no_routes_available(tmp_path):
    """A non-existent route with no project-context/ at all gives a clear message."""
    result = read_context_route(str(tmp_path), "nonexistent")

    assert "not found" in result


def test_invalid_mode_returns_error(tmp_path):
    """An unrecognized mode returns an error listing valid modes."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(str(tmp_path), "tasks", mode="full")

    assert "invalid mode" in result
    assert "toc" in result
    assert "tldr" in result
    assert "section" in result


def test_empty_route_returns_error(tmp_path):
    """An empty route string returns an error."""
    result = read_context_route(str(tmp_path), "")

    assert "route is required" in result


# ---------------------------------------------------------------------------
# Multi-section mode
# ---------------------------------------------------------------------------


def test_multi_section_returns_all_matched(tmp_path):
    """Comma-separated section names return all matched sections concatenated."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(
        str(tmp_path), "tasks", mode="section", section_name="Purpose, Architecture Decisions"
    )

    assert "## Purpose" in result
    assert "kanban board" in result
    assert "## Architecture Decisions" in result
    assert "merge conflicts" in result
    # Should NOT include other sections
    assert "TL;DR" not in result
    assert "Route-Specific Constraints" not in result


def test_single_section_unchanged_with_multi_section_support(tmp_path):
    """A single section name (no comma) still works identically."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(
        str(tmp_path), "tasks", mode="section", section_name="Purpose"
    )

    assert "## Purpose" in result
    assert "kanban board" in result
    assert "TL;DR" not in result


def test_multi_section_partial_match_with_note(tmp_path):
    """When some sections are found and some are not, return found + note."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(
        str(tmp_path), "tasks", mode="section", section_name="Purpose, NonExistent, AlsoMissing"
    )

    assert "## Purpose" in result
    assert "kanban board" in result
    assert "Note: section(s) not found: NonExistent, AlsoMissing" in result
    assert "Available:" in result


def test_multi_section_all_missing(tmp_path):
    """When all requested sections are missing, return error with available sections."""
    _make_route(tmp_path, "tasks", _SAMPLE_README)

    result = read_context_route(
        str(tmp_path), "tasks", mode="section", section_name="NonExistent, AlsoMissing"
    )

    assert "not found" in result
    assert "Available sections:" in result
    assert "TL;DR" in result
    assert "Purpose" in result


# ---------------------------------------------------------------------------
# linkedProjects tests
# ---------------------------------------------------------------------------


def _link_projects(project_root, linked):
    """Write a linkedProjects setting into a throwaway project's general.yaml."""
    config_dir = project_root / ".praxis" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    joined = ",".join(str(p) for p in linked)
    (config_dir / "general.yaml").write_text(
        f'linkedProjects: "{joined}"\n', encoding="utf-8",
    )


def test_route_only_in_linked_project_resolves_by_name(tmp_path):
    """A route present only in a linked project resolves by plain name."""
    current = tmp_path / "current"
    linked = tmp_path / "linked"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])
    _make_route(linked, "mcp", "# MCP\n\n## TL;DR\nLinked MCP info.\n")

    result = read_context_route(str(current), "mcp", mode="tldr")

    assert "Linked MCP info" in result


def test_route_in_both_resolves_to_current_project(tmp_path):
    """A route name present in both current and linked resolves to current's copy."""
    current = tmp_path / "current"
    linked = tmp_path / "linked"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])
    _make_route(current, "tasks", "# Tasks\n\n## TL;DR\nCurrent tasks info.\n")
    _make_route(linked, "tasks", "# Tasks\n\n## TL;DR\nLinked tasks info.\n")

    result = read_context_route(str(current), "tasks", mode="tldr")

    assert "Current tasks info" in result
    assert "Linked tasks info" not in result


def test_absolute_path_to_linked_route_reads_that_route(tmp_path):
    """An absolute path to a linked project's route directory reads that route."""
    current = tmp_path / "current"
    linked = tmp_path / "linked"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])
    route_dir = _make_route(linked, "ai-chat", "# AI Chat\n\n## TL;DR\nLinked chat info.\n")

    result = read_context_route(str(current), str(route_dir), mode="tldr")

    assert "Linked chat info" in result


def test_absolute_path_outside_any_project_context_not_found(tmp_path):
    """An absolute path outside any project-context boundary returns not-found."""
    current = tmp_path / "current"
    linked = tmp_path / "linked"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])

    outside = tmp_path / "outside_route"
    outside.mkdir()
    (outside / "README.md").write_text("# Outside\n\n## TL;DR\nShould not be reachable.\n")

    result = read_context_route(str(current), str(outside), mode="tldr")

    assert "not found" in result


def test_not_found_error_lists_linked_routes_as_absolute_paths(tmp_path):
    """The not-found error message lists linked routes as absolute paths."""
    current = tmp_path / "current"
    linked = tmp_path / "linked"
    current.mkdir()
    linked.mkdir()
    _link_projects(current, [linked])
    route_dir = _make_route(linked, "billing", "# Billing\n\n## TL;DR\nLinked billing info.\n")

    result = read_context_route(str(current), "nonexistent")

    assert "not found" in result
    assert str(route_dir) in result
