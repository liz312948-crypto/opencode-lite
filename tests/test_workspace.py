from __future__ import annotations

from pathlib import Path

import pytest

from repopilot_lite.workspace import WorkspaceError, WorkspaceManager


def test_workspace_is_isolated_and_ignores_large_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("private", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "module.js").write_text("ignored", encoding="utf-8")

    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create_workspace("task-1", source)

    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (workspace / ".git").exists()
    assert not (workspace / "node_modules").exists()

    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_workspace_reset_restores_source_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create_workspace("task-2", source)
    (workspace / "app.py").write_text("VALUE = 99\n", encoding="utf-8")

    restored = manager.reset_workspace("task-2", source)

    assert restored == workspace
    assert manager.workspace_matches_source(restored, source)
    assert (restored / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_workspace_rejects_unsafe_task_id(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")

    with pytest.raises(WorkspaceError, match="not safe"):
        manager.workspace_path("../escape")
