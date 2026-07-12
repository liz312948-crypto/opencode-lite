from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from repopilot_lite.editing_service import EditingError, SafeEditingService
from repopilot_lite.models import (
    CommandResult,
    CommandSpec,
    PatchApproval,
    PatchProposalCreate,
    TaskRecord,
    TaskResult,
    TaskStatus,
)
from repopilot_lite.storage import Storage
from repopilot_lite.workspace import WorkspaceError, WorkspaceManager

VALID_PATCH = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


class _ResetFailureWorkspaceManager(WorkspaceManager):
    def reset_workspace(self, task_id: str, source_repo_path: str | Path) -> Path:
        raise WorkspaceError("simulated rollback reset failure")


def _approved_context(
    tmp_path: Path,
    test_source: str,
    *,
    manager_type: type[WorkspaceManager] = WorkspaceManager,
) -> tuple[SafeEditingService, Storage, TaskRecord, Path, WorkspaceManager]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "kept.txt").write_text("keep\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_behavior.py").write_text(test_source, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    storage = Storage(tmp_path / "data")
    manager = manager_type(tmp_path / "workspaces")
    task = TaskRecord(
        task_id="task-rollback",
        repo_path=str(repo),
        question="Apply the patch and verify rollback",
        status=TaskStatus.SUCCESS,
        result=TaskResult(repo_summary="Rollback fixture"),
        test_command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        test_timeout_seconds=10,
    )
    storage.create_task(task)
    service = SafeEditingService(storage, manager)
    patch = service.submit_patch(task, PatchProposalCreate(unified_diff=VALID_PATCH))
    current = storage.get_task(task.task_id)
    assert current is not None
    service.approve_patch(
        current,
        PatchApproval(
            patch_id=patch.id,
            expected_content_hash=patch.content_hash,
        ),
    )
    approved_task = storage.get_task(task.task_id)
    assert approved_task is not None
    return service, storage, approved_task, repo, manager


def test_failed_tests_restore_modified_created_and_deleted_files(tmp_path: Path) -> None:
    test_source = """from pathlib import Path

def test_mutate_then_fail():
    Path('app.py').write_text('VALUE = 999\\n', encoding='utf-8')
    Path('kept.txt').unlink()
    Path('created.txt').write_text('temporary\\n', encoding='utf-8')
    assert False
"""
    service, _, task, repo, manager = _approved_context(tmp_path, test_source)
    source_before = manager.manifest(repo)

    result = service.execute_patch(task)

    report = result.execution_report
    assert result.status == TaskStatus.FAILED
    assert report is not None
    assert report.rollback_triggered is True
    assert report.rollback_succeeded is True
    assert report.baseline_manifest_hash == report.final_manifest_hash
    assert report.source_unchanged is True
    workspace = Path(result.workspace_path or "")
    assert manager.manifest(workspace) == source_before
    assert manager.manifest(repo) == source_before
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (workspace / "kept.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (workspace / "created.txt").exists()


def test_passing_tests_that_mutate_workspace_are_rolled_back(tmp_path: Path) -> None:
    test_source = """from pathlib import Path

def test_mutate_and_pass():
    Path('created.txt').write_text('unexpected\\n', encoding='utf-8')
    assert True
"""
    service, _, task, repo, manager = _approved_context(tmp_path, test_source)
    source_before = manager.manifest(repo)

    result = service.execute_patch(task)

    report = result.execution_report
    assert result.status == TaskStatus.FAILED
    assert result.error_code == "WORKSPACE_CHANGED_DURING_TESTS"
    assert report is not None
    assert report.failure_stage == "integrity_check"
    assert report.test_results[0].exit_code == 0
    assert report.rollback_succeeded is True
    assert manager.manifest(result.workspace_path or "") == source_before
    assert manager.manifest(repo) == source_before


def test_rollback_failure_is_never_reported_as_success(tmp_path: Path) -> None:
    test_source = """def test_failure():
    assert False
"""
    service, _, task, repo, manager = _approved_context(
        tmp_path,
        test_source,
        manager_type=_ResetFailureWorkspaceManager,
    )
    source_before = manager.manifest(repo)

    result = service.execute_patch(task)

    report = result.execution_report
    assert result.status == TaskStatus.FAILED
    assert report is not None
    assert report.rollback_triggered is True
    assert report.rollback_succeeded is False
    assert report.rollback_error is not None
    assert "simulated rollback reset failure" in report.rollback_error
    assert "Rollback issue" in (result.error_message or "")
    assert manager.manifest(repo) == source_before


def test_unverified_process_cleanup_prevents_verified_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_source = """def test_passes():
    assert True
"""
    service, _, task, repo, manager = _approved_context(tmp_path, test_source)
    source_before = manager.manifest(repo)

    def unclean_timeout(self: object, spec: CommandSpec) -> CommandResult:
        now = datetime.now(UTC)
        return CommandResult(
            argv=list(spec.argv),
            cwd=spec.cwd,
            exit_code=None,
            timed_out=True,
            process_tree_terminated=False,
            termination_error="simulated cleanup failure",
            started_at=now,
            finished_at=now,
        )

    monkeypatch.setattr("repopilot_lite.editing_service.CommandRunner.run", unclean_timeout)

    result = service.execute_patch(task)

    report = result.execution_report
    assert result.status == TaskStatus.FAILED
    assert result.error_code == "TEST_TIMEOUT"
    assert report is not None
    assert report.rollback_succeeded is False
    assert report.rollback_error is not None
    assert "cleanup was not verified" in report.rollback_error
    assert manager.manifest(result.workspace_path or "") == source_before
    assert manager.manifest(repo) == source_before


def test_source_drift_before_execute_is_rejected_without_applying_patch(
    tmp_path: Path,
) -> None:
    test_source = """def test_passes():
    assert True
"""
    service, _, task, repo, _ = _approved_context(tmp_path, test_source)
    (repo / "kept.txt").write_text("changed externally\n", encoding="utf-8")

    with pytest.raises(EditingError) as captured:
        service.execute_patch(task)

    assert captured.value.error_code == "WORKSPACE_BASELINE_MISMATCH"
    workspace = Path(task.workspace_path or "")
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
