"""Tests for git_history_ops — commit history, branches, checkout for POS-2337."""

from __future__ import annotations

import shutil
import subprocess

import pytest

import git_history_ops
from git_history_ops import (
    checkout,
    file_at_commit,
    is_valid_hash,
    is_valid_ref,
    list_branches,
    list_commits,
)


# ---------------------------------------------------------------------------
# is_valid_ref / is_valid_hash
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref", ["-x", "a..b", ""])
def test_is_valid_ref_rejects(ref):
    assert is_valid_ref(ref) is False


@pytest.mark.parametrize("ref", ["main", "origin/main", "feature/x-1"])
def test_is_valid_ref_accepts(ref):
    assert is_valid_ref(ref) is True


def test_is_valid_hash():
    assert is_valid_hash("abc123") is True
    assert is_valid_hash("not a hash!") is False
    assert is_valid_hash(None) is False


# ---------------------------------------------------------------------------
# fixtures — a real temporary git repo
# ---------------------------------------------------------------------------

def _git_available() -> bool:
    return shutil.which("git") is not None


def _run(args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


@pytest.fixture()
def git_repo(tmp_path):
    if not _git_available():
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["init"], repo)
    _run(["config", "user.email", "t@t"], repo)
    _run(["config", "user.name", "t"], repo)
    return repo


def _commit(repo, message="commit"):
    _run(["add", "-A"], repo)
    _run(["commit", "-m", message], repo)
    hash_result = _run(["rev-parse", "HEAD"], repo)
    return hash_result.stdout.strip()


# ---------------------------------------------------------------------------
# list_commits
# ---------------------------------------------------------------------------

def test_list_commits_returns_newest_first_with_fields(git_repo):
    (git_repo / "a.txt").write_text("one\n")
    _commit(git_repo, "first commit")
    (git_repo / "a.txt").write_text("two\n")
    _commit(git_repo, "second commit")

    result = list_commits(str(git_repo), offset=0, limit=20)
    assert "error" not in result
    commits = result["commits"]
    assert len(commits) == 2
    assert commits[0]["message"] == "second commit"
    assert commits[1]["message"] == "first commit"

    top = commits[0]
    assert len(top["hash"]) == 40
    assert top["short_hash"]
    assert top["author"] == "t"
    assert top["date"]
    assert top["files"] == ["a.txt"]


def test_list_commits_pagination(git_repo):
    _commit_hashes = []
    (git_repo / "a.txt").write_text("one\n")
    _commit_hashes.append(_commit(git_repo, "commit 1"))
    (git_repo / "a.txt").write_text("two\n")
    _commit_hashes.append(_commit(git_repo, "commit 2"))
    (git_repo / "a.txt").write_text("three\n")
    _commit_hashes.append(_commit(git_repo, "commit 3"))

    page0 = list_commits(str(git_repo), offset=0, limit=1)
    page1 = list_commits(str(git_repo), offset=1, limit=1)
    page2 = list_commits(str(git_repo), offset=2, limit=1)

    assert page0["commits"][0]["message"] == "commit 3"
    assert page1["commits"][0]["message"] == "commit 2"
    assert page2["commits"][0]["message"] == "commit 1"

    assert page0["has_more"] is True
    assert page1["has_more"] is True
    assert page2["has_more"] is False


def test_list_commits_search_by_author_prefix(git_repo):
    (git_repo / "a.txt").write_text("one\n")
    _commit(git_repo, "first commit")

    result = list_commits(str(git_repo), offset=0, limit=20, search="t")
    assert [c["message"] for c in result["commits"]] == ["first commit"]


def test_list_commits_search_by_hash_prefix(git_repo):
    (git_repo / "a.txt").write_text("one\n")
    commit_hash = _commit(git_repo, "first commit")

    result = list_commits(str(git_repo), offset=0, limit=20, search=commit_hash[:6])
    assert [c["message"] for c in result["commits"]] == ["first commit"]


def test_list_commits_search_no_match(git_repo):
    (git_repo / "a.txt").write_text("one\n")
    _commit(git_repo, "first commit")

    result = list_commits(str(git_repo), offset=0, limit=20, search="nonexistent-needle")
    assert result["commits"] == []
    assert result["has_more"] is False


def test_list_commits_empty_repo(git_repo):
    result = list_commits(str(git_repo), offset=0, limit=20)
    assert result == {"commits": [], "has_more": False}
    assert "error" not in result


# ---------------------------------------------------------------------------
# list_branches
# ---------------------------------------------------------------------------

def test_list_branches_current_and_local(git_repo):
    (git_repo / "a.txt").write_text("one\n")
    _commit(git_repo, "first commit")

    result = list_branches(str(git_repo))
    assert "error" not in result
    assert result["current"] in ("main", "master")
    local_names = [b["name"] for b in result["local"]]
    assert result["current"] in local_names


# ---------------------------------------------------------------------------
# file_at_commit
# ---------------------------------------------------------------------------

def test_file_at_commit_returns_old_content(git_repo):
    (git_repo / "f.txt").write_text("old content\n")
    old_hash = _commit(git_repo, "add file")

    (git_repo / "f.txt").write_text("new content\n")
    _commit(git_repo, "modify file")

    result = file_at_commit(str(git_repo), old_hash, "f.txt")
    assert "error" not in result
    assert result["binary"] is False
    assert result["content"] == "old content\n"


def test_file_at_commit_unknown_path_is_error(git_repo):
    (git_repo / "f.txt").write_text("content\n")
    commit_hash = _commit(git_repo, "add file")

    result = file_at_commit(str(git_repo), commit_hash, "does-not-exist.txt")
    assert "error" in result


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------

def test_checkout_invalid_ref_is_error(git_repo):
    (git_repo / "a.txt").write_text("one\n")
    _commit(git_repo, "first commit")

    result = checkout(str(git_repo), "-x")
    assert result == {"error": "invalid ref"}


def test_checkout_valid_branch(git_repo):
    (git_repo / "a.txt").write_text("one\n")
    _commit(git_repo, "first commit")
    _run(["branch", "other"], git_repo)

    result = checkout(str(git_repo), "other")
    assert result == {"ok": True}


def test_list_commits_reports_file_statuses_and_parent(git_repo):
    (git_repo / "modified.txt").write_text("one\n")
    (git_repo / "deleted.txt").write_text("gone\n")
    root_hash = _commit(git_repo, "root commit")

    (git_repo / "new.txt").write_text("new\n")
    (git_repo / "modified.txt").write_text("two\n")
    (git_repo / "deleted.txt").unlink()
    second_hash = _commit(git_repo, "second commit")

    result = list_commits(str(git_repo), offset=0, limit=20)
    assert "error" not in result
    commits = result["commits"]
    assert len(commits) == 2

    top = commits[0]
    assert top["hash"] == second_hash
    assert top["file_statuses"] == {
        "new.txt": "added",
        "modified.txt": "modified",
        "deleted.txt": "deleted",
    }
    assert set(top["files"]) == {"new.txt", "modified.txt", "deleted.txt"}
    assert top["parent"] == root_hash

    root = commits[1]
    assert root["hash"] == root_hash
    assert root["parent"] == ""


def test_parse_log_rename_maps_to_new_path_modified():
    parent_hash = "b" * 40
    record = git_history_ops._RECORD_SEP + git_history_ops._FIELD_SEP.join([
        "a" * 40, "aaaaaaa", "subject", "Author", "2026-01-01T00:00:00+00:00",
        "",
        parent_hash,
    ])
    record += "\nR100\told.txt\tnew.txt"

    parsed = git_history_ops._parse_log(record)[0]
    assert parsed["files"] == ["new.txt"]
    assert parsed["file_statuses"] == {"new.txt": "modified"}
    assert parsed["parent"] == parent_hash


def test_parse_log_drops_symbolic_head_refs():
    """``HEAD``, ``HEAD -> main`` and ``origin/HEAD`` must not become ref labels."""
    record = git_history_ops._RECORD_SEP + git_history_ops._FIELD_SEP.join([
        "a" * 40, "aaaaaaa", "subject", "Author", "2026-01-01T00:00:00+00:00",
        "HEAD -> main, origin/main, origin/HEAD, tag: v1.0",
    ])
    refs = git_history_ops._parse_log(record)[0]["refs"]
    assert refs == [
        {"name": "main", "type": "local"},
        {"name": "origin/main", "type": "remote"},
        {"name": "v1.0", "type": "tag"},
    ]
