from __future__ import annotations

from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException

from repopilot_lite.editing_service import EditingError, SafeEditingService
from repopilot_lite.executor import Executor
from repopilot_lite.models import (
    PatchDecision,
    PatchProposal,
    PatchProposalCreate,
    StepLog,
    TaskCreate,
    TaskCreated,
    TaskRecord,
    TaskStatus,
    ToolInfo,
)
from repopilot_lite.planner import Planner
from repopilot_lite.storage import Storage
from repopilot_lite.tools import ToolRegistry, create_default_registry
from repopilot_lite.workspace import WorkspaceManager

app = FastAPI(
    title="OpenCode-Lite",
    description="A safe, inspectable, and test-driven coding agent harness for repository-level tasks.",
    version="0.3.0-alpha",
)

storage = Storage()
planner = Planner()
tool_registry = create_default_registry()
workspace_manager = WorkspaceManager()


def get_storage() -> Storage:
    return storage


def get_planner() -> Planner:
    return planner


def get_tool_registry() -> ToolRegistry:
    return tool_registry


def get_workspace_manager() -> WorkspaceManager:
    return workspace_manager


@app.post("/tasks", response_model=TaskCreated)
def create_task(payload: TaskCreate, store: Storage = Depends(get_storage)) -> TaskCreated:
    task = TaskRecord(
        task_id=str(uuid4()),
        repo_path=payload.repo_path,
        question=payload.question,
        status=TaskStatus.PENDING,
        test_command=payload.test_command,
        test_timeout_seconds=payload.test_timeout_seconds,
    )
    store.create_task(task)
    store.add_log(
        StepLog(
            task_id=task.task_id,
            step="state",
            status="CREATED",
            message="Task created in PENDING state.",
            data={"to_status": TaskStatus.PENDING.value},
        )
    )
    return TaskCreated(task_id=task.task_id, status=task.status)


@app.post("/tasks/{task_id}/run", response_model=TaskRecord)
def run_task(
    task_id: str,
    store: Storage = Depends(get_storage),
    task_planner: Planner = Depends(get_planner),
    registry: ToolRegistry = Depends(get_tool_registry),
) -> TaskRecord:
    task = _get_task_or_404(task_id, store)

    if task.status in {TaskStatus.SUCCESS, TaskStatus.SUCCEEDED}:
        return task

    if task.status not in {TaskStatus.PENDING, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        raise HTTPException(status_code=409, detail=f"Task cannot be run from status {task.status}.")

    store.transition_status(task, TaskStatus.PLANNING)
    try:
        task.plan = task_planner.create_plan(task)
        task.result = None
        task.execution_report = None
        task.current_patch_id = None
        task.approved_patch_id = None
        store.update_task(task)
    except Exception as exc:
        store.transition_status(
            task,
            TaskStatus.FAILED,
            error_code="PLANNING_FAILED",
            error_message=str(exc),
        )
        return task

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


@app.post("/tasks/{task_id}/patches", response_model=PatchProposal, status_code=201)
def create_patch_proposal(
    task_id: str,
    payload: PatchProposalCreate,
    store: Storage = Depends(get_storage),
    manager: WorkspaceManager = Depends(get_workspace_manager),
) -> PatchProposal:
    task = _get_task_or_404(task_id, store)
    service = SafeEditingService(store, manager)
    try:
        return service.submit_patch(task, payload)
    except EditingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


@app.get("/tasks/{task_id}/diff", response_model=PatchProposal)
def get_task_diff(
    task_id: str,
    store: Storage = Depends(get_storage),
    manager: WorkspaceManager = Depends(get_workspace_manager),
) -> PatchProposal:
    task = _get_task_or_404(task_id, store)
    service = SafeEditingService(store, manager)
    try:
        return service.get_current_patch(task)
    except EditingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


@app.post("/tasks/{task_id}/approve", response_model=PatchProposal)
def approve_task_patch(
    task_id: str,
    payload: PatchDecision,
    store: Storage = Depends(get_storage),
    manager: WorkspaceManager = Depends(get_workspace_manager),
) -> PatchProposal:
    task = _get_task_or_404(task_id, store)
    service = SafeEditingService(store, manager)
    try:
        return service.approve_patch(task, payload)
    except EditingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


@app.post("/tasks/{task_id}/reject", response_model=PatchProposal)
def reject_task_patch(
    task_id: str,
    payload: PatchDecision,
    store: Storage = Depends(get_storage),
    manager: WorkspaceManager = Depends(get_workspace_manager),
) -> PatchProposal:
    task = _get_task_or_404(task_id, store)
    service = SafeEditingService(store, manager)
    try:
        return service.reject_patch(task, payload)
    except EditingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


@app.post("/tasks/{task_id}/execute", response_model=TaskRecord)
def execute_task_patch(
    task_id: str,
    store: Storage = Depends(get_storage),
    manager: WorkspaceManager = Depends(get_workspace_manager),
) -> TaskRecord:
    task = _get_task_or_404(task_id, store)
    service = SafeEditingService(store, manager)
    try:
        return service.execute_patch(task)
    except EditingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


def _get_task_or_404(task_id: str, store: Storage) -> TaskRecord:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task
