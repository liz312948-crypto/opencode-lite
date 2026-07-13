from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PATCH_PROPOSED = "PATCH_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPLYING_PATCH = "APPLYING_PATCH"
    TESTING = "TESTING"
    ROLLING_BACK = "ROLLING_BACK"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PatchValidationStatus(StrEnum):
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TaskCreate(BaseModel):
    repo_path: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    test_command: list[str] | None = Field(default=None, max_length=32)
    test_timeout_seconds: int = Field(default=60, ge=1, le=300)

    @field_validator("test_command")
    @classmethod
    def validate_test_command(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value or any(not part.strip() for part in value):
            raise ValueError("test_command must contain non-empty argv values")
        return value


class TaskCreated(BaseModel):
    task_id: str
    status: TaskStatus


class PlanStep(BaseModel):
    name: str
    description: str
    args: dict[str, Any] = Field(default_factory=dict)


class ModificationPlanStep(BaseModel):
    title: str
    target_files: list[str] = Field(default_factory=list)
    action: str
    reason: str


class TaskResult(BaseModel):
    repo_summary: str
    key_files: list[str] = Field(default_factory=list)
    modification_plan: list[ModificationPlanStep] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    llm_used: bool = False
    suggestions: list[str] = Field(default_factory=list)


class CommandSpec(BaseModel):
    argv: list[str] = Field(..., min_length=1)
    cwd: str
    timeout_seconds: int = Field(..., ge=1, le=300)
    env_overrides: dict[str, str] = Field(default_factory=dict, repr=False)


class CommandResult(BaseModel):
    argv: list[str]
    cwd: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_truncated: bool = False
    process_tree_terminated: bool | None = None
    termination_error: str | None = None
    stdout_bytes_discarded: int = 0
    stderr_bytes_discarded: int = 0
    duration_ms: int = 0
    started_at: datetime
    finished_at: datetime


class PatchProposalCreate(BaseModel):
    unified_diff: str = Field(..., min_length=1, max_length=1_000_000)
    reason: str = Field(default="User-submitted patch proposal.", min_length=1)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    target_files: list[str] = Field(default_factory=list, max_length=100)
    generated_by: str = Field(default="user", min_length=1, max_length=50)


class PatchDecision(BaseModel):
    patch_id: str = Field(..., min_length=1)


class PatchApproval(PatchDecision):
    expected_content_hash: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class PatchProposal(BaseModel):
    id: str
    task_id: str
    target_files: list[str] = Field(default_factory=list)
    unified_diff: str
    reason: str
    risk_level: RiskLevel = RiskLevel.MEDIUM
    generated_by: str = "user"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    validation_status: PatchValidationStatus = PatchValidationStatus.PENDING
    validation_error: str | None = None
    content_hash: str
    approved_hash: str | None = None
    approved_command_hash: str | None = None
    approved_task_revision: int | None = None
    approved_at: datetime | None = None


class ExecutionReport(BaseModel):
    task_id: str
    source_repo_path: str
    workspace_path: str
    patch_id: str
    modified_files: list[str] = Field(default_factory=list)
    diff: str = ""
    commands_executed: list[CommandSpec] = Field(default_factory=list)
    test_results: list[CommandResult] = Field(default_factory=list)
    tests_passed: bool = False
    rollback_triggered: bool = False
    rollback_succeeded: bool | None = None
    rollback_error: str | None = None
    baseline_manifest_hash: str | None = None
    expected_manifest_hash: str | None = None
    final_manifest_hash: str | None = None
    source_manifest_before_hash: str | None = None
    source_manifest_after_hash: str | None = None
    source_unchanged: bool | None = None
    attempted_files: list[str] = Field(default_factory=list)
    replaced_files: list[str] = Field(default_factory=list)
    restored_files: list[str] = Field(default_factory=list)
    restore_errors: list[str] = Field(default_factory=list)
    removed_ignored_artifacts: list[str] = Field(default_factory=list)
    final_status: TaskStatus
    failure_stage: str | None = None
    risk_notes: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    llm_used: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class TaskRecord(BaseModel):
    task_id: str
    repo_path: str
    question: str
    status: TaskStatus = TaskStatus.PENDING
    plan: list[PlanStep] = Field(default_factory=list)
    result: TaskResult | None = None
    execution_report: ExecutionReport | None = None
    source_repo_path: str | None = None
    workspace_path: str | None = None
    current_patch_id: str | None = None
    approved_patch_id: str | None = None
    revision: int = Field(default=0, ge=0)
    test_command: list[str] | None = None
    test_timeout_seconds: int = Field(default=60, ge=1, le=300)
    error: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class StepLog(BaseModel):
    task_id: str
    step: str
    status: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ToolInfo(BaseModel):
    name: str
    description: str
