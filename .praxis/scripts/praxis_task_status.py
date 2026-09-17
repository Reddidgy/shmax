#!/usr/bin/env python3
"""praxis_task_status.py — publish per-task execution status markers
and move task JSON files between status directories.

Creates marker files under:
    .praxis/config/state/{task_id}/{status}
    .praxis/config/state/{task_id}/agentFeedback.yml  (when --agent-name is provided)

Moves task JSON files between:
    .praxis/tasks/{old_status}/{id}.json → .praxis/tasks/{new_status}/{id}.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


PraxisOS_DIR = ".praxis"
CONFIG_DIR = "config"
STATE_DIR = "state"
TASKS_DIR = "tasks"
AGENT_FEEDBACK_FILE = "agentFeedback.yml"
VALID_STATUSES = ("in_progress", "to_review", "error")
STATUS_DIRECTORIES = ("new", "in_progress", "completed", "on_hold", "archived", "to_review")
MOVABLE_STATUSES = ("in_progress", "to_review")
AGENT_NAME_MAX_LENGTH = 10
AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]{0,9}$")


def find_project_root() -> Path:
    """Walk upward from cwd to find the directory that contains .praxis/."""
    current = Path.cwd()
    for directory in [current, *current.parents]:
        if (directory / PraxisOS_DIR).is_dir():
            return directory
    return current


def find_task_file(project_root: Path, task_id: int) -> Path | None:
    """Find the task JSON file across all status directories."""
    tasks_root = project_root / PraxisOS_DIR / TASKS_DIR
    filename = f"{task_id}.json"
    for status_dir in STATUS_DIRECTORIES:
        candidate = tasks_root / status_dir / filename
        if candidate.exists():
            return candidate
    return None


def move_task_file(project_root: Path, task_id: int, target_status: str) -> None:
    """Move a task JSON file to the target status directory and update its status field."""
    if target_status not in MOVABLE_STATUSES:
        return

    source_path = find_task_file(project_root, task_id)
    if source_path is None:
        print(f"Warning: task file {task_id}.json not found in any status directory.", file=sys.stderr)
        return

    tasks_root = project_root / PraxisOS_DIR / TASKS_DIR
    target_dir = tasks_root / target_status
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{task_id}.json"

    if source_path == target_path:
        return

    try:
        content = source_path.read_text(encoding="utf-8")
        data = json.loads(content)
        data["status"] = target_status
        target_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        source_path.unlink()
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: could not update task JSON, falling back to raw move: {exc}", file=sys.stderr)
        try:
            source_path.rename(target_path)
        except OSError as move_exc:
            print(f"Error: failed to move task file: {move_exc}", file=sys.stderr)

    source_dir = source_path.parent
    target_dir_name = target_dir.name
    print(f"Task file {task_id}.json moved: {source_dir.name}/ → {target_dir_name}/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a status marker file for an PraxisOS task. "
            "Marker path: .praxis/config/state/{id}/{status}"
        )
    )
    parser.add_argument("--id", type=int, required=True, help="Task numeric ID, e.g. 909")
    parser.add_argument(
        "--status",
        choices=VALID_STATUSES,
        required=True,
        help="Task execution status marker",
    )
    parser.add_argument(
        "--agent-name",
        required=False,
        default=None,
        help="Short agent app name (e.g. claude, cline, opencode). Lowercase, 1 word, max 10 chars.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.id <= 0:
        print("Task ID must be a positive integer.", file=sys.stderr)
        return 1

    project_root = find_project_root()
    marker_dir = project_root / PraxisOS_DIR / CONFIG_DIR / STATE_DIR / str(args.id)
    marker_dir.mkdir(parents=True, exist_ok=True)

    if args.status == "in_progress":
        stale_marker = marker_dir / "to_review"
        if stale_marker.exists():
            stale_marker.unlink()

    marker_path = marker_dir / args.status
    marker_path.touch(exist_ok=True)

    not_started_marker = marker_dir / "agent_not_started"
    if not_started_marker.exists():
        not_started_marker.unlink()

    move_task_file(project_root, args.id, args.status)

    agent_name: str | None = getattr(args, "agent_name", None)
    if agent_name is not None:
        agent_name = agent_name.strip().lower()
        if not AGENT_NAME_PATTERN.match(agent_name):
            print(
                f"Invalid --agent-name '{agent_name}': must be lowercase letters/digits, "
                f"start with a letter, max {AGENT_NAME_MAX_LENGTH} chars.",
                file=sys.stderr,
            )
            return 1
        feedback_path = marker_dir / AGENT_FEEDBACK_FILE
        feedback_path.write_text(f"agentName: {agent_name}\n", encoding="utf-8")

    status_label = args.status.replace("_", " ")
    print(f"Task {args.id} is {status_label} now")

    if args.status == "to_review":
        print(
            "\n--- Documentation Checklist (mandatory before review) ---\n"
            "Did this task change business logic, data structures, or domain rules?\n"
            "If YES, verify you updated the corresponding domain README in project-context/domains/.\n"
            "If you documented new features, confirm they are IMPLEMENTED, not planned.\n"
            "-----------------------------------------------------------"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())