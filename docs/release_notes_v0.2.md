# v0.2.0 - Coding Agent Backend Prototype

RepoPilot-Lite v0.2.0 packages the project as a lightweight Coding Agent backend prototype for repository understanding and modification planning.

Product Walkthrough: [RepoPilot-Lite v0.2 Product Walkthrough](https://github.com/liz312948-crypto/opencode-lite/releases/download/v0.2.0/RepoPilot-Lite-v0.2-Product-Walkthrough.mp4)

## Highlights

- Structured modification planning with `title`, `target_files`, `action`, and `reason`.
- Risk notes that make the generated plan easier to review before code changes.
- Lightweight Agent Loop for keyword search, with bounded retries and `RETRY` logs.
- Optional OpenAI-compatible LLM summarizer controlled by environment variables.
- Rule-based fallback when no LLM key is configured or the LLM request fails.
- Observable execution logs across task planning and tool execution.

## Product Boundary

RepoPilot-Lite does not automatically edit files, run arbitrary shell commands, or act as a full IDE. It focuses on the backend workflow behind coding-agent-style repository understanding and modification planning.

## Demo Flow

1. Create a task with a local repository path and question.
2. Run the task.
3. Inspect `repo_summary`, `key_files`, `modification_plan`, `risk_notes`, `suggestions`, and `llm_used`.
4. Inspect logs for step-by-step execution and bounded retry behavior.
