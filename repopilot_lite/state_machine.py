from __future__ import annotations

from repopilot_lite.models import TaskRecord, TaskStatus


class InvalidStateTransition(ValueError):
    """Raised when a task attempts an undeclared status transition."""

    error_code = "INVALID_STATE_TRANSITION"


ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED}),
    TaskStatus.PLANNING: frozenset({TaskStatus.RUNNING, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.SUCCESS, TaskStatus.FAILED}),
    TaskStatus.SUCCESS: frozenset({TaskStatus.PATCH_PROPOSED, TaskStatus.CANCELLED}),
    TaskStatus.PATCH_PROPOSED: frozenset(
        {TaskStatus.AWAITING_APPROVAL, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.APPLYING_PATCH, TaskStatus.CANCELLED}
    ),
    TaskStatus.APPLYING_PATCH: frozenset({TaskStatus.TESTING, TaskStatus.ROLLING_BACK}),
    TaskStatus.TESTING: frozenset({TaskStatus.SUCCEEDED, TaskStatus.ROLLING_BACK}),
    TaskStatus.ROLLING_BACK: frozenset({TaskStatus.FAILED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(
        {TaskStatus.PLANNING, TaskStatus.PATCH_PROPOSED, TaskStatus.CANCELLED}
    ),
    TaskStatus.CANCELLED: frozenset({TaskStatus.PLANNING, TaskStatus.PATCH_PROPOSED}),
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(f"Task cannot transition from {current} to {target}.")


def transition_task(task: TaskRecord, target: TaskStatus) -> TaskRecord:
    validate_transition(task.status, target)
    task.status = target
    return task
