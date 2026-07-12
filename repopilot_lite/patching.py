from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repopilot_lite.filesystem_safety import (
    FileSystemSafetyError,
    ValidatedFile,
    read_verified_bytes,
    revalidate_regular_file,
    validate_directory,
    validate_regular_file,
)

HUNK_HEADER = re.compile(
    r"^@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? "
    r"@@(?:.*?)(?:\r?\n)?$"
)
MAX_PATCH_CHARS = 1_000_000
MAX_PATCH_FILES = 100
MAX_HUNKS_PER_FILE = 1_000


class PatchValidationError(ValueError):
    """Raised when a unified diff is malformed or violates a path boundary."""

    error_code = "PATCH_VALIDATION_FAILED"


class PatchApplyError(PatchValidationError):
    """Raised when a prepared multi-file apply cannot commit or restore completely."""

    def __init__(
        self,
        message: str,
        *,
        attempted_files: list[str],
        replaced_files: list[str],
        restored_files: list[str],
        restore_errors: list[str],
    ) -> None:
        super().__init__(message)
        self.attempted_files = attempted_files
        self.replaced_files = replaced_files
        self.restored_files = restored_files
        self.restore_errors = restore_errors


@dataclass(frozen=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class FilePatch:
    path: str
    hunks: tuple[PatchHunk, ...]


@dataclass(frozen=True)
class PreparedChange:
    path: str
    token: ValidatedFile
    original_content: bytes
    new_content: str


def patch_content_hash(unified_diff: str) -> str:
    return hashlib.sha256(unified_diff.encode("utf-8")).hexdigest()


class PatchApplier:
    """Validates and applies a bounded subset of text unified diffs."""

    def parse(self, unified_diff: str) -> list[FilePatch]:
        if not unified_diff.strip():
            raise PatchValidationError("Patch content is empty.")
        if len(unified_diff) > MAX_PATCH_CHARS:
            raise PatchValidationError(f"Patch exceeds the {MAX_PATCH_CHARS}-character limit.")
        if "GIT binary patch" in unified_diff or "Binary files " in unified_diff:
            raise PatchValidationError("Binary patches are not supported.")

        lines = unified_diff.splitlines(keepends=True)
        file_patches: list[FilePatch] = []
        seen_paths: set[str] = set()
        index = 0

        while index < len(lines):
            line = lines[index]
            if line.startswith(("diff --git ", "index ")) or not line.strip():
                index += 1
                continue
            if not line.startswith("--- "):
                raise PatchValidationError(f"Unexpected patch metadata: {line.strip()[:120]}")

            old_path = self._parse_header_path(line[4:])
            index += 1
            if index >= len(lines) or not lines[index].startswith("+++ "):
                raise PatchValidationError("Each file patch requires a +++ header.")
            new_path = self._parse_header_path(lines[index][4:])
            index += 1

            if old_path == "/dev/null" or new_path == "/dev/null":
                raise PatchValidationError(
                    "File creation and deletion are not supported in this alpha."
                )
            normalized_old = self._normalize_patch_path(old_path)
            normalized_new = self._normalize_patch_path(new_path)
            if normalized_old != normalized_new:
                raise PatchValidationError("Patch renames are not supported.")
            path_key = self._path_key(normalized_new)
            if path_key in seen_paths:
                raise PatchValidationError(f"Duplicate file patch: {normalized_new}")

            hunks: list[PatchHunk] = []
            while index < len(lines) and lines[index].startswith("@@ "):
                if len(hunks) >= MAX_HUNKS_PER_FILE:
                    raise PatchValidationError(
                        f"Patch for {normalized_new} exceeds the hunk limit."
                    )
                hunk, index = self._parse_hunk(lines, index)
                hunks.append(hunk)

            if not hunks:
                raise PatchValidationError(f"Patch for {normalized_new} has no hunks.")

            seen_paths.add(path_key)
            file_patches.append(FilePatch(path=normalized_new, hunks=tuple(hunks)))
            if len(file_patches) > MAX_PATCH_FILES:
                raise PatchValidationError(f"Patch exceeds the {MAX_PATCH_FILES}-file limit.")

        if not file_patches:
            raise PatchValidationError("Patch does not contain a file modification.")
        return file_patches

    def validate(self, workspace_path: str | Path, unified_diff: str) -> list[str]:
        workspace = self._validate_workspace(workspace_path)
        file_patches = self.parse(unified_diff)
        self._prepare_changes(workspace, file_patches)
        return [file_patch.path for file_patch in file_patches]

    def apply(self, workspace_path: str | Path, unified_diff: str) -> list[str]:
        workspace = self._validate_workspace(workspace_path)
        file_patches = self.parse(unified_diff)
        prepared = self._prepare_changes(workspace, file_patches)

        staged: list[tuple[PreparedChange, Path, Path]] = []
        cleanup_artifacts: list[Path] = []
        replaced: list[tuple[PreparedChange, Path]] = []
        restored_files: list[str] = []
        restore_errors: list[str] = []
        try:
            for change in prepared:
                current = revalidate_regular_file(change.token)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{current.path.name}.",
                    suffix=".opencode-lite.tmp",
                    dir=current.path.parent,
                )
                temporary = Path(temporary_name)
                cleanup_artifacts.append(temporary)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                    handle.write(change.new_content)
                    handle.flush()
                    os.fsync(handle.fileno())
                shutil.copymode(current.path, temporary)

                backup_descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{current.path.name}.",
                    suffix=".opencode-lite.backup",
                    dir=current.path.parent,
                )
                backup = Path(backup_name)
                cleanup_artifacts.append(backup)
                with os.fdopen(backup_descriptor, "wb") as backup_handle:
                    backup_handle.write(change.original_content)
                    backup_handle.flush()
                    os.fsync(backup_handle.fileno())
                shutil.copymode(current.path, backup)
                staged.append((change, temporary, backup))

            for change, temporary, backup in staged:
                current = revalidate_regular_file(change.token)
                if read_verified_bytes(current) != change.original_content:
                    raise PatchValidationError(
                        f"Patch target changed before commit: {change.path}"
                    )
                temporary.replace(current.path)
                replaced.append((change, backup))
                committed = validate_regular_file(workspace, change.path)
                if self._read_text(committed) != change.new_content:
                    raise PatchValidationError(
                        f"Patch target did not match prepared content: {change.path}"
                    )
        except Exception as exc:
            for change, backup in reversed(replaced):
                try:
                    current = validate_regular_file(workspace, change.path)
                    backup.replace(current.path)
                    restored = validate_regular_file(workspace, change.path)
                    if read_verified_bytes(restored) != change.original_content:
                        raise PatchValidationError("restored content hash did not match")
                    restored_files.append(change.path)
                except Exception as restore_exc:
                    restore_errors.append(f"{change.path}: {restore_exc}")
            raise PatchApplyError(
                f"Patch commit failed: {exc}",
                attempted_files=[change.path for change in prepared],
                replaced_files=[change.path for change, _ in replaced],
                restored_files=restored_files,
                restore_errors=restore_errors,
            ) from exc
        finally:
            for artifact in cleanup_artifacts:
                artifact.unlink(missing_ok=True)

        return [file_patch.path for file_patch in file_patches]

    def actual_diff(
        self,
        source_repo_path: str | Path,
        workspace_path: str | Path,
        target_files: list[str],
    ) -> str:
        source = self._validate_workspace(source_repo_path)
        workspace = self._validate_workspace(workspace_path)
        output: list[str] = []

        for relative_path in target_files:
            safe_path = self._normalize_patch_path(relative_path)
            source_file = self._validated_file(source, safe_path)
            workspace_file = self._validated_file(workspace, safe_path)
            source_lines = self._read_text(source_file).splitlines(keepends=True)
            workspace_lines = self._read_text(workspace_file).splitlines(keepends=True)
            output.extend(
                difflib.unified_diff(
                    source_lines,
                    workspace_lines,
                    fromfile=f"a/{safe_path}",
                    tofile=f"b/{safe_path}",
                )
            )
        return "".join(output)

    def _prepare_changes(
        self,
        workspace: Path,
        file_patches: list[FilePatch],
    ) -> list[PreparedChange]:
        prepared: list[PreparedChange] = []
        identities: set[tuple[int, int]] = set()
        for file_patch in file_patches:
            token = self._validated_file(workspace, file_patch.path)
            identity_key = (token.file_identity.device, token.file_identity.inode)
            if identity_key in identities:
                raise PatchValidationError(
                    f"Multiple patch paths resolve to the same file: {file_patch.path}"
                )
            identities.add(identity_key)
            try:
                original_content = read_verified_bytes(token)
            except FileSystemSafetyError as exc:
                raise PatchValidationError(str(exc)) from exc
            source_text = self._decode_text(original_content, file_patch.path)
            new_content = self._apply_hunks(source_text, file_patch)
            if new_content == source_text:
                raise PatchValidationError(
                    f"Patch does not change the target file: {file_patch.path}"
                )
            prepared.append(
                PreparedChange(
                    path=file_patch.path,
                    token=token,
                    original_content=original_content,
                    new_content=new_content,
                )
            )
        return prepared

    def _parse_hunk(self, lines: list[str], index: int) -> tuple[PatchHunk, int]:
        match = HUNK_HEADER.match(lines[index])
        if match is None:
            raise PatchValidationError(f"Invalid hunk header: {lines[index].strip()}")

        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        if old_count == 0 and new_count == 0:
            raise PatchValidationError("A hunk must contain a semantic file change.")
        index += 1
        hunk_lines: list[str] = []
        old_seen = 0
        new_seen = 0

        while old_seen < old_count or new_seen < new_count:
            if index >= len(lines):
                raise PatchValidationError("Hunk ended before its declared line counts.")
            patch_line = lines[index]
            if patch_line.startswith("\\ No newline at end of file"):
                raise PatchValidationError("No-newline markers are not supported in this alpha.")
            if not patch_line or patch_line[0] not in {" ", "+", "-"}:
                raise PatchValidationError(f"Invalid hunk line: {patch_line.strip()[:120]}")

            prefix = patch_line[0]
            if prefix in {" ", "-"}:
                old_seen += 1
            if prefix in {" ", "+"}:
                new_seen += 1
            if old_seen > old_count or new_seen > new_count:
                raise PatchValidationError("Hunk contains more lines than declared.")
            hunk_lines.append(patch_line)
            index += 1

        if index < len(lines) and lines[index].startswith("\\ No newline at end of file"):
            raise PatchValidationError("No-newline markers are not supported in this alpha.")

        return (
            PatchHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=tuple(hunk_lines),
            ),
            index,
        )

    def _apply_hunks(self, source_text: str, file_patch: FilePatch) -> str:
        source_lines = source_text.splitlines(keepends=True)
        newline = "\r\n" if "\r\n" in source_text else "\n"
        output: list[str] = []
        cursor = 0

        for hunk in file_patch.hunks:
            old_index = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
            if old_index < cursor or old_index > len(source_lines):
                raise PatchValidationError(
                    f"Hunk position is outside or overlaps in {file_patch.path}."
                )
            output.extend(source_lines[cursor:old_index])
            cursor = old_index
            new_index = hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1
            if new_index != len(output):
                raise PatchValidationError(
                    f"New hunk position is inconsistent in {file_patch.path}."
                )

            for patch_line in hunk.lines:
                prefix = patch_line[0]
                content = patch_line[1:]
                if prefix in {" ", "-"} and (
                    cursor >= len(source_lines)
                    or not self._same_line(source_lines[cursor], content)
                ):
                    raise PatchValidationError(
                        f"Hunk context does not match {file_patch.path} at line {cursor + 1}."
                    )
                if prefix == " ":
                    output.append(source_lines[cursor])
                    cursor += 1
                elif prefix == "-":
                    cursor += 1
                else:
                    output.append(self._convert_newline(content, newline))

        output.extend(source_lines[cursor:])
        result = "".join(output)
        if result == source_text:
            raise PatchValidationError(f"Patch is a semantic no-op for {file_patch.path}.")
        return result

    @staticmethod
    def _parse_header_path(raw_path: str) -> str:
        path = raw_path.rstrip("\r\n").split("\t", maxsplit=1)[0]
        if path.startswith('"'):
            raise PatchValidationError("Quoted Git paths are not supported in this alpha.")
        if any(ord(character) < 32 for character in path):
            raise PatchValidationError("Patch paths cannot contain control characters.")
        return path

    @staticmethod
    def _normalize_patch_path(raw_path: str) -> str:
        path = raw_path
        if path != path.strip():
            raise PatchValidationError(f"Patch path is not canonical: {raw_path}")
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if not path or "\x00" in path or "\\" in path or ":" in path:
            raise PatchValidationError(f"Patch path is not a safe relative path: {raw_path}")
        pure_path = PurePosixPath(path)
        if pure_path.is_absolute() or any(part in {"", ".", ".."} for part in pure_path.parts):
            raise PatchValidationError(f"Patch path is not a safe relative path: {raw_path}")
        normalized = pure_path.as_posix()
        if normalized != path:
            raise PatchValidationError(f"Patch path is not canonical: {raw_path}")
        return normalized

    @staticmethod
    def _path_key(path: str) -> str:
        return os.path.normcase(path).casefold() if os.name == "nt" else path

    @staticmethod
    def _validate_workspace(workspace_path: str | Path) -> Path:
        try:
            return validate_directory(workspace_path)
        except FileSystemSafetyError as exc:
            raise PatchValidationError(str(exc)) from exc

    @staticmethod
    def _validated_file(root: Path, relative_path: str) -> ValidatedFile:
        try:
            return validate_regular_file(root, relative_path)
        except FileSystemSafetyError as exc:
            raise PatchValidationError(str(exc)) from exc

    @staticmethod
    def _read_text(token: ValidatedFile) -> str:
        try:
            return read_verified_bytes(token).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchValidationError(
                f"Patch target is not UTF-8 text: {token.relative_path}"
            ) from exc
        except FileSystemSafetyError as exc:
            raise PatchValidationError(str(exc)) from exc

    @staticmethod
    def _decode_text(content: bytes, relative_path: str) -> str:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchValidationError(
                f"Patch target is not UTF-8 text: {relative_path}"
            ) from exc

    @staticmethod
    def _same_line(source_line: str, patch_line: str) -> bool:
        return source_line.rstrip("\r\n") == patch_line.rstrip("\r\n")

    @staticmethod
    def _convert_newline(line: str, newline: str) -> str:
        body = line.rstrip("\r\n")
        has_newline = line.endswith(("\r", "\n"))
        return body + newline if has_newline else body
