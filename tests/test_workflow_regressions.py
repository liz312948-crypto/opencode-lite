from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from repopilot_lite.editing_service import EditingError, SafeEditingService
from repopilot_lite.models import (
    CommandResult,
    CommandSpec,
    PatchApproval,
    PatchDecision,
    PatchProposalCreate,
    PatchValidationStatus,
    TaskRecord,
    TaskResult,
    TaskStatus,
)
from repopilot_lite.storage import Storage
from repopilot_lite.task_locks import TaskLockManager
from repopilot_lite.workspace import WorkspaceManager

VALID_PATCH = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def _editing_context(
    tmp_path: Path,
) -> tuple[SafeEditingService, Storage, TaskRecord, Path, TaskLockManager]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert True\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    storage = Storage(tmp_path / "data")
    task = TaskRecord(
        task_id="task-1",
        repo_path=str(repo),
        question="Apply the reviewed patch",
        status=TaskStatus.SUCCESS,
        result=TaskResult(repo_summary="Fixture repository"),
        test_command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        test_timeout_seconds=10,
    )
    storage.create_task(task)
    lock_manager = TaskLockManager()
    service = SafeEditingService(
        storage,
        WorkspaceManager(tmp_path / "workspaces"),
        task_lock_manager=lock_manager,
    )
    patch = service.submit_patch(task, PatchProposalCreate(unified_diff=VALID_PATCH))
    persisted_task = storage.get_task(task.task_id)
    assert persisted_task is not None
    assert patch.validation_status == PatchValidationStatus.VALID
    return service, storage, persisted_task, repo, lock_manager


def _approve(
    service: SafeEditingService,
    task: TaskRecord,
) -> PatchApproval:
    patch = service.get_current_patch(task)
    decision = PatchApproval(
        patch_id=patch.id,
        expected_content_hash=patch.content_hash,
    )
    approved = service.approve_patch(task, decision)
    assert approved.validation_status == PatchValidationStatus.APPROVED
    return decision


def test_stale_expected_patch_hash_is_rejected_without_approval(tmp_path: Path) -> None:
    service, storage, task, _, _ = _editing_context(tmp_path)
    patch = service.get_current_patch(task)

    with pytest.raises(EditingError) as captured:
        service.approve_patch(
            task,
            PatchApproval(patch_id=patch.id, expected_content_hash="0" * 64),
        )

    assert captured.value.error_code == "PATCH_CONTENT_CHANGED"
    persisted = storage.get_patch(patch.id)
    assert persisted is not None
    assert persisted.validation_status == PatchValidationStatus.VALID
    assert persisted.approved_hash is None


def test_old_approve_request_cannot_refresh_changed_command(tmp_path: Path) -> None:
    service, storage, task, _, _ = _editing_context(tmp_path)
    decision = _approve(service, task)
    changed = storage.get_task(task.task_id)
    assert changed is not None
    changed.test_timeout_seconds += 1
    storage.update_task(changed)

    with pytest.raises(EditingError) as captured:
        service.approve_patch(changed, decision)

    assert captured.value.error_code == "APPROVAL_STALE"
    invalidated = storage.get_patch(decision.patch_id)
    assert invalidated is not None
    assert invalidated.validation_status == PatchValidationStatus.VALID
    assert invalidated.approved_hash is None
    logs = storage.get_logs(task.task_id)
    assert logs[-1].status == "INVALIDATED"
    assert logs[-1].data["approved_command_hash"] is not None

    current = storage.get_task(task.task_id)
    assert current is not None
    renewed = service.approve_patch(current, decision)
    renewed_task = storage.get_task(task.task_id)
    assert renewed_task is not None
    assert renewed.validation_status == PatchValidationStatus.APPROVED
    assert renewed.approved_task_revision == renewed_task.revision


def test_execute_invalidates_approval_bound_to_stale_task_revision(
    tmp_path: Path,
) -> None:
    service, storage, task, repo, _ = _editing_context(tmp_path)
    decision = _approve(service, task)
    changed = storage.get_task(task.task_id)
    assert changed is not None
    changed.question = "Updated task metadata"
    storage.update_task(changed)

    with pytest.raises(EditingError) as captured:
        service.execute_patch(changed)

    assert captured.value.error_code == "PATCH_NOT_APPROVED"
    invalidated = storage.get_patch(decision.patch_id)
    assert invalidated is not None
    assert invalidated.validation_status == PatchValidationStatus.VALID
    assert invalidated.approved_hash is None
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_corrupted_patch_bytes_invalidate_approval_before_apply(tmp_path: Path) -> None:
    service, storage, task, repo, _ = _editing_context(tmp_path)
    decision = _approve(service, task)
    patches = json.loads(storage.patches_file.read_text(encoding="utf-8"))
    patches[decision.patch_id]["unified_diff"] += "\n"
    storage.patches_file.write_text(
        json.dumps(patches, indent=2),
        encoding="utf-8",
    )

    current = storage.get_task(task.task_id)
    assert current is not None
    with pytest.raises(EditingError) as captured:
        service.execute_patch(current)

    assert captured.value.error_code == "PATCH_CONTENT_CHANGED"
    invalidated = storage.get_patch(decision.patch_id)
    assert invalidated is not None
    assert invalidated.validation_status == PatchValidationStatus.VALID
    assert invalidated.approved_hash is None
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_concurrent_execute_enters_runner_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, storage, task, _, lock_manager = _editing_context(tmp_path)
    _approve(service, task)
    first_snapshot = storage.get_task(task.task_id)
    second_snapshot = storage.get_task(task.task_id)
    assert first_snapshot is not None and second_snapshot is not None

    entered = Event()
    release = Event()
    second_started = Event()
    counter_guard = Lock()
    call_count = 0

    def controlled_run(self: object, spec: CommandSpec) -> CommandResult:
        nonlocal call_count
        with counter_guard:
            call_count += 1
        entered.set()
        assert release.wait(timeout=5), "controlled runner was not released"
        now = datetime.now(UTC)
        return CommandResult(
            argv=list(spec.argv),
            cwd=spec.cwd,
            exit_code=0,
            started_at=now,
            finished_at=now,
        )

    monkeypatch.setattr("repopilot_lite.editing_service.CommandRunner.run", controlled_run)
    results: list[TaskRecord] = []
    errors: list[BaseException] = []

    def execute(snapshot: TaskRecord, started: Event | None = None) -> None:
        if started is not None:
            started.set()
        try:
            results.append(service.execute_patch(snapshot))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = Thread(target=execute, args=(first_snapshot,))
    second = Thread(target=execute, args=(second_snapshot, second_started))
    first.start()
    assert entered.wait(timeout=5), "first execute did not enter the runner"
    second.start()
    assert second_started.wait(timeout=5), "second execute did not start"
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert call_count == 1
    assert len(results) == 2
    assert all(result.status == TaskStatus.SUCCEEDED for result in results)
    assert results[0].execution_report == results[1].execution_report
    statuses = [
        log.data.get("to_status")
        for log in storage.get_logs(task.task_id)
        if log.step == "state"
    ]
    assert statuses.count(TaskStatus.APPLYING_PATCH.value) == 1
    assert statuses.count(TaskStatus.TESTING.value) == 1
    assert statuses.count(TaskStatus.SUCCEEDED.value) == 1
    assert lock_manager.active_task_count == 0


def test_reject_after_approval_permanently_blocks_that_execute(tmp_path: Path) -> None:
    service, storage, task, _, _ = _editing_context(tmp_path)
    decision = _approve(service, task)
    current = storage.get_task(task.task_id)
    assert current is not None
    rejected = service.reject_patch(current, PatchDecision(patch_id=decision.patch_id))
    assert rejected.validation_status == PatchValidationStatus.REJECTED

    cancelled = storage.get_task(task.task_id)
    assert cancelled is not None
    with pytest.raises(EditingError) as captured:
        service.execute_patch(cancelled)

    assert captured.value.error_code == "PATCH_NOT_APPROVED"
    persisted = storage.get_task(task.task_id)
    assert persisted is not None
    assert persisted.status == TaskStatus.CANCELLED
