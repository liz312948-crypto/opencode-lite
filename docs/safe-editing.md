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

The copy ignores ordinary entries named:

- `.git`
- `.venv` and `venv`
- `node_modules`
- `__pycache__`
- `.pytest_cache`, `.mypy_cache`, and `.ruff_cache`
- `dist` and `build`

Symlinks, Windows junctions, and other reparse points are not ignored or followed: the
source/workspace validation fails closed. Copy, manifest, reset, cleanup, repository
readers, and patch targets use the same no-follow checks. Existing path components and
file identities are revalidated before reads, replacements, and destructive cleanup.

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

All target files are patched in memory before any write. Prepared contents and backups
use unique sibling files. Each replacement is verified; if a later replacement fails,
already replaced files are compensated from their backups and the attempted/replaced/
restored ledger is retained. The execution flow then resets the entire workspace from
the source and independently verifies the baseline manifest.

## Approval Gate

A valid proposal enters `AWAITING_APPROVAL`. The API exposes the exact diff, target
files, risk level, validation state, patch ID, and SHA-256 hash.

Approval requires the current `patch_id` and the `expected_content_hash` returned by
the diff endpoint. Before recording approval, the service:

1. Loads the current patch for the same task.
2. Verifies that its stored hash still matches the diff text.
3. Dry-run validates it again against the current workspace.
4. Verifies the client-returned SHA-256 is the current diff hash.
5. Canonicalizes the selected test command and stores its hash, the task revision,
   `approved_hash`, and `approved_at` in the same recoverable storage bundle.

Execution reloads the task while holding its per-task lock and repeats the complete
tuple check. Patch, command, or revision drift atomically invalidates the approval and
adds an `INVALIDATED` audit log. An already-approved request cannot silently refresh a
changed command: it first returns `APPROVAL_STALE`, requiring another explicit approval
action. Rejected patches transition the task to `CANCELLED`; a new proposal gets a new
ID and requires new approval.

## Command Policy

`CommandRunner` always uses:

- `subprocess.Popen` with an argv list and `shell=False`.
- A resolved cwd inside the task workspace.
- A timeout between 1 and 300 seconds.
- Concurrently drained UTF-8 stdout and stderr with a 20,000-byte retained budget for
  each stream and discarded-byte counters.
- A minimal inherited environment that excludes API keys and common secrets.
- A new POSIX process group, or a Windows process created suspended and assigned to a
  kill-on-close Job Object before it is resumed.

Timeout cleanup is bounded. Windows uses `TerminateJobObject` and verifies that the
Job has zero active processes; if Job setup cannot be proven, the command is not
started. POSIX signals the process group. Cleanup failures are recorded in
`CommandResult` and prevent rollback from being described as verified.

`TestRunner` permits only these command shapes:

- `pytest ...`
- `python -m pytest ...`
- `npm test ...`

Arguments containing absolute paths, Windows drive paths, or `..` are rejected. If no
command is submitted, detection is limited to an obvious pytest project with a tests
directory or a `package.json` with a test script. The LLM never selects a command.

## Rollback

Before patch application, the service captures a full workspace manifest and a source
manifest using the documented copy exclusions, and requires them to match. After apply
it captures the full approved workspace manifest. After passing tests, cache/build
artifacts are removed with no-follow cleanup; a new full workspace manifest must equal
the approved result. A non-zero exit, timeout, cleanup problem, exception, or remaining
post-test drift moves the task to `ROLLING_BACK`. The manager deletes the validated
workspace, recopies the source, and compares it with the execution baseline; the
copy-policy source before/after manifests must also match.

The final `ExecutionReport` retains:

- Patch ID and target files.
- The approved/observed diff and apply compensation ledger.
- Executed command and bounded test output.
- Exit code or timeout state.
- Failure stage and structured task error.
- Baseline, expected, final, and source before/after manifest hashes.
- Cache/build artifact paths removed before a successful final integrity comparison.
- Whether rollback ran, whether it matches the execution baseline, and whether the
  source stayed unchanged.

A rollback error is appended to the task error but never replaces the original failure
cause. The source repository is never used as a rollback target.

## Storage And Concurrency

Each mutating task workflow holds a bounded, reference-counted per-task `RLock` and
reloads the task after acquiring it. Monotonic task revisions reject stale snapshots;
ordinary storage updates cannot change status outside `transition_status`.

JSON files use unique sibling temporary files, flush, best-effort fsync, and atomic
replace. Changes spanning task, patch, and logs first persist a redo journal; every
read completes an interrupted journal before returning data. Candidate models are
copied and synchronized back to callers only after durable write success.

This design supports concurrent threads in one process only. Run exactly one Uvicorn
worker. It does not provide a cross-process lock, distributed lease, or durable command
supervisor.

## Residual Risks

- Repository test code can perform actions outside its cwd because this is not an OS
  sandbox.
- A same-account process that changes directory topology in the final filesystem-call
  window remains outside what portable path/identity revalidation can make atomic.
- A whole API-process crash may leave a task in `APPLYING_PATCH`, `TESTING`, or
  `ROLLING_BACK`. The JSON journal recovers record bundles, but v0.3 does not
  automatically reconcile in-flight filesystem/process work; inspect/reset it before
  retrying.
- JSON records can be edited by an operator outside the process; hashes and path checks
  detect patch tampering at execution time but JSON Storage is not an access-control
  system.
- Large repositories can make copy and reset operations expensive.
- The conservative patch subset does not cover every valid output from `git diff`.
- Logs have no rotation/retention policy and may include bounded user file excerpts,
  search matches, local paths, and command output.
- `source_unchanged` covers the source entries eligible for workspace copying; ignored
  `.git`, dependency, cache, and build trees are outside that report field.
