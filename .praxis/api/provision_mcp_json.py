"""Ensure .mcp.json has the praxis MCP Streamable HTTP entry.

Called by start.sh / start.bat with: python provision_mcp_json.py <mcp_json_path> <port>
Idempotent: exits 0 whether it wrote or skipped.
"""

from __future__ import annotations

import json
import os
import sys


def ensure_gitignore(project_root: str) -> None:
    """Ensure .mcp.json is listed in the project's .gitignore."""
    gi_path = os.path.join(project_root, ".gitignore")
    git_dir = os.path.join(project_root, ".git")

    if not os.path.isdir(git_dir):
        return

    entry = ".mcp.json"
    current = ""
    if os.path.exists(gi_path):
        try:
            with open(gi_path, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            return

    # Check if already present (with or without leading /)
    for line in current.splitlines():
        stripped = line.strip()
        if stripped == entry or stripped == f"/{entry}":
            return

    # Append the entry
    lines = current.rstrip("\n").split("\n") if current.strip() else []
    lines.append(entry)
    with open(gi_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Added {entry} to .gitignore")


def main() -> None:
    mcp_path, port = sys.argv[1], sys.argv[2]
    entry = {"type": "streamable-http", "url": f"http://127.0.0.1:{port}/stream"}

    data: dict = {}
    if os.path.exists(mcp_path):
        try:
            with open(mcp_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
        if not isinstance(data, dict):
            data = {}

    servers = data.setdefault("mcpServers", {})
    if servers.get("praxis") == entry:
        print(".mcp.json already configured")
    else:
        servers["praxis"] = entry
        with open(mcp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        print(f"Updated {mcp_path}")

    # Ensure .mcp.json is in .gitignore
    project_root = os.path.dirname(mcp_path)
    ensure_gitignore(project_root)


if __name__ == "__main__":
    main()
