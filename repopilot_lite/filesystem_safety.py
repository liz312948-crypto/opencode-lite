from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ManifestEntry = dict[str, str | int | None]
WorkspaceManifest = dict[str, ManifestEntry]


class FileSystemSafetyError(ValueError):
    """Raised when a path cannot be used without following filesystem links."""


@dataclass(frozen=True)
class FileSystemIdentity:
    device: int
    inode: int
    file_type: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileSystemIdentity:
        return cls(value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))


@dataclass(frozen=True)
class ValidatedFile:
    root: Path
    path: Path
    relative_path: str
    root_identity: FileSystemIdentity
    parent_identity: FileSystemIdentity
    file_identity: FileSystemIdentity


@dataclass(frozen=True)
class WalkEntry:
    path: Path
    relative_path: str
    file_type: str
    identity: FileSystemIdentity
    size: int


def is_link_or_reparse(path: str | Path, value: os.stat_result | None = None) -> bool:
    candidate = Path(path)
    try:
        metadata = value or candidate.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse_flag and attributes & reparse_flag:
        return True
    is_junction = getattr(candidate, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def absolute_lexical(path: str | Path) -> Path:
    raw = os.fspath(Path(path).expanduser())
    if "\x00" in raw:
        raise FileSystemSafetyError("Filesystem paths cannot contain null bytes.")
    if os.name == "nt":
        normalized = raw.replace("/", "\\")
        if normalized.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
            raise FileSystemSafetyError(
                "UNC and Windows device namespace paths are not supported."
            )
        drive, tail = os.path.splitdrive(normalized)
        if drive and not tail.startswith(("\\", "/")):
            raise FileSystemSafetyError("Windows drive-relative paths are not supported.")
    return Path(os.path.abspath(raw))


def validate_directory(path: str | Path) -> Path:
    lexical = absolute_lexical(path)
    if not os.path.lexists(lexical):
        raise FileSystemSafetyError(f"Directory does not exist: {lexical}")
    reject_link_components(lexical)
    try:
        metadata = lexical.stat()
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise FileSystemSafetyError(
            f"Directory could not be resolved safely: {lexical}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise FileSystemSafetyError(f"Path is not a directory: {lexical}")
    if is_link_or_reparse(resolved):
        raise FileSystemSafetyError(
            f"Directory cannot be a link or reparse point: {lexical}"
        )
    return resolved


def normalize_relative_path(relative_path: str) -> str:
    if (
        not relative_path
        or "\x00" in relative_path
        or "\\" in relative_path
        or ":" in relative_path
    ):
        raise FileSystemSafetyError(
            f"Path is not a safe relative POSIX path: {relative_path}"
        )
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise FileSystemSafetyError(
            f"Path is not a safe relative POSIX path: {relative_path}"
        )
    normalized = pure.as_posix()
    if normalized != relative_path:
        raise FileSystemSafetyError(f"Path is not canonical: {relative_path}")
    return normalized


def validate_regular_file(root: str | Path, relative_path: str) -> ValidatedFile:
    safe_root = validate_directory(root)
    normalized = normalize_relative_path(relative_path)
    lexical_target = safe_root.joinpath(*PurePosixPath(normalized).parts)
    reject_link_components(lexical_target, stop_at=safe_root)
    try:
        root_stat = safe_root.stat()
        parent_stat = lexical_target.parent.stat()
        file_stat = lexical_target.stat()
        resolved_target = lexical_target.resolve(strict=True)
    except OSError as exc:
        raise FileSystemSafetyError(
            f"File does not exist or could not be resolved: {normalized}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise FileSystemSafetyError(f"Path is not a regular file: {normalized}")
    if is_link_or_reparse(lexical_target) or is_link_or_reparse(resolved_target):
        raise FileSystemSafetyError(
            f"File cannot be a link or reparse point: {normalized}"
        )
    if not is_lexically_within(resolved_target, safe_root):
        raise FileSystemSafetyError(f"File path escapes its root: {normalized}")
    return ValidatedFile(
        root=safe_root,
        path=resolved_target,
        relative_path=normalized,
        root_identity=FileSystemIdentity.from_stat(root_stat),
        parent_identity=FileSystemIdentity.from_stat(parent_stat),
        file_identity=FileSystemIdentity.from_stat(file_stat),
    )


def revalidate_regular_file(token: ValidatedFile) -> ValidatedFile:
    current = validate_regular_file(token.root, token.relative_path)
    if (
        current.root_identity != token.root_identity
        or current.parent_identity != token.parent_identity
        or current.file_identity != token.file_identity
    ):
        raise FileSystemSafetyError(
            f"File identity changed during validation: {token.relative_path}"
        )
    return current


def read_verified_bytes(token: ValidatedFile) -> bytes:
    current = revalidate_regular_file(token)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(current.path, flags)
    except OSError as exc:
        raise FileSystemSafetyError(
            f"File could not be opened safely: {token.relative_path}"
        ) from exc
    try:
        before = FileSystemIdentity.from_stat(os.fstat(descriptor))
        if before != token.file_identity:
            raise FileSystemSafetyError(
                f"File identity changed before read: {token.relative_path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = FileSystemIdentity.from_stat(os.fstat(descriptor))
        if after != before:
            raise FileSystemSafetyError(
                f"File identity changed during read: {token.relative_path}"
            )
    finally:
        os.close(descriptor)
    revalidate_regular_file(token)
    return b"".join(chunks)


def walk_tree_no_follow(
    root_path: str | Path,
    ignored_names: frozenset[str] = frozenset(),
) -> Iterator[WalkEntry]:
    root = validate_directory(root_path)
    root_identity = FileSystemIdentity.from_stat(root.stat())

    def visit(directory: Path, relative_parent: PurePosixPath) -> Iterator[WalkEntry]:
        expected_directory = FileSystemIdentity.from_stat(directory.stat())
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise FileSystemSafetyError(
                f"Directory could not be scanned safely: {directory}"
            ) from exc
        if FileSystemIdentity.from_stat(directory.stat()) != expected_directory:
            raise FileSystemSafetyError(
                f"Directory identity changed during scan: {directory}"
            )

        for entry in entries:
            candidate = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise FileSystemSafetyError(
                    f"Tree entry could not be inspected: {candidate}"
                ) from exc
            if is_link_or_reparse(candidate, metadata):
                raise FileSystemSafetyError(
                    f"Links and reparse points are not allowed: {candidate.name}"
                )
            if entry.name in ignored_names:
                continue
            relative = relative_parent / entry.name
            relative_text = relative.as_posix()
            identity = FileSystemIdentity.from_stat(metadata)
            if stat.S_ISDIR(metadata.st_mode):
                yield WalkEntry(candidate, relative_text, "directory", identity, 0)
                yield from visit(candidate, relative)
            elif stat.S_ISREG(metadata.st_mode):
                yield WalkEntry(
                    candidate,
                    relative_text,
                    "file",
                    identity,
                    metadata.st_size,
                )
            else:
                raise FileSystemSafetyError(
                    f"Special filesystem entries are not supported: {relative_text}"
                )

        if FileSystemIdentity.from_stat(directory.stat()) != expected_directory:
            raise FileSystemSafetyError(
                f"Directory identity changed during traversal: {directory}"
            )

    yield from visit(root, PurePosixPath())
    if FileSystemIdentity.from_stat(root.stat()) != root_identity:
        raise FileSystemSafetyError(
            f"Root directory identity changed during traversal: {root}"
        )


def build_manifest(
    root_path: str | Path,
    ignored_names: frozenset[str] = frozenset(),
) -> WorkspaceManifest:
    root = validate_directory(root_path)
    manifest: WorkspaceManifest = {}
    for entry in walk_tree_no_follow(root, ignored_names):
        if entry.file_type == "directory":
            manifest[entry.relative_path] = {
                "type": "directory",
                "size": 0,
                "sha256": None,
            }
            continue
        token = validate_regular_file(root, entry.relative_path)
        content = read_verified_bytes(token)
        manifest[entry.relative_path] = {
            "type": "file",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return manifest


def manifest_hash(manifest: WorkspaceManifest) -> str:
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reject_link_components(path: Path, *, stop_at: Path | None = None) -> None:
    lexical = absolute_lexical(path)
    stop = absolute_lexical(stop_at) if stop_at is not None else None
    started = stop is None
    for component in path_components(lexical):
        if stop is not None and component == stop:
            started = True
        if not started:
            continue
        if not os.path.lexists(component):
            raise FileSystemSafetyError(f"Path component does not exist: {component}")
        if is_link_or_reparse(component):
            raise FileSystemSafetyError(
                f"Path component cannot be a link or reparse point: {component}"
            )


def reject_existing_link_components(path: Path) -> None:
    lexical = absolute_lexical(path)
    for component in path_components(lexical):
        if os.path.lexists(component) and is_link_or_reparse(component):
            raise FileSystemSafetyError(
                f"Path component cannot be a link or reparse point: {component}"
            )


def path_components(path: Path) -> list[Path]:
    components: list[Path] = []
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        components.append(current)
    return components


def same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )


def is_lexically_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
