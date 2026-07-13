from __future__ import annotations

from pathlib import Path

import pytest

from repopilot_lite.models import TaskRecord, TaskStatus
from repopilot_lite.state_machine import InvalidStateTransition, transition_task
from repopilot_lite.storage import Storage


def make_task() -> TaskRecord:
    return TaskRecord(task_id="task-1", repo_path=".", question="Inspect the project")


def test_legal_transition_updates_status() -> None:
    task = make_task()

    transition_task(task, TaskStatus.PLANNING)

    assert task.status == TaskStatus.PLANNING


def test_illegal_transition_is_rejected() -> None:
    task = make_task()

    with pytest.raises(InvalidStateTransition, match="PENDING to TESTING"):
        transition_task(task, TaskStatus.TESTING)


def test_storage_transition_records_log(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data")
    task = make_task()
    storage.create_task(task)

    storage.transition_status(task, TaskStatus.PLANNING)

    logs = storage.get_logs(task.task_id)
    assert logs[-1].step == "state"
    assert logs[-1].status == "TRANSITION"
    assert logs[-1].data == {
        "from_status": "PENDING",
        "to_status": "PLANNING",
        "error_code": None,
    }
