"""Unit tests for :mod:`task_ops` — the pure task-write logic.

Run from the ``api/`` directory:

    python3 -m pytest test_task_ops.py -v

These cover the contract independent of Flask: required-field validation,
server-set fields, caller-supplied ``id``/``status`` being ignored, the
atomic write landing a schema-correct file in ``new/``, the ``fcntl`` lock
keeping concurrent creates collision-free, and the ``max(counter, fs_max)+1``
rule surviving a stale counter.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

import task_ops


@pytest.fixture
def project(tmp_path):
    """A throwaway PraxisOS project tree: ``.praxis/config`` + status dirs."""
    root = tmp_path / "proj"
    (root / ".praxis" / "config").mkdir(parents=True)
    for status in ("new", "in_progress", "completed", "on_hold", "archived"):
        (root / ".praxis" / "tasks" / status).mkdir(parents=True)
    return root


@pytest.fixture
def tagged_project(project):
    """A project with ``taskTitleTag: TSK`` in ``general.yaml``."""
    (project / ".praxis" / "config" / "general.yaml").write_text(
        "taskTitleTag: TSK\n"
    )
    return project


def _valid_payload(**overrides) -> dict:
    payload = {
        "title": "Add a thing",
        "why_we_need_this": "Because the board needs it.",
        "acceptance_criteria": "The thing appears in New.",
    }
    payload.update(overrides)
    return payload


def _read_task(project_root, task_id: int) -> dict:
    path = project_root / ".praxis" / "tasks" / "new" / f"{task_id}.json"
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# happy path + schema
# ---------------------------------------------------------------------------
def test_create_task_writes_schema_correct_file(project):
    (project / ".praxis" / "config" / "task_counter").write_text("42")

    result = task_ops.create_task(str(project), _valid_payload())

    assert result["id"] == 43
    assert result["path"] == ".praxis/tasks/new/43.json"

    written = project / ".praxis" / "tasks" / "new" / "43.json"
    assert written.exists()

    task = _read_task(project, 43)
    assert task["id"] == 43
    assert task["status"] == "new"
    assert task["title"] == "[43] Add a thing"
    assert task["why_we_need_this"] == "Because the board needs it."
    assert task["acceptance_criteria"] == "The thing appears in New."
    # server-set defaults
    assert task["important_constraints"] == ""
    assert task["labels"] == []
    assert task["assignee"] is None
    assert task["on_hold_reason"] == ""
    assert task["contextRouterIncluded"] is True
    # epoch milliseconds (13-digit range), both timestamps set together
    assert isinstance(task["created_at"], int) and task["created_at"] > 1_000_000_000_000
    assert task["column_entered_at"] == task["created_at"]
    # priority omitted when not provided
    assert "priority" not in task


def test_optional_fields_are_carried(project):
    payload = _valid_payload(
        important_constraints="No new deps.",
        labels=["api", "poc"],
        priority="high",
        assignee="someone.json",
    )
    result = task_ops.create_task(str(project), payload)
    task = _read_task(project, result["id"])
    assert task["important_constraints"] == "No new deps."
    assert task["labels"] == ["api", "poc"]
    assert task["priority"] == "high"
    assert task["assignee"] == "someone.json"


def test_blank_priority_is_omitted(project):
    result = task_ops.create_task(str(project), _valid_payload(priority="   "))
    assert "priority" not in _read_task(project, result["id"])


def test_written_json_style_matches_repo(project):
    result = task_ops.create_task(str(project), _valid_payload())
    raw = (project / ".praxis" / "tasks" / "new" / f"{result['id']}.json").read_text()
    # 2-space indent + trailing newline (matches existing task files).
    assert raw.endswith("}\n")
    assert '\n  "id":' in raw


# ---------------------------------------------------------------------------
# caller-supplied server fields are ignored
# ---------------------------------------------------------------------------
def test_caller_supplied_id_and_status_are_ignored(project):
    (project / ".praxis" / "config" / "task_counter").write_text("100")
    payload = _valid_payload(
        id=5,
        status="completed",
        created_at=1,
        column_entered_at=2,
    )
    result = task_ops.create_task(str(project), payload)
    assert result["id"] == 101  # not the caller's 5

    task = _read_task(project, 101)
    assert task["id"] == 101
    assert task["status"] == "new"  # not "completed"
    assert task["created_at"] != 1
    assert task["column_entered_at"] != 2
    # no file was written at the caller's requested id
    assert not (project / ".praxis" / "tasks" / "new" / "5.json").exists()


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "field", ["title", "why_we_need_this", "acceptance_criteria"]
)
def test_missing_required_field_raises_with_name(project, field):
    payload = _valid_payload()
    del payload[field]
    with pytest.raises(task_ops.ValidationError) as exc_info:
        task_ops.create_task(str(project), payload)
    assert exc_info.value.field == field


@pytest.mark.parametrize(
    "field", ["title", "why_we_need_this", "acceptance_criteria"]
)
def test_blank_required_field_raises_with_name(project, field):
    payload = _valid_payload(**{field: "   "})
    with pytest.raises(task_ops.ValidationError) as exc_info:
        task_ops.create_task(str(project), payload)
    assert exc_info.value.field == field


def test_non_string_required_field_raises(project):
    payload = _valid_payload(title=123)
    with pytest.raises(task_ops.ValidationError) as exc_info:
        task_ops.create_task(str(project), payload)
    assert exc_info.value.field == "title"


def test_validation_failure_writes_no_file(project):
    payload = _valid_payload()
    del payload["title"]
    with pytest.raises(task_ops.ValidationError):
        task_ops.create_task(str(project), payload)
    new_dir = project / ".praxis" / "tasks" / "new"
    assert list(new_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# id allocation: stale counter + filesystem max
# ---------------------------------------------------------------------------
def test_stale_counter_does_not_collide_with_existing_file(project):
    # Counter says 5, but a task #900 already lives on disk.
    (project / ".praxis" / "config" / "task_counter").write_text("5")
    (project / ".praxis" / "tasks" / "in_progress" / "900.json").write_text("{}")

    result = task_ops.create_task(str(project), _valid_payload())

    assert result["id"] == 901  # fs_max(900) + 1, not counter(5) + 1
    assert (project / ".praxis" / "config" / "task_counter").read_text() == "901"


def test_missing_counter_recovers_from_filesystem(project):
    (project / ".praxis" / "tasks" / "completed" / "7.json").write_text("{}")
    result = task_ops.create_task(str(project), _valid_payload())
    assert result["id"] == 8


def test_first_task_when_empty_starts_at_one(project):
    result = task_ops.create_task(str(project), _valid_payload())
    assert result["id"] == 1


def test_non_integer_filenames_are_ignored(project):
    (project / ".praxis" / "tasks" / "new" / "not-a-number.json").write_text("{}")
    (project / ".praxis" / "tasks" / "new" / "12.json").write_text("{}")
    result = task_ops.create_task(str(project), _valid_payload())
    assert result["id"] == 13


def test_creates_missing_directories(tmp_path):
    # Only the project root exists; create_task must materialize .praxis/...
    root = tmp_path / "bare"
    root.mkdir()
    result = task_ops.create_task(str(root), _valid_payload())
    assert result["id"] == 1
    assert (root / ".praxis" / "tasks" / "new" / "1.json").exists()


# ---------------------------------------------------------------------------
# concurrency: distinct ids, no clobber
# ---------------------------------------------------------------------------
def test_concurrent_creates_yield_distinct_ids_no_clobber(project):
    n = 25

    def _create(_i: int) -> int:
        return task_ops.create_task(str(project), _valid_payload())["id"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(_create, range(n)))

    # All ids distinct.
    assert len(set(ids)) == n
    # Every create produced exactly one file; none clobbered another.
    files = list((project / ".praxis" / "tasks" / "new").glob("*.json"))
    assert len(files) == n
    file_ids = sorted(int(p.stem) for p in files)
    assert file_ids == sorted(ids)
    # A contiguous block (no gaps) because allocation is serialized.
    assert file_ids == list(range(min(file_ids), min(file_ids) + n))


def test_concurrent_creates_with_seeded_fs_max(project):
    """Concurrency starting from an on-disk max still never collides."""
    (project / ".praxis" / "tasks" / "archived" / "500.json").write_text("{}")
    n = 10

    def _create(_i: int) -> int:
        return task_ops.create_task(str(project), _valid_payload())["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(_create, range(n)))

    assert len(set(ids)) == n
    assert min(ids) >= 501
    new_files = list((project / ".praxis" / "tasks" / "new").glob("*.json"))
    assert len(new_files) == n


# ---------------------------------------------------------------------------
# title formatting — mirrors task-formatters.ts / task-formatters.test.ts
# ---------------------------------------------------------------------------
class TestFormatTitle:
    """Verify the API formats titles identically to the frontend."""

    def test_no_tag_plain_title(self, project):
        result = task_ops.create_task(str(project), _valid_payload(title="my task"))
        task = _read_task(project, result["id"])
        assert task["title"] == f"[{result['id']}] My task"

    def test_with_tag_from_general_yaml(self, tagged_project):
        result = task_ops.create_task(
            str(tagged_project), _valid_payload(title="task title")
        )
        task = _read_task(tagged_project, result["id"])
        assert task["title"] == f"[TSK-{result['id']}] Task title"

    def test_empty_title_gets_default(self, tagged_project):
        """An empty title becomes ``[TAG-id] Task {id}``."""
        result = task_ops.create_task(
            str(tagged_project), _valid_payload(title="   non-empty")
        )
        # Need a second call with truly empty-ish content — but the required
        # field validator rejects blank titles, so this path is only reachable
        # through _format_title directly. We test the internal helper instead.
        tid = result["id"]
        assert task_ops._format_title(tid, "", "TSK") == f"[TSK-{tid}] Task {tid}"
        assert task_ops._format_title(tid, "   ", "TSK") == f"[TSK-{tid}] Task {tid}"

    def test_strips_existing_prefix_prevents_duplication(self, tagged_project):
        result = task_ops.create_task(
            str(tagged_project), _valid_payload(title="[ABC-42] old title")
        )
        task = _read_task(tagged_project, result["id"])
        assert task["title"] == f"[TSK-{result['id']}] Old title"

    def test_strips_numeric_dot_prefix(self, tagged_project):
        result = task_ops.create_task(
            str(tagged_project), _valid_payload(title="123. some title")
        )
        task = _read_task(tagged_project, result["id"])
        assert task["title"] == f"[TSK-{result['id']}] Some title"

    def test_capitalizes_first_letter(self, project):
        result = task_ops.create_task(
            str(project), _valid_payload(title="lowercase start")
        )
        task = _read_task(project, result["id"])
        assert task["title"] == f"[{result['id']}] Lowercase start"

    def test_preserves_all_characters_in_title(self, project):
        result = task_ops.create_task(
            str(project), _valid_payload(title='file: "name" <ok>')
        )
        task = _read_task(project, result["id"])
        assert task["title"] == f'[{result["id"]}] File: "name" <ok>'

    def test_missing_general_yaml_falls_back_to_no_tag(self, project):
        """No general.yaml → titles use ``[id]`` prefix, no crash."""
        result = task_ops.create_task(
            str(project), _valid_payload(title="works anyway")
        )
        task = _read_task(project, result["id"])
        assert task["title"] == f"[{result['id']}] Works anyway"

    def test_null_tag_in_general_yaml(self, project):
        """``taskTitleTag: null`` → same as no tag."""
        (project / ".praxis" / "config" / "general.yaml").write_text(
            "taskTitleTag: null\n"
        )
        result = task_ops.create_task(
            str(project), _valid_payload(title="null tag")
        )
        task = _read_task(project, result["id"])
        assert task["title"] == f"[{result['id']}] Null tag"

    def test_tag_normalization(self, project):
        """Tags are uppercased, non-alphanumeric stripped, capped at 3 chars."""
        (project / ".praxis" / "config" / "general.yaml").write_text(
            "taskTitleTag: pos\n"
        )
        result = task_ops.create_task(
            str(project), _valid_payload(title="test")
        )
        task = _read_task(project, result["id"])
        assert task["title"] == f"[POS-{result['id']}] Test"


class TestNormalizeTaskTitleTag:
    """Unit tests for _normalize_task_title_tag."""

    def test_none(self):
        assert task_ops._normalize_task_title_tag(None) is None

    def test_empty(self):
        assert task_ops._normalize_task_title_tag("") is None

    def test_whitespace(self):
        assert task_ops._normalize_task_title_tag("   ") is None

    def test_uppercases(self):
        assert task_ops._normalize_task_title_tag("pos") == "POS"

    def test_strips_non_alphanumeric(self):
        assert task_ops._normalize_task_title_tag("a-b") == "AB"

    def test_truncates_to_three(self):
        assert task_ops._normalize_task_title_tag("ABCDE") == "ABC"


class TestReadTaskTitleTag:
    """Unit tests for _read_task_title_tag."""

    def test_reads_tag_from_yaml(self, project):
        (project / ".praxis" / "config" / "general.yaml").write_text(
            "absoluteProjectPath: /x\ntaskTitleTag: POS\n"
        )
        assert task_ops._read_task_title_tag(str(project)) == "POS"

    def test_quoted_tag(self, project):
        (project / ".praxis" / "config" / "general.yaml").write_text(
            "taskTitleTag: 'ABC'\n"
        )
        assert task_ops._read_task_title_tag(str(project)) == "ABC"

    def test_double_quoted_tag(self, project):
        (project / ".praxis" / "config" / "general.yaml").write_text(
            'taskTitleTag: "XYZ"\n'
        )
        assert task_ops._read_task_title_tag(str(project)) == "XYZ"

    def test_null_value(self, project):
        (project / ".praxis" / "config" / "general.yaml").write_text(
            "taskTitleTag: null\n"
        )
        assert task_ops._read_task_title_tag(str(project)) is None

    def test_missing_key(self, project):
        (project / ".praxis" / "config" / "general.yaml").write_text(
            "otherKey: value\n"
        )
        assert task_ops._read_task_title_tag(str(project)) is None

    def test_missing_file(self, project):
        assert task_ops._read_task_title_tag(str(project)) is None


# ---------------------------------------------------------------------------
# update_task — partial field updates
# ---------------------------------------------------------------------------

def _write_task_file(project, status: str, task_id: int, **extra) -> None:
    """Write a task JSON file into the given status directory."""
    data = {
        "id": task_id,
        "status": status,
        "title": f"[{task_id}] Task {task_id}",
        "why_we_need_this": "Because.",
        "acceptance_criteria": "Done when done.",
        "important_constraints": "",
        "labels": [],
        "assignee": None,
        "on_hold_reason": "",
        "created_at": 1700000000000,
        "column_entered_at": 1700000000000,
        **extra,
    }
    path = project / ".praxis" / "tasks" / status / f"{task_id}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class TestUpdateTask:
    """Tests for :func:`task_ops.update_task`."""

    def test_update_single_field(self, project):
        _write_task_file(project, "new", 10, priority="low")
        result = task_ops.update_task(str(project), 10, {"priority": "high"})
        assert result == {"ok": True, "task_id": 10, "updated_fields": ["priority"]}
        # Verify on disk
        data = json.loads(
            (project / ".praxis" / "tasks" / "new" / "10.json").read_text()
        )
        assert data["priority"] == "high"
        # Other fields unchanged
        assert data["title"] == "[10] Task 10"
        assert data["labels"] == []

    def test_update_multiple_fields(self, project):
        _write_task_file(project, "in_progress", 20)
        result = task_ops.update_task(str(project), 20, {
            "labels": ["bug", "urgent"],
            "on_hold_reason": "Waiting on upstream",
        })
        assert result["ok"] is True
        assert sorted(result["updated_fields"]) == ["labels", "on_hold_reason"]
        data = json.loads(
            (project / ".praxis" / "tasks" / "in_progress" / "20.json").read_text()
        )
        assert data["labels"] == ["bug", "urgent"]
        assert data["on_hold_reason"] == "Waiting on upstream"

    def test_unsupported_fields_silently_ignored(self, project):
        _write_task_file(project, "new", 30)
        result = task_ops.update_task(str(project), 30, {
            "id": 999,
            "status": "completed",
            "created_at": 1,
            "column_entered_at": 2,
            "some_random_key": "value",
        })
        assert result == {"ok": True, "task_id": 30, "updated_fields": []}
        data = json.loads(
            (project / ".praxis" / "tasks" / "new" / "30.json").read_text()
        )
        assert data["id"] == 30
        assert data["status"] == "new"
        assert data["created_at"] == 1700000000000

    def test_invalid_priority_raises(self, project):
        _write_task_file(project, "new", 40)
        with pytest.raises(task_ops.ValidationError) as exc_info:
            task_ops.update_task(str(project), 40, {"priority": "ultra"})
        assert exc_info.value.field == "priority"

    def test_valid_priorities_accepted(self, project):
        _write_task_file(project, "new", 41)
        for p in ("low", "medium", "high", "critical"):
            task_ops.update_task(str(project), 41, {"priority": p})
        data = json.loads(
            (project / ".praxis" / "tasks" / "new" / "41.json").read_text()
        )
        assert data["priority"] == "critical"  # last one written

    def test_invalid_assignee_raises(self, project):
        _write_task_file(project, "new", 50)
        with pytest.raises(task_ops.ValidationError) as exc_info:
            task_ops.update_task(str(project), 50, {"assignee": "nonexistent.json"})
        assert exc_info.value.field == "assignee"

    def test_valid_assignee_accepted(self, project):
        assignees_dir = project / ".praxis" / "assignees"
        assignees_dir.mkdir(parents=True, exist_ok=True)
        (assignees_dir / "alice.json").write_text(
            json.dumps({"name": "Alice", "role": "Dev"}), encoding="utf-8"
        )
        _write_task_file(project, "new", 51)
        result = task_ops.update_task(str(project), 51, {"assignee": "alice.json"})
        assert "assignee" in result["updated_fields"]
        data = json.loads(
            (project / ".praxis" / "tasks" / "new" / "51.json").read_text()
        )
        assert data["assignee"] == "alice.json"

    def test_clear_assignee(self, project):
        _write_task_file(project, "new", 52, assignee="old.json")
        result = task_ops.update_task(str(project), 52, {"assignee": ""})
        assert "assignee" in result["updated_fields"]
        data = json.loads(
            (project / ".praxis" / "tasks" / "new" / "52.json").read_text()
        )
        assert data["assignee"] is None

    def test_invalid_labels_type_raises(self, project):
        _write_task_file(project, "new", 60)
        with pytest.raises(task_ops.ValidationError) as exc_info:
            task_ops.update_task(str(project), 60, {"labels": "not-a-list"})
        assert exc_info.value.field == "labels"

    def test_labels_must_be_strings(self, project):
        _write_task_file(project, "new", 61)
        with pytest.raises(task_ops.ValidationError) as exc_info:
            task_ops.update_task(str(project), 61, {"labels": [1, 2]})
        assert exc_info.value.field == "labels"

    def test_task_not_found_raises(self, project):
        with pytest.raises(task_ops.TaskNotFoundError):
            task_ops.update_task(str(project), 9999, {"title": "nope"})

    def test_no_valid_fields_returns_empty(self, project):
        _write_task_file(project, "new", 70)
        result = task_ops.update_task(str(project), 70, {"nonexistent": "value"})
        assert result == {"ok": True, "task_id": 70, "updated_fields": []}

    def test_file_not_moved_on_update(self, project):
        _write_task_file(project, "on_hold", 80, on_hold_reason="stuck")
        task_ops.update_task(str(project), 80, {"on_hold_reason": "new reason"})
        assert (project / ".praxis" / "tasks" / "on_hold" / "80.json").exists()
        assert not (project / ".praxis" / "tasks" / "new" / "80.json").exists()

    def test_atomic_write_consistency(self, project):
        _write_task_file(project, "new", 90, labels=["old"])
        task_ops.update_task(str(project), 90, {"labels": ["new1", "new2"]})
        raw = (project / ".praxis" / "tasks" / "new" / "90.json").read_text()
        # Must be valid JSON
        data = json.loads(raw)
        assert data["labels"] == ["new1", "new2"]
        # Follows repo JSON style
        assert raw.endswith("}\n")
        assert '\n  "id":' in raw

    def test_update_title_formats_with_id_prefix(self, project):
        _write_task_file(project, "new", 100)
        task_ops.update_task(str(project), 100, {"title": "Custom Title Without Prefix"})
        data = json.loads(
            (project / ".praxis" / "tasks" / "new" / "100.json").read_text()
        )
        assert data["title"] == "[100] Custom Title Without Prefix"

    def test_update_title_formats_with_tag(self, tagged_project):
        _write_task_file(tagged_project, "new", 101)
        task_ops.update_task(str(tagged_project), 101, {"title": "new title here"})
        data = json.loads(
            (tagged_project / ".praxis" / "tasks" / "new" / "101.json").read_text()
        )
        assert data["title"] == "[TSK-101] New title here"

    def test_update_title_strips_existing_prefix(self, tagged_project):
        _write_task_file(tagged_project, "new", 102)
        task_ops.update_task(str(tagged_project), 102, {"title": "[OLD-99] some task"})
        data = json.loads(
            (tagged_project / ".praxis" / "tasks" / "new" / "102.json").read_text()
        )
        assert data["title"] == "[TSK-102] Some task"

    def test_string_field_type_check(self, project):
        _write_task_file(project, "new", 110)
        with pytest.raises(task_ops.ValidationError) as exc_info:
            task_ops.update_task(str(project), 110, {"title": 123})
        assert exc_info.value.field == "title"

    def test_find_task_across_status_dirs(self, project):
        """update_task finds tasks in any status directory."""
        for status in ("new", "in_progress", "completed", "on_hold", "archived"):
            tid = 200 + hash(status) % 100
            _write_task_file(project, status, tid)
            result = task_ops.update_task(str(project), tid, {"on_hold_reason": "test"})
            assert result["ok"] is True
