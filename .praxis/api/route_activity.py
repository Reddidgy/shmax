"""Route activity analytics for the Blueprint Heatmap (POS-2348).

Computes per-route activity metrics from git history (line changes) and
search query logs (search_project_context hits).  Results are cached
in memory with a configurable TTL so repeated requests within the same
Blueprint session don't re-run git log.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import time
from threading import Lock

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 15

# ── In-memory cache ───────────────────────────────────────────────────

_cache_lock = Lock()
_cache: dict[str, tuple[float, dict]] = {}  # key → (expires_at, data)
_CACHE_TTL_SECONDS = 60


def _cache_key(project_root: str, period: str) -> str:
    return f"{project_root}::{period}"


def _get_cached(project_root: str, period: str) -> dict | None:
    key = _cache_key(project_root, period)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if time.time() > expires_at:
            del _cache[key]
            return None
        return data


def _set_cached(project_root: str, period: str, data: dict) -> None:
    key = _cache_key(project_root, period)
    with _cache_lock:
        _cache[key] = (time.time() + _CACHE_TTL_SECONDS, data)


# ── Period helpers ────────────────────────────────────────────────────

_PERIOD_SINCE = {
    "day": "24 hours ago",
    "week": "7 days ago",
    "month": "30 days ago",
}


def _period_cutoff_epoch(period: str) -> float:
    """Return the Unix epoch timestamp for the start of the given period."""
    now = time.time()
    if period == "day":
        return now - 86400
    if period == "week":
        return now - 86400 * 7
    return now - 86400 * 30


# ── Git line-change analysis ─────────────────────────────────────────


def _git_line_changes(project_root: str, period: str) -> dict[str, int]:
    """Run ``git log --numstat`` on ``project-context/`` and sum lines added/removed per route.

    Returns ``{routeId: totalLinesChanged}``.
    """
    since = _PERIOD_SINCE.get(period, "30 days ago")
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={since}",
                "--numstat",
                "--pretty=format:",
                "--diff-filter=ACDMR",
                "--", "project-context/",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git log failed for heatmap: %s", exc)
        return {}

    if result.returncode != 0:
        logger.warning("git log exited %d: %s", result.returncode, result.stderr[:200])
        return {}

    changes: dict[str, int] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_str, removed_str, file_path = parts[0], parts[1], parts[2]
        # Binary files show "-" for added/removed
        try:
            added = int(added_str)
        except ValueError:
            added = 0
        try:
            removed = int(removed_str)
        except ValueError:
            removed = 0

        # Extract route id from path like "project-context/<routeId>/..."
        if not file_path.startswith("project-context/"):
            continue
        remainder = file_path[len("project-context/"):]
        route_id = remainder.split("/")[0] if "/" in remainder else remainder
        if not route_id:
            continue

        changes[route_id] = changes.get(route_id, 0) + added + removed

    return changes


# ── Search hit log ────────────────────────────────────────────────────

_SEARCH_LOG_FILENAME = "search_hits.jsonl"


def _search_log_path(project_root: str) -> str:
    return os.path.join(project_root, ".praxis", "analytics", _SEARCH_LOG_FILENAME)


def log_search_hit(project_root: str, matched_routes: list[str]) -> None:
    """Append a search hit record to the JSONL log.

    Called from the ``search_project_context`` MCP tool after each search.
    """
    if not matched_routes:
        return
    log_path = _search_log_path(project_root)
    log_dir = os.path.dirname(log_path)
    try:
        os.makedirs(log_dir, exist_ok=True)
        record = json.dumps({"ts": time.time(), "routes": matched_routes})
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(record + "\n")
    except OSError as exc:
        logger.debug("Failed to write search hit log: %s", exc)


def _search_hits(project_root: str, period: str) -> dict[str, int]:
    """Count search hits per route from the JSONL log for the given period."""
    log_path = _search_log_path(project_root)
    if not os.path.isfile(log_path):
        return {}

    cutoff = _period_cutoff_epoch(period)
    hits: dict[str, int] = {}
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = record.get("ts", 0)
                if ts < cutoff:
                    continue
                for route in record.get("routes", []):
                    hits[route] = hits.get(route, 0) + 1
    except OSError as exc:
        logger.debug("Failed to read search hit log: %s", exc)

    return hits


# ── Combined activity computation ─────────────────────────────────────


def compute_route_activity(project_root: str, period: str) -> dict:
    """Compute per-route activity for the requested period.

    Returns ``{"routes": {routeId: {"lineChanges": int, "searchHits": int, "heat": float}}, "available": true}``.
    ``heat`` is normalized 0..1 across all routes.
    """
    cached = _get_cached(project_root, period)
    if cached is not None:
        return cached

    line_changes = _git_line_changes(project_root, period)
    search_hits = _search_hits(project_root, period)

    # Merge all known route ids
    all_routes = set(line_changes.keys()) | set(search_hits.keys())

    # Also discover routes from project-context/ directory
    pc_dir = os.path.join(project_root, "project-context")
    if os.path.isdir(pc_dir):
        for entry in os.listdir(pc_dir):
            entry_path = os.path.join(pc_dir, entry)
            if os.path.isdir(entry_path) and not entry.startswith("."):
                all_routes.add(entry)

    if not all_routes:
        result = {"routes": {}, "available": False}
        _set_cached(project_root, period, result)
        return result

    # Build raw scores
    raw_scores: dict[str, float] = {}
    routes_data: dict[str, dict] = {}
    for route_id in all_routes:
        lc = line_changes.get(route_id, 0)
        sh = search_hits.get(route_id, 0)
        routes_data[route_id] = {"lineChanges": lc, "searchHits": sh}
        # Combined score: weighted sum (line changes matter more, search adds context)
        raw_scores[route_id] = lc + sh * 5

    # Normalize to 0..1
    max_score = max(raw_scores.values()) if raw_scores else 0
    for route_id in all_routes:
        if max_score > 0:
            heat = raw_scores[route_id] / max_score
        else:
            heat = 0.0
        # Apply sqrt for better visual distribution (avoid most routes at 0)
        routes_data[route_id]["heat"] = round(math.sqrt(heat), 3)

    has_any_activity = any(
        d["lineChanges"] > 0 or d["searchHits"] > 0 for d in routes_data.values()
    )

    result = {"routes": routes_data, "available": has_any_activity}
    _set_cached(project_root, period, result)
    return result
