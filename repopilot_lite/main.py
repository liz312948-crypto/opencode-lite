from __future__ import annotations

from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException

from repopilot_lite.executor import Executor
from repopilot_lite.models import StepLog, TaskCreate, TaskCreated, TaskRecord, TaskStatus, ToolInfo
from repopilot_lite.planner import Planner
from repopilot_lite.storage import Storage
from repopilot_lite.tools import ToolRegistry, create_default_registry

app = FastAPI(
    title="OpenCode-Lite",
    description="A safe, inspectable, and test-driven coding agent harness for repository-level tasks.",
    version="0.3.0-alpha",
)

storage = Storage()
planner = Planner()
tool_registry = create_default_registry()


def get_storage() -> Storage:
    return storage


def get_planner() -> Planner:
    return planner


def get_tool_registry() -> ToolRegistry:
    return tool_registry


@app.post("/tasks", response_model=TaskCreated)
def create_task(payload: TaskCreate, store: Storage = Depends(get_storage)) -> TaskCreated:
    task = TaskRecord(
        task_id=str(uuid4()),
        repo_path=payload.repo_path,
        question=payload.question,
        status=TaskStatus.PENDING,
    )
    store.create_task(task)
    return TaskCreated(task_id=task.task_id, status=task.status)


@app.post("/tasks/{task_id}/run", response_model=TaskRecord)
def run_task(
    task_id: str,
    store: Storage = Depends(get_storage),
    task_planner: Planner = Depends(get_planner),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> TaskRecord:
    task = _get_task_or_404(task_id, store)

    if task.status == TaskStatus.SUCCESS:
        return task

    if task.status not in {TaskStatus.PENDING, TaskStatus.FAILED}:
        raise HTTPException(status_code=409, detail=f"Task cannot be run from status {task.status}.")

    store.update_status(task, TaskStatus.PLANNING)
    task.plan = task_planner.create_plan(task)
    task.result = None
    task.error = None
    store.update_task(task)

    executor = Executor(registry=registry, storage=store)
    return executor.run(task)


@app.get("/tasks/{task_id}", response_model=TaskRecord)
def get_task(task_id: str, store: Storage = Depends(get_storage)) -> TaskRecord:
    return _get_task_or_404(task_id, store)


@app.get("/tasks/{task_id}/logs", response_model=list[StepLog])
def get_task_logs(task_id: str, store: Storage = Depends(get_storage)) -> list[StepLog]:
    _get_task_or_404(task_id, store)
    return store.get_logs(task_id)


@app.get("/tools", response_model=list[ToolInfo])
def get_tools(registry: ToolRegistry = Depends(get_tool_registry)) -> list[ToolInfo]:
    return [ToolInfo.model_validate(tool) for tool in registry.list_tools()]


def _get_task_or_404(task_id: str, store: Storage) -> TaskRecord:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task
