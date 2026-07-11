from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from repopilot_lite.models import (
    PatchProposal,
    PatchValidationStatus,
    StepLog,
    TaskRecord,
    TaskStatus,
)
from repopilot_lite.state_machine import transition_task


class StorageConflict(RuntimeError):
    """Raised when a stale task revision attempts to overwrite newer state."""

    error_code = "STORAGE_REVISION_CONFLICT"


class Storage:
    """Small JSON-file persistence layer for tasks, patches, and execution logs."""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.tasks_file = self.data_dir / "tasks.json"
        self.logs_file = self.data_dir / "logs.json"
        self.patches_file = self.data_dir / "patches.json"
        self.journal_file = self.data_dir / "transaction.json"
        self._lock = RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._ensure_file(self.tasks_file, {})
            self._ensure_file(self.logs_file, {})
            self._ensure_file(self.patches_file, {})
            self._recover_journal()

    def create_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            self._recover_journal()
            tasks = self._read_json(self.tasks_file)
            if task.task_id in tasks:
                raise StorageConflict(f"Task already exists: {task.task_id}")
            tasks[task.task_id] = task.model_dump(mode="json")
            self._write_json(self.tasks_file, tasks)
        return task

    def create_task_with_log(self, task: TaskRecord, log: StepLog) -> TaskRecord:
        """Create a task and its initial audit record as one recoverable bundle."""
        if log.task_id != task.task_id:
            raise StorageConflict("Initial log does not belong to the created task.")
        with self._lock:
            self._recover_journal()
            tasks = self._read_json(self.tasks_file)
            logs = self._read_json(self.logs_file)
            if task.task_id in tasks:
                raise StorageConflict(f"Task already exists: {task.task_id}")
            tasks[task.task_id] = task.model_dump(mode="json")
            logs.setdefault(task.task_id, []).append(log.model_dump(mode="json"))
            self._write_many({self.tasks_file: tasks, self.logs_file: logs})
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            self._recover_journal()
            tasks = self._read_json(self.tasks_file)
        raw_task = tasks.get(task_id)
        if raw_task is None:
            return None
        return TaskRecord.model_validate(raw_task)

    def update_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            self._recover_journal()
            tasks = self._read_json(self.tasks_file)
            self._prepare_task_update(tasks, task)
            self._write_json(self.tasks_file, tasks)
        return task

    def transition_status(
        self,
        task: TaskRecord,
        status: TaskStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        message: str | None = None,
        patch: PatchProposal | None = None,
        additional_logs: Iterable[StepLog] = (),
    ) -> TaskRecord:
        bundled_logs = tuple(additional_logs)
        if any(item.task_id != task.task_id for item in bundled_logs):
            raise StorageConflict("Log does not belong to the transitioned task.")
        with self._lock:
            self._recover_journal()
            tasks = self._read_json(self.tasks_file)
            logs = self._read_json(self.logs_file)
            self._assert_task_revision(tasks, task)
            patches: dict[str, Any] | None = None
            if patch is not None:
                if patch.task_id != task.task_id:
                    raise StorageConflict("Patch does not belong to the transitioned task.")
                patches = self._read_json(self.patches_file)
                self._assert_patch_update(patches, patch)
            previous = task.status
            transition_task(task, status)

            if status == TaskStatus.FAILED:
                task.error_code = error_code or "TASK_FAILED"
                task.error_message = error_message or "Task execution failed."
                task.error = task.error_message
            else:
                task.error_code = None
                task.error_message = None
                task.error = None

            self._prepare_task_update(tasks, task, revision_checked=True)
            transition_log = StepLog(
                task_id=task.task_id,
                step="state",
                status="TRANSITION",
                message=message or f"Task transitioned from {previous} to {status}.",
                data={
                    "from_status": previous.value,
                    "to_status": status.value,
                    "error_code": task.error_code,
                },
            )
            for item in (*bundled_logs, transition_log):
                logs.setdefault(item.task_id, []).append(item.model_dump(mode="json"))

            updates: dict[Path, object] = {
                self.tasks_file: tasks,
                self.logs_file: logs,
            }
            if patch is not None and patches is not None:
                self._prepare_patch_update(patches, patch, validated=True)
                updates[self.patches_file] = patches
            self._write_many(updates)
        return task

    def update_status(
        self,
        task: TaskRecord,
        status: TaskStatus,
        error: str | None = None,
    ) -> TaskRecord:
        """Compatibility wrapper for callers from the v0.2 implementation."""
        return self.transition_status(
            task,
            status,
            error_code="TASK_FAILED" if error else None,
            error_message=error,
        )

    def create_patch(self, patch: PatchProposal) -> PatchProposal:
        with self._lock:
            self._recover_journal()
            patches = self._read_json(self.patches_file)
            if patch.id in patches:
                raise ValueError(f"Patch already exists: {patch.id}")
            patches[patch.id] = patch.model_dump(mode="json")
            self._write_json(self.patches_file, patches)
        return patch

    def create_patch_and_transition(
        self,
        task: TaskRecord,
        patch: PatchProposal,
        status: TaskStatus,
        *,
        message: str | None = None,
    ) -> tuple[TaskRecord, PatchProposal]:
        """Create a patch and advance its task as one recoverable bundle."""
        if patch.task_id != task.task_id:
            raise StorageConflict("Patch does not belong to the transitioned task.")
        with self._lock:
            self._recover_journal()
            tasks = self._read_json(self.tasks_file)
            patches = self._read_json(self.patches_file)
            logs = self._read_json(self.logs_file)
            self._assert_task_revision(tasks, task)
            if patch.id in patches:
                raise StorageConflict(f"Patch already exists: {patch.id}")

            previous = task.status
            transition_task(task, status)
            task.error = None
            task.error_code = None
            task.error_message = None
            self._prepare_task_update(tasks, task, revision_checked=True)
            patches[patch.id] = patch.model_dump(mode="json")
            transition_log = StepLog(
                task_id=task.task_id,
                step="state",
                status="TRANSITION",
                message=message or f"Task transitioned from {previous} to {status}.",
                data={
                    "from_status": previous.value,
                    "to_status": status.value,
                    "error_code": None,
                },
            )
            logs.setdefault(task.task_id, []).append(
                transition_log.model_dump(mode="json")
            )
            self._write_many(
                {
                    self.tasks_file: tasks,
                    self.patches_file: patches,
                    self.logs_file: logs,
                }
            )
        return task, patch

    def get_patch(self, patch_id: str) -> PatchProposal | None:
        with self._lock:
            self._recover_journal()
            patches = self._read_json(self.patches_file)
        raw_patch = patches.get(patch_id)
        if raw_patch is None:
            return None
        return PatchProposal.model_validate(raw_patch)

    def update_patch(self, patch: PatchProposal) -> PatchProposal:
        with self._lock:
            self._recover_journal()
            patches = self._read_json(self.patches_file)
            self._prepare_patch_update(patches, patch)
            self._write_json(self.patches_file, patches)
        return patch

    def update_task_and_patch(
        self,
        task: TaskRecord,
        patch: PatchProposal,
        *,
        logs_to_add: Iterable[StepLog] = (),
    ) -> tuple[TaskRecord, PatchProposal]:
        if patch.task_id != task.task_id:
            raise StorageConflict("Patch does not belong to the updated task.")
        with self._lock:
            self._recover_journal()
            tasks = self._read_json(self.tasks_file)
            patches = self._read_json(self.patches_file)
            logs = self._read_json(self.logs_file)
            self._assert_patch_update(patches, patch)
            self._prepare_task_update(tasks, task)
            self._prepare_patch_update(patches, patch, validated=True)
            for item in logs_to_add:
                if item.task_id != task.task_id:
                    raise StorageConflict("Log does not belong to the updated task.")
                logs.setdefault(item.task_id, []).append(item.model_dump(mode="json"))
            self._write_many(
                {
                    self.tasks_file: tasks,
                    self.patches_file: patches,
                    self.logs_file: logs,
                }
            )
        return task, patch

    def get_task_patches(self, task_id: str) -> list[PatchProposal]:
        with self._lock:
            self._recover_journal()
            patches = self._read_json(self.patches_file)
        return [
            PatchProposal.model_validate(raw_patch)
            for raw_patch in patches.values()
            if raw_patch.get("task_id") == task_id
        ]

    def add_log(self, log: StepLog) -> StepLog:
        with self._lock:
            self._recover_journal()
            logs = self._read_json(self.logs_file)
            logs.setdefault(log.task_id, []).append(log.model_dump(mode="json"))
            self._write_json(self.logs_file, logs)
        return log

    def get_logs(self, task_id: str) -> list[StepLog]:
        with self._lock:
            self._recover_journal()
            logs = self._read_json(self.logs_file)
        return [StepLog.model_validate(item) for item in logs.get(task_id, [])]

    def _ensure_file(self, path: Path, default_value: object) -> None:
        if not path.exists():
            self._write_json_atomic(path, default_value)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(path)
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"Empty JSON storage file: {path}")
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"Expected a JSON object in {path}")
        return loaded

    def _write_json(self, path: Path, value: object) -> None:
        self._write_json_atomic(path, value)

    def _assert_task_revision(
        self,
        tasks: dict[str, Any],
        task: TaskRecord,
    ) -> None:
        raw_task = tasks.get(task.task_id)
        if raw_task is None:
            raise KeyError(f"Task not found: {task.task_id}")
        persisted = TaskRecord.model_validate(raw_task)
        if task.revision != persisted.revision:
            raise StorageConflict(
                f"Task {task.task_id} has revision {persisted.revision}; "
                f"received stale revision {task.revision}."
            )

    def _prepare_task_update(
        self,
        tasks: dict[str, Any],
        task: TaskRecord,
        *,
        revision_checked: bool = False,
    ) -> None:
        if not revision_checked:
            self._assert_task_revision(tasks, task)
        task.revision += 1
        task.updated_at = datetime.now(UTC)
        tasks[task.task_id] = task.model_dump(mode="json")

    @staticmethod
    def _assert_patch_update(
        patches: dict[str, Any],
        patch: PatchProposal,
    ) -> None:
        raw_patch = patches.get(patch.id)
        if raw_patch is None:
            raise KeyError(f"Patch not found: {patch.id}")
        persisted = PatchProposal.model_validate(raw_patch)
        immutable_fields = (
            "id",
            "task_id",
            "unified_diff",
            "reason",
            "risk_level",
            "generated_by",
            "created_at",
            "content_hash",
        )
        changed = [
            name
            for name in immutable_fields
            if getattr(persisted, name) != getattr(patch, name)
        ]
        if changed:
            raise StorageConflict(
                "Immutable patch fields changed: " + ", ".join(sorted(changed))
            )
        if (
            persisted.validation_status != PatchValidationStatus.PENDING
            and persisted.target_files != patch.target_files
        ):
            raise StorageConflict("Validated patch target_files cannot be changed.")

    def _prepare_patch_update(
        self,
        patches: dict[str, Any],
        patch: PatchProposal,
        *,
        validated: bool = False,
    ) -> None:
        if not validated:
            self._assert_patch_update(patches, patch)
        patch.updated_at = datetime.now(UTC)
        patches[patch.id] = patch.model_dump(mode="json")

    def _write_many(self, updates: dict[Path, object]) -> None:
        if not updates:
            return
        if len(updates) == 1:
            path, value = next(iter(updates.items()))
            self._write_json_atomic(path, value)
            return

        allowed_paths = {self.tasks_file, self.logs_file, self.patches_file}
        if not set(updates).issubset(allowed_paths):
            raise ValueError("Transaction contains an unsupported storage file.")
        journal = {
            "version": 1,
            "updates": {
                path.name: value
                for path, value in sorted(updates.items(), key=lambda item: item[0].name)
            },
        }
        self._write_json_atomic(self.journal_file, journal)
        for path, value in sorted(updates.items(), key=lambda item: item[0].name):
            self._write_json_atomic(path, value)
        self.journal_file.unlink()
        self._fsync_directory_best_effort()

    def _recover_journal(self) -> None:
        if not self.journal_file.exists():
            return
        journal = self._read_json(self.journal_file)
        if journal.get("version") != 1:
            raise ValueError("Unsupported storage transaction journal version.")
        updates = journal.get("updates")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("Storage transaction journal has no updates.")
        targets = {
            self.tasks_file.name: self.tasks_file,
            self.logs_file.name: self.logs_file,
            self.patches_file.name: self.patches_file,
        }
        if not set(updates).issubset(targets):
            raise ValueError("Storage transaction journal contains an unknown target.")
        for name, value in sorted(updates.items()):
            if not isinstance(value, dict):
                raise ValueError(f"Invalid transaction payload for {name}.")
            self._write_json_atomic(targets[name], value)
        self.journal_file.unlink()
        self._fsync_directory_best_effort()

    def _write_json_atomic(self, path: Path, value: object) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = -1
                json.dump(value, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            self._fsync_directory_best_effort()
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary_path.unlink()

    def _fsync_directory_best_effort(self) -> None:
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(self.data_dir, flags)
            os.fsync(descriptor)
        except OSError:
            # Windows does not expose portable directory fsync semantics.
            pass
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
