"""Git commit history, branch listing and historical file reads for the
Commits History Panel (POS-2337).

Endpoints in ``praxis_local_api.py``: ``/api/git/commits``, ``/api/git/branches``,
``/api/git/checkout``, ``/api/git/file-at-commit``.
"""

from __future__ import annotations

import os
import re
import subprocess

_GIT_TIMEOUT_SECONDS = 15

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_LOG_FORMAT = _RECORD_SEP + _FIELD_SEP.join(["%H", "%h", "%s", "%an", "%aI", "%D", "%P"])

SEARCH_SCAN_LIMIT = 2000  # bounded scan when a search filter is active

HASH_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,255}$")


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _run_git_bytes(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=_GIT_TIMEOUT_SECONDS)


def is_valid_ref(ref: object) -> bool:
    """True when ``ref`` is a safe branch/tag/ref name for ``git checkout``."""
    if not isinstance(ref, str):
        return False
    candidate = ref.strip()
    if not candidate or candidate.startswith("-"):
        return False
    if ".." in candidate:
        return False
    return bool(REF_RE.match(candidate))


def is_valid_hash(value: object) -> bool:
    """True when ``value`` looks like a (possibly abbreviated) git commit hash."""
    return isinstance(value, str) and bool(HASH_RE.match(value))


def _is_safe_relative_path(file_path: object) -> bool:
    """True when ``file_path`` is a non-empty relative path with no ``..`` segments."""
    if not isinstance(file_path, str):
        return False
    candidate = file_path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or os.path.isabs(candidate):
        return False
    return all(segment not in ("", "..") for segment in candidate.split("/"))


def _parse_log(stdout: str) -> list[dict]:
    """Parse ``git log --name-status --pretty=format:<_LOG_FORMAT>`` output into records."""
    records: list[dict] = []
    for chunk in stdout.split(_RECORD_SEP):
        if not chunk:
            continue
        lines = chunk.split("\n")
        header = lines[0]
        fields = header.split(_FIELD_SEP)
        if len(fields) < 6:
            continue

        parent = fields[6].split()[0] if len(fields) > 6 and fields[6].strip() else ""

        files: list[str] = []
        seen_files: set[str] = set()
        file_statuses: dict[str, str] = {}
        file_old_paths: dict[str, str] = {}
        for raw_line in lines[1:]:
            file_line = raw_line.strip()
            if not file_line:
                continue
            parts = file_line.split("\t")
            if len(parts) < 2:
                continue
            path = parts[-1].strip()
            if not path or path in seen_files:
                continue
            seen_files.add(path)
            files.append(path)
            code = parts[0].strip()[:1]
            if code == "A":
                status = "added"
            elif code == "D":
                status = "deleted"
            else:
                status = "modified"
            file_statuses[path] = status
            # Renamed/copied files: keep the pre-rename source path (POS-2350).
            if code in ("R", "C") and len(parts) >= 3:
                old_path = parts[1].strip()
                if old_path:
                    file_old_paths[path] = old_path

        refs: list[dict] = []
        refs_field = fields[5].strip()
        if refs_field:
            for entry in refs_field.split(", "):
                name = entry.strip()
                if not name:
                    continue
                if name.startswith("HEAD -> "):
                    name = name[len("HEAD -> "):]
                elif name == "HEAD":
                    continue

                # ``origin/HEAD`` is a symbolic alias for the remote's default
                # branch, not a ref worth labelling a commit with.
                if name.split("/")[-1] == "HEAD":
                    continue

                if name.startswith("tag: "):
                    ref_name = name[len("tag: "):]
                    if not ref_name:
                        continue
                    refs.append({"name": ref_name, "type": "tag"})
                else:
                    if not name:
                        continue
                    ref_type = "remote" if name.startswith("origin/") or "/" in name else "local"
                    refs.append({"name": name, "type": ref_type})

        records.append({
            "hash": fields[0],
            "short_hash": fields[1],
            "message": fields[2],
            "author": fields[3],
            "date": fields[4],
            "refs": refs,
            "files": files,
            "file_statuses": file_statuses,
            "file_old_paths": file_old_paths,
            "parent": parent,
        })

    return records


def list_commits(project_root: str, offset: int, limit: int, search: str = "") -> dict:
    """Return ``{commits, has_more}`` (or ``{error}``) — a paginated slice of commit history.

    ``search`` (case-insensitive) matches commits whose hash or author name starts
    with it; when set the search scans at most :data:`SEARCH_SCAN_LIMIT` commits.
    """
    offset = max(0, offset)
    limit = max(1, min(limit, 100))
    search = (search or "").strip()

    args = ["log", "--name-status", "--date-order", f"--pretty=format:{_LOG_FORMAT}"]

    if not search:
        args += [f"--skip={offset}", f"--max-count={limit + 1}"]
        result = _run_git(args, project_root)
        if result.returncode != 0:
            stderr = result.stderr or ""
            if (
                "does not have any commits" in stderr
                or "unknown revision" in stderr
                or "bad default revision" in stderr
            ):
                return {"commits": [], "has_more": False, "total": 0}
            return {"error": stderr.strip() or "git log failed"}

        records = _parse_log(result.stdout)
        has_more = len(records) > limit

        # Get total commit count
        count_result = _run_git(["rev-list", "--count", "HEAD"], project_root)
        try:
            total = int(count_result.stdout.strip()) if count_result.returncode == 0 else 0
        except ValueError:
            total = 0

        return {"commits": records[:limit], "has_more": has_more, "total": total}

    args += [f"--max-count={SEARCH_SCAN_LIMIT}"]
    result = _run_git(args, project_root)
    if result.returncode != 0:
        stderr = result.stderr or ""
        if (
            "does not have any commits" in stderr
            or "unknown revision" in stderr
            or "bad default revision" in stderr
        ):
            return {"commits": [], "has_more": False, "total": 0}
        return {"error": stderr.strip() or "git log failed"}

    records = _parse_log(result.stdout)
    needle = search.lower()
    match_hash = bool(HASH_RE.match(search))
    filtered = [
        rec for rec in records
        if (match_hash and rec["hash"].lower().startswith(needle))
        or rec["author"].lower().startswith(needle)
    ]
    page = filtered[offset:offset + limit + 1]
    has_more = len(page) > limit
    result = {"commits": page[:limit], "has_more": has_more, "total": len(filtered)}
    if len(records) >= SEARCH_SCAN_LIMIT:
        result["total_capped"] = True
    return result


def list_branches(project_root: str) -> dict:
    """Return ``{current, local, remote}`` (or ``{error}``) — local/remote branch listings."""
    current_result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], project_root)
    current = current_result.stdout.strip() if current_result.returncode == 0 else ""

    local_result = _run_git(
        ["for-each-ref", "--format=%(refname:short)" + _FIELD_SEP + "%(objectname:short)", "refs/heads"],
        project_root,
    )
    remote_result = _run_git(
        ["for-each-ref", "--format=%(refname:short)" + _FIELD_SEP + "%(objectname:short)", "refs/remotes"],
        project_root,
    )

    if local_result.returncode != 0 and remote_result.returncode != 0:
        stderr = local_result.stderr or remote_result.stderr or ""
        return {"error": stderr.strip() or "git for-each-ref failed"}

    def _parse_refs(stdout: str) -> list[dict]:
        entries: list[dict] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(_FIELD_SEP)
            if len(parts) < 2:
                continue
            name = parts[0].strip()
            sha = parts[1].strip()
            if not name:
                continue
            if name.rsplit("/", 1)[-1] == "HEAD":
                continue
            entries.append({"name": name, "short_hash": sha})
        entries.sort(key=lambda e: e["name"])
        return entries

    local = _parse_refs(local_result.stdout) if local_result.returncode == 0 else []
    remote = _parse_refs(remote_result.stdout) if remote_result.returncode == 0 else []

    return {"current": current, "local": local, "remote": remote}


def checkout(project_root: str, ref: str) -> dict:
    """Check out ``ref`` (branch, tag, or commit hash). Returns ``{ok: True}`` or ``{error}``.

    Note: git writes checkout progress to stderr even on success, so only a
    non-zero returncode is treated as failure.
    """
    if not (is_valid_ref(ref) or is_valid_hash(ref)):
        return {"error": "invalid ref"}

    result = _run_git(["checkout", ref], project_root)
    if result.returncode != 0:
        return {"error": result.stderr.strip() or result.stdout.strip() or "git checkout failed"}
    return {"ok": True}


def file_at_commit(project_root: str, commit_hash: str, file_path: str) -> dict:
    """Return ``{content, binary}`` for ``file_path`` as it existed at ``commit_hash``."""
    if not is_valid_hash(commit_hash):
        return {"error": "invalid commit hash"}
    if not _is_safe_relative_path(file_path):
        return {"error": "invalid file path"}

    result = _run_git_bytes(["show", f"{commit_hash}:{file_path}"], project_root)
    if result.returncode != 0:
        return {"error": "file not found in commit"}
    if b"\x00" in result.stdout:
        return {"content": None, "binary": True}
    return {"content": result.stdout.decode("utf-8", errors="replace"), "binary": False}
