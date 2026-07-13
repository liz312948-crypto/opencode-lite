from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot_lite.main import app, get_storage, get_workspace_manager
from repopilot_lite.storage import Storage
from repopilot_lite.workspace import WorkspaceManager

VALID_PATCH = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


@pytest.fixture
def editing_client(tmp_path: Path) -> tuple[TestClient, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (repo / "app.py").write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    storage = Storage(tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "workspaces")
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_workspace_manager] = lambda: manager
    yield TestClient(app), repo
    app.dependency_overrides.clear()


def test_patch_review_and_approval_bind_exact_patch(
    editing_client: tuple[TestClient, Path],
) -> None:
    client, repo = editing_client
    task_id = _create_analyzed_task(client, repo)

    proposal_response = client.post(
        f"/tasks/{task_id}/patches",
        json={"unified_diff": VALID_PATCH, "reason": "Fix addition."},
    )
    assert proposal_response.status_code == 201
    proposal = proposal_response.json()
    assert proposal["target_files"] == ["app.py"]
    assert proposal["validation_status"] == "VALID"

    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "AWAITING_APPROVAL"
    assert Path(task["workspace_path"]).is_dir()
    assert (repo / "app.py").read_text(encoding="utf-8").endswith("return a - b\n")

    diff_response = client.get(f"/tasks/{task_id}/diff")
    assert diff_response.status_code == 200
    assert diff_response.json()["id"] == proposal["id"]

    unapproved_execution = client.post(f"/tasks/{task_id}/execute")
    assert unapproved_execution.status_code == 409
    assert unapproved_execution.json()["detail"]["error_code"] == "PATCH_NOT_APPROVED"

    wrong_approval = client.post(
        f"/tasks/{task_id}/approve",
        json={"patch_id": "wrong-patch", "expected_content_hash": proposal["content_hash"]},
    )
    assert wrong_approval.status_code == 409
    assert wrong_approval.json()["detail"]["error_code"] == "PATCH_ID_MISMATCH"

    approval = client.post(
        f"/tasks/{task_id}/approve",
        json={
            "patch_id": proposal["id"],
            "expected_content_hash": proposal["content_hash"],
        },
    )
    assert approval.status_code == 200
    assert approval.json()["validation_status"] == "APPROVED"
    assert approval.json()["approved_hash"] == proposal["content_hash"]


def test_rejected_patch_cancels_editing_flow(
    editing_client: tuple[TestClient, Path],
) -> None:
    client, repo = editing_client
    task_id = _create_analyzed_task(client, repo)
    proposal = client.post(
        f"/tasks/{task_id}/patches",
        json={"unified_diff": VALID_PATCH},
    ).json()

    rejection = client.post(
        f"/tasks/{task_id}/reject",
        json={"patch_id": proposal["id"]},
    )

    assert rejection.status_code == 200
    assert rejection.json()["validation_status"] == "REJECTED"
    assert client.get(f"/tasks/{task_id}").json()["status"] == "CANCELLED"


def test_invalid_patch_is_persisted_and_reported(
    editing_client: tuple[TestClient, Path],
) -> None:
    client, repo = editing_client
    task_id = _create_analyzed_task(client, repo)
    traversal_patch = """--- a/../outside.py
+++ b/../outside.py
@@ -1 +1 @@
-old
+new
"""

    response = client.post(
        f"/tasks/{task_id}/patches",
        json={"unified_diff": traversal_patch},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "PATCH_VALIDATION_FAILED"
    task = client.get(f"/tasks/{task_id}").json()
    assert task["status"] == "FAILED"
    assert task["error_code"] == "PATCH_VALIDATION_FAILED"


def _create_analyzed_task(client: TestClient, repo: Path) -> str:
    created = client.post(
        "/tasks",
        json={
            "repo_path": str(repo),
            "question": "Fix the add function",
            "test_command": [sys.executable, "-m", "pytest", "-q"],
        },
    ).json()
    response = client.post(f"/tasks/{created['task_id']}/run")
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"
    return str(created["task_id"])
