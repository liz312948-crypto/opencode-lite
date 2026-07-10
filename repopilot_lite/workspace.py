from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

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


class WorkspaceError(ValueError):
    """Raised when a workspace request violates an isolation boundary."""


class WorkspaceManager:
    """Creates task-specific copies so editing never targets the source repository."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        default_root = Path(tempfile.gettempdir()) / "opencode-lite-workspaces"
        self.base_dir = Path(base_dir or default_root).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_workspace(
        self,
        task_id: str,
        source_repo_path: str | Path,
        *,
        replace: bool = False,
    ) -> Path:
        source = self.validate_source(source_repo_path)
        destination = self.workspace_path(task_id)

        if self._is_within(self.base_dir, source) or self._is_within(source, self.base_dir):
            raise WorkspaceError(
                "Workspace root and source repository must not contain each other."
            )

        if destination.exists():
            if not replace:
                if not destination.is_dir():
                    raise WorkspaceError(f"Workspace path is not a directory: {destination}")
                return destination
            self.cleanup_workspace(destination)

        shutil.copytree(source, destination, ignore=self._ignore_entries)
        return destination

    def reset_workspace(self, task_id: str, source_repo_path: str | Path) -> Path:
        return self.create_workspace(task_id, source_repo_path, replace=True)

    def cleanup_workspace(self, workspace_path: str | Path) -> None:
        workspace = self.validate_workspace(workspace_path)
        if workspace.exists():
            shutil.rmtree(workspace)

    def workspace_path(self, task_id: str) -> Path:
        if not task_id or Path(task_id).name != task_id or any(char in task_id for char in "/\\:"):
            raise WorkspaceError("Task ID is not safe for a workspace path.")
        workspace = (self.base_dir / task_id).resolve()
        if workspace.parent != self.base_dir:
            raise WorkspaceError("Workspace path escaped the configured root.")
        return workspace

    def validate_workspace(self, workspace_path: str | Path) -> Path:
        workspace = Path(workspace_path).expanduser().resolve()
        if not self._is_within(workspace, self.base_dir):
            raise WorkspaceError("Workspace path is outside the configured root.")
        return workspace

    @staticmethod
    def validate_source(source_repo_path: str | Path) -> Path:
        source = Path(source_repo_path).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            raise WorkspaceError(
                f"Source repository does not exist or is not a directory: {source_repo_path}"
            )
        return source

    def snapshot(self, root_path: str | Path) -> dict[str, str]:
        root = Path(root_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise WorkspaceError(f"Snapshot root does not exist: {root}")

        snapshot: dict[str, str] = {}
        for path in root.rglob("*"):
            if self._should_skip(path, root) or not path.is_file() or path.is_symlink():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[path.relative_to(root).as_posix()] = digest
        return snapshot

    def workspace_matches_source(
        self,
        workspace_path: str | Path,
        source_repo_path: str | Path,
    ) -> bool:
        workspace = self.validate_workspace(workspace_path)
        source = self.validate_source(source_repo_path)
        return self.snapshot(workspace) == self.snapshot(source)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    @staticmethod
    def _ignore_entries(directory: str, names: list[str]) -> set[str]:
        root = Path(directory)
        ignored = {name for name in names if name in IGNORED_WORKSPACE_DIRS}
        ignored.update(name for name in names if (root / name).is_symlink())
        return ignored

    @staticmethod
    def _should_skip(path: Path, root: Path) -> bool:
        return any(part in IGNORED_WORKSPACE_DIRS for part in path.relative_to(root).parts)
