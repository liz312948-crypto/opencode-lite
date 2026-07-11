from __future__ import annotations

import os
import re
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path

from repopilot_lite.filesystem_safety import (
    FileSystemIdentity,
    FileSystemSafetyError,
    WorkspaceManifest,
    absolute_lexical,
    build_manifest,
    is_lexically_within,
    is_link_or_reparse,
    manifest_hash,
    read_verified_bytes,
    reject_existing_link_components,
    same_file,
    validate_directory,
    validate_regular_file,
    walk_tree_no_follow,
)

IGNORED_WORKSPACE_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)
SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class WorkspaceError(ValueError):
    """Raised when a workspace request violates an isolation boundary."""

    error_code = "WORKSPACE_BOUNDARY_VIOLATION"


class WorkspaceManager:
    """Creates task-specific copies so editing never targets the source repository."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        default_root = Path(tempfile.gettempdir()) / "opencode-lite-workspaces"
        try:
            lexical_base = absolute_lexical(base_dir or default_root)
            reject_existing_link_components(lexical_base.parent)
            lexical_base.mkdir(parents=True, exist_ok=True)
            self.base_dir = validate_directory(lexical_base)
        except FileSystemSafetyError as exc:
            raise WorkspaceError(str(exc)) from exc
        self._base_identity = FileSystemIdentity.from_stat(self.base_dir.stat())

    def create_workspace(
        self,
        task_id: str,
        source_repo_path: str | Path,
        *,
        replace: bool = False,
    ) -> Path:
        self._assert_base_unchanged()
        source = self.validate_source(source_repo_path)
        destination = self.workspace_path(task_id)

        if self._is_within(self.base_dir, source) or self._is_within(source, self.base_dir):
            raise WorkspaceError(
                "Workspace root and source repository must not contain each other."
            )

        source_manifest = self.manifest(source)
        if os.path.lexists(destination):
            if not replace:
                existing = self.validate_workspace(destination, task_id=task_id)
                if self.manifest(existing) != source_manifest:
                    raise WorkspaceError("Existing workspace does not match the source baseline.")
                return existing
            self.cleanup_workspace(destination, task_id=task_id)

        try:
            self._copy_tree(source, destination)
            workspace = self.validate_workspace(destination, task_id=task_id)
            if self.manifest(source) != source_manifest:
                raise WorkspaceError("Source repository changed while the workspace was copied.")
            if self.manifest(workspace) != source_manifest:
                raise WorkspaceError("Workspace copy does not match the source repository.")
            return workspace
        except Exception:
            if os.path.lexists(destination):
                self._remove_tree_no_follow(destination)
            raise

    def reset_workspace(self, task_id: str, source_repo_path: str | Path) -> Path:
        return self.create_workspace(task_id, source_repo_path, replace=True)

    def cleanup_workspace(
        self,
        workspace_path: str | Path,
        *,
        task_id: str | None = None,
    ) -> None:
        self._assert_base_unchanged()
        workspace = self.validate_workspace(workspace_path, task_id=task_id)
        self._remove_tree_no_follow(workspace)

    def workspace_path(self, task_id: str) -> Path:
        self._assert_base_unchanged()
        if (
            not SAFE_TASK_ID.fullmatch(task_id)
            or task_id.endswith((".", " "))
            or Path(task_id).name != task_id
        ):
            raise WorkspaceError("Task ID is not safe for a workspace path.")
        workspace = self.base_dir / task_id
        if workspace.parent != self.base_dir:
            raise WorkspaceError("Workspace path escaped the configured root.")
        if os.path.lexists(workspace):
            if is_link_or_reparse(workspace):
                raise WorkspaceError("Workspace path cannot be a link or reparse point.")
            try:
                resolved = validate_directory(workspace)
            except FileSystemSafetyError as exc:
                raise WorkspaceError(str(exc)) from exc
            if not same_file(resolved.parent, self.base_dir):
                raise WorkspaceError("Workspace path escaped the configured root.")
            return resolved
        return workspace

    def validate_workspace(
        self,
        workspace_path: str | Path,
        *,
        task_id: str | None = None,
    ) -> Path:
        self._assert_base_unchanged()
        try:
            lexical = absolute_lexical(workspace_path)
        except FileSystemSafetyError as exc:
            raise WorkspaceError(str(exc)) from exc
        if lexical == self.base_dir or not os.path.lexists(lexical):
            raise WorkspaceError("Workspace path must be an existing task directory.")
        if is_link_or_reparse(lexical):
            raise WorkspaceError("Workspace path cannot be a link or reparse point.")
        try:
            workspace = validate_directory(lexical)
        except FileSystemSafetyError as exc:
            raise WorkspaceError(str(exc)) from exc
        if not same_file(workspace.parent, self.base_dir):
            raise WorkspaceError("Workspace path is not a direct child of the configured root.")
        if task_id is not None and workspace.name != task_id:
            raise WorkspaceError("Workspace path is not assigned to the requested task.")
        return workspace

    @staticmethod
    def validate_source(source_repo_path: str | Path) -> Path:
        try:
            return validate_directory(source_repo_path)
        except FileSystemSafetyError as exc:
            raise WorkspaceError(
                f"Source repository does not exist or is unsafe: {source_repo_path}"
            ) from exc

    def manifest(self, root_path: str | Path) -> WorkspaceManifest:
        try:
            return build_manifest(root_path, IGNORED_WORKSPACE_DIRS)
        except FileSystemSafetyError as exc:
            raise WorkspaceError(str(exc)) from exc

    @staticmethod
    def manifest_hash(value: WorkspaceManifest) -> str:
        return manifest_hash(value)

    def snapshot(self, root_path: str | Path) -> dict[str, str]:
        return {
            path: str(entry["sha256"])
            for path, entry in self.manifest(root_path).items()
            if entry["type"] == "file"
        }

    def workspace_matches_source(
        self,
        workspace_path: str | Path,
        source_repo_path: str | Path,
    ) -> bool:
        workspace = self.validate_workspace(workspace_path)
        source = self.validate_source(source_repo_path)
        return self.manifest(workspace) == self.manifest(source)

    def _copy_tree(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=False, exist_ok=False)
        for entry in walk_tree_no_follow(source, IGNORED_WORKSPACE_DIRS):
            relative_parts = Path(entry.relative_path).parts
            target = destination.joinpath(*relative_parts)
            if entry.file_type == "directory":
                target.mkdir(exist_ok=False)
                shutil.copystat(entry.path, target, follow_symlinks=False)
                continue
            try:
                token = validate_regular_file(source, entry.relative_path)
                content = read_verified_bytes(token)
            except FileSystemSafetyError as exc:
                raise WorkspaceError(str(exc)) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(content)
                handle.flush()
                _fsync_best_effort(handle.fileno())
            shutil.copymode(token.path, target, follow_symlinks=False)
        shutil.copystat(source, destination, follow_symlinks=False)

    def _remove_tree_no_follow(self, root_path: str | Path) -> None:
        root = Path(root_path)
        if not os.path.lexists(root):
            return
        if is_link_or_reparse(root):
            _remove_link_object(root)
            return
        try:
            root_stat = root.stat()
        except OSError as exc:
            raise WorkspaceError(f"Cleanup target could not be inspected: {root}") from exc
        expected = FileSystemIdentity.from_stat(root_stat)
        if not root.is_dir():
            raise WorkspaceError(f"Cleanup target is not a directory: {root}")
        try:
            with os.scandir(root) as iterator:
                entries = list(iterator)
        except OSError as exc:
            raise WorkspaceError(
                f"Workspace could not be enumerated for cleanup: {root}"
            ) from exc
        for entry in entries:
            candidate = Path(entry.path)
            metadata = entry.stat(follow_symlinks=False)
            if is_link_or_reparse(candidate, metadata):
                _remove_link_object(candidate, metadata)
            elif entry.is_dir(follow_symlinks=False):
                self._remove_tree_no_follow(candidate)
            else:
                candidate.unlink()
        if FileSystemIdentity.from_stat(root.stat()) != expected:
            raise WorkspaceError(f"Workspace identity changed during cleanup: {root}")
        root.rmdir()

    def _assert_base_unchanged(self) -> None:
        if not os.path.lexists(self.base_dir) or is_link_or_reparse(self.base_dir):
            raise WorkspaceError("Workspace root identity is no longer valid.")
        if FileSystemIdentity.from_stat(self.base_dir.stat()) != self._base_identity:
            raise WorkspaceError("Workspace root identity changed after initialization.")

    @classmethod
    def _is_within(cls, path: Path, root: Path) -> bool:
        resolved_path = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        if is_lexically_within(resolved_path, resolved_root):
            return True
        return any(
            os.path.lexists(candidate) and same_file(candidate, resolved_root)
            for candidate in (resolved_path, *resolved_path.parents)
        )


def _remove_link_object(path: Path, value: os.stat_result | None = None) -> None:
    metadata = value or path.lstat()
    if stat_is_directory(metadata) and not stat_is_link(metadata):
        os.rmdir(path)
    else:
        path.unlink()


def stat_is_directory(value: os.stat_result) -> bool:
    return (value.st_mode & 0o170000) == 0o040000


def stat_is_link(value: os.stat_result) -> bool:
    return (value.st_mode & 0o170000) == 0o120000


def _fsync_best_effort(descriptor: int) -> None:
    with suppress(OSError):
        os.fsync(descriptor)
