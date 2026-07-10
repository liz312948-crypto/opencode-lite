from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from repopilot_lite.models import (
    CommandResult,
    CommandSpec,
    ExecutionReport,
    PatchDecision,
    PatchProposal,
    PatchProposalCreate,
    PatchValidationStatus,
    StepLog,
    TaskRecord,
    TaskStatus,
)
from repopilot_lite.patching import PatchApplier, PatchValidationError, patch_content_hash
from repopilot_lite.runners import CommandPolicyError, CommandRunner, TestRunner
from repopilot_lite.storage import Storage
from repopilot_lite.workspace import WorkspaceError, WorkspaceManager


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


class SafeEditingService:
    """Coordinates immutable patch proposals and the explicit approval gate."""

    def __init__(
        self,
        storage: Storage,
        workspace_manager: WorkspaceManager,
        patch_applier: PatchApplier | None = None,
        test_runner: TestRunner | None = None,
    ) -> None:
        self.storage = storage
        self.workspace_manager = workspace_manager
        self.patch_applier = patch_applier or PatchApplier()
        self.test_runner = test_runner or TestRunner()

    def submit_patch(self, task: TaskRecord, payload: PatchProposalCreate) -> PatchProposal:
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
        self.storage.create_patch(patch)
        task.current_patch_id = patch.id
        task.approved_patch_id = None
        self.storage.update_task(task)
        self.storage.transition_status(task, TaskStatus.PATCH_PROPOSED)

        try:
            source = self.workspace_manager.validate_source(task.repo_path)
            workspace = self.workspace_manager.create_workspace(
                task.task_id,
                source,
                replace=True,
            )
            parsed_targets = self.patch_applier.validate(workspace, payload.unified_diff)
            if payload.target_files and sorted(payload.target_files) != sorted(parsed_targets):
                raise PatchValidationError(
                    "Declared target_files do not match the unified diff targets."
                )
        except (PatchValidationError, WorkspaceError, OSError) as exc:
            patch.validation_status = PatchValidationStatus.INVALID
            patch.validation_error = str(exc)
            self.storage.update_patch(patch)
            self.storage.transition_status(
                task,
                TaskStatus.FAILED,
                error_code=getattr(exc, "error_code", "WORKSPACE_SETUP_FAILED"),
                error_message=str(exc),
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
        self.storage.update_patch(patch)
        task.source_repo_path = str(source)
        task.workspace_path = str(workspace)
        self.storage.update_task(task)
        self.storage.transition_status(task, TaskStatus.AWAITING_APPROVAL)
        return patch

    def get_current_patch(self, task: TaskRecord) -> PatchProposal:
        if task.current_patch_id is None:
            raise EditingError(404, "PATCH_NOT_FOUND", "Task has no patch proposal.")
        patch = self.storage.get_patch(task.current_patch_id)
        if patch is None or patch.task_id != task.task_id:
            raise EditingError(404, "PATCH_NOT_FOUND", "Current patch proposal was not found.")
        return patch

    def approve_patch(self, task: TaskRecord, decision: PatchDecision) -> PatchProposal:
        patch = self._require_decision_patch(task, decision)
        if (
            patch.validation_status == PatchValidationStatus.APPROVED
            and task.approved_patch_id == patch.id
        ):
            return patch
        if task.status != TaskStatus.AWAITING_APPROVAL:
            raise EditingError(
                409,
                "APPROVAL_NOT_ALLOWED",
                f"A patch cannot be approved from status {task.status}.",
            )
        if patch.validation_status != PatchValidationStatus.VALID:
            raise EditingError(409, "PATCH_NOT_VALID", "Only a valid patch can be approved.")
        if patch.content_hash != patch_content_hash(patch.unified_diff):
            raise EditingError(409, "PATCH_CONTENT_CHANGED", "Patch content hash has changed.")
        if task.workspace_path is None:
            raise EditingError(409, "WORKSPACE_NOT_FOUND", "Task workspace is not available.")

        try:
            self.patch_applier.validate(task.workspace_path, patch.unified_diff)
        except PatchValidationError as exc:
            raise EditingError(422, exc.error_code, str(exc), patch_id=patch.id) from exc

        patch.validation_status = PatchValidationStatus.APPROVED
        patch.approved_hash = patch.content_hash
        patch.approved_at = datetime.now(UTC)
        self.storage.update_patch(patch)
        task.approved_patch_id = patch.id
        self.storage.update_task(task)
        self._log(task, "approval", "APPROVED", "Patch approved for execution.", patch)
        return patch

    def reject_patch(self, task: TaskRecord, decision: PatchDecision) -> PatchProposal:
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
        patch.approved_at = None
        self.storage.update_patch(patch)
        task.approved_patch_id = None
        self.storage.update_task(task)
        self._log(task, "approval", "REJECTED", "Patch rejected by the user.", patch)
        self.storage.transition_status(task, TaskStatus.CANCELLED)
        return patch

    def execute_patch(self, task: TaskRecord) -> TaskRecord:
        if task.status == TaskStatus.SUCCEEDED and task.execution_report is not None:
            return task
        if task.status != TaskStatus.AWAITING_APPROVAL:
            raise EditingError(
                409,
                "PATCH_NOT_APPROVED",
                f"An approved patch cannot be executed from status {task.status}.",
            )

        patch = self.get_current_patch(task)
        if (
            patch.validation_status != PatchValidationStatus.APPROVED
            or task.approved_patch_id != patch.id
            or patch.approved_hash != patch.content_hash
        ):
            raise EditingError(
                409,
                "PATCH_NOT_APPROVED",
                "The task's current patch has not been approved.",
            )
        if patch.content_hash != patch_content_hash(patch.unified_diff):
            raise EditingError(409, "PATCH_CONTENT_CHANGED", "Approved patch content has changed.")
        if task.workspace_path is None or task.source_repo_path is None:
            raise EditingError(409, "WORKSPACE_NOT_FOUND", "Task workspace is not available.")

        try:
            workspace = self.workspace_manager.validate_workspace(task.workspace_path)
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

        self.storage.transition_status(task, TaskStatus.APPLYING_PATCH)
        modified_files: list[str] = []
        actual_diff = ""
        test_results: list[CommandResult] = []

        try:
            modified_files = self.patch_applier.apply(workspace, patch.unified_diff)
            actual_diff = self.patch_applier.actual_diff(source, workspace, modified_files)
            self.storage.add_log(
                StepLog(
                    task_id=task.task_id,
                    step="patch_apply",
                    status="SUCCESS",
                    message="Approved patch applied inside the task workspace.",
                    data={"patch_id": patch.id, "modified_files": modified_files},
                )
            )
            self.storage.transition_status(task, TaskStatus.TESTING)

            command_result = CommandRunner(workspace).run(command_spec)
            test_results.append(command_result)
            self.storage.add_log(
                StepLog(
                    task_id=task.task_id,
                    step="test_runner",
                    status="FAILED"
                    if command_result.timed_out or command_result.exit_code != 0
                    else "SUCCESS",
                    message="Test command completed.",
                    data=command_result.model_dump(mode="json"),
                )
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
            )

        patch.validation_status = PatchValidationStatus.APPLIED
        self.storage.update_patch(patch)
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
            final_status=TaskStatus.SUCCEEDED,
            failure_stage=None,
        )
        self.storage.update_task(task)
        self.storage.transition_status(task, TaskStatus.SUCCEEDED)
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
    ) -> TaskRecord:
        self.storage.transition_status(task, TaskStatus.ROLLING_BACK)
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
            rollback_succeeded = self.workspace_manager.workspace_matches_source(
                restored,
                task.source_repo_path,
            )
            if not rollback_succeeded:
                rollback_error = "Restored workspace does not match the source repository."
        except (OSError, WorkspaceError) as exc:
            rollback_error = str(exc)

        self.storage.add_log(
            StepLog(
                task_id=task.task_id,
                step="rollback",
                status="SUCCESS" if rollback_succeeded else "FAILED",
                message=rollback_error or "Workspace restored from the source repository.",
                data={"patch_id": patch.id, "rollback_succeeded": rollback_succeeded},
            )
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
            final_status=TaskStatus.FAILED,
            failure_stage=failure_stage,
        )
        self.storage.update_task(task)
        self.storage.transition_status(
            task,
            TaskStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
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
        final_status: TaskStatus,
        failure_stage: str | None,
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
            final_status=final_status,
            failure_stage=failure_stage,
            risk_notes=result.risk_notes if result else [],
            suggestions=result.suggestions if result else [],
            llm_used=result.llm_used if result else False,
        )

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

    def _log(
        self,
        task: TaskRecord,
        step: str,
        status: str,
        message: str,
        patch: PatchProposal,
    ) -> None:
        self.storage.add_log(
            StepLog(
                task_id=task.task_id,
                step=step,
                status=status,
                message=message,
                data={"patch_id": patch.id, "content_hash": patch.content_hash},
            )
        )
