from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from repopilot_lite.main import app, get_storage, get_workspace_manager
from repopilot_lite.storage import Storage
from repopilot_lite.workspace import WorkspaceManager


FIXTURE_REPO = Path(__file__).parent / "fixtures" / "sample_repo"
PASSING_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left, right):
-    return left - right
+    return left + right
"""
FAILING_PATCH = """--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
 def add(left, right):
-    return left - right
+    return left + right + 1
"""


def test_safe_editing_success_keeps_source_unchanged(tmp_path: Path) -> None:
    client = _client(tmp_path)
    original_source = (FIXTURE_REPO / "calculator.py").read_bytes()
    task_id = _create_analyzed_task(client, FIXTURE_REPO, timeout=30)
    patch = _submit_and_approve(client, task_id, PASSING_PATCH)

    execution = client.post(f"/tasks/{task_id}/execute")

    assert execution.status_code == 200
    task = execution.json()
    assert task["status"] == "SUCCEEDED"
    assert task["execution_report"]["tests_passed"] is True
    assert task["execution_report"]["rollback_triggered"] is False
    assert task["execution_report"]["modified_files"] == ["calculator.py"]
    assert task["execution_report"]["commands_executed"]
    assert task["execution_report"]["test_results"][0]["exit_code"] == 0
    assert "return left + right" in task["execution_report"]["diff"]

    workspace = Path(task["workspace_path"])
    assert (workspace / "calculator.py").read_text(encoding="utf-8").endswith(
        "return left + right\n"
    )
    assert (FIXTURE_REPO / "calculator.py").read_bytes() == original_source
    assert client.get(f"/tasks/{task_id}/diff").json()["validation_status"] == "APPLIED"

    repeated = client.post(f"/tasks/{task_id}/execute")
    assert repeated.status_code == 200
    assert repeated.json()["execution_report"]["patch_id"] == patch["id"]

    statuses = [
        log["data"].get("to_status")
        for log in client.get(f"/tasks/{task_id}/logs").json()
        if log["step"] == "state"
    ]
    assert "APPLYING_PATCH" in statuses
    assert "TESTING" in statuses
    assert statuses[-1] == "SUCCEEDED"
    app.dependency_overrides.clear()


def test_failed_tests_trigger_verified_rollback(tmp_path: Path) -> None:
    client = _client(tmp_path)
    original_source = (FIXTURE_REPO / "calculator.py").read_bytes()
    task_id = _create_analyzed_task(client, FIXTURE_REPO, timeout=30)
    _submit_and_approve(client, task_id, FAILING_PATCH)

    execution = client.post(f"/tasks/{task_id}/execute")

    assert execution.status_code == 200
    task = execution.json()
    report = task["execution_report"]
    assert task["status"] == "FAILED"
    assert task["error_code"] == "TESTS_FAILED"
    assert report["tests_passed"] is False
    assert report["rollback_triggered"] is True
    assert report["rollback_succeeded"] is True
    assert report["failure_stage"] == "testing"
    assert report["test_results"][0]["exit_code"] != 0
    assert report["test_results"][0]["stdout"] or report["test_results"][0]["stderr"]
    workspace = Path(task["workspace_path"])
    assert (workspace / "calculator.py").read_bytes() == original_source
    assert (FIXTURE_REPO / "calculator.py").read_bytes() == original_source
    app.dependency_overrides.clear()


def test_timeout_triggers_rollback(tmp_path: Path) -> None:
    repo = tmp_path / "slow_repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Slow fixture\n", encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(3)\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    timeout_patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    original_source = (repo / "app.py").read_bytes()
    client = _client(tmp_path)
    task_id = _create_analyzed_task(client, repo, timeout=1)
    _submit_and_approve(client, task_id, timeout_patch)

    execution = client.post(f"/tasks/{task_id}/execute")

    task = execution.json()
    report = task["execution_report"]
    assert task["status"] == "FAILED"
    assert task["error_code"] == "TEST_TIMEOUT"
    assert report["rollback_succeeded"] is True
    assert report["test_results"][0]["timed_out"] is True
    assert Path(task["workspace_path"]).joinpath("app.py").read_bytes() == original_source
    assert (repo / "app.py").read_bytes() == original_source
    app.dependency_overrides.clear()


def _client(tmp_path: Path) -> TestClient:
    storage = Storage(tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "workspaces")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_workspace_manager] = lambda: manager
    return TestClient(app)


def _create_analyzed_task(
    client: TestClient,
    repo: Path,
    *,
    timeout: int,
) -> str:
    created = client.post(
        "/tasks",
        json={
            "repo_path": str(repo),
            "question": "Fix the broken implementation",
            "test_command": [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            "test_timeout_seconds": timeout,
        },
    ).json()
    analyzed = client.post(f"/tasks/{created['task_id']}/run")
    assert analyzed.status_code == 200
    assert analyzed.json()["status"] == "SUCCESS"
    return str(created["task_id"])


def _submit_and_approve(
    client: TestClient,
    task_id: str,
    unified_diff: str,
) -> dict[str, object]:
    proposal = client.post(
        f"/tasks/{task_id}/patches",
        json={"unified_diff": unified_diff, "reason": "Fixture change."},
    )
    assert proposal.status_code == 201
    patch = proposal.json()
    approval = client.post(
        f"/tasks/{task_id}/approve",
        json={"patch_id": patch["id"]},
    )
    assert approval.status_code == 200
    return patch
