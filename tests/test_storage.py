from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Thread

import pytest

from repopilot_lite.models import (
    PatchProposal,
    PatchValidationStatus,
    StepLog,
    TaskRecord,
)
from repopilot_lite.storage import Storage, StorageConflict


def _task(task_id: str = "task-1") -> TaskRecord:
    return TaskRecord(task_id=task_id, repo_path=".", question="Inspect the project")


def _patch(task_id: str = "task-1") -> PatchProposal:
    unified_diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
"""
    return PatchProposal(
        id="patch-1",
        task_id=task_id,
        unified_diff=unified_diff,
        reason="Test patch.",
        content_hash=hashlib.sha256(unified_diff.encode("utf-8")).hexdigest(),
    )


def test_stale_task_revision_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data")
    storage.create_task(_task())
    first = storage.get_task("task-1")
    stale = storage.get_task("task-1")
    assert first is not None and stale is not None

    first.question = "newer"
    storage.update_task(first)
    stale.question = "stale"

    with pytest.raises(StorageConflict, match="stale revision"):
        storage.update_task(stale)

    persisted = storage.get_task("task-1")
    assert persisted is not None
    assert persisted.question == "newer"
    assert persisted.revision == 1


def test_patch_content_is_immutable_after_creation(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data")
    storage.create_patch(_patch())
    changed = storage.get_patch("patch-1")
    assert changed is not None
    changed.unified_diff += "\n"

    with pytest.raises(StorageConflict, match="unified_diff"):
        storage.update_patch(changed)

    persisted = storage.get_patch("patch-1")
    assert persisted is not None
    assert persisted.unified_diff != changed.unified_diff


def test_concurrent_log_writes_do_not_lose_records(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data")
    storage.create_task(_task())
    per_thread = 20

    def write_logs(worker: int) -> None:
        for sequence in range(per_thread):
            storage.add_log(
                StepLog(
                    task_id="task-1",
                    step="worker",
                    status="SUCCESS",
                    message=f"{worker}:{sequence}",
                )
            )

    threads = [Thread(target=write_logs, args=(worker,)) for worker in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(storage.get_logs("task-1")) == 6 * per_thread
    assert isinstance(json.loads(storage.logs_file.read_text(encoding="utf-8")), dict)


def test_failed_atomic_replace_preserves_previous_valid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path / "data")
    task = _task()
    storage.create_task(task)
    before = storage.tasks_file.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError(f"replace failed for {source} -> {destination}")

    with monkeypatch.context() as context:
        context.setattr("repopilot_lite.storage.os.replace", fail_replace)
        task.question = "not committed"
        with pytest.raises(OSError, match="replace failed"):
            storage.update_task(task)

    assert storage.tasks_file.read_bytes() == before
    assert isinstance(json.loads(storage.tasks_file.read_text(encoding="utf-8")), dict)
    assert not list(storage.data_dir.glob(".tasks.json.*.tmp"))


def test_incomplete_bundle_is_completed_from_journal_on_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    storage = Storage(data_dir)
    task = _task()
    patch = _patch()
    storage.create_task(task)
    storage.create_patch(patch)
    task.current_patch_id = patch.id
    patch.target_files = ["app.py"]
    patch.validation_status = PatchValidationStatus.VALID
    audit_log = StepLog(
        task_id=task.task_id,
        step="bundle",
        status="SUCCESS",
        message="commit all records",
    )
    original_write = storage._write_json_atomic

    def interrupt_patch_write(path: Path, value: object) -> None:
        if path == storage.patches_file:
            raise OSError("simulated interruption")
        original_write(path, value)

    monkeypatch.setattr(storage, "_write_json_atomic", interrupt_patch_write)
    with pytest.raises(OSError, match="simulated interruption"):
        storage.update_task_and_patch(task, patch, logs_to_add=(audit_log,))
    assert storage.journal_file.exists()

    recovered = Storage(data_dir)

    persisted_task = recovered.get_task(task.task_id)
    persisted_patch = recovered.get_patch(patch.id)
    assert persisted_task is not None and persisted_patch is not None
    assert persisted_task.current_patch_id == patch.id
    assert persisted_task.revision == 1
    assert persisted_patch.target_files == ["app.py"]
    assert persisted_patch.validation_status == PatchValidationStatus.VALID
    assert [log.message for log in recovered.get_logs(task.task_id)] == [
        "commit all records"
    ]
    assert not recovered.journal_file.exists()
