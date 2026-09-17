"""MCP connection health checker — shells out to `claude mcp list` and parses status.

Isolated parser module so the text-parsing logic is easy to update if the CLI
output format changes.  Falls back to direct HTTP probing when the CLI is
unavailable or times out.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from threading import Lock

__all__ = [
    "check_mcp_connections",
    "parse_claude_mcp_list",
]

# ---------------------------------------------------------------------------
# Cache — 60s TTL, guarded by a lock so concurrent requests share one probe.
# ---------------------------------------------------------------------------

_cache_lock = Lock()
_cache: dict = {"result": None, "timestamp": 0.0}
_CACHE_TTL_SECONDS = 60.0

# CLI timeout — if `claude mcp list` takes longer, fall back to HTTP probe.
_CLI_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Parser — isolated for easy update if CLI output format changes.
# ---------------------------------------------------------------------------

# Example line: "barley: https://mcp.barley.provectus.pro/mcp (HTTP) - ✔ Connected"
_LINE_RE = re.compile(
    r"^(\S+):\s+(\S+)\s+\((\w+)\)\s+-\s+(.+)$"
)


def parse_claude_mcp_list(output: str) -> list[dict]:
    """Parse the human-readable output of ``claude mcp list`` into structured data.

    Returns a list of dicts: ``{name, status, url, transport}``.
    Status is one of: ``connected``, ``disconnected``, ``pending``, ``error``.
    """
    servers: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Checking"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        name, url, transport, raw_status = m.groups()
        if "Connected" in raw_status and "✔" in raw_status:
            status = "connected"
        elif "Pending" in raw_status or "⏸" in raw_status:
            status = "pending"
        elif "✘" in raw_status:
            status = "error"
        else:
            status = "disconnected"
        servers.append({
            "name": name,
            "status": status,
            "url": url,
            "transport": transport,
        })
    return servers


# ---------------------------------------------------------------------------
# HTTP probe fallback
# ---------------------------------------------------------------------------


def _discover_mcp_server_urls(project_root: str) -> dict[str, str]:
    """Read ``~/.claude.json`` and ``.mcp.json`` for server URLs."""
    urls: dict[str, str] = {}
    paths = [
        Path.home() / ".claude.json",
        Path(project_root) / ".mcp.json",
    ]
    for cfg_path in paths:
        try:
            with open(cfg_path) as f:
                data = json.load(f)
            for name, cfg in data.get("mcpServers", {}).items():
                if isinstance(cfg, dict) and "url" in cfg:
                    urls[name] = cfg["url"]
        except Exception:
            continue
    return urls


def _fallback_http_probe(project_root: str) -> list[dict]:
    """Probe discovered MCP server URLs with a 3s connect timeout.

    Any HTTP response (even 401) = ``reachable``; timeout/000 = ``unreachable``.
    """
    urls = _discover_mcp_server_urls(project_root)
    servers: list[dict] = []
    for name, url in urls.items():
        try:
            result = subprocess.run(
                [
                    "curl", "-s", "-o", "/dev/null",
                    "-w", "%{http_code}",
                    "--connect-timeout", "3",
                    "-m", "5",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            code = result.stdout.strip()
            status = "reachable" if code != "000" else "unreachable"
        except Exception:
            status = "unreachable"
        servers.append({
            "name": name,
            "status": status,
            "url": url,
        })
    return servers


# ---------------------------------------------------------------------------
# MCPMode check
# ---------------------------------------------------------------------------


def _read_mcp_mode(project_root: str) -> bool:
    """Read ``MCPMode`` from ``general.yaml``.  Returns ``False`` on any error."""
    config_path = os.path.join(project_root, ".praxis", "config", "general.yaml")
    try:
        with open(config_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("MCPMode:"):
                    value = stripped.split(":", 1)[1].strip().lower()
                    return value == "true"
    except (FileNotFoundError, OSError):
        pass
    return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def check_mcp_connections(
    claude_bin: str | None,
    project_root: str,
) -> dict:
    """Return structured MCP connection status, with 60s caching.

    Parameters
    ----------
    claude_bin:
        Absolute path to the ``claude`` binary, or ``None`` if not found.
    project_root:
        Absolute path to the project root.

    Returns
    -------
    dict
        ``{ok, mcp_mode, servers, source, error?}``
    """
    # Check MCPMode first — skip everything if MCP is off.
    mcp_mode = _read_mcp_mode(project_root)
    if not mcp_mode:
        return {
            "ok": True,
            "mcp_mode": False,
            "servers": [],
            "source": "skipped",
        }

    # Check cache.
    now = time.time()
    with _cache_lock:
        if _cache["result"] is not None and (now - _cache["timestamp"]) < _CACHE_TTL_SECONDS:
            return _cache["result"]

    # Try CLI first.
    result = _probe_via_cli(claude_bin, project_root)

    # Cache the result.
    with _cache_lock:
        _cache["result"] = result
        _cache["timestamp"] = time.time()

    return result


def _probe_via_cli(claude_bin: str | None, project_root: str) -> dict:
    """Attempt ``claude mcp list``; fall back to HTTP probe on failure."""
    if claude_bin:
        try:
            proc = subprocess.run(
                [claude_bin, "mcp", "list"],
                capture_output=True,
                text=True,
                timeout=_CLI_TIMEOUT_SECONDS,
                cwd=project_root,
            )
            # Combine stdout and stderr — the CLI may write status lines to either.
            output = (proc.stdout or "") + "\n" + (proc.stderr or "")
            servers = parse_claude_mcp_list(output)
            if servers:
                return {
                    "ok": True,
                    "mcp_mode": True,
                    "servers": servers,
                    "source": "cli",
                }
        except subprocess.TimeoutExpired:
            pass  # Fall through to HTTP probe.
        except Exception:
            pass  # Fall through to HTTP probe.

    # Fallback: HTTP probe.
    try:
        servers = _fallback_http_probe(project_root)
        return {
            "ok": True,
            "mcp_mode": True,
            "servers": servers,
            "source": "http_probe",
        }
    except Exception as exc:
        return {
            "ok": False,
            "mcp_mode": True,
            "servers": [],
            "source": "error",
            "error": str(exc),
        }
