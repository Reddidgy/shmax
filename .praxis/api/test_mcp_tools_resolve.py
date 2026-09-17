"""Tests for _resolve_project_root in mcp_tools."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_tools import (
    _file_uri_to_path,
    _get_client_project_root,
    _resolve_project_root,
    _resolve_project_root_with_ctx,
)


class TestExplicitPath:
    """Tests when project_root is explicitly provided (non-empty)."""

    def test_valid_explicit_path(self, tmp_path: object) -> None:
        """Explicit valid path (absolute, exists, contains .praxis/) returns the path."""
        praxis_dir = tmp_path / ".praxis"  # type: ignore[operator]
        praxis_dir.mkdir()
        root = str(tmp_path)

        result = _resolve_project_root(root)

        assert result == root

    def test_explicit_path_with_whitespace(self, tmp_path: object) -> None:
        """Leading/trailing whitespace is stripped before validation."""
        praxis_dir = tmp_path / ".praxis"  # type: ignore[operator]
        praxis_dir.mkdir()
        root = str(tmp_path)

        result = _resolve_project_root(f"  {root}  ")

        assert result == root

    def test_explicit_invalid_path_no_praxis(self, tmp_path: object) -> None:
        """Explicit path without .praxis/ returns None."""
        root = str(tmp_path)

        result = _resolve_project_root(root)

        assert result is None

    def test_explicit_nonexistent_path(self) -> None:
        """Explicit path that does not exist returns None."""
        result = _resolve_project_root("/nonexistent/path/to/project")

        assert result is None

    def test_explicit_relative_path(self) -> None:
        """Explicit relative path returns None (must be absolute)."""
        result = _resolve_project_root("relative/path")

        assert result is None


class TestCwdFallback:
    """Tests when project_root is empty (falls back to os.getcwd())."""

    def test_empty_string_with_valid_cwd(self, tmp_path: object) -> None:
        """Empty project_root with cwd pointing to a valid project returns cwd."""
        praxis_dir = tmp_path / ".praxis"  # type: ignore[operator]
        praxis_dir.mkdir()
        cwd = str(tmp_path)

        with patch("mcp_tools.os.getcwd", return_value=cwd):
            result = _resolve_project_root("")

        assert result == cwd

    def test_empty_string_with_non_project_cwd(self) -> None:
        """Empty project_root with cwd that has no .praxis/ returns None."""
        with patch("mcp_tools.os.getcwd", return_value="/tmp"):
            result = _resolve_project_root("")

        assert result is None

    def test_whitespace_only_falls_back_to_cwd(self, tmp_path: object) -> None:
        """Whitespace-only string is treated the same as empty."""
        praxis_dir = tmp_path / ".praxis"  # type: ignore[operator]
        praxis_dir.mkdir()
        cwd = str(tmp_path)

        with patch("mcp_tools.os.getcwd", return_value=cwd):
            result = _resolve_project_root("   ")

        assert result == cwd


class TestOriginalValidatorUnchanged:
    """Verify _validate_project_root still exists and works independently."""

    def test_validate_still_exported(self) -> None:
        from mcp_tools import _validate_project_root

        assert callable(_validate_project_root)

    def test_validate_returns_none_for_empty(self) -> None:
        from mcp_tools import _validate_project_root

        assert _validate_project_root("") is None


class TestFileUriToPath:
    """Tests for _file_uri_to_path helper."""

    def test_file_uri(self) -> None:
        assert _file_uri_to_path("file:///Users/alice/project") == "/Users/alice/project"

    def test_percent_encoded_uri(self) -> None:
        assert _file_uri_to_path("file:///Users/alice/my%20project") == "/Users/alice/my project"

    def test_non_file_scheme(self) -> None:
        assert _file_uri_to_path("https://example.com") is None

    def test_empty_path(self) -> None:
        assert _file_uri_to_path("file://") is None


def _make_ctx_with_roots(root_uris: list[str]) -> MagicMock:
    """Build a mock Context whose session.list_roots() returns the given URIs."""
    from mcp.types import Root

    roots = [Root(uri=uri) for uri in root_uris]
    result = MagicMock()
    result.roots = roots

    session = MagicMock()
    session.list_roots = AsyncMock(return_value=result)

    ctx = MagicMock()
    ctx.session = session
    return ctx


class TestGetClientProjectRoot:
    """Tests for _get_client_project_root (async, reads MCP client roots)."""

    def test_returns_first_valid_root(self, tmp_path) -> None:
        (tmp_path / ".praxis").mkdir()
        ctx = _make_ctx_with_roots([f"file://{tmp_path}"])

        result = asyncio.run(_get_client_project_root(ctx))

        assert result == str(tmp_path)

    def test_skips_non_project_roots(self, tmp_path) -> None:
        non_project = tmp_path / "no-praxis"
        non_project.mkdir()
        project = tmp_path / "has-praxis"
        project.mkdir()
        (project / ".praxis").mkdir()

        ctx = _make_ctx_with_roots([f"file://{non_project}", f"file://{project}"])

        result = asyncio.run(_get_client_project_root(ctx))

        assert result == str(project)

    def test_returns_none_when_no_valid_roots(self, tmp_path) -> None:
        ctx = _make_ctx_with_roots([f"file://{tmp_path}"])

        result = asyncio.run(_get_client_project_root(ctx))

        assert result is None

    def test_returns_none_on_exception(self) -> None:
        ctx = MagicMock()
        ctx.session.list_roots = AsyncMock(side_effect=Exception("not supported"))

        result = asyncio.run(_get_client_project_root(ctx))

        assert result is None

    def test_returns_none_when_empty_roots(self) -> None:
        ctx = _make_ctx_with_roots([])

        result = asyncio.run(_get_client_project_root(ctx))

        assert result is None


class TestResolveProjectRootWithCtx:
    """Tests for _resolve_project_root_with_ctx (full async resolution chain)."""

    def test_explicit_path_takes_priority(self, tmp_path) -> None:
        (tmp_path / ".praxis").mkdir()
        other = tmp_path / "other"
        other.mkdir()
        (other / ".praxis").mkdir()
        ctx = _make_ctx_with_roots([f"file://{other}"])

        result = asyncio.run(
            _resolve_project_root_with_ctx(str(tmp_path), ctx)
        )

        assert result == str(tmp_path)

    def test_client_roots_used_when_project_root_empty(self, tmp_path) -> None:
        (tmp_path / ".praxis").mkdir()
        ctx = _make_ctx_with_roots([f"file://{tmp_path}"])

        result = asyncio.run(
            _resolve_project_root_with_ctx("", ctx)
        )

        assert result == str(tmp_path)

    def test_cwd_fallback_when_no_roots(self, tmp_path) -> None:
        (tmp_path / ".praxis").mkdir()
        ctx = _make_ctx_with_roots([])

        with patch("mcp_tools.os.getcwd", return_value=str(tmp_path)):
            result = asyncio.run(
                _resolve_project_root_with_ctx("", ctx)
            )

        assert result == str(tmp_path)

    def test_cwd_fallback_when_roots_not_projects(self, tmp_path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".praxis").mkdir()
        non_project = tmp_path / "non-project"
        non_project.mkdir()

        ctx = _make_ctx_with_roots([f"file://{non_project}"])

        with patch("mcp_tools.os.getcwd", return_value=str(project)):
            result = asyncio.run(
                _resolve_project_root_with_ctx("", ctx)
            )

        assert result == str(project)
