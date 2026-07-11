from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from repopilot_lite.models import (
    CommandResult,
    CommandSpec,
    ExecutionReport,
    PatchApproval,
    PatchDecision,
    PatchProposal,
    PatchProposalCreate,
    PatchValidationStatus,
    StepLog,
    TaskRecord,
    TaskStatus,
)
from repopilot_lite.patching import (
    PatchApplier,
    PatchApplyError,
    PatchValidationError,
    patch_content_hash,
)
from repopilot_lite.runners import CommandPolicyError, CommandRunner, TestRunner
from repopilot_lite.storage import Storage, StorageConflict
from repopilot_lite.task_locks import TaskLockManager, default_task_lock_manager
from repopilot_lite.workspace import WorkspaceError, WorkspaceManager, WorkspaceManifest


class EditingError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details

    def as_detail(self) -> dict[str, Any]:
        return {"error_code": self.error_code, "message": self.message, **self.details}


class _ExecutionFailure(RuntimeError):
    def __init__(self, error_code: str, message: str, stage: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.stage = stage


@dataclass
class _ExecutionIntegrity:
    baseline_manifest: WorkspaceManifest
    source_manifest_before: WorkspaceManifest
    expected_manifest: WorkspaceManifest | None = None
    final_manifest: WorkspaceManifest | None = None
    source_manifest_after: WorkspaceManifest | None = None
    attempted_files: list[str] = field(default_factory=list)
    replaced_files: list[str] = field(default_factory=list)
    restored_files: list[str] = field(default_factory=list)
    restore_errors: list[str] = field(default_factory=list)


class SafeEditingService:
    """Coordinates immutable patch proposals and the explicit approval gate."""

    def __init__(
        self,
        storage: Storage,
        workspace_manager: WorkspaceManager,
        patch_applier: PatchApplier | None = None,
        test_runner: TestRunner | None = None,
        task_lock_manager: TaskLockManager | None = None,
    ) -> None:
        self.storage = storage
        self.workspace_manager = workspace_manager
        self.patch_applier = patch_applier or PatchApplier()
        self.test_runner = test_runner or TestRunner()
        self.task_locks = task_lock_manager or default_task_lock_manager

    def submit_patch(self, task: TaskRecord, payload: PatchProposalCreate) -> PatchProposal:
        try:
            with self.task_locks.lock(task.task_id):
                return self._submit_patch_locked(self._reload_task(task), payload)
        except StorageConflict as exc:
            raise EditingError(409, exc.error_code, str(exc)) from exc

    def _submit_patch_locked(
        self,
        task: TaskRecord,
        payload: PatchProposalCreate,
    ) -> PatchProposal:
        if task.status not in {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            raise EditingError(
                409,
                "PATCH_NOT_ALLOWED",
                f"A patch cannot be submitted from status {task.status}.",
            )
        if task.result is None:
            raise EditingError(409, "ANALYSIS_REQUIRED", "Run repository analysis first.")

        patch = PatchProposal(
            id=str(uuid4()),
            task_id=task.task_id,
            target_files=payload.target_files,
            unified_diff=payload.unified_diff,
            reason=payload.reason,
            risk_level=payload.risk_level,
            generated_by=payload.generated_by,
            content_hash=patch_content_hash(payload.unified_diff),
        )
        task.current_patch_id = patch.id
        task.approved_patch_id = None
        self.storage.create_patch_and_transition(
            task,
            patch,
            TaskStatus.PATCH_PROPOSED,
        )

        try:
            source = self.workspace_manager.validate_source(task.repo_path)
            workspace = self.workspace_manager.create_workspace(
                task.task_id,
                source,
                replace=True,
            )
            task.source_repo_path = str(source)
            task.workspace_path = str(workspace)
            parsed_targets = self.patch_applier.validate(workspace, payload.unified_diff)
            if payload.target_files and sorted(payload.target_files) != sorted(parsed_targets):
                raise PatchValidationError(
                    "Declared target_files do not match the unified diff targets."
                )
        except (PatchValidationError, WorkspaceError, OSError) as exc:
            patch.validation_status = PatchValidationStatus.INVALID
            patch.validation_error = str(exc)
            self.storage.transition_status(
                task,
                TaskStatus.FAILED,
                error_code=getattr(exc, "error_code", "WORKSPACE_SETUP_FAILED"),
                error_message=str(exc),
                patch=patch,
            )
            raise EditingError(
                422,
                getattr(exc, "error_code", "WORKSPACE_SETUP_FAILED"),
                str(exc),
                patch_id=patch.id,
            ) from exc

        patch.target_files = parsed_targets
        patch.validation_status = PatchValidationStatus.VALID
        patch.validation_error = None
        self.storage.transition_status(
            task,
            TaskStatus.AWAITING_APPROVAL,
            patch=patch,
        )
        return patch

    def get_current_patch(self, task: TaskRecord) -> PatchProposal:
        with self.task_locks.lock(task.task_id):
            return self._get_current_patch_locked(self._reload_task(task))

    def _get_current_patch_locked(self, task: TaskRecord) -> PatchProposal:
        if task.current_patch_id is None:
            raise EditingError(404, "PATCH_NOT_FOUND", "Task has no patch proposal.")
        patch = self.storage.get_patch(task.current_patch_id)
        if patch is None or patch.task_id != task.task_id:
            raise EditingError(404, "PATCH_NOT_FOUND", "Current patch proposal was not found.")
        return patch

    def approve_patch(self, task: TaskRecord, decision: PatchApproval) -> PatchProposal:
        try:
            with self.task_locks.lock(task.task_id):
                return self._approve_patch_locked(self._reload_task(task), decision)
        except StorageConflict as exc:
            raise EditingError(409, exc.error_code, str(exc)) from exc

    def _approve_patch_locked(
        self,
        task: TaskRecord,
        decision: PatchApproval,
    ) -> PatchProposal:
        patch = self._require_decision_patch(task, decision)
        if task.status != TaskStatus.AWAITING_APPROVAL:
            raise EditingError(
                409,
                "APPROVAL_NOT_ALLOWED",
                f"A patch cannot be approved from status {task.status}.",
            )
        if patch.validation_status not in {
            PatchValidationStatus.VALID,
            PatchValidationStatus.APPROVED,
        }:
            raise EditingError(409, "PATCH_NOT_VALID", "Only a valid patch can be approved.")
        current_hash = patch_content_hash(patch.unified_diff)
        if patch.content_hash != current_hash:
            raise EditingError(409, "PATCH_CONTENT_CHANGED", "Patch content hash has changed.")
        if decision.expected_content_hash != current_hash:
            raise EditingError(
                409,
                "PATCH_CONTENT_CHANGED",
                "The patch content no longer matches the reviewed SHA-256.",
            )
        if task.workspace_path is None:
            raise EditingError(409, "WORKSPACE_NOT_FOUND", "Task workspace is not available.")

        try:
            self.patch_applier.validate(task.workspace_path, patch.unified_diff)
            command_spec = self.test_runner.select_command(
                task.workspace_path,
                task.test_command,
                task.test_timeout_seconds,
            )
        except (PatchValidationError, CommandPolicyError) as exc:
            raise EditingError(422, exc.error_code, str(exc), patch_id=patch.id) from exc

        command_hash = self._command_spec_hash(command_spec)
        if (
            patch.validation_status == PatchValidationStatus.APPROVED
            and task.approved_patch_id == patch.id
            and patch.approved_hash == current_hash
            and patch.approved_command_hash == command_hash
            and patch.approved_task_revision == task.revision
        ):
            return patch

        patch.validation_status = PatchValidationStatus.APPROVED
        patch.approved_hash = current_hash
        patch.approved_command_hash = command_hash
        patch.approved_task_revision = task.revision + 1
        patch.approved_at = datetime.now(UTC)
        task.approved_patch_id = patch.id
        self.storage.update_task_and_patch(
            task,
            patch,
            logs_to_add=(
                self._log_entry(
                    task,
                    "approval",
                    "APPROVED",
                    "Patch approved for execution.",
                    patch,
                ),
            ),
        )
        return patch

    def reject_patch(self, task: TaskRecord, decision: PatchDecision) -> PatchProposal:
        try:
            with self.task_locks.lock(task.task_id):
                return self._reject_patch_locked(self._reload_task(task), decision)
        except StorageConflict as exc:
            raise EditingError(409, exc.error_code, str(exc)) from exc

    def _reject_patch_locked(
        self,
        task: TaskRecord,
        decision: PatchDecision,
    ) -> PatchProposal:
        patch = self._require_decision_patch(task, decision)
        if patch.validation_status == PatchValidationStatus.REJECTED:
            return patch
        if task.status != TaskStatus.AWAITING_APPROVAL:
            raise EditingError(
                409,
                "REJECTION_NOT_ALLOWED",
                f"A patch cannot be rejected from status {task.status}.",
            )

        patch.validation_status = PatchValidationStatus.REJECTED
        patch.approved_hash = None
        patch.approved_command_hash = None
        patch.approved_task_revision = None
        patch.approved_at = None
        task.approved_patch_id = None
        self.storage.transition_status(
            task,
            TaskStatus.CANCELLED,
            patch=patch,
            additional_logs=(
                self._log_entry(
                    task,
                    "approval",
                    "REJECTED",
                    "Patch rejected by the user.",
                    patch,
                ),
            ),
        )
        return patch

    def execute_patch(self, task: TaskRecord) -> TaskRecord:
        try:
            with self.task_locks.lock(task.task_id):
                return self._execute_patch_locked(self._reload_task(task))
        except StorageConflict as exc:
            raise EditingError(409, exc.error_code, str(exc)) from exc

    def _execute_patch_locked(self, task: TaskRecord) -> TaskRecord:
        if (
            task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}
            and task.execution_report is not None
            and task.execution_report.patch_id == task.current_patch_id
        ):
            return task
        if task.status != TaskStatus.AWAITING_APPROVAL:
            raise EditingError(
                409,
                "PATCH_NOT_APPROVED",
                f"An approved patch cannot be executed from status {task.status}.",
            )

        patch = self._get_current_patch_locked(task)
        current_hash = patch_content_hash(patch.unified_diff)
        if (
            patch.validation_status != PatchValidationStatus.APPROVED
            or task.approved_patch_id != patch.id
            or patch.approved_hash != patch.content_hash
            or patch.approved_task_revision != task.revision
        ):
            raise EditingError(
                409,
                "PATCH_NOT_APPROVED",
                "The task's current patch has not been approved.",
            )
        if patch.content_hash != current_hash:
            self._invalidate_approval(task, patch)
            raise EditingError(409, "PATCH_CONTENT_CHANGED", "Approved patch content has changed.")
        if task.workspace_path is None or task.source_repo_path is None:
            raise EditingError(409, "WORKSPACE_NOT_FOUND", "Task workspace is not available.")

        try:
            workspace = self.workspace_manager.validate_workspace(
                task.workspace_path,
                task_id=task.task_id,
            )
            source = self.workspace_manager.validate_source(task.source_repo_path)
            self.patch_applier.validate(workspace, patch.unified_diff)
            command_spec = self.test_runner.select_command(
                workspace,
                task.test_command,
                task.test_timeout_seconds,
            )
        except (PatchValidationError, WorkspaceError, CommandPolicyError) as exc:
            raise EditingError(
                422,
                getattr(exc, "error_code", "WORKSPACE_NOT_FOUND"),
                str(exc),
                patch_id=patch.id,
            ) from exc

        if patch.approved_command_hash != self._command_spec_hash(command_spec):
            self._invalidate_approval(task, patch)
            raise EditingError(
                409,
                "APPROVED_COMMAND_CHANGED",
                "The approved test command has changed and must be approved again.",
            )

        baseline_manifest = self.workspace_manager.manifest(workspace)
        source_manifest_before = self.workspace_manager.manifest(source)
        if baseline_manifest != source_manifest_before:
            raise EditingError(
                409,
                "WORKSPACE_BASELINE_MISMATCH",
                "The task workspace no longer matches its source baseline.",
                patch_id=patch.id,
            )
        integrity = _ExecutionIntegrity(
            baseline_manifest=baseline_manifest,
            source_manifest_before=source_manifest_before,
            attempted_files=list(patch.target_files),
        )

        self.storage.transition_status(task, TaskStatus.APPLYING_PATCH)
        modified_files: list[str] = []
        actual_diff = ""
        test_results: list[CommandResult] = []
        test_log: StepLog | None = None

        try:
            modified_files = self.patch_applier.apply(workspace, patch.unified_diff)
            integrity.replaced_files = list(modified_files)
            integrity.expected_manifest = self.workspace_manager.manifest(workspace)
            actual_diff = self.patch_applier.actual_diff(source, workspace, modified_files)
            patch_apply_log = StepLog(
                task_id=task.task_id,
                step="patch_apply",
                status="SUCCESS",
                message="Approved patch applied inside the task workspace.",
                data={"patch_id": patch.id, "modified_files": modified_files},
            )
            self.storage.transition_status(
                task,
                TaskStatus.TESTING,
                additional_logs=(patch_apply_log,),
            )

            command_result = CommandRunner(workspace).run(command_spec)
            test_results.append(command_result)
            test_log = StepLog(
                task_id=task.task_id,
                step="test_runner",
                status="FAILED"
                if command_result.timed_out or command_result.exit_code != 0
                else "SUCCESS",
                message="Test command completed.",
                data=command_result.model_dump(mode="json"),
            )
            if command_result.timed_out:
                raise _ExecutionFailure(
                    "TEST_TIMEOUT",
                    f"Test command timed out after {command_spec.timeout_seconds} seconds.",
                    "testing",
                )
            if command_result.exit_code != 0:
                raise _ExecutionFailure(
                    "TESTS_FAILED",
                    f"Test command failed with exit code {command_result.exit_code}.",
                    "testing",
                )
            integrity.final_manifest = self.workspace_manager.manifest(workspace)
            integrity.source_manifest_after = self.workspace_manager.manifest(source)
            if integrity.source_manifest_after != integrity.source_manifest_before:
                raise _ExecutionFailure(
                    "SOURCE_REPOSITORY_CHANGED",
                    "The source repository changed during test execution.",
                    "integrity_check",
                )
            if integrity.final_manifest != integrity.expected_manifest:
                raise _ExecutionFailure(
                    "WORKSPACE_CHANGED_DURING_TESTS",
                    "Tests changed files outside the approved workspace result.",
                    "integrity_check",
                )
            actual_diff = self.patch_applier.actual_diff(source, workspace, modified_files)
        except PatchApplyError as exc:
            integrity.attempted_files = list(exc.attempted_files)
            integrity.replaced_files = list(exc.replaced_files)
            integrity.restored_files = list(exc.restored_files)
            integrity.restore_errors = list(exc.restore_errors)
            modified_files = list(exc.replaced_files)
            return self._rollback_failure(
                task,
                patch,
                command_spec,
                test_results,
                modified_files,
                actual_diff,
                "PATCH_APPLY_FAILED",
                str(exc),
                "patch_apply",
                integrity,
                test_log,
            )
        except _ExecutionFailure as exc:
            return self._rollback_failure(
                task,
                patch,
                command_spec,
                test_results,
                modified_files,
                actual_diff,
                exc.error_code,
                exc.message,
                exc.stage,
                integrity,
                test_log,
            )
        except Exception as exc:
            stage = "testing" if task.status == TaskStatus.TESTING else "patch_apply"
            error_code = "TEST_RUNNER_FAILED" if stage == "testing" else "PATCH_APPLY_FAILED"
            return self._rollback_failure(
                task,
                patch,
                command_spec,
                test_results,
                modified_files,
                actual_diff,
                error_code,
                str(exc),
                stage,
                integrity,
                test_log,
            )

        patch.validation_status = PatchValidationStatus.APPLIED
        task.execution_report = self._build_report(
            task=task,
            patch=patch,
            command_spec=command_spec,
            test_results=test_results,
            modified_files=modified_files,
            actual_diff=actual_diff,
            tests_passed=True,
            rollback_triggered=False,
            rollback_succeeded=None,
            rollback_error=None,
            final_status=TaskStatus.SUCCEEDED,
            failure_stage=None,
            integrity=integrity,
        )
        self.storage.transition_status(
            task,
            TaskStatus.SUCCEEDED,
            patch=patch,
            additional_logs=(test_log,) if test_log is not None else (),
        )
        return self.storage.get_task(task.task_id) or task

    def _rollback_failure(
        self,
        task: TaskRecord,
        patch: PatchProposal,
        command_spec: CommandSpec,
        test_results: list[CommandResult],
        modified_files: list[str],
        actual_diff: str,
        error_code: str,
        error_message: str,
        failure_stage: str,
        integrity: _ExecutionIntegrity,
        test_log: StepLog | None = None,
    ) -> TaskRecord:
        self.storage.transition_status(
            task,
            TaskStatus.ROLLING_BACK,
            additional_logs=(test_log,) if test_log is not None else (),
        )
        rollback_succeeded = False
        rollback_error: str | None = None

        try:
            if task.source_repo_path is None:
                raise WorkspaceError("Source repository is unavailable for rollback.")
            restored = self.workspace_manager.reset_workspace(
                task.task_id,
                task.source_repo_path,
            )
            task.workspace_path = str(restored)
            restored_manifest = self.workspace_manager.manifest(restored)
            integrity.final_manifest = restored_manifest
            integrity.source_manifest_after = self.workspace_manager.manifest(
                task.source_repo_path
            )
            rollback_succeeded = (
                restored_manifest == integrity.baseline_manifest
                and integrity.source_manifest_after == integrity.source_manifest_before
            )
            if not rollback_succeeded:
                rollback_error = (
                    "Restored workspace or source repository does not match the execution baseline."
                )
        except (OSError, WorkspaceError) as exc:
            rollback_error = str(exc)
        if integrity.source_manifest_after is None and task.source_repo_path is not None:
            try:
                integrity.source_manifest_after = self.workspace_manager.manifest(
                    task.source_repo_path
                )
            except (OSError, WorkspaceError) as exc:
                source_error = f"Source integrity could not be verified: {exc}"
                rollback_error = (
                    f"{rollback_error} {source_error}" if rollback_error else source_error
                )

        rollback_log = StepLog(
            task_id=task.task_id,
            step="rollback",
            status="SUCCESS" if rollback_succeeded else "FAILED",
            message=rollback_error or "Workspace restored from the source repository.",
            data={"patch_id": patch.id, "rollback_succeeded": rollback_succeeded},
        )
        if rollback_error:
            error_message = f"{error_message} Rollback issue: {rollback_error}"

        task.execution_report = self._build_report(
            task=task,
            patch=patch,
            command_spec=command_spec,
            test_results=test_results,
            modified_files=modified_files,
            actual_diff=actual_diff,
            tests_passed=False,
            rollback_triggered=True,
            rollback_succeeded=rollback_succeeded,
            rollback_error=rollback_error,
            final_status=TaskStatus.FAILED,
            failure_stage=failure_stage,
            integrity=integrity,
        )
        self.storage.transition_status(
            task,
            TaskStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            additional_logs=(rollback_log,),
        )
        return self.storage.get_task(task.task_id) or task

    @staticmethod
    def _build_report(
        *,
        task: TaskRecord,
        patch: PatchProposal,
        command_spec: CommandSpec,
        test_results: list[CommandResult],
        modified_files: list[str],
        actual_diff: str,
        tests_passed: bool,
        rollback_triggered: bool,
        rollback_succeeded: bool | None,
        rollback_error: str | None,
        final_status: TaskStatus,
        failure_stage: str | None,
        integrity: _ExecutionIntegrity,
    ) -> ExecutionReport:
        result = task.result
        report_command = command_spec.model_copy(update={"env_overrides": {}})
        return ExecutionReport(
            task_id=task.task_id,
            source_repo_path=task.source_repo_path or task.repo_path,
            workspace_path=task.workspace_path or "",
            patch_id=patch.id,
            modified_files=modified_files,
            diff=actual_diff,
            commands_executed=[report_command] if test_results else [],
            test_results=test_results,
            tests_passed=tests_passed,
            rollback_triggered=rollback_triggered,
            rollback_succeeded=rollback_succeeded,
            rollback_error=rollback_error,
            baseline_manifest_hash=WorkspaceManager.manifest_hash(
                integrity.baseline_manifest
            ),
            expected_manifest_hash=(
                WorkspaceManager.manifest_hash(integrity.expected_manifest)
                if integrity.expected_manifest is not None
                else None
            ),
            final_manifest_hash=(
                WorkspaceManager.manifest_hash(integrity.final_manifest)
                if integrity.final_manifest is not None
                else None
            ),
            source_manifest_before_hash=WorkspaceManager.manifest_hash(
                integrity.source_manifest_before
            ),
            source_manifest_after_hash=(
                WorkspaceManager.manifest_hash(integrity.source_manifest_after)
                if integrity.source_manifest_after is not None
                else None
            ),
            source_unchanged=(
                integrity.source_manifest_after == integrity.source_manifest_before
                if integrity.source_manifest_after is not None
                else None
            ),
            attempted_files=integrity.attempted_files,
            replaced_files=integrity.replaced_files,
            restored_files=integrity.restored_files,
            restore_errors=integrity.restore_errors,
            final_status=final_status,
            failure_stage=failure_stage,
            risk_notes=result.risk_notes if result else [],
            suggestions=result.suggestions if result else [],
            llm_used=result.llm_used if result else False,
        )

    def _reload_task(self, task: TaskRecord) -> TaskRecord:
        current = self.storage.get_task(task.task_id)
        if current is None:
            raise EditingError(404, "TASK_NOT_FOUND", "Task was not found.")
        return current

    def _invalidate_approval(self, task: TaskRecord, patch: PatchProposal) -> None:
        patch.validation_status = PatchValidationStatus.VALID
        patch.approved_hash = None
        patch.approved_command_hash = None
        patch.approved_task_revision = None
        patch.approved_at = None
        task.approved_patch_id = None
        self.storage.update_task_and_patch(task, patch)

    @staticmethod
    def _command_spec_hash(command_spec: CommandSpec) -> str:
        payload = json.dumps(
            {
                "argv": command_spec.argv,
                "timeout_seconds": command_spec.timeout_seconds,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _require_decision_patch(
        self,
        task: TaskRecord,
        decision: PatchDecision,
    ) -> PatchProposal:
        if task.current_patch_id != decision.patch_id:
            raise EditingError(
                409,
                "PATCH_ID_MISMATCH",
                "The decision does not target the task's current patch.",
                current_patch_id=task.current_patch_id,
            )
        patch = self.storage.get_patch(decision.patch_id)
        if patch is None or patch.task_id != task.task_id:
            raise EditingError(404, "PATCH_NOT_FOUND", "Patch proposal was not found.")
        return patch

    @staticmethod
    def _log_entry(
        task: TaskRecord,
        step: str,
        status: str,
        message: str,
        patch: PatchProposal,
    ) -> StepLog:
        return StepLog(
            task_id=task.task_id,
            step=step,
            status=status,
            message=message,
            data={"patch_id": patch.id, "content_hash": patch.content_hash},
        )
