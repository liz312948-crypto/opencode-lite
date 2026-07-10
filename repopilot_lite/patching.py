from __future__ import annotations

import difflib
import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*?)(?:\r?\n)?$"
)


class PatchValidationError(ValueError):
    """Raised when a unified diff is malformed or violates a path boundary."""

    error_code = "PATCH_VALIDATION_FAILED"


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


def patch_content_hash(unified_diff: str) -> str:
    return hashlib.sha256(unified_diff.encode("utf-8")).hexdigest()


class PatchApplier:
    """Validates and applies a bounded subset of text unified diffs."""

    def parse(self, unified_diff: str) -> list[FilePatch]:
        if not unified_diff.strip():
            raise PatchValidationError("Patch content is empty.")
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
                raise PatchValidationError("File creation and deletion are not supported in this alpha.")
            normalized_old = self._normalize_patch_path(old_path)
            normalized_new = self._normalize_patch_path(new_path)
            if normalized_old != normalized_new:
                raise PatchValidationError("Patch renames are not supported.")
            if normalized_new in seen_paths:
                raise PatchValidationError(f"Duplicate file patch: {normalized_new}")

            hunks: list[PatchHunk] = []
            while index < len(lines) and lines[index].startswith("@@ "):
                hunk, index = self._parse_hunk(lines, index)
                hunks.append(hunk)

            if not hunks:
                raise PatchValidationError(f"Patch for {normalized_new} has no hunks.")

            seen_paths.add(normalized_new)
            file_patches.append(FilePatch(path=normalized_new, hunks=tuple(hunks)))

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

        temporary_files: list[Path] = []
        try:
            for target, content in prepared.items():
                temporary = target.with_name(f".{target.name}.opencode-lite.tmp")
                with temporary.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
                shutil.copymode(target, temporary)
                temporary_files.append(temporary)

            for target, temporary in zip(prepared, temporary_files, strict=True):
                temporary.replace(target)
        finally:
            for temporary in temporary_files:
                temporary.unlink(missing_ok=True)

        return [file_patch.path for file_patch in file_patches]

    def actual_diff(
        self,
        source_repo_path: str | Path,
        workspace_path: str | Path,
        target_files: list[str],
    ) -> str:
        source = Path(source_repo_path).expanduser().resolve()
        workspace = self._validate_workspace(workspace_path)
        output: list[str] = []

        for relative_path in target_files:
            safe_path = self._normalize_patch_path(relative_path)
            source_file = self._resolve_inside(source, safe_path)
            workspace_file = self._resolve_inside(workspace, safe_path)
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
    ) -> dict[Path, str]:
        prepared: dict[Path, str] = {}
        for file_patch in file_patches:
            target = self._resolve_inside(workspace, file_patch.path)
            if not target.exists() or not target.is_file():
                raise PatchValidationError(f"Patch target does not exist: {file_patch.path}")
            if target.is_symlink():
                raise PatchValidationError(f"Patch target cannot be a symlink: {file_patch.path}")
            source_text = self._read_text(target)
            prepared[target] = self._apply_hunks(source_text, file_patch)
        return prepared

    def _parse_hunk(self, lines: list[str], index: int) -> tuple[PatchHunk, int]:
        match = HUNK_HEADER.match(lines[index])
        if match is None:
            raise PatchValidationError(f"Invalid hunk header: {lines[index].strip()}")

        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
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

            for patch_line in hunk.lines:
                prefix = patch_line[0]
                content = patch_line[1:]
                if prefix in {" ", "-"}:
                    if cursor >= len(source_lines) or not self._same_line(
                        source_lines[cursor], content
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
        return "".join(output)

    @staticmethod
    def _parse_header_path(raw_path: str) -> str:
        path = raw_path.rstrip("\r\n").split("\t", maxsplit=1)[0]
        if path.startswith('"'):
            raise PatchValidationError("Quoted Git paths are not supported in this alpha.")
        return path

    @staticmethod
    def _normalize_patch_path(raw_path: str) -> str:
        path = raw_path.strip()
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if not path or "\\" in path or ":" in path:
            raise PatchValidationError(f"Patch path is not a safe relative path: {raw_path}")
        pure_path = PurePosixPath(path)
        if pure_path.is_absolute() or any(part in {"", ".", ".."} for part in pure_path.parts):
            raise PatchValidationError(f"Patch path is not a safe relative path: {raw_path}")
        return pure_path.as_posix()

    @staticmethod
    def _validate_workspace(workspace_path: str | Path) -> Path:
        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise PatchValidationError(f"Workspace does not exist: {workspace_path}")
        return workspace

    @staticmethod
    def _resolve_inside(root: Path, relative_path: str) -> Path:
        target = (root / relative_path).resolve()
        if target != root and root not in target.parents:
            raise PatchValidationError(f"Patch path escapes its root: {relative_path}")
        return target

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                return handle.read()
        except UnicodeDecodeError as exc:
            raise PatchValidationError(f"Patch target is not UTF-8 text: {path.name}") from exc

    @staticmethod
    def _same_line(source_line: str, patch_line: str) -> bool:
        return source_line.rstrip("\r\n") == patch_line.rstrip("\r\n")

    @staticmethod
    def _convert_newline(line: str, newline: str) -> str:
        body = line.rstrip("\r\n")
        has_newline = line.endswith(("\r", "\n"))
        return body + newline if has_newline else body
