"""Tests for :mod:`file_listing` — the project file lister (pure, no Flask).

Run from the ``api/`` directory:

    ./venv/bin/python -m pytest test_file_listing.py -v

These build a temp tree with ``tmp_path`` and assert that
:func:`list_project_files` returns sorted, relative, POSIX-style paths, prunes
the heavyweight/generated directories, and respects ``MAX_FILES``.
"""

from __future__ import annotations

import file_listing
from file_listing import MAX_FILES, list_project_files


def test_returns_sorted_relative_posix_paths(tmp_path):
    """Paths are project-relative, forward-slashed, and sorted."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("b")
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "README.md").write_text("readme")

    result = list_project_files(str(tmp_path))

    assert result == ["README.md", "src/a.py", "src/b.py"]
    assert result == sorted(result)
    assert all("\\" not in path for path in result)


def test_prunes_node_modules_and_git(tmp_path):
    """Files under pruned directory names never appear in the result."""
    (tmp_path / "keep.py").write_text("keep")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "abc").write_text("y")

    result = list_project_files(str(tmp_path))

    assert result == ["keep.py"]
    assert not any(path.startswith("node_modules/") for path in result)
    assert not any(path.startswith(".git/") for path in result)


def test_prunes_nested_pruned_dirs(tmp_path):
    """Pruned names are excluded wherever they appear, not just at the root."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("m")
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "mod.cpython.pyc").write_text("c")

    result = list_project_files(str(tmp_path))

    assert result == ["pkg/mod.py"]


def test_respects_max_files(tmp_path, monkeypatch):
    """A tree larger than MAX_FILES is truncated to the first MAX_FILES sorted."""
    monkeypatch.setattr(file_listing, "MAX_FILES", 3)
    for index in range(10):
        (tmp_path / f"f{index:02d}.txt").write_text("x")

    result = list_project_files(str(tmp_path))

    assert len(result) == 3
    assert result == ["f00.txt", "f01.txt", "f02.txt"]


def test_max_files_default_is_8000():
    """The module-level cap is the documented 8000."""
    assert MAX_FILES == 8000


def test_empty_project_returns_empty_list(tmp_path):
    """A project with no files yields an empty list, not an error."""
    assert list_project_files(str(tmp_path)) == []


def test_prunes_general_yaml_sidecar_files(tmp_path):
    """The general.yaml backup/lock/corrupted/tmp sidecars are never @-mentionable."""
    config_dir = tmp_path / ".praxis" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "general.yaml").write_text("chatEnabled: true\n")
    (config_dir / "general.yaml.bak").write_text("chatEnabled: true\n")
    (config_dir / "general.yaml.lock").write_text("")
    (config_dir / "general.yaml.corrupted").write_text("garbage")
    (config_dir / "general.yaml.corrupted.1694100000").write_text("garbage")
    (config_dir / ".general.yaml.abc.tmp").write_text("partial")

    result = list_project_files(str(tmp_path))

    assert result == [".praxis/config/general.yaml"]
