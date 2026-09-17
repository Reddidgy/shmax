#!/usr/bin/env python3
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
SEMVER_RE = re.compile(r"^\d+\.\d+\.(?:\d+|-1)$")


def normalize_target_path(raw_path: str) -> Path:
    cleaned = raw_path.strip()
    if not cleaned:
        raise ValueError("Version file path is empty.")

    relative = cleaned[1:] if cleaned.startswith("/") else cleaned
    path = Path(relative)

    if path.name == "":
        raise ValueError(f"Invalid version file path '{raw_path}'.")

    if any(part in (".", "..") for part in path.parts):
        raise ValueError("Path traversal is not allowed.")

    return ROOT_DIR / path


def parse_semver(version_value: str) -> tuple[int, int, int]:
    normalized = version_value.strip()
    if not SEMVER_RE.match(normalized):
        raise ValueError(
            f"Invalid version format '{version_value}'. Expected X.Y.Z or X.Y.-1."
        )

    major_str, minor_str, patch_str = normalized.split(".")
    return int(major_str), int(minor_str), int(patch_str)


def git_add(path: Path) -> None:
    try:
        repo_relative = path.relative_to(ROOT_DIR)
    except ValueError:
        repo_relative = path
    subprocess.run(["git", "add", "--", str(repo_relative)], check=True)


def bump_version(argv: Sequence[str]) -> None:
    target_path = argv[1] if len(argv) > 1 else "/version"
    version_file = normalize_target_path(target_path)

    if not version_file.exists():
        print(f"Error: {version_file} not found.")
        sys.exit(1)

    original_version_content = version_file.read_text(encoding="utf-8")

    try:
        current_version = original_version_content.strip()
        major, minor, patch = parse_semver(current_version)
        new_version = f"{major}.{minor}.{patch + 1}"

        version_file.write_text(f"{new_version}\n", encoding="utf-8")
        git_add(version_file)
    except Exception as exc:
        version_file.write_text(original_version_content, encoding="utf-8")
        print(f"Error updating version: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    bump_version(sys.argv)