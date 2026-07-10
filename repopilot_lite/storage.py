from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from repopilot_lite.models import PatchProposal, StepLog, TaskRecord, TaskStatus
from repopilot_lite.state_machine import transition_task


class Storage:
    """Small JSON-file persistence layer for tasks, patches, and execution logs."""

    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.tasks_file = self.data_dir / "tasks.json"
        self.logs_file = self.data_dir / "logs.json"
        self.patches_file = self.data_dir / "patches.json"
        self._lock = RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_file(self.tasks_file, {})
        self._ensure_file(self.logs_file, {})
        self._ensure_file(self.patches_file, {})

    def create_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            tasks = self._read_json(self.tasks_file)
            tasks[task.task_id] = task.model_dump(mode="json")
            self._write_json(self.tasks_file, tasks)
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            tasks = self._read_json(self.tasks_file)
        raw_task = tasks.get(task_id)
        if raw_task is None:
            return None
        return TaskRecord.model_validate(raw_task)

    def update_task(self, task: TaskRecord) -> TaskRecord:
        task.updated_at = datetime.now(timezone.utc)
        with self._lock:
            tasks = self._read_json(self.tasks_file)
            tasks[task.task_id] = task.model_dump(mode="json")
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
    ) -> TaskRecord:
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

        self.update_task(task)
        self.add_log(
            StepLog(
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
        )
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
            patches = self._read_json(self.patches_file)
            if patch.id in patches:
                raise ValueError(f"Patch already exists: {patch.id}")
            patches[patch.id] = patch.model_dump(mode="json")
            self._write_json(self.patches_file, patches)
        return patch

    def get_patch(self, patch_id: str) -> PatchProposal | None:
        with self._lock:
            patches = self._read_json(self.patches_file)
        raw_patch = patches.get(patch_id)
        if raw_patch is None:
            return None
        return PatchProposal.model_validate(raw_patch)

    def update_patch(self, patch: PatchProposal) -> PatchProposal:
        patch.updated_at = datetime.now(timezone.utc)
        with self._lock:
            patches = self._read_json(self.patches_file)
            if patch.id not in patches:
                raise KeyError(f"Patch not found: {patch.id}")
            patches[patch.id] = patch.model_dump(mode="json")
            self._write_json(self.patches_file, patches)
        return patch

    def get_task_patches(self, task_id: str) -> list[PatchProposal]:
        with self._lock:
            patches = self._read_json(self.patches_file)
        return [
            PatchProposal.model_validate(raw_patch)
            for raw_patch in patches.values()
            if raw_patch.get("task_id") == task_id
        ]

    def add_log(self, log: StepLog) -> StepLog:
        with self._lock:
            logs = self._read_json(self.logs_file)
            logs.setdefault(log.task_id, []).append(log.model_dump(mode="json"))
            self._write_json(self.logs_file, logs)
        return log

    def get_logs(self, task_id: str) -> list[StepLog]:
        with self._lock:
            logs = self._read_json(self.logs_file)
        return [StepLog.model_validate(item) for item in logs.get(task_id, [])]

    @staticmethod
    def _ensure_file(path: Path, default_value: object) -> None:
        if not path.exists():
            path.write_text(json.dumps(default_value, indent=2), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"Expected a JSON object in {path}")
        return loaded

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
