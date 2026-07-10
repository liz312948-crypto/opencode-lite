# API Examples

Base URL used below: `http://127.0.0.1:8000`.

## 1. Create A Task

```http
POST /tasks
Content-Type: application/json
```

```json
{
  "repo_path": "D:\\projects\\sample-repo",
  "question": "Fix the calculator add behavior",
  "test_command": ["python", "-m", "pytest", "-q"],
  "test_timeout_seconds": 60
}
```

`test_command` and `test_timeout_seconds` are optional. Existing v0.2 request bodies
with only `repo_path` and `question` remain valid.

```json
{
  "task_id": "123e4567-e89b-12d3-a456-426614174000",
  "status": "PENDING"
}
```

## 2. Run Repository Analysis

```http
POST /tasks/{task_id}/run
```

The compatible analysis phase ends in `SUCCESS` or `FAILED`. A successful response
contains `plan` and `result`, including `repo_summary`, `key_files`,
`modification_plan`, `risk_notes`, `suggestions`, and `llm_used`.

## 3. Submit A Patch

```http
POST /tasks/{task_id}/patches
Content-Type: application/json
```

```json
{
  "unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
  "reason": "Correct addition behavior.",
  "risk_level": "LOW"
}
```

A valid proposal returns HTTP `201`, `validation_status: "VALID"`, and moves the task
to `AWAITING_APPROVAL`. Invalid patches return HTTP `422` and remain persisted for
audit.

## 4. Inspect The Diff

```http
GET /tasks/{task_id}/diff
```

Important fields:

```json
{
  "id": "patch-id",
  "target_files": ["calculator.py"],
  "unified_diff": "...",
  "validation_status": "VALID",
  "content_hash": "sha256...",
  "approved_hash": null
}
```

## 5. Approve Or Reject

```http
POST /tasks/{task_id}/approve
Content-Type: application/json
```

```json
{
  "patch_id": "patch-id"
}
```

Use the same body with `POST /tasks/{task_id}/reject` to reject it. Repeating approval
of the same unchanged patch is idempotent. A different or stale ID returns HTTP `409`.

## 6. Execute

```http
POST /tasks/{task_id}/execute
```

Unapproved execution returns HTTP `409`. Approved execution returns the complete task.
Operational test failure is represented by `status: "FAILED"` and a structured
`execution_report`, matching the behavior of the existing synchronous `/run` API.

Successful report fields:

```json
{
  "status": "SUCCEEDED",
  "execution_report": {
    "modified_files": ["calculator.py"],
    "tests_passed": true,
    "rollback_triggered": false,
    "final_status": "SUCCEEDED"
  }
}
```

Failure report fields:

```json
{
  "status": "FAILED",
  "error_code": "TESTS_FAILED",
  "execution_report": {
    "tests_passed": false,
    "rollback_triggered": true,
    "rollback_succeeded": true,
    "failure_stage": "testing",
    "final_status": "FAILED"
  }
}
```

## 7. Inspect Result And Logs

```http
GET /tasks/{task_id}
GET /tasks/{task_id}/logs
```

State logs use `step: "state"`, `status: "TRANSITION"`, and include `from_status` and
`to_status`. Search retries use `status: "RETRY"`. Patch application, test execution,
approval, rejection, and rollback also produce dedicated logs.

## Structured HTTP Errors

Safe-editing API errors use an object in FastAPI's `detail` field:

```json
{
  "detail": {
    "error_code": "PATCH_ID_MISMATCH",
    "message": "The decision does not target the task's current patch.",
    "current_patch_id": "expected-id"
  }
}
```

Common HTTP precondition codes include `ANALYSIS_REQUIRED`,
`PATCH_VALIDATION_FAILED`, `PATCH_ID_MISMATCH`, `PATCH_NOT_APPROVED`,
`PATCH_CONTENT_CHANGED`, and `COMMAND_POLICY_VIOLATION`. Operational failures after
execution begins are returned in the task's `error_code`, such as `TESTS_FAILED` and
`TEST_TIMEOUT`.

## Complete PowerShell Flow

The copy-ready fixture workflow is maintained in the README under
**End-To-End Demo**. It creates, analyzes, proposes, previews, approves, executes, and
then reads the task and logs without modifying the fixture source.
