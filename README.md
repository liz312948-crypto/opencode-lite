# OpenCode-Lite

OpenCode-Lite is a safe, inspectable, and test-driven coding agent harness for repository-level tasks.

A developer submits a local repository path and a question, reviews a proposed patch, explicitly approves it, and runs bounded tests inside an isolated workspace.

> OpenCode-Lite is an independent educational and engineering project. It is not affiliated with or endorsed by the OpenCode project.
>
> OpenCode-Lite 是一个独立的学习与工程项目，不隶属于 OpenCode 项目，也未获得其认可或背书。

[RepoPilot-Lite v0.2 Product Walkthrough](https://github.com/liz312948-crypto/RepoPilot-Lite/releases/download/v0.2.0/RepoPilot-Lite-v0.2-Product-Walkthrough.mp4)

## Why This Project

OpenCode-Lite simulates the core backend workflow behind AI coding tools: task creation, planning, tool execution, context collection, modification planning, logs, and fallback behavior.

It is not a full IDE, does not automatically modify code, and does not replace Claude Code, TRAE, or other AI programming tools. The project focuses on a small backend prototype that makes the coding-agent execution path observable, bounded, and easy to explain.

## Features

- Repository understanding through README reading, file listing, and keyword search.
- Modification planning with target files, actions, reasons, risk notes, and suggestions.
- Planner / Executor / ToolRegistry architecture for clear task planning and tool invocation.
- Lightweight Agent Loop that broadens search keywords when the first search has no matches, capped at two retries.
- Observable execution logs for each planning and tool-execution step.
- Optional OpenAI-compatible LLM summarizer using `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL`.
- Rule-based fallback when no LLM key is configured or the LLM call fails.

## Demo Workflow

### 1. Create A Task

Submit a local repository path and a question.

Example input:

```json
{
  "repo_path": "D:\\OpenCode-Lite",
  "question": "How should we add a new repository analysis feature?"
}
```

### 2. Run The Task

Call `POST /tasks/{task_id}/run`. The backend moves through planning, execution, context collection, and summarization.

### 3. Inspect Result

Example output fields:

```json
{
  "status": "SUCCESS",
  "result": {
    "repo_summary": "Repository summary text",
    "key_files": ["README.md", "repopilot_lite/main.py"],
    "modification_plan": ["Confirm existing behavior", "Design the smallest code change"],
    "risk_notes": ["This prototype only plans changes."],
    "llm_used": false
  }
}
```

### 4. Inspect Logs

Call `GET /tasks/{task_id}/logs` to see `STARTED`, `SUCCESS`, `FAILED`, and bounded `RETRY` entries from the execution path.

## Product Boundary

- It does not automatically edit files.
- It does not run arbitrary shell commands.
- It focuses on repository understanding and modification planning.
- It keeps execution observable and bounded.

## Project Structure

```text
repopilot-lite/
+-- repopilot_lite/
|   +-- main.py        # FastAPI app and routes
|   +-- models.py      # Pydantic request, response, task, log, and result models
|   +-- planner.py     # Fixed task planning logic
|   +-- executor.py    # Step execution, retry loop, and logging
|   +-- tools.py       # ToolRegistry and repository tools
|   +-- llm_client.py  # Optional OpenAI-compatible summarizer client
|   +-- storage.py     # JSON file persistence
+-- docs/
|   +-- release_notes_v0.2.md
+-- tests/
|   +-- test_api.py
+-- data/              # Created at runtime for tasks.json and logs.json
+-- requirements.txt
+-- pyproject.toml
+-- README.md
```

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

For tests:

```bash
pip install -e ".[dev]"
```

## Run

```bash
uvicorn repopilot_lite.main:app --reload
```

Open the API docs at:

```text
http://127.0.0.1:8000/docs
```

## API

### Create Task

```bash
curl -X POST http://127.0.0.1:8000/tasks ^
  -H "Content-Type: application/json" ^
  -d "{"repo_path":"D:\\OpenCode-Lite","question":"How should we add a new repository analysis feature?"}"
```

### Run Task

```bash
curl -X POST http://127.0.0.1:8000/tasks/{task_id}/run
```

### Get Task

```bash
curl http://127.0.0.1:8000/tasks/{task_id}
```

### Get Logs

```bash
curl http://127.0.0.1:8000/tasks/{task_id}/logs
```

### Get Tools

```bash
curl http://127.0.0.1:8000/tools
```

## API Execution Flow

1. `POST /tasks` creates a `PENDING` task and stores it in `data/tasks.json`.
2. `POST /tasks/{task_id}/run` changes the task to `PLANNING`.
3. `Planner` creates a small fixed plan.
4. `Executor` changes the task to `RUNNING`, executes steps through `ToolRegistry`, and writes logs.
5. `search_keywords` may retry with broader keywords if no matches are found.
6. `summarize_repo` generates repository understanding and modification planning output.
7. The task ends in `SUCCESS` or `FAILED`.

## Agent Loop

OpenCode-Lite keeps the loop intentionally small and bounded:

```text
initial keyword search
  -> if matches found: continue
  -> if no matches: broaden keywords and retry
  -> retry at most 2 times
  -> write RETRY logs for each retry
```

This avoids infinite execution while showing how an Executor can adapt tool calls based on intermediate results.

## Modification Planning

The output is planning-focused. Each modification step contains:

- `title`: the planning step name.
- `target_files`: likely files or directories to inspect or change.
- `action`: what a coding agent or developer should do next.
- `reason`: why the step matters.

OpenCode-Lite helps identify what to inspect, where changes may belong, and what risks should be checked first.

## Optional LLM Configuration

The app runs without LLM credentials. When no key is configured, `summarize_repo` uses the rule-based fallback and returns `llm_used: false`.

To enable the optional OpenAI-compatible summarizer:

```bash
set OPENAI_API_KEY=your_key
set OPENAI_BASE_URL=https://api.openai.com/v1
set OPENAI_MODEL=gpt-4o-mini
```

If the LLM request fails, the tool falls back to the rule summarizer and still returns a valid task result with `llm_used: false`.

## State Machine

```text
PENDING -> PLANNING -> RUNNING -> SUCCESS
                              -> FAILED
```

Failed tasks can be run again. Successful tasks are returned as-is if run again.

## JSON Persistence

Runtime data is stored under `data/`:

- `data/tasks.json`
- `data/logs.json`

This is enough for a prototype and easy to inspect manually. It is not intended for high concurrency or production durability.

## Release Notes

See [v0.2.0 release notes](docs/release_notes_v0.2.md).

## Roadmap

These are planned directions, not implemented features:

- Entrypoint / dependency detection.
- Test-aware modification planning.
- Path safety / allowlist.
- MCP-style tool interface.
- Evaluator-style plan review.
