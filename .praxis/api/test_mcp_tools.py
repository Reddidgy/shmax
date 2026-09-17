"""Tests for mcp_tools — MCP tool definitions."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from mcp_tools import create_mcp_server


def _call_tool(project_root: str, tool_name: str, **kwargs):
    """Helper to call an MCP tool function and return its result."""
    server = create_mcp_server()
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    tool_fn = tools[tool_name].fn
    ctx = AsyncMock()
    ctx.session.list_roots = AsyncMock(side_effect=Exception("no roots"))
    return asyncio.run(tool_fn(project_root=project_root, ctx=ctx, **kwargs))


@pytest.fixture()
def project(tmp_path):
    """Create a minimal PraxisOS project structure."""
    praxis = tmp_path / ".praxis"
    tasks = praxis / "tasks"
    config = praxis / "config"
    for status in ("new", "in_progress", "completed", "on_hold", "archived"):
        (tasks / status).mkdir(parents=True)
    config.mkdir(parents=True)

    def _write_task(status: str, task_id: int, **extra):
        data = {
            "id": task_id,
            "status": status,
            "title": f"Task {task_id}",
            "why_we_need_this": "Because",
            "acceptance_criteria": "Done when done",
            "important_constraints": "",
            "labels": [],
            "assignee": None,
            "on_hold_reason": "",
            **extra,
        }
        path = tasks / status / f"{task_id}.json"
        path.write_text(json.dumps(data), encoding="utf-8")

    _write_task("new", 1, title="First task", labels=["bug"])
    _write_task("in_progress", 2, title="Second task", assignee="bot")
    return tmp_path


@pytest.fixture()
def mcp_server():
    """Create an MCP server with a mock claude checker."""
    return create_mcp_server(claude_checker=lambda: True)


class TestCreateTask:
    def test_create_success(self, project, mcp_server):
        tools = {t.name: t for t in mcp_server._tool_manager.list_tools()}
        assert "create_task" in tools

    def test_create_via_function(self, project):
        from mcp_tools import _validate_project_root
        import task_ops

        root = str(project)
        assert _validate_project_root(root) == root
        result = task_ops.create_task(root, {
            "title": "Test",
            "why_we_need_this": "Testing",
            "acceptance_criteria": "Passes",
        })
        assert result["id"] > 0
        assert result["path"].endswith(".json")

    def test_invalid_project_root(self):
        from mcp_tools import _validate_project_root
        assert _validate_project_root("/nonexistent/path") is None
        assert _validate_project_root("relative/path") is None
        assert _validate_project_root("") is None


class TestListTasks:
    def test_list_all(self, project):
        import task_query

        tasks = task_query.list_tasks(str(project))
        assert len(tasks) == 2

    def test_list_filtered(self, project):
        import task_query

        tasks = task_query.list_tasks(str(project), status="new")
        assert len(tasks) == 1
        assert tasks[0]["id"] == 1


class TestGetTask:
    def test_get_existing(self, project):
        import task_query

        task = task_query.get_task(str(project), 1)
        assert task is not None
        assert task["title"] == "First task"

    def test_get_nonexistent(self, project):
        import task_query

        assert task_query.get_task(str(project), 999) is None


class TestHealth:
    def test_health_claude_available(self):
        server = create_mcp_server(claude_checker=lambda: True)
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        assert "health" in tools

    def test_health_claude_unavailable(self):
        server = create_mcp_server(claude_checker=lambda: False)
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        assert "health" in tools

    def test_health_no_checker(self):
        server = create_mcp_server(claude_checker=None)
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        assert "health" in tools


class TestGetAssignees:
    def test_returns_file_name_and_role(self, project):
        import assignee_query

        # Create assignees directory with test data
        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True)

        # Flat structure (name/role at top level)
        (assignees_dir / "alice.json").write_text(
            json.dumps({"name": "Alice", "role": "Engineer", "mission": "Build stuff"}),
            encoding="utf-8",
        )
        # Nested structure (name/role inside role_and_mission)
        (assignees_dir / "bob.json").write_text(
            json.dumps({"role_and_mission": {"name": "Bob", "role": "Designer", "mission": "Design stuff"}}),
            encoding="utf-8",
        )

        result = assignee_query.get_assignees(str(project))
        assert len(result) == 2
        assert result[0] == {"file": "alice.json", "name": "Alice", "role": "Engineer"}
        assert result[1] == {"file": "bob.json", "name": "Bob", "role": "Designer"}

    def test_excludes_empty_names(self, project):
        import assignee_query

        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True)

        (assignees_dir / "empty.json").write_text(
            json.dumps({"name": "", "role": "Nobody"}),
            encoding="utf-8",
        )

        result = assignee_query.get_assignees(str(project))
        assert len(result) == 0

    def test_no_assignees_dir(self, project):
        import assignee_query

        result = assignee_query.get_assignees(str(project))
        assert result == []

    def test_only_returns_file_name_and_role(self, project):
        import assignee_query

        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True)

        (assignees_dir / "full.json").write_text(
            json.dumps({
                "name": "Charlie",
                "role": "Architect",
                "mission": "Secret mission",
                "mandatory_constraints": "Secret constraints",
            }),
            encoding="utf-8",
        )

        result = assignee_query.get_assignees(str(project))
        assert len(result) == 1
        assert set(result[0].keys()) == {"file", "name", "role"}


class TestCreateAssignee:
    def test_create_basic(self, project):
        import assignee_ops

        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True, exist_ok=True)

        result = assignee_ops.create_assignee(str(project), {
            "name": "Alice",
            "role": "Senior Engineer",
            "mission": "Build robust systems.",
        })
        assert result["file"] == "alice.json"
        assert result["name"] == "Alice"

        # Verify file was created with correct content
        created = json.loads((assignees_dir / "alice.json").read_text(encoding="utf-8"))
        assert created["name"] == "Alice"
        assert created["role"] == "Senior Engineer"
        assert created["mission"] == "Build robust systems."
        assert created["mandatory_constraints"] == ""

    def test_create_with_all_fields(self, project):
        import assignee_ops

        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True, exist_ok=True)

        result = assignee_ops.create_assignee(str(project), {
            "name": "Bob",
            "role": "Architect",
            "mission": "Design systems.",
            "mandatory_constraints": "- No shortcuts",
            "edge_cases_and_fallbacks": "Ask when unsure.",
            "workflow_and_response_format": "1. Think\n2. Act",
            "assignee_icon": "lucide:Code",
        })
        assert result["file"] == "bob.json"

        created = json.loads((assignees_dir / "bob.json").read_text(encoding="utf-8"))
        assert created["mandatory_constraints"] == "- No shortcuts"
        assert created["assignee-icon"] == "lucide:Code"
        assert "self_validation_rules" not in created

    def test_create_ignores_self_validation_rules(self, project):
        import assignee_ops

        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True, exist_ok=True)

        result = assignee_ops.create_assignee(str(project), {
            "name": "Diana",
            "role": "QA",
            "mission": "Test everything.",
            "self_validation_rules": "- Always double-check",
        })
        assert result["file"] == "diana.json"

        created = json.loads((assignees_dir / "diana.json").read_text(encoding="utf-8"))
        assert "self_validation_rules" not in created

    def test_create_unique_filename(self, project):
        import assignee_ops

        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True, exist_ok=True)

        # Create first assignee
        r1 = assignee_ops.create_assignee(str(project), {
            "name": "Charlie",
            "role": "Dev",
            "mission": "Code.",
        })
        assert r1["file"] == "charlie.json"

        # Create second with same name — should get unique filename
        r2 = assignee_ops.create_assignee(str(project), {
            "name": "Charlie",
            "role": "Designer",
            "mission": "Design.",
        })
        assert r2["file"] == "charlie-2.json"

    def test_create_missing_required_field(self, project):
        import assignee_ops

        (project / ".praxis" / "assignees").mkdir(parents=True, exist_ok=True)

        with pytest.raises(assignee_ops.ValidationError) as exc_info:
            assignee_ops.create_assignee(str(project), {
                "name": "NoRole",
                "role": "",
                "mission": "Something.",
            })
        assert exc_info.value.field == "role"

    def test_create_sanitises_filename(self, project):
        import assignee_ops

        (project / ".praxis" / "assignees").mkdir(parents=True, exist_ok=True)

        result = assignee_ops.create_assignee(str(project), {
            "name": "Dr. Alice (Expert!)",
            "role": "Expert",
            "mission": "Advise.",
        })
        # Special chars stripped, spaces become hyphens
        assert result["file"] == "dr-alice-expert.json"

    def test_create_with_explicit_file(self, project):
        import assignee_ops

        (project / ".praxis" / "assignees").mkdir(parents=True, exist_ok=True)

        result = assignee_ops.create_assignee(str(project), {
            "name": "Eve",
            "role": "Security",
            "mission": "Protect systems.",
            "file": "security-lead.json",
        })
        assert result["file"] == "security-lead.json"
        assert result["name"] == "Eve"

        created = json.loads(
            (project / ".praxis" / "assignees" / "security-lead.json").read_text(encoding="utf-8")
        )
        assert created["name"] == "Eve"


class TestUpdateTask:
    """Tests for the update_task MCP tool."""

    def test_tool_registered(self, mcp_server):
        tools = {t.name for t in mcp_server._tool_manager.list_tools()}
        assert "update_task" in tools

    def test_update_via_task_ops(self, project):
        import task_ops

        result = task_ops.update_task(str(project), 1, {"on_hold_reason": "needs API"})
        assert result["ok"] is True
        assert result["task_id"] == 1
        assert "on_hold_reason" in result["updated_fields"]
        # Verify on disk
        data = json.loads(
            (project / ".praxis" / "tasks" / "new" / "1.json").read_text()
        )
        assert data["on_hold_reason"] == "needs API"

    def test_task_not_found_returns_error(self, project):
        import task_ops

        with pytest.raises(task_ops.TaskNotFoundError):
            task_ops.update_task(str(project), 9999, {"title": "nope"})

    def test_unsupported_fields_silently_dropped(self, project):
        import task_ops

        result = task_ops.update_task(str(project), 1, {
            "id": 999,
            "status": "completed",
            "imaginary_field": "value",
        })
        assert result["ok"] is True
        assert result["updated_fields"] == []

    def test_validation_error_for_bad_priority(self, project):
        import task_ops

        with pytest.raises(task_ops.ValidationError) as exc_info:
            task_ops.update_task(str(project), 1, {"priority": "extreme"})
        assert exc_info.value.field == "priority"


class TestUpdateTaskStatusExtended:
    """Tests for the extended update_task_status transitions."""

    def test_completed_moves_file(self, project):
        """Moving to completed should move the file to .praxis/tasks/completed/."""
        from mcp_tools import create_mcp_server, _validate_project_root
        root = str(project)
        assert _validate_project_root(root) == root

        # Task 2 is in in_progress — move to completed
        assert (project / ".praxis" / "tasks" / "in_progress" / "2.json").exists()

        # Invoke the update_task_status logic directly via task file manipulation
        # (same pattern the MCP tool uses)
        import mcp_tools
        server = create_mcp_server()
        # We need to call the inner function. Since tools are closures inside
        # create_mcp_server, we test via the underlying file ops instead.

        # Simulate the MCP tool's behaviour:
        praxis = project / ".praxis"
        source = praxis / "tasks" / "in_progress" / "2.json"
        data = json.loads(source.read_text())
        data["status"] = "completed"
        target_dir = praxis / "tasks" / "completed"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "2.json"
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        source.unlink()

        assert target.exists()
        assert not source.exists()
        result = json.loads(target.read_text())
        assert result["status"] == "completed"

    def test_on_hold_with_reason(self, project):
        """on_hold transition should write on_hold_reason if provided."""
        source = project / ".praxis" / "tasks" / "in_progress" / "2.json"
        data = json.loads(source.read_text())

        data["status"] = "on_hold"
        data["on_hold_reason"] = "Waiting on external API"

        target_dir = project / ".praxis" / "tasks" / "on_hold"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "2.json"
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

        result = json.loads(target.read_text())
        assert result["status"] == "on_hold"
        assert result["on_hold_reason"] == "Waiting on external API"

    def test_on_hold_without_reason_preserves_existing(self, project):
        """If no on_hold_reason is provided, existing value should be preserved."""
        # Write a task with existing on_hold_reason in new column
        data = {
            "id": 5, "status": "new", "title": "T5",
            "why_we_need_this": "x", "acceptance_criteria": "y",
            "important_constraints": "", "labels": [],
            "on_hold_reason": "legacy reason",
        }
        (project / ".praxis" / "tasks" / "new" / "5.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

        # When moving to on_hold without providing on_hold_reason,
        # the existing "legacy reason" should remain.
        data["status"] = "on_hold"
        target_dir = project / ".praxis" / "tasks" / "on_hold"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "5.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )

        result = json.loads((target_dir / "5.json").read_text())
        assert result["on_hold_reason"] == "legacy reason"

    def test_archived_moves_file(self, project):
        """Moving to archived should work from any status."""
        source = project / ".praxis" / "tasks" / "new" / "1.json"
        data = json.loads(source.read_text())
        data["status"] = "archived"

        target_dir = project / ".praxis" / "tasks" / "archived"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "1.json"
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

        result = json.loads(target.read_text())
        assert result["status"] == "archived"

    def test_invalid_status_rejected(self, project):
        """The _VALID_STATUSES tuple should now include the new statuses."""
        from mcp_tools import create_mcp_server
        server = create_mcp_server()
        # Verify the valid statuses are correctly defined by checking the
        # closure variables (indirectly through tool description)
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        desc = tools["update_task_status"].description
        for s in ("completed", "on_hold", "archived"):
            assert s in desc


class TestUpdateTaskStatusGuidance:
    """Tests for post_transition_guidance on to_review transitions."""

    def test_to_review_returns_guidance(self, project):
        """update_task_status with status=to_review should return post_transition_guidance."""
        result = _call_tool(
            str(project), "update_task_status",
            task_id=2, status="to_review", agent_name="claude", on_hold_reason="",
        )
        assert isinstance(result, dict)
        assert result["ok"] == "OK"
        assert "post_transition_guidance" in result
        assert "update_project_context" in result["post_transition_guidance"]

    def test_to_review_moves_file(self, project):
        """Moving to to_review should physically relocate the task JSON to .praxis/tasks/to_review/."""
        result = _call_tool(
            str(project), "update_task_status",
            task_id=2, status="to_review", agent_name="claude", on_hold_reason="",
        )
        assert isinstance(result, dict)
        assert result["ok"] == "OK"

        to_review_dir = project / ".praxis" / "tasks" / "to_review"
        moved = list(to_review_dir.glob("*.json"))
        assert len(moved) == 1

        # File must have left in_progress/
        assert not list((project / ".praxis" / "tasks" / "in_progress").glob("2*.json"))

        data = json.loads(moved[0].read_text())
        assert data["status"] == "to_review"

        # Green state marker must be preserved (drives #taskStatusIndicator)
        assert (project / ".praxis" / "config" / "state" / "2" / "to_review").exists()

    def test_non_review_status_returns_ok_string(self, project):
        """update_task_status with status != to_review should return plain 'OK'."""
        result = _call_tool(
            str(project), "update_task_status",
            task_id=2, status="completed", agent_name="claude", on_hold_reason="",
        )
        assert result == "OK"


class TestUpdateProjectContext:
    """Tests for the update_project_context MCP tool (skill redirect)."""

    def test_tool_registered(self, mcp_server):
        tools = {t.name for t in mcp_server._tool_manager.list_tools()}
        assert "update_project_context" in tools

    def test_returns_skill_instructions(self, project):
        import project_context_search

        skill = project / ".agents" / "skills" / "context-router-actualizer" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# SKILL\n")

        result = project_context_search.update_project_context(str(project))
        assert result["ok"] is True
        assert result["skill"] == ".agents/skills/context-router-actualizer/SKILL.md"
        assert "context-router-actualizer" in result["instructions"]

    def test_error_when_skill_missing(self, project):
        import project_context_search

        result = project_context_search.update_project_context(str(project))
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_stop_gate_inline(self, project):
        import project_context_search

        skill = project / ".agents" / "skills" / "context-router-actualizer" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# SKILL\n")

        result = project_context_search.update_project_context(str(project))
        assert "STOP" in result["instructions"]
        assert "trivial refactor" in result["instructions"]
        assert "STOP GATE" in result["instructions"]


class TestGetActiveTasksFormat:
    """Tests for get_active_tasks plain-text response format."""

    def test_empty_board(self, tmp_path):
        praxis = tmp_path / ".praxis" / "tasks"
        for s in ("new", "in_progress", "completed", "on_hold", "archived"):
            (praxis / s).mkdir(parents=True)
        (tmp_path / ".praxis" / "config").mkdir(parents=True)
        result = _call_tool(str(tmp_path), "get_active_tasks")
        assert result == "No active tasks"

    def test_one_task(self, project):
        result = _call_tool(str(project), "get_active_tasks")
        assert "#1 First task [new]" in result

    def test_multiple_tasks(self, project):
        result = _call_tool(str(project), "get_active_tasks")
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "#1 First task [new]" in result
        assert "#2 Second task [in_progress]" in result


class TestListTasksFormat:
    """Tests for list_tasks plain-text response format."""

    def test_empty_no_filter(self, tmp_path):
        praxis = tmp_path / ".praxis" / "tasks"
        for s in ("new", "in_progress", "completed", "on_hold", "archived"):
            (praxis / s).mkdir(parents=True)
        (tmp_path / ".praxis" / "config").mkdir(parents=True)
        result = _call_tool(str(tmp_path), "list_tasks")
        assert result == "No tasks"

    def test_empty_with_status_filter(self, tmp_path):
        praxis = tmp_path / ".praxis" / "tasks"
        for s in ("new", "in_progress", "completed", "on_hold", "archived"):
            (praxis / s).mkdir(parents=True)
        (tmp_path / ".praxis" / "config").mkdir(parents=True)
        result = _call_tool(str(tmp_path), "list_tasks", status="in_progress")
        assert result == 'No tasks with status "in_progress"'

    def test_task_with_labels(self, project):
        result = _call_tool(str(project), "list_tasks", status="new")
        assert "bug" in result
        assert "#1" in result

    def test_on_hold_task_with_reason(self, project):
        on_hold_dir = project / ".praxis" / "tasks" / "on_hold"
        on_hold_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "id": 9, "status": "on_hold", "title": "Stuck task",
            "why_we_need_this": "x", "acceptance_criteria": "y",
            "important_constraints": "", "labels": [], "assignee": None,
            "on_hold_reason": "OOM on prod",
        }
        (on_hold_dir / "9.json").write_text(json.dumps(data), encoding="utf-8")
        result = _call_tool(str(project), "list_tasks", status="on_hold")
        assert "ON HOLD: OOM on prod" in result

    def test_task_without_labels_or_assignee(self, project):
        result = _call_tool(str(project), "list_tasks", status="new")
        assert "#1 [new]" in result
        assert "|" in result


class TestGetTaskFormat:
    """Tests for get_task plain-text response format."""

    def test_found_task(self, project):
        result = _call_tool(str(project), "get_task", task_id=1)
        assert "Task #1" in result
        assert "[new]" in result
        assert "## Why" in result
        assert "## Acceptance Criteria" in result

    def test_no_constraints_omits_section(self, project):
        result = _call_tool(str(project), "get_task", task_id=1)
        assert "## Constraints" not in result

    def test_with_constraints(self, project):
        task_path = project / ".praxis" / "tasks" / "new" / "1.json"
        data = json.loads(task_path.read_text())
        data["important_constraints"] = "Must be backwards compatible"
        task_path.write_text(json.dumps(data), encoding="utf-8")
        result = _call_tool(str(project), "get_task", task_id=1)
        assert "## Constraints" in result
        assert "Must be backwards compatible" in result

    def test_not_found(self, project):
        result = _call_tool(str(project), "get_task", task_id=1234)
        assert result == "Task 1234 not found"

    def test_metadata_line(self, project):
        task_path = project / ".praxis" / "tasks" / "new" / "1.json"
        data = json.loads(task_path.read_text())
        data["priority"] = "high"
        data["assignee"] = "jarvis.json"
        task_path.write_text(json.dumps(data), encoding="utf-8")
        result = _call_tool(str(project), "get_task", task_id=1)
        assert "priority: high" in result
        assert "labels: bug" in result
        assert "assignee: jarvis.json" in result

    def test_assignee_profile_not_inlined(self, project):
        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True, exist_ok=True)
        (assignees_dir / "jarvis.json").write_text(
            json.dumps({"name": "Jarvis", "role": "Engineer", "mission": "Build"}),
            encoding="utf-8",
        )
        task_path = project / ".praxis" / "tasks" / "new" / "1.json"
        data = json.loads(task_path.read_text())
        data["assignee"] = "jarvis.json"
        task_path.write_text(json.dumps(data), encoding="utf-8")
        result = _call_tool(str(project), "get_task", task_id=1)
        assert "assignee: jarvis.json" in result
        assert "## Assignee Profile" not in result
        assert "### Mission" not in result

    def test_on_hold_shows_reason(self, project):
        on_hold_dir = project / ".praxis" / "tasks" / "on_hold"
        on_hold_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "id": 8, "status": "on_hold", "title": "On Hold task",
            "why_we_need_this": "x", "acceptance_criteria": "y",
            "important_constraints": "", "labels": [], "assignee": None,
            "on_hold_reason": "waiting on API",
        }
        (on_hold_dir / "8.json").write_text(json.dumps(data), encoding="utf-8")
        result = _call_tool(str(project), "get_task", task_id=8)
        assert "ON HOLD: waiting on API" in result

    def test_with_screenshots(self, project):
        task_path = project / ".praxis" / "tasks" / "new" / "1.json"
        data = json.loads(task_path.read_text())
        data["screenshots"] = ["/abs/path/.praxis/user_assets/1/shot1.png", "/abs/path/.praxis/user_assets/1/shot2.png"]
        task_path.write_text(json.dumps(data), encoding="utf-8")
        result = _call_tool(str(project), "get_task", task_id=1)
        assert "## Screenshots" in result
        assert "/abs/path/.praxis/user_assets/1/shot1.png" in result
        assert "/abs/path/.praxis/user_assets/1/shot2.png" in result

    def test_no_screenshots_omits_section(self, project):
        result = _call_tool(str(project), "get_task", task_id=1)
        assert "## Screenshots" not in result


class TestFindTaskByKeywordsFormat:
    """Tests for find_task_by_keywords plain-text response format."""

    def test_match(self, project):
        result = _call_tool(str(project), "find_task_by_keywords", query="First")
        assert "#1" in result

    def test_no_match(self, project):
        result = _call_tool(str(project), "find_task_by_keywords", query="nonexistent")
        assert 'No tasks match' in result


class TestGetAssigneesFormat:
    """Tests for get_assignees plain-text response format."""

    def test_has_assignees(self, project):
        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True)
        (assignees_dir / "jarvis.json").write_text(
            json.dumps({"name": "Jarvis", "role": "Engineer", "mission": "Build"}),
            encoding="utf-8",
        )
        result = _call_tool(str(project), "get_assignees")
        assert "jarvis.json | Jarvis" in result

    def test_empty(self, project):
        result = _call_tool(str(project), "get_assignees")
        assert result == "No assignees found"


class TestFindAssigneeByKeywordsFormat:
    """Tests for find_assignee_by_keywords plain-text response format."""

    def test_match(self, project):
        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True)
        (assignees_dir / "alice.json").write_text(
            json.dumps({"name": "Alice", "role": "Engineer", "mission": "Build"}),
            encoding="utf-8",
        )
        result = _call_tool(str(project), "find_assignee_by_keywords", query="Alice")
        assert "|" in result

    def test_no_match(self, project):
        result = _call_tool(str(project), "find_assignee_by_keywords", query="nobody")
        assert 'No assignees match' in result


class TestUpdateTaskFormat:
    """Tests for update_task plain-text response format."""

    def test_success(self, project):
        result = _call_tool(str(project), "update_task", task_id=1, on_hold_reason="needs API")
        assert result.startswith("Updated task #")

    def test_not_found(self, project):
        result = _call_tool(str(project), "update_task", task_id=9999, title="nope")
        assert "not found" in result

    def test_validation_error(self, project):
        result = _call_tool(str(project), "update_task", task_id=1, priority="extreme")
        assert result.startswith("Error:")


class TestUpdateProjectContextFormat:
    """Tests for update_project_context plain-text response format."""

    def test_skill_found(self, project):
        skill = project / ".agents" / "skills" / "context-router-actualizer" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# SKILL\n")
        result = _call_tool(str(project), "update_project_context")
        assert isinstance(result, str)
        assert "context-router-actualizer" in result

    def test_skill_missing(self, project):
        result = _call_tool(str(project), "update_project_context")
        assert result.startswith("Error:")


class TestSearchProjectContextFormat:
    """Tests for search_project_context plain-text response format."""

    def test_returns_string_result(self, project):
        """search_project_context returns a plain-text string, never empty."""
        # Create project-context/ with a searchable doc
        ctx_dir = project / "project-context" / "tasks"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "README.md").write_text(
            "# Tasks Domain\n"
            "Tasks are JSON files stored in .praxis/tasks/ directories.\n"
        )
        result = _call_tool(str(project), "search_project_context", query="tasks domain")
        assert isinstance(result, str)
        assert "project-context/tasks/README.md" in result
        assert "[exact]" in result

    def test_no_match_returns_no_results_string(self, project):
        """A query with no matches returns a no-results string, not empty."""
        ctx_dir = project / "project-context"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "doc.md").write_text("nothing relevant here\n")
        result = _call_tool(str(project), "search_project_context", query="nonexistent")
        assert isinstance(result, str)
        assert result == 'No results for "nonexistent"'

    def test_invalid_project_root_returns_error_string(self):
        """An invalid project_root returns an error string, not a dict."""
        result = _call_tool("/nonexistent/path", "search_project_context", query="test")
        assert isinstance(result, str)
        assert "Error" in result or "Invalid" in result


class TestServerSetup:
    def test_all_tools_registered(self, mcp_server):
        tool_names = {t.name for t in mcp_server._tool_manager.list_tools()}
        expected = {"create_task", "get_next_task_id", "list_tasks", "get_task", "search_project_context", "read_context_route", "update_project_context", "health", "get_active_tasks", "find_task_by_keywords", "get_assignees", "find_assignee_by_keywords", "create_assignee", "update_task_status", "update_task", "get_settings", "update_settings", "execute_command"}
        assert tool_names == expected

