"""Tests for git_diff_ops — line-level git diff parsing for POS-2308."""

from __future__ import annotations

import shutil
import subprocess

import pytest

import git_diff_ops
from git_diff_ops import diff_file_against_head, is_safe_relative_path, parse_unified_diff


# ---------------------------------------------------------------------------
# is_safe_relative_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ["src/a.ts", "a b.md", "file.txt", "a/b/c.py"],
)
def test_is_safe_relative_path_accepts(path):
    assert is_safe_relative_path(path) is True


@pytest.mark.parametrize(
    "path",
    [None, "", "/abs", "../x", "a/../b", "a//b"],
)
def test_is_safe_relative_path_rejects(path):
    assert is_safe_relative_path(path) is False


# ---------------------------------------------------------------------------
# parse_unified_diff
# ---------------------------------------------------------------------------

def test_parse_unified_diff_pure_addition():
    text = (
        "diff --git a/f.txt b/f.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -3,0 +4,2 @@\n"
        "+new line one\n"
        "+new line two\n"
    )
    hunks, is_binary = parse_unified_diff(text)
    assert is_binary is False
    assert len(hunks) == 1
    lines = hunks[0]["lines"]
    assert len(lines) == 2
    assert [l["type"] for l in lines] == ["added", "added"]
    assert [l["new_line"] for l in lines] == [4, 5]
    assert all(l["old_line"] is None for l in lines)


def test_parse_unified_diff_pure_deletion():
    text = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -5,2 +4,0 @@\n"
        "-old line one\n"
        "-old line two\n"
    )
    hunks, is_binary = parse_unified_diff(text)
    assert is_binary is False
    lines = hunks[0]["lines"]
    assert len(lines) == 2
    assert [l["type"] for l in lines] == ["removed", "removed"]
    assert [l["old_line"] for l in lines] == [5, 6]
    assert [l["anchor_new_line"] for l in lines] == [5, 5]
    assert all(l["new_line"] is None for l in lines)


def test_parse_unified_diff_modification():
    text = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -8,2 +8,1 @@\n"
        "-removed one\n"
        "-removed two\n"
        "+added one\n"
    )
    hunks, is_binary = parse_unified_diff(text)
    assert is_binary is False
    lines = hunks[0]["lines"]
    removed = [l for l in lines if l["type"] == "removed"]
    added = [l for l in lines if l["type"] == "added"]
    assert [l["anchor_new_line"] for l in removed] == [8, 8]
    assert [l["new_line"] for l in added] == [8]


def test_parse_unified_diff_no_newline_at_eof_ignored():
    text = (
        "diff --git a/f.txt b/f.txt\n"
        "--- a/f.txt\n"
        "+++ b/f.txt\n"
        "@@ -1,0 +2,1 @@\n"
        "+last line\n"
        "\\ No newline at end of file\n"
    )
    hunks, is_binary = parse_unified_diff(text)
    assert is_binary is False
    assert len(hunks) == 1
    assert len(hunks[0]["lines"]) == 1
    assert hunks[0]["lines"][0]["type"] == "added"


def test_parse_unified_diff_binary_file():
    text = "Binary files a/img.png and b/img.png differ\n"
    hunks, is_binary = parse_unified_diff(text)
    assert is_binary is True
    assert hunks == []


# ---------------------------------------------------------------------------
# diff_file_against_head — integration with a real temp git repo
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
    _run(["-c", "user.email=t@t", "-c", "user.name=t", "config", "user.email", "t@t"], repo)
    return repo


def _commit(repo, message="commit"):
    _run(["add", "-A"], repo)
    _run(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", message], repo)


def test_diff_file_against_head_modified_file(git_repo):
    file_path = git_repo / "f.txt"
    committed_text = "line1\nline2\nline3\nline4\nline5\n"
    file_path.write_text(committed_text)
    _commit(git_repo)

    file_path.write_text("line1\nline2-changed\nline3\nline5\nline6\n")

    result = diff_file_against_head(str(git_repo), "f.txt")
    assert result["tracked"] is True
    assert result["binary"] is False
    assert len(result["hunks"]) > 0
    assert result["head_content"] == committed_text


def test_diff_file_against_head_untracked_file(git_repo):
    file_path = git_repo / "committed.txt"
    file_path.write_text("hello\n")
    _commit(git_repo)

    untracked = git_repo / "new.txt"
    untracked.write_text("brand new\n")

    result = diff_file_against_head(str(git_repo), "new.txt")
    assert result["tracked"] is False
    assert result["hunks"] == []
    assert result["head_content"] is None


def test_diff_file_against_head_staged_new_file(git_repo):
    file_path = git_repo / "committed.txt"
    file_path.write_text("hello\n")
    _commit(git_repo)

    staged = git_repo / "staged.txt"
    staged.write_text("staged content\n")
    _run(["add", "staged.txt"], git_repo)

    result = diff_file_against_head(str(git_repo), "staged.txt")
    assert result["tracked"] is False
    assert result["hunks"] == []


def test_diff_file_against_head_binary_file(git_repo):
    file_path = git_repo / "img.bin"
    file_path.write_bytes(b"\x00\x01\x02\x03")
    _commit(git_repo)

    file_path.write_bytes(b"\x00\x01\xff\xff")

    result = diff_file_against_head(str(git_repo), "img.bin")
    assert result["tracked"] is True
    assert result["binary"] is True
    assert result["hunks"] == []
    assert result["head_content"] is None
