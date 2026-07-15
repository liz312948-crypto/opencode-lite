# OpenCode-Lite Agent Guide

## Project Positioning

OpenCode-Lite is a small FastAPI teaching project for repository understanding and a
human-approved, test-driven safe-editing workflow. Version `v0.3.0-alpha` demonstrates
control-flow, isolation, approval, rollback, and evidence collection; it is not a full
IDE, an autonomous coding product, or an operating-system sandbox.

The public project name is OpenCode-Lite. Keep the internal Python package name
`repopilot_lite` and the Uvicorn entry point `repopilot_lite.main:app` compatible in
v0.3.

## Repository Map

- `repopilot_lite/`: FastAPI routes, schemas, analysis workflow, safe-editing
  orchestration, filesystem policy, command runner, and JSON persistence.
- `tests/`: unit, API, isolation, concurrency, process-tree, rollback, and storage
  recovery tests.
- `tests/fixtures/sample_repo/`: the only repository used by the reproducible demos.
- `scripts/`: local PowerShell demo clients. They call the public API and must not
  bypass the approval workflow.
- `docs/architecture.md`: module ownership, data flow, and invariants.
- `docs/safe-editing.md`: trust model, safety policy, and residual risks.
- `docs/api-examples.md`: current API request, response, and error shapes.
- `docs/interview-notes.md`: design rationale and interview discussion prompts.
- `.github/workflows/ci.yml`: Windows and Linux checks on Python 3.12.

## Safety Boundary

Preserve these v0.3 invariants:

1. Harness-managed patch, reset, cleanup, and command-cwd operations target only a
   validated task workspace, never the submitted source repository.
2. Patch paths are relative, canonical POSIX paths; links, junctions, reparse points,
   traversal, and targets outside the workspace are rejected.
3. Approval binds the current patch ID, the client-reviewed SHA-256, the normalized
   test-command digest, and the task revision. Drift requires new approval.
4. Commands use an argv list, `shell=False`, a workspace cwd, bounded output, a
   timeout, a minimal environment, and bounded process-tree cleanup.
5. `SUCCEEDED` requires the final workspace manifest to match the approved post-patch
   manifest. Failure or uncertainty enters rollback and records an `ExecutionReport`.
6. JSON storage, task locks, and revision checks support one process, one Uvicorn
   worker, and the application's one shared `Storage` instance only.

These controls are a workflow harness, not OS isolation. Trusted pytest or npm code
runs with the API process permissions and can access paths outside its cwd. Never
claim that arbitrary repository code is sandboxed or that `source_unchanged` attests
ignored `.git`, dependency, cache, build, or other host paths.

## Development Rules

- Read the relevant implementation, tests, and architecture/safety documentation
  before editing.
- Prefer small changes that follow existing module ownership and typed Pydantic
  contracts.
- Keep public v0.2/v0.3 endpoint paths and request semantics compatible unless a task
  explicitly authorizes a breaking change.
- Keep filesystem, subprocess, storage, LLM, and external-service dependencies behind
  their existing boundaries. Avoid hidden network calls and implicit global side
  effects.
- Add tests for observable invariants and failure behavior, not only implementation
  details.
- Update README or architecture/safety documentation when a user-visible contract or
  accepted limitation changes.
- Preserve unrelated local changes. Do not rewrite or delete files merely to simplify
  an edit.

## Prohibited Operations

Unless the user explicitly requests and authorizes them, do not:

- write an approved workspace change back to the source repository;
- weaken path, patch, approval, command, state-machine, rollback, or storage checks;
- replace argv execution with shell command strings or set `shell=True`;
- add arbitrary command execution, automatic patch approval, or unbounded retries;
- enable multiple Uvicorn workers against JSON storage;
- expose the unauthenticated teaching API on a public or multi-tenant network;
- add v0.4 scope such as SQLite, AST indexing, vector retrieval, dynamic planning,
  multi-agent orchestration, MCP, GUI, or TUI as incidental work;
- run tests from an untrusted repository or send repository content to an LLM without
  explicit operator consent;
- force-push, publish a release, merge, or delete user work without explicit approval.

## Verification Commands

Run the strongest relevant checks after code changes, in this order:

```powershell
python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m mypy --python-version 3.12 repopilot_lite
python -m compileall repopilot_lite
git diff --check
```

For documentation-only changes, at minimum run `git diff --check` and validate any
referenced commands, links, API fields, and Mermaid syntax against the repository.

### Windows pytest basetemp

Some Windows environments deny pytest's default temporary directory. Create a parent
directory first and keep `--basetemp` outside the repository fixture:

```powershell
New-Item -ItemType Directory -Force "D:\pytest_tmp_opencode_lite" | Out-Null
python -m pytest -q -p no:cacheprovider `
    --basetemp "D:\pytest_tmp_opencode_lite\basetemp"
```

Do not pass an absolute `--basetemp` through a task's API `test_command`: the runtime
policy intentionally rejects command arguments that reference paths outside the task
workspace. The command above is for repository development checks only.

## Commit Convention

- Use small, thematic commits that leave the worktree in a reviewable state.
- Use imperative Conventional Commit subjects, for example `docs: ...`, `feat: ...`,
  `fix: ...`, or `test: ...`.
- Stage only files owned by the current task. Inspect `git diff --cached` before every
  commit.
- Do not mix core safety changes with documentation, demo, or formatting changes.
- Do not amend, rebase, force-push, push, create a PR, or publish a release unless the
  user explicitly asks.

## Current v0.3 Limitations

- Synchronous analysis and execution block the API request.
- JSON persistence is single-process and single-worker, with no cross-process lease.
- Whole-process crashes have redo-journal record recovery but no durable command
  supervisor or automatic task/workspace reconciliation.
- Tests run trusted repository code without a container, VM, network policy, or
  low-privilege OS boundary.
- Patch support is limited to bounded modifications of existing UTF-8 text files; no
  create/delete, rename, binary, or advanced Git patch forms.
- Workspace copy cost grows with repository size.
- Logs have no automatic rotation or retention policy and may contain bounded file
  excerpts, command output, diffs, and local paths.
- The optional LLM summarizer may send bounded repository context to the configured
  endpoint; it is disabled when no API key is present.

## Codex Execution Principles

When Codex changes this project:

1. Confirm the branch, worktree, recent commits, local instructions, and requested
   scope before editing.
2. Trace the affected API-to-storage or API-to-workspace call chain before proposing a
   core change.
3. Treat `WorkspaceManager`, `PatchApplier`, `CommandRunner`, `SafeEditingService`,
   state transitions, and `Storage` as security-sensitive. Change them only when the
   request explicitly requires it and pair the change with focused regression tests.
4. Use the public API for demos; never bypass approval by calling internal helpers or
   editing persisted JSON.
5. Use only `tests/fixtures/sample_repo` for repository-writing demos. Hash or snapshot
   its source before and after the demo and fail if it changes.
6. Stop and report any conflict with existing user changes or any uncertainty that
   would broaden the requested scope.
7. Report tests, lint, typecheck, compile checks, skipped checks, commits, and the final
   Git status. Never imply a skipped platform check passed.
