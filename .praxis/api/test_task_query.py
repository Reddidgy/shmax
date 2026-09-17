"""Tests for task_query — read-only task operations."""

from __future__ import annotations

import json
import os

import pytest

from task_query import get_task, list_tasks


@pytest.fixture()
def project(tmp_path):
    """Create a minimal .praxis/tasks/ tree with fixture tasks."""
    praxis = tmp_path / ".praxis"
    tasks = praxis / "tasks"
    for status in ("new", "in_progress", "completed", "on_hold", "archived"):
        (tasks / status).mkdir(parents=True)

    def _write(status: str, task_id: int, extra: dict | None = None):
        data = {
            "id": task_id,
            "status": status,
            "title": f"Task {task_id}",
            "priority": "medium",
            "labels": ["test"],
            "assignee": "bot",
            **(extra or {}),
        }
        path = tasks / status / f"{task_id}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    _write("new", 1)
    _write("new", 2, {"priority": "high", "labels": ["urgent"]})
    _write("in_progress", 3)
    _write("completed", 4, {"assignee": None})

    return tmp_path


class TestListTasks:
    def test_list_all(self, project):
        result = list_tasks(str(project))
        assert len(result) == 4
        ids = {t["id"] for t in result}
        assert ids == {1, 2, 3, 4}

    def test_filter_by_status(self, project):
        result = list_tasks(str(project), status="new")
        assert len(result) == 2
        assert all(t["status"] == "new" for t in result)

    def test_filter_empty_status(self, project):
        result = list_tasks(str(project), status="on_hold")
        assert result == []

    def test_summary_keys(self, project):
        result = list_tasks(str(project))
        expected_keys = {"id", "title", "status", "priority", "labels", "assignee"}
        for task in result:
            assert set(task.keys()) == expected_keys

    def test_empty_project(self, tmp_path):
        praxis = tmp_path / ".praxis" / "tasks"
        praxis.mkdir(parents=True)
        result = list_tasks(str(tmp_path))
        assert result == []

    def test_no_praxis_dir(self, tmp_path):
        result = list_tasks(str(tmp_path))
        assert result == []

    def test_malformed_json_skipped(self, project):
        bad = project / ".praxis" / "tasks" / "new" / "99.json"
        bad.write_text("not json{{{", encoding="utf-8")
        result = list_tasks(str(project), status="new")
        assert len(result) == 2
        assert 99 not in {t["id"] for t in result}

    def test_invalid_status_raises(self, project):
        with pytest.raises(ValueError, match="unknown status"):
            list_tasks(str(project), status="nonexistent")

    def test_missing_status_dir(self, tmp_path):
        praxis = tmp_path / ".praxis" / "tasks" / "new"
        praxis.mkdir(parents=True)
        result = list_tasks(str(tmp_path), status="in_progress")
        assert result == []


class TestGetTask:
    def test_get_existing(self, project):
        task = get_task(str(project), 1)
        assert task is not None
        assert task["id"] == 1
        assert task["title"] == "Task 1"

    def test_get_from_other_status(self, project):
        task = get_task(str(project), 3)
        assert task is not None
        assert task["status"] == "in_progress"

    def test_get_nonexistent(self, project):
        assert get_task(str(project), 999) is None

    def test_get_full_fields(self, project):
        task = get_task(str(project), 2)
        assert task is not None
        assert task["priority"] == "high"
        assert task["labels"] == ["urgent"]

    def test_get_no_praxis(self, tmp_path):
        assert get_task(str(tmp_path), 1) is None

    def test_get_malformed_file(self, project):
        bad = project / ".praxis" / "tasks" / "on_hold" / "50.json"
        bad.write_text("{broken", encoding="utf-8")
        assert get_task(str(project), 50) is None


class TestStatusDesyncListTasks:
    """Regression: folder location is authoritative over JSON status field."""

    def test_folder_overrides_json_status(self, project):
        """A task in completed/ with JSON status='to_review' must show completed."""
        # Write a task in completed/ with wrong JSON status
        task_path = project / ".praxis" / "tasks" / "completed" / "10.json"
        task_path.write_text(
            json.dumps({
                "id": 10,
                "status": "to_review",  # Wrong — file is in completed/
                "title": "Task 10",
                "priority": "medium",
                "labels": [],
                "assignee": None,
            }),
            encoding="utf-8",
        )
        result = list_tasks(str(project), status="completed")
        ids = {t["id"] for t in result}
        assert 10 in ids
        task_10 = next(t for t in result if t["id"] == 10)
        assert task_10["status"] == "completed"

    def test_all_statuses_derived_from_folder(self, project):
        """Every task returned by list_tasks must have status matching its folder."""
        # Add desynced tasks in multiple folders
        for folder, wrong_status in [
            ("in_progress", "new"),
            ("completed", "to_review"),
            ("on_hold", "in_progress"),
        ]:
            folder_path = project / ".praxis" / "tasks" / folder
            folder_path.mkdir(parents=True, exist_ok=True)
            task_id = hash(folder) % 1000 + 100
            (folder_path / f"{task_id}.json").write_text(
                json.dumps({
                    "id": task_id,
                    "status": wrong_status,
                    "title": f"Desynced {folder}",
                    "priority": "low",
                    "labels": [],
                    "assignee": None,
                }),
                encoding="utf-8",
            )
        result = list_tasks(str(project))
        for t in result:
            # Find which folder this task is in by re-scanning
            tasks_dir = project / ".praxis" / "tasks"
            for status_dir in ("new", "in_progress", "to_review", "completed", "on_hold", "archived"):
                if (tasks_dir / status_dir / f"{t['id']}.json").exists():
                    assert t["status"] == status_dir, (
                        f"Task {t['id']} in {status_dir}/ has status={t['status']}"
                    )
                    break


class TestStatusDesyncGetTask:
    """Regression: get_task derives status from folder, not JSON field."""

    def test_get_task_overrides_json_status(self, project):
        task_path = project / ".praxis" / "tasks" / "completed" / "20.json"
        task_path.write_text(
            json.dumps({
                "id": 20,
                "status": "to_review",
                "title": "Task 20",
                "priority": "medium",
                "labels": [],
                "assignee": None,
            }),
            encoding="utf-8",
        )
        task = get_task(str(project), 20)
        assert task is not None
        assert task["status"] == "completed"


class TestReconcileTaskStatuses:
    """Test bulk repair of status field desync."""

    def test_reconcile_fixes_desynced_files(self, project):
        from task_query import reconcile_task_statuses

        # Create desynced tasks
        completed_dir = project / ".praxis" / "tasks" / "completed"
        for i in range(30, 33):
            (completed_dir / f"{i}.json").write_text(
                json.dumps({
                    "id": i,
                    "status": "to_review",
                    "title": f"Task {i}",
                }),
                encoding="utf-8",
            )

        result = reconcile_task_statuses(str(project))
        assert len(result["fixed"]) == 3
        assert len(result["errors"]) == 0

        # Verify files are actually fixed on disk
        for i in range(30, 33):
            with open(completed_dir / f"{i}.json", encoding="utf-8") as fh:
                data = json.load(fh)
            assert data["status"] == "completed"

    def test_reconcile_noop_when_consistent(self, project):
        from task_query import reconcile_task_statuses

        result = reconcile_task_statuses(str(project))
        assert result["fixed"] == []
        assert result["errors"] == []

    def test_reconcile_reports_unreadable_files(self, project):
        from task_query import reconcile_task_statuses

        bad = project / ".praxis" / "tasks" / "new" / "bad.json"
        bad.write_text("not json{{{", encoding="utf-8")

        result = reconcile_task_statuses(str(project))
        assert len(result["errors"]) == 1
        assert result["errors"][0]["file"] == "bad.json"
