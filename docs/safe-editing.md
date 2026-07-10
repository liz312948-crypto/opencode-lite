# Safe Editing Model

## Scope

OpenCode-Lite v0.3.0-alpha protects the submitted repository from direct writes by
moving all editing and test execution into a copied task workspace. It provides an
inspectable approval and rollback harness, not an operating-system sandbox.

## Trust Model

The API operator is expected to trust the repository enough to run its tests. Test
files and package scripts are executable code and run with the permissions of the API
process. OpenCode-Lite constrains command shape, cwd, environment, output, and time, but
does not virtualize the operating system.

The following inputs are treated as untrusted and validated:

- Submitted repository paths.
- Unified-diff paths, metadata, hunk counts, and context.
- Patch IDs and proposal content after approval.
- Test argv and path-like arguments.
- Workspace paths loaded from JSON persistence.

## Workspace Isolation

`WorkspaceManager` resolves and validates the source directory, then copies it to a
task-specific directory below the configured workspace root. The task ID cannot contain
path separators or a drive prefix.

The copy skips:

- `.git`
- `.venv` and `venv`
- `node_modules`
- `__pycache__`
- `.pytest_cache`, `.mypy_cache`, and `.ruff_cache`
- `dist` and `build`
- all symlinks

The source and workspace roots may not contain each other. Workspace reset deletes only
a validated path below the configured workspace root, then copies the source again.

## Patch Policy

The alpha patch engine accepts UTF-8 unified diffs that modify existing text files.

Accepted properties:

- Relative POSIX paths such as `repopilot_lite/main.py`.
- One or more standard `@@ -old +new @@` hunks.
- Context, deletion, and addition lines with matching declared counts.
- LF patches applied to either LF or CRLF source text.

Rejected properties:

- Absolute, drive-prefixed, backslash, `.`, or `..` paths.
- Targets resolving outside the workspace.
- Missing files, directories, symlinks, or non-UTF-8 targets.
- Binary patches, file creation/deletion, renames, quoted Git paths, and no-newline
  markers.
- Duplicate file entries, malformed metadata, mismatched context, or overlapping hunks.
- Patches above 1,000,000 characters, 100 files, or 1,000 hunks per file.

All target files are patched in memory before any write. Prepared contents are written
to unique temporary files beside their targets and then replaced. If any later write
fails, the execution flow resets the entire workspace from the source.

## Approval Gate

A valid proposal enters `AWAITING_APPROVAL`. The API exposes the exact diff, target
files, risk level, validation state, patch ID, and SHA-256 hash.

Approval requires the current `patch_id`. Before recording approval, the service:

1. Loads the current patch for the same task.
2. Verifies that its stored hash still matches the diff text.
3. Dry-run validates it again against the current workspace.
4. Stores `approved_hash` and `approved_at`.

Execution repeats these checks. An old approval cannot authorize a replacement patch.
Rejected patches transition the task to `CANCELLED`; a new proposal gets a new ID and
requires new approval.

## Command Policy

`CommandRunner` always uses:

- `subprocess.run` with an argv list and `shell=False`.
- A resolved cwd inside the task workspace.
- A timeout between 1 and 300 seconds.
- Captured UTF-8 stdout and stderr.
- A 20,000-character limit for each output stream.
- A minimal inherited environment that excludes API keys and common secrets.

`TestRunner` permits only these command shapes:

- `pytest ...`
- `python -m pytest ...`
- `npm test ...`

Arguments containing absolute paths, Windows drive paths, or `..` are rejected. If no
command is submitted, detection is limited to an obvious pytest project with a tests
directory or a `package.json` with a test script. The LLM never selects a command.

## Rollback

After patch application, any exception, non-zero test exit, or timeout transitions the
task to `ROLLING_BACK`. The manager deletes the validated task workspace, recopies the
source repository, and compares file hashes while respecting the copy ignore policy.

The final `ExecutionReport` retains:

- Patch ID and target files.
- The actual pre-rollback diff.
- Executed command and bounded test output.
- Exit code or timeout state.
- Failure stage and structured task error.
- Whether rollback ran and whether the restored workspace matches the source.

A rollback error is appended to the task error but never replaces the original failure
cause. The source repository is never used as a rollback target.

## Residual Risks

- Repository test code can perform actions outside its cwd because this is not an OS
  sandbox.
- Process timeout may not terminate every descendant on every platform.
- JSON records can be edited by an operator outside the process; hashes and path checks
  detect patch tampering at execution time but JSON Storage is not an access-control
  system.
- Large repositories can make copy and reset operations expensive.
- The conservative patch subset does not cover every valid output from `git diff`.
