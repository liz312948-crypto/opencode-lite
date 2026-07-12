# Changelog

All notable changes to this project are documented in this file.

## v0.3.0-alpha

### Added

- OpenCode-Lite product name and independent-project disclaimer.
- Explicit task state machine and observable transition logs.
- Per-task temporary workspace creation, reset, cleanup, and hash verification.
- Structured patch proposals with dry-run validation and diff inspection.
- Human approval and rejection endpoints bound to patch ID and content hash.
- Safe text unified-diff application inside the isolated workspace.
- argv-based CommandRunner with cwd containment, timeout, bounded output, and minimal
  environment inheritance.
- Allowlisted TestRunner support for pytest, `python -m pytest`, and `npm test`.
- Success, test-failure, and timeout execution paths with verified rollback.
- Structured ExecutionReport data on task responses.
- Unit, integration, and end-to-end coverage for isolation and safety boundaries.
- Architecture, safe-editing, API example, and implementation-plan documentation.
- Windows Job Object and POSIX process-group cleanup evidence.
- Revision conflict detection and redo-journal recovery for JSON record bundles.

### Changed

- External package metadata moved from RepoPilot-Lite to OpenCode-Lite.
- Version advanced from `0.2.0` to PEP 440 version `0.3.0a1`.
- `POST /tasks` accepts optional test command and timeout fields.
- JSON Storage now persists patch records in `data/patches.json`.
- JSON writes now use unique temporary files, flush/fsync, atomic replace, and a
  recoverable cross-record journal; model state changes only after a successful write.
- Approval requests now return the reviewed Patch SHA-256 and stale approval tuples are
  invalidated instead of silently refreshing changed execution context.
- Runtime quality checks now include Ruff and Mypy.

### Compatibility

- The internal `repopilot_lite` Python package and Uvicorn import path are retained.
- Existing task, run, lookup, log, and tool endpoint paths remain available.
- Existing request bodies with only `repo_path` and `question` remain valid.
- The v0.2 analysis completion state `SUCCESS` is retained; safe editing completes at
  `SUCCEEDED`.

### Security

- Source repositories are never patch or command targets.
- Patch paths, size, file count, hunk count, context, and workspace containment are
  validated before writes.
- Symlinks, junctions, reparse points, duplicate physical targets, and changed path/file
  identities are rejected.
- Unapproved, changed, or stale-revision patches cannot execute; approval also binds the
  normalized command hash.
- Commands use `shell=False`, an allowlisted shape, a workspace cwd, streaming output
  limits, a timeout, and bounded process-tree termination.
- Failure, timeout, post-test workspace drift, or cleanup uncertainty trigger workspace
  recreation; rollback success requires baseline/source manifests and cleanup evidence.

## v0.2.0

- Added structured modification planning and risk notes.
- Added bounded keyword-search retry logs.
- Added optional OpenAI-compatible summarization with rule fallback.
- Preserved the repository-understanding-only product boundary.

See [v0.2 release notes](docs/release_notes_v0.2.md).
