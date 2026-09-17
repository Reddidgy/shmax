"""Section-based reader for project-context route READMEs (pure, no Flask).

Allows agents to read specific sections of a route README instead of the
entire file — saving ~92% of tokens when only the TL;DR is needed.

Modes:
- ``toc``: table of contents with line counts per section
- ``tldr``: the ``## TL;DR`` section only (default)
- ``section``: a specific named ``## `` section

When the project declares ``linkedProjects`` in ``.praxis/config/general.yaml``,
a route is resolved in the current project first and then in each linked
project.  ``route`` may also be an absolute path to a route directory (or its
README.md) — accepted only when it lies inside one of those projects'
``project-context/`` directories — so a name collision between projects is
always addressable.

This module is deliberately free of any Flask import so the logic is
unit-testable and reusable; the MCP layer (``mcp_tools.py``) is a thin
adapter that validates the ``project_root`` and calls this module.
"""

from __future__ import annotations

import os

import settings_ops

__all__ = ["read_context_route"]

CONTEXT_DIRNAME = "project-context"

_VALID_MODES = ("toc", "tldr", "section")


def _parse_sections(content: str) -> list[dict]:
    """Parse markdown content into a list of sections split by ``## `` headings.

    Returns a list of dicts with keys:
    - ``name``: heading text (without the ``## `` prefix)
    - ``content``: full text of the section including the heading line
    - ``line_count``: number of lines in the section (including heading)
    """
    sections: list[dict] = []
    current_name: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        if line.startswith("## "):
            # Save previous section if any
            if current_name is not None:
                sections.append({
                    "name": current_name,
                    "content": "\n".join(current_lines),
                    "line_count": len(current_lines),
                })
            current_name = line[3:].strip()
            current_lines = [line]
        else:
            if current_name is not None:
                current_lines.append(line)

    # Save last section
    if current_name is not None:
        sections.append({
            "name": current_name,
            "content": "\n".join(current_lines),
            "line_count": len(current_lines),
        })

    return sections


def _resolve_roots(project_root: str) -> list[str]:
    """Return the current project root followed by its linked project roots.

    A config read failure degrades to the current project alone — a
    misconfigured linked project must never break reading a local route.
    """
    try:
        return settings_ops.resolve_project_roots(project_root)
    except Exception:
        return [project_root]


def _resolve_readme_path(project_root: str, route: str) -> str | None:
    """Locate a route README across the current project and its linked projects.

    A plain route name resolves current-project-first, then linked projects in
    configuration order, so a local route always shadows a same-named linked
    one.  An absolute *route* addresses a specific project's route directly and
    is accepted only when it resolves inside some project's
    ``project-context/`` boundary, so it cannot be used to read arbitrary files.
    """
    roots = _resolve_roots(project_root)

    if os.path.isabs(route):
        candidate = route
        if os.path.basename(candidate) != "README.md":
            candidate = os.path.join(candidate, "README.md")
        if not os.path.isfile(candidate):
            return None
        real = os.path.realpath(candidate)
        for root in roots:
            base_real = os.path.realpath(os.path.join(root, CONTEXT_DIRNAME))
            if real.startswith(base_real + os.sep):
                return candidate
        return None

    for root in roots:
        candidate = os.path.join(root, CONTEXT_DIRNAME, route, "README.md")
        if os.path.isfile(candidate):
            return candidate
    return None


def _list_available_routes(project_root: str) -> list[str]:
    """Return route labels across the current project and its linked projects.

    Current-project routes are listed as plain names; linked-project routes are
    listed as absolute directory paths, which is exactly the form
    :func:`_resolve_readme_path` accepts for them.
    """
    labels: list[str] = []
    for index, root in enumerate(_resolve_roots(project_root)):
        base = os.path.join(root, CONTEXT_DIRNAME)
        if not os.path.isdir(base):
            continue
        try:
            for entry in sorted(os.scandir(base), key=lambda e: e.name):
                if entry.is_dir() and os.path.isfile(
                    os.path.join(entry.path, "README.md")
                ):
                    labels.append(entry.name if index == 0 else entry.path)
        except OSError:
            continue
    return labels


def read_context_route(
    project_root: str,
    route: str,
    mode: str = "tldr",
    section_name: str = "",
) -> str:
    """Read a specific section of a project-context route README.

    Parameters
    ----------
    project_root:
        Absolute path to the project root (containing ``.praxis/``).
    route:
        Route name (subdirectory of ``project-context/``), e.g. ``"tasks"``,
        ``"mcp"``, ``"ai-chat"`` — resolved in the current project first, then
        in linked projects.  May also be an absolute path to a linked
        project's route directory.
    mode:
        Reading mode — ``"toc"``, ``"tldr"`` (default), or ``"section"``.
    section_name:
        Section name for ``mode="section"`` (without the ``## `` prefix),
        e.g. ``"Architecture Decisions"``.

    Returns
    -------
    str
        The requested content, or an error message with guidance.
    """
    mode = mode.strip().lower() if mode else "tldr"
    if mode not in _VALID_MODES:
        return f"Error: invalid mode '{mode}'. Must be one of: {', '.join(_VALID_MODES)}"

    route = route.strip() if route else ""
    if not route:
        return "Error: route is required"

    readme_path = _resolve_readme_path(project_root, route)
    if readme_path is None:
        available = _list_available_routes(project_root)
        if available:
            return f"Error: route '{route}' not found. Available routes: {', '.join(available)}"
        return f"Error: route '{route}' not found. No routes available in project-context/"

    try:
        with open(readme_path, encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError as exc:
        return f"Error: could not read {readme_path}: {exc}"

    sections = _parse_sections(content)

    if mode == "toc":
        if not sections:
            return f"No ## sections found in {route}/README.md"
        lines = [f"## {s['name']} ({s['line_count']} lines)" for s in sections]
        return "\n".join(lines)

    if mode == "tldr":
        for s in sections:
            if s["name"] == "TL;DR":
                return s["content"]
        # Fallback: no TL;DR section found
        available_names = [s["name"] for s in sections]
        if available_names:
            return f"No ## TL;DR section found in {route}/README.md. Available sections: {', '.join(available_names)}"
        return f"No ## TL;DR section found in {route}/README.md"

    # mode == "section"
    section_name = section_name.strip() if section_name else ""
    if not section_name:
        return "Error: section_name is required when mode='section'"

    requested = [n.strip() for n in section_name.split(",")]
    available_names = [s["name"] for s in sections]
    section_map = {s["name"]: s["content"] for s in sections}

    found: list[str] = []
    not_found: list[str] = []
    for name in requested:
        if name in section_map:
            found.append(section_map[name])
        else:
            not_found.append(name)

    if not found:
        label = section_name if len(requested) == 1 else ", ".join(not_found)
        if available_names:
            return f"Section '{label}' not found in {route}/README.md. Available sections: {', '.join(available_names)}"
        return f"Section '{label}' not found in {route}/README.md (no sections found)"

    result = "\n\n".join(found)
    if not_found:
        result += f"\n\nNote: section(s) not found: {', '.join(not_found)}. Available: {', '.join(available_names)}"
    return result
