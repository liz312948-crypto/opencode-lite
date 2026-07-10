from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from repopilot_lite.models import (
    PatchDecision,
    PatchProposal,
    PatchProposalCreate,
    PatchValidationStatus,
    StepLog,
    TaskRecord,
    TaskStatus,
)
from repopilot_lite.patching import PatchApplier, PatchValidationError, patch_content_hash
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


class SafeEditingService:
    """Coordinates immutable patch proposals and the explicit approval gate."""

    def __init__(
        self,
        storage: Storage,
        workspace_manager: WorkspaceManager,
        patch_applier: PatchApplier | None = None,
    ) -> None:
        self.storage = storage
        self.workspace_manager = workspace_manager
        self.patch_applier = patch_applier or PatchApplier()

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
        patch.approved_at = datetime.now(timezone.utc)
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
