from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from repopilot_lite.patching import PatchApplier, PatchValidationError
from repopilot_lite.workspace import WorkspaceError, WorkspaceManager


def _create_symlink_or_skip(target: Path, link: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"Creating symlinks is not available in this environment: {exc}")


def test_workspace_rejects_source_file_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside.txt"
    source.mkdir()
    outside.write_text("outside", encoding="utf-8")
    _create_symlink_or_skip(outside, source / "linked.txt", directory=False)
    manager = WorkspaceManager(tmp_path / "workspaces")

    with pytest.raises(WorkspaceError, match="Links and reparse"):
        manager.create_workspace("task-file-link", source)

    assert outside.read_text(encoding="utf-8") == "outside"


def test_workspace_rejects_source_directory_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "sentinel.txt").write_text("outside", encoding="utf-8")
    _create_symlink_or_skip(outside, source / "linked", directory=True)
    manager = WorkspaceManager(tmp_path / "workspaces")

    with pytest.raises(WorkspaceError, match="Links and reparse"):
        manager.create_workspace("task-directory-link", source)

    assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_workspace_rejects_windows_junction(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    junction = source / "junction"
    source.mkdir()
    outside.mkdir()
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        timeout=10,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"Creating a junction is not available: {completed.stderr.strip()}")
    try:
        manager = WorkspaceManager(tmp_path / "workspaces")
        with pytest.raises(WorkspaceError, match="Links and reparse"):
            manager.create_workspace("task-junction", source)
    finally:
        os.rmdir(junction)


def test_patch_rejects_target_resolving_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.py"
    workspace.mkdir()
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    _create_symlink_or_skip(outside, workspace / "app.py", directory=False)
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""

    with pytest.raises(PatchValidationError, match="link or reparse"):
        PatchApplier().validate(workspace, patch)

    assert outside.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_workspace_copy_preserves_source_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    manager = WorkspaceManager(tmp_path / "workspaces")
    before = manager.manifest(source)
    before_hash = manager.manifest_hash(before)

    workspace = manager.create_workspace("task-manifest", source)

    assert manager.manifest(source) == before
    assert manager.manifest_hash(manager.manifest(source)) == before_hash
    assert manager.manifest(workspace) == before
    assert before["empty"]["type"] == "directory"


def test_cleanup_removes_link_without_touching_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create_workspace("task-cleanup", source)
    _create_symlink_or_skip(outside, workspace / "late-link", directory=True)

    manager.cleanup_workspace(workspace, task_id="task-cleanup")

    assert not workspace.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
