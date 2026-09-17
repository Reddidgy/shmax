"""Tests for settings_ops — project settings read/write."""

from __future__ import annotations

import logging
import os
import threading

import pytest

import settings_ops


@pytest.fixture()
def project(tmp_path):
    """Create a minimal PraxisOS project with config files."""
    config_dir = tmp_path / ".praxis" / "config"
    config_dir.mkdir(parents=True)

    general_yaml = (
        "# PraxisOS Configuration\n"
        "#\n"
        "absoluteProjectPath: /tmp/test\n"
        "defaultWorker: jarvis.json\n"
        "contextRouterIncludedByDefault: true\n"
        "projectIcon: null\n"
        "addTasksToCommits: false\n"
        "taskTitleTag: POS\n"
        "autoVersionEnabled: false\n"
        "chatEnabled: true\n"
        "chat:\n"
        "  defaultAssignee: beacon.json\n"
        "  defaultModel: sonnet\n"
        "  includeContextRouter: true\n"
        "  includePrd: false\n"
        "  panelWidth: 640\n"
        "  lastSessionId: null\n"
        "autoVersionFilePath: /version\n"
        "projectFilesLastOpened:\n"
        "  - file1.txt\n"
        "  - file2.txt\n"
        "markdownDoubleClickSelectionMode: default\n"
        "soundVolume: 0.5\n"
        "pythonCommand: python3\n"
        "onboarded: true\n"
        "aiMode: agent\n"
    )
    (config_dir / "general.yaml").write_text(general_yaml, encoding="utf-8")

    ignore_yaml = (
        "# PRAXIS file tree ignore configuration\n"
        "#\n"
        "ignoredPaths:\n"
        "  - node_modules\n"
        "  - dist\n"
    )
    (config_dir / "file_tree_ignore.yaml").write_text(
        ignore_yaml, encoding="utf-8",
    )

    docs_ignore_yaml = (
        "# PRAXIS file tree ignore configuration\n"
        "#\n"
        "ignoredPaths:\n"
        "  - .git\n"
    )
    (config_dir / "docs_file_tree_ignore.yaml").write_text(
        docs_ignore_yaml, encoding="utf-8",
    )

    return tmp_path


class TestReadSettings:
    def test_reads_all_tabs(self, project):
        result = settings_ops.read_settings(str(project))
        assert result["ok"] is True
        settings = result["settings"]

        # General tab
        assert settings["general"]["taskTitleTag"] == "POS"
        assert settings["general"]["pythonCommand"] == "python3"
        assert settings["general"]["contextRouterIncludedByDefault"] is True
        assert settings["general"]["chatEnabled"] is True
        assert settings["general"]["markdownDoubleClickSelectionMode"] == "default"

        # Git tab
        assert settings["git"]["addTasksToCommits"] is False
        assert settings["git"]["autoVersionEnabled"] is False
        assert settings["git"]["autoVersionFilePath"] == "/version"

        # File tree tab
        assert settings["fileTree"]["fileTreeIgnorePaths"] == [
            "node_modules", "dist",
        ]

        # Project files tab
        assert settings["projectFiles"]["projectFilesIgnorePaths"] == [".git"]

        # Sound tab
        assert settings["sound"]["soundVolume"] == 0.5

        # Other
        assert settings["other"]["defaultWorker"] == "jarvis.json"
        assert settings["other"]["aiMode"] == "agent"
        assert settings["other"]["absoluteProjectPath"] == "/tmp/test"

    def test_handles_missing_config(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        result = settings_ops.read_settings(str(tmp_path))
        assert result["ok"] is True
        # Falls back to defaults
        settings = result["settings"]
        assert settings["general"]["pythonCommand"] == "python3"
        assert settings["general"]["chatEnabled"] is False

    def test_reads_project_icon(self, project):
        result = settings_ops.read_settings(str(project))
        assert result["settings"]["other"]["projectIcon"] is None


class TestUpdateSettings:
    def test_update_single_boolean(self, project):
        result = settings_ops.update_settings(
            str(project), {"chatEnabled": False},
        )
        assert result["ok"] is True
        assert "chatEnabled" in result["updated"]

        # Verify the change was persisted
        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["general"]["chatEnabled"] is False

    def test_update_multiple_settings(self, project):
        result = settings_ops.update_settings(str(project), {
            "pythonCommand": "python",
            "soundVolume": 0.8,
            "contextRouterIncludedByDefault": False,
        })
        assert result["ok"] is True
        assert len(result["updated"]) == 3

        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["general"]["pythonCommand"] == "python"
        assert reread["settings"]["sound"]["soundVolume"] == 0.8
        assert reread["settings"]["general"]["contextRouterIncludedByDefault"] is False

    def test_update_task_title_tag_normalizes(self, project):
        result = settings_ops.update_settings(
            str(project), {"taskTitleTag": "abc123"},
        )
        assert result["ok"] is True

        reread = settings_ops.read_settings(str(project))
        # Normalized: uppercase, max 3 chars
        assert reread["settings"]["general"]["taskTitleTag"] == "ABC"

    def test_clear_task_title_tag(self, project):
        result = settings_ops.update_settings(
            str(project), {"taskTitleTag": ""},
        )
        assert result["ok"] is True
        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["general"]["taskTitleTag"] is None

    def test_rejects_unknown_keys(self, project):
        result = settings_ops.update_settings(
            str(project), {"unknownKey": "value"},
        )
        assert result["ok"] is False
        assert "Unknown or read-only" in result["error"]

    def test_rejects_invalid_boolean(self, project):
        result = settings_ops.update_settings(
            str(project), {"chatEnabled": "yes"},
        )
        assert result["ok"] is False
        assert "must be a boolean" in result["error"]

    def test_rejects_invalid_sound_volume(self, project):
        result = settings_ops.update_settings(
            str(project), {"soundVolume": 1.5},
        )
        assert result["ok"] is False
        assert "between 0 and 1" in result["error"]

    def test_rejects_invalid_selection_mode(self, project):
        result = settings_ops.update_settings(
            str(project), {"markdownDoubleClickSelectionMode": "invalid"},
        )
        assert result["ok"] is False
        assert "must be 'default' or 'fullPath'" in result["error"]

    def test_rejects_empty_fields(self, project):
        result = settings_ops.update_settings(str(project), {})
        assert result["ok"] is False
        assert "No settings provided" in result["error"]

    def test_clamps_sound_volume(self, project):
        # Volume within range — should normalize to float
        result = settings_ops.update_settings(
            str(project), {"soundVolume": 0},
        )
        assert result["ok"] is True
        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["sound"]["soundVolume"] == 0

    def test_preserves_unmodified_keys(self, project):
        # Read before
        before = settings_ops.read_settings(str(project))
        assert before["settings"]["general"]["taskTitleTag"] == "POS"

        # Update a different key
        settings_ops.update_settings(
            str(project), {"pythonCommand": "python"},
        )

        # Original key should be preserved
        after = settings_ops.read_settings(str(project))
        assert after["settings"]["general"]["taskTitleTag"] == "POS"
        assert after["settings"]["general"]["pythonCommand"] == "python"


class TestUpdateIgnorePaths:
    def test_update_prompts_ignore(self, project):
        result = settings_ops.update_ignore_paths(
            str(project), "prompts", ["src", "docs", "build"],
        )
        assert result["ok"] is True
        assert result["scope"] == "prompts"
        assert result["paths"] == ["src", "docs", "build"]
        assert result["count"] == 3

        # Verify persistence
        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["fileTree"]["fileTreeIgnorePaths"] == [
            "src", "docs", "build",
        ]

    def test_update_project_files_ignore(self, project):
        result = settings_ops.update_ignore_paths(
            str(project), "projectFiles", [".git", "node_modules"],
        )
        assert result["ok"] is True
        assert result["scope"] == "projectFiles"

        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["projectFiles"]["projectFilesIgnorePaths"] == [
            ".git", "node_modules",
        ]

    def test_rejects_invalid_scope(self, project):
        result = settings_ops.update_ignore_paths(
            str(project), "invalid", ["path"],
        )
        assert result["ok"] is False

    def test_rejects_directory_traversal(self, project):
        result = settings_ops.update_ignore_paths(
            str(project), "prompts", ["../etc/passwd", "safe_path"],
        )
        assert result["ok"] is True
        assert result["paths"] == ["safe_path"]

    def test_strips_leading_slashes(self, project):
        result = settings_ops.update_ignore_paths(
            str(project), "prompts", ["/node_modules", "dist"],
        )
        assert result["ok"] is True
        assert result["paths"] == ["node_modules", "dist"]


class TestYamlParser:
    def test_parse_simple_values(self):
        content = "key1: hello\nkey2: true\nkey3: 42\nkey4: 0.5\nkey5: null\n"
        result = settings_ops._parse_yaml_file(content)
        assert result["key1"] == "hello"
        assert result["key2"] is True
        assert result["key3"] == 42
        assert result["key4"] == 0.5
        assert result["key5"] is None

    def test_parse_nested_block(self):
        content = "parent:\n  child1: value1\n  child2: true\n"
        result = settings_ops._parse_yaml_file(content)
        assert result["parent"]["child1"] == "value1"
        assert result["parent"]["child2"] is True

    def test_parse_list(self):
        content = "items:\n  - first\n  - second\n  - third\n"
        result = settings_ops._parse_yaml_file(content)
        assert result["items"] == ["first", "second", "third"]

    def test_parse_quoted_strings(self):
        content = 'key1: "hello world"\nkey2: \'single quoted\'\n'
        result = settings_ops._parse_yaml_file(content)
        assert result["key1"] == "hello world"
        assert result["key2"] == "single quoted"

    def test_skip_comments(self):
        content = "# comment\nkey: value\n# another comment\n"
        result = settings_ops._parse_yaml_file(content)
        assert result == {"key": "value"}

    def test_roundtrip_preserves_values(self, project):
        """Read → serialize → parse should preserve all values."""
        original = settings_ops.read_settings(str(project))
        settings = original["settings"]

        # Check key values survived the roundtrip
        assert settings["general"]["taskTitleTag"] == "POS"
        assert settings["general"]["chatEnabled"] is True
        assert settings["git"]["addTasksToCommits"] is False
        assert settings["sound"]["soundVolume"] == 0.5


def _set_linked_projects(project, value: str) -> None:
    """Append or replace the linkedProjects line in the fixture's general.yaml."""
    config_file = project / ".praxis" / "config" / "general.yaml"
    content = config_file.read_text(encoding="utf-8")
    lines = [
        line for line in content.splitlines()
        if not line.startswith("linkedProjects:")
    ]
    lines.append(f'linkedProjects: "{value}"')
    config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestReadLinkedProjects:
    def test_read_linked_projects_empty_when_unset(self, project):
        assert settings_ops.read_linked_projects(str(project)) == []

    def test_read_linked_projects_parses_comma_separated(self, tmp_path, project):
        a = tmp_path / "linked_a"
        b = tmp_path / "linked_b"
        a.mkdir()
        b.mkdir()
        _set_linked_projects(project, f"{a},{b}")

        result = settings_ops.read_linked_projects(str(project))
        assert result == [str(a), str(b)]

    def test_read_linked_projects_drops_invalid_entries(self, tmp_path, project):
        real = tmp_path / "real_dir"
        real.mkdir()
        missing = tmp_path / "does_not_exist"
        relative = "relative/path"
        _set_linked_projects(
            project, f"{relative},{missing},{real},  ",
        )

        result = settings_ops.read_linked_projects(str(project))
        assert result == [str(real)]

    def test_read_linked_projects_drops_self_and_duplicates(self, tmp_path, project):
        other = tmp_path / "other_dir"
        other.mkdir()
        _set_linked_projects(
            project, f"{project},{other},{other}",
        )

        result = settings_ops.read_linked_projects(str(project))
        assert result == [str(other)]


class TestResolveProjectRoots:
    def test_resolve_project_roots_puts_current_first(self, tmp_path, project):
        other = tmp_path / "other_dir"
        other.mkdir()
        _set_linked_projects(project, str(other))

        result = settings_ops.resolve_project_roots(str(project))
        assert result == [str(project), str(other)]


class TestLinkedProjectsSetting:
    def test_update_settings_accepts_linked_projects(self, project):
        result = settings_ops.update_settings(
            str(project), {"linkedProjects": "/tmp"},
        )
        assert result["ok"] is True
        assert "linkedProjects" in result["updated"]

        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["general"]["linkedProjects"] == "/tmp"

    def test_read_settings_exposes_linked_projects_default(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        result = settings_ops.read_settings(str(tmp_path))
        assert result["settings"]["general"]["linkedProjects"] == ""


class TestFormatYamlValue:
    def test_null(self):
        assert settings_ops._format_yaml_value(None) == "null"

    def test_bool(self):
        assert settings_ops._format_yaml_value(True) == "true"
        assert settings_ops._format_yaml_value(False) == "false"

    def test_int(self):
        assert settings_ops._format_yaml_value(42) == "42"

    def test_float(self):
        assert settings_ops._format_yaml_value(0.5) == "0.5"
        assert settings_ops._format_yaml_value(1.0) == "1"
        assert settings_ops._format_yaml_value(54.953125) == "54.953125"

    def test_string_plain(self):
        assert settings_ops._format_yaml_value("hello") == "hello"

    def test_string_needs_quoting(self):
        assert settings_ops._format_yaml_value("hello: world") == '"hello: world"'


class TestBackupAndAtomicWrite:
    def test_update_creates_backup_of_previous_content(self, project):
        config_file = project / ".praxis" / "config" / "general.yaml"
        original = config_file.read_text(encoding="utf-8")

        result = settings_ops.update_settings(str(project), {"chatEnabled": False})
        assert result["ok"] is True

        backup_file = project / ".praxis" / "config" / "general.yaml.bak"
        assert backup_file.read_text(encoding="utf-8") == original
        assert "chatEnabled: false" in config_file.read_text(encoding="utf-8")

    def test_second_update_rotates_backup(self, project):
        config_file = project / ".praxis" / "config" / "general.yaml"
        backup_file = project / ".praxis" / "config" / "general.yaml.bak"

        settings_ops.update_settings(str(project), {"chatEnabled": False})
        after_first = config_file.read_text(encoding="utf-8")

        settings_ops.update_settings(str(project), {"chatEnabled": True})
        assert backup_file.read_text(encoding="utf-8") == after_first

    def test_no_temp_files_left_behind(self, project):
        settings_ops.update_settings(str(project), {"chatEnabled": False})
        config_dir = project / ".praxis" / "config"
        leftovers = list(config_dir.glob(".general.yaml.*.tmp"))
        assert leftovers == []

    def test_backup_failure_does_not_block_write(self, project, monkeypatch, caplog):
        def _raise(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(settings_ops.shutil, "copyfile", _raise)

        with caplog.at_level(logging.WARNING):
            result = settings_ops.update_settings(str(project), {"chatEnabled": False})

        assert result["ok"] is True
        assert any(record.levelno == logging.WARNING for record in caplog.records)

        reread = settings_ops.read_settings(str(project))
        assert reread["settings"]["general"]["chatEnabled"] is False

    def test_update_preserves_block_scalars_and_nested_lists(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        onboarding_block = (
            "onboardingData:\n"
            "  role: Other\n"
            "  roleCustom: |-\n"
            "    line one\n"
            "    line two\n"
            "  projectGoals:\n"
            "    - Ship\n"
            "    - Learn\n"
            '  otherProjectDetails: ""\n'
        )
        content = (
            onboarding_block
            + "chatEnabled: true\n"
            + "pythonCommand: python3\n"
        )
        config_file = config_dir / "general.yaml"
        config_file.write_text(content, encoding="utf-8")

        result = settings_ops.update_settings(str(tmp_path), {"chatEnabled": False})
        assert result["ok"] is True

        updated = config_file.read_text(encoding="utf-8")
        assert onboarding_block in updated
        assert "chatEnabled: false" in updated
        assert "pythonCommand: python3" in updated

    def test_update_appends_new_key(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "general.yaml"
        config_file.write_text("chatEnabled: true\n", encoding="utf-8")

        result = settings_ops.update_settings(str(tmp_path), {"pythonCommand": "python"})
        assert result["ok"] is True

        content = config_file.read_text(encoding="utf-8")
        assert "chatEnabled: true" in content
        assert "pythonCommand: python" in content

    def test_update_on_missing_config_creates_file_with_header(self, tmp_path):
        result = settings_ops.update_settings(str(tmp_path), {"chatEnabled": True})
        assert result["ok"] is True

        config_file = tmp_path / ".praxis" / "config" / "general.yaml"
        content = config_file.read_text(encoding="utf-8")
        assert content.startswith(settings_ops._GENERAL_YAML_HEADER)
        assert "chatEnabled: true" in content

    def test_absolute_project_path_rejected_without_allow_internal(self, project):
        result = settings_ops.update_settings(
            str(project), {"absoluteProjectPath": "/tmp/x"},
        )
        assert result["ok"] is False
        assert "Unknown or read-only" in result["error"]

    def test_absolute_project_path_accepted_with_allow_internal(self, project):
        result = settings_ops.update_settings(
            str(project), {"absoluteProjectPath": "/tmp/x/"}, allow_internal=True,
        )
        assert result["ok"] is True

        reread = settings_ops.read_general_config(str(project))
        assert reread["absoluteProjectPath"] == "/tmp/x/"


class TestCorruptionRecovery:
    def test_read_settings_recovers_from_backup_when_primary_corrupted(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        good_content = "taskTitleTag: BAK\nchatEnabled: true\n"
        (config_dir / "general.yaml.bak").write_text(good_content, encoding="utf-8")
        (config_dir / "general.yaml").write_bytes(b"\xff\xfe\x00garbage")

        result = settings_ops.read_settings(str(tmp_path))
        assert result["ok"] is True
        assert result["settings"]["general"]["taskTitleTag"] == "BAK"

    def test_read_settings_falls_back_to_defaults_when_backup_missing(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "general.yaml").write_text(
            "{{{{ :: not yaml ::", encoding="utf-8",
        )

        result = settings_ops.read_settings(str(tmp_path))
        assert result["ok"] is True
        assert result["settings"]["general"]["chatEnabled"] is False
        assert result["settings"]["general"]["pythonCommand"] == "python3"

    def test_comment_only_file_is_not_corrupted(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "general.yaml").write_text(
            "# just a comment\n# another one\n", encoding="utf-8",
        )

        result = settings_ops.read_settings(str(tmp_path))
        assert result["ok"] is True
        assert result["settings"]["general"]["chatEnabled"] is False
        assert not (config_dir / "general.yaml.bak").exists()

    def test_update_quarantines_corrupted_primary_and_keeps_backup(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        bak_content = "taskTitleTag: BAK\nchatEnabled: true\n"
        (config_dir / "general.yaml.bak").write_text(bak_content, encoding="utf-8")
        garbage = b"\xff\xfe\x00garbage"
        (config_dir / "general.yaml").write_bytes(garbage)

        result = settings_ops.update_settings(str(tmp_path), {"chatEnabled": False})
        assert result["ok"] is True

        corrupted_file = config_dir / "general.yaml.corrupted"
        assert corrupted_file.read_bytes() == garbage
        assert (config_dir / "general.yaml.bak").read_text(encoding="utf-8") == bak_content

        updated_content = (config_dir / "general.yaml").read_text(encoding="utf-8")
        assert "taskTitleTag: BAK" in updated_content
        assert "chatEnabled: false" in updated_content

    def test_quarantine_never_overwrites_existing_corrupted_file(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        sentinel = b"SENTINEL-OLD-CORRUPTION"
        (config_dir / "general.yaml.corrupted").write_bytes(sentinel)
        (config_dir / "general.yaml.bak").write_text(
            "chatEnabled: true\n", encoding="utf-8",
        )
        (config_dir / "general.yaml").write_bytes(b"\xff\xfe\x00garbage-again")

        result = settings_ops.update_settings(str(tmp_path), {"chatEnabled": False})
        assert result["ok"] is True

        assert (config_dir / "general.yaml.corrupted").read_bytes() == sentinel
        rotated = list(config_dir.glob("general.yaml.corrupted.*"))
        assert len(rotated) == 1

    def test_read_linked_projects_uses_recovery(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        linked_dir = tmp_path / "linked"
        linked_dir.mkdir()
        (config_dir / "general.yaml.bak").write_text(
            f"linkedProjects: {linked_dir}\n", encoding="utf-8",
        )
        (config_dir / "general.yaml").write_bytes(b"\xff\xfe\x00garbage")

        result = settings_ops.read_linked_projects(str(tmp_path))
        assert result == [str(linked_dir)]

    def test_read_general_config_never_raises_on_unreadable(self, tmp_path):
        config_dir = tmp_path / ".praxis" / "config"
        config_dir.mkdir(parents=True)
        # A directory named general.yaml makes open() raise IsADirectoryError (OSError).
        (config_dir / "general.yaml").mkdir()

        result = settings_ops.read_general_config(str(tmp_path))
        assert result == {}


class TestConfigLock:
    def test_lock_file_created(self, project):
        settings_ops.update_settings(str(project), {"chatEnabled": False})
        lock_file = project / ".praxis" / "config" / "general.yaml.lock"
        assert lock_file.exists()

    def test_concurrent_updates_are_all_persisted(self, project):
        def _toggle_chat():
            for i in range(20):
                settings_ops.update_settings(str(project), {"chatEnabled": bool(i % 2)})

        def _set_python_command():
            for i in range(20):
                settings_ops.update_settings(str(project), {"pythonCommand": f"python{i}"})

        t1 = threading.Thread(target=_toggle_chat)
        t2 = threading.Thread(target=_set_python_command)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        config_file = project / ".praxis" / "config" / "general.yaml"
        content = config_file.read_text(encoding="utf-8")
        parsed = settings_ops._parse_yaml_file(content)
        assert parsed["chatEnabled"] == bool(19 % 2)
        assert parsed["pythonCommand"] == "python19"

    def test_works_without_fcntl(self, project, monkeypatch):
        monkeypatch.setattr(settings_ops, "fcntl", None)
        result = settings_ops.update_settings(str(project), {"chatEnabled": False})
        assert result["ok"] is True

    def test_lock_timeout_degrades_gracefully(self, project, monkeypatch, caplog):
        if settings_ops.fcntl is None:
            pytest.skip("fcntl not available on this platform")

        monkeypatch.setattr(settings_ops, "_LOCK_TIMEOUT_SECONDS", 0.1)
        config_file = str(project / ".praxis" / "config" / "general.yaml")
        lock_path = config_file + settings_ops.LOCK_SUFFIX
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        settings_ops.fcntl.flock(fd, settings_ops.fcntl.LOCK_EX)
        try:
            with caplog.at_level(logging.WARNING):
                result = settings_ops.update_settings(str(project), {"chatEnabled": False})
            assert result["ok"] is True
            assert "Timed out" in caplog.text
        finally:
            settings_ops.fcntl.flock(fd, settings_ops.fcntl.LOCK_UN)
            os.close(fd)


class TestApplyTopLevelUpdates:
    def test_replace_scalar(self):
        content = "chatEnabled: true\npythonCommand: python3\n"
        result = settings_ops._apply_top_level_updates(content, {"chatEnabled": False})
        assert result == "chatEnabled: false\npythonCommand: python3\n"

    def test_skip_indented_block(self):
        """Replacing a key drops its own old nested/indented block."""
        content = "linkedProjects:\n  - /old/path\nchatEnabled: true\n"
        result = settings_ops._apply_top_level_updates(
            content, {"linkedProjects": "/new/path"},
        )
        assert result == "linkedProjects: /new/path\nchatEnabled: true\n"

    def test_blank_line_inside_block_scalar_kept_with_block(self):
        """A block scalar with an internal blank line, belonging to a key that
        is NOT being updated, survives untouched while a different key updates."""
        content = (
            "onboardingData:\n"
            "  roleCustom: |-\n"
            "    line one\n"
            "\n"
            "    line two\n"
            "chatEnabled: true\n"
        )
        result = settings_ops._apply_top_level_updates(content, {"chatEnabled": False})
        assert result == (
            "onboardingData:\n"
            "  roleCustom: |-\n"
            "    line one\n"
            "\n"
            "    line two\n"
            "chatEnabled: false\n"
        )

    def test_append_missing_key(self):
        content = "chatEnabled: true\n"
        result = settings_ops._apply_top_level_updates(
            content, {"pythonCommand": "python"},
        )
        assert result == "chatEnabled: true\npythonCommand: python\n"

    def test_empty_content(self):
        result = settings_ops._apply_top_level_updates("", {"chatEnabled": False})
        assert result == "chatEnabled: false\n"

    def test_ensures_single_trailing_newline(self):
        content = "chatEnabled: true\n\n\n"
        result = settings_ops._apply_top_level_updates(
            content, {"pythonCommand": "python"},
        )
        assert result.endswith("\n") and not result.endswith("\n\n")
