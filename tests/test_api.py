from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from repopilot_lite.main import app, get_storage
from repopilot_lite.storage import Storage


def test_task_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Sample\n\nFastAPI service demo.", encoding="utf-8")
    (repo / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )

    storage = Storage(tmp_path / "data")
    app.dependency_overrides[get_storage] = lambda: storage
    client = TestClient(app)

    create_response = client.post(
        "/tasks",
        json={"repo_path": str(repo), "question": "How does the FastAPI service start?"},
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["status"] == "PENDING"

    run_response = client.post(f"/tasks/{created['task_id']}/run")
    assert run_response.status_code == 200
    task = run_response.json()
    assert task["status"] == "SUCCESS"
    assert task["plan"]
    assert task["result"]["key_files"]
    assert task["result"]["llm_used"] is False
    assert len(task["result"]["modification_plan"]) >= 3
    assert task["result"]["risk_notes"]

    logs_response = client.get(f"/tasks/{created['task_id']}/logs")
    assert logs_response.status_code == 200
    assert any(log["step"] == "list_files" for log in logs_response.json())

    app.dependency_overrides.clear()


def test_search_keywords_retries_when_no_matches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Sample\n\nA small API task demo.", encoding="utf-8")

    storage = Storage(tmp_path / "data")
    app.dependency_overrides[get_storage] = lambda: storage
    client = TestClient(app)

    create_response = client.post(
        "/tasks",
        json={"repo_path": str(repo), "question": "zzznomatch qqqmissing"},
    )
    task_id = create_response.json()["task_id"]

    run_response = client.post(f"/tasks/{task_id}/run")
    assert run_response.status_code == 200
    task = run_response.json()
    assert task["status"] == "SUCCESS"

    logs_response = client.get(f"/tasks/{task_id}/logs")
    retry_logs = [log for log in logs_response.json() if log["status"] == "RETRY"]
    assert 1 <= len(retry_logs) <= 2

    app.dependency_overrides.clear()


def test_tools_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/tools")
    assert response.status_code == 200
    tool_names = {tool["name"] for tool in response.json()}
    assert {"list_files", "read_file", "search_text", "summarize_repo"} <= tool_names
