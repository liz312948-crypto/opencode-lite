from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repopilot_lite.filesystem_safety import (
    FileSystemSafetyError,
    WalkEntry,
    read_verified_bytes,
    validate_directory,
    validate_regular_file,
    walk_tree_no_follow,
)
from repopilot_lite.llm_client import OptionalLLMSummarizer

IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build"}
TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    handler: Callable[..., dict[str, Any]]


class ToolRegistry:
    """Registers tool handlers and exposes one invocation point for the executor."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        handler: Callable[..., dict[str, Any]],
    ) -> None:
        self._tools[name] = RegisteredTool(name=name, description=description, handler=handler)

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Tool is not registered: {name}")
        return tool.handler(**kwargs)

    def list_tools(self) -> list[dict[str, str]]:
        return [
            {"name": tool.name, "description": tool.description} for tool in self._tools.values()
        ]


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("list_files", "List readable files in a repository.", list_files)
    registry.register(
        "read_file", "Read a text file from a repository by relative path.", read_file
    )
    registry.register("search_text", "Search text keywords inside repository files.", search_text)
    registry.register(
        "summarize_repo",
        "Generate repository understanding and modification planning output.",
        summarize_repo,
    )
    return registry


def list_files(repo_path: str, max_files: int = 200) -> dict[str, Any]:
    repo = _resolve_repo(repo_path)
    files: list[str] = []

    for entry in _safe_repo_entries(repo):
        if len(files) >= max_files:
            break
        if entry.file_type != "file":
            continue
        files.append(entry.relative_path)

    return {"files": files, "count": len(files), "truncated": len(files) >= max_files}


def read_file(repo_path: str, file_path: str, max_chars: int = 12000) -> dict[str, Any]:
    repo = _resolve_repo(repo_path)
    try:
        token = validate_regular_file(repo, file_path)
        content = read_verified_bytes(token)
    except FileSystemSafetyError as exc:
        raise ValueError(str(exc)) from exc
    text = content.decode("utf-8", errors="ignore")
    return {
        "file_path": token.relative_path,
        "content": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def search_text(repo_path: str, keywords: list[str], max_matches: int = 50) -> dict[str, Any]:
    repo = _resolve_repo(repo_path)
    normalized_keywords = [item.lower() for item in keywords if item.strip()]
    matches: list[dict[str, Any]] = []

    if not normalized_keywords:
        return {"matches": matches, "count": 0, "keywords": []}

    for entry in _safe_repo_entries(repo):
        if len(matches) >= max_matches:
            break
        if entry.file_type != "file" or not _looks_text_file(entry.path):
            continue

        try:
            token = validate_regular_file(repo, entry.relative_path)
            lines = read_verified_bytes(token).decode("utf-8", errors="ignore").splitlines()
        except (FileSystemSafetyError, OSError):
            continue

        for line_number, line in enumerate(lines, start=1):
            lowered = line.lower()
            hit_keywords = [word for word in normalized_keywords if word in lowered]
            if not hit_keywords:
                continue
            matches.append(
                {
                    "file_path": entry.relative_path,
                    "line_number": line_number,
                    "line": line.strip()[:300],
                    "keywords": hit_keywords,
                }
            )
            if len(matches) >= max_matches:
                break

    return {"matches": matches, "count": len(matches), "keywords": normalized_keywords}


def summarize_repo(
    repo_path: str,
    question: str,
    files: list[str],
    readme: str | None = None,
    search_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    repo = _resolve_repo(repo_path)
    search_matches = search_matches or []
    key_files = _select_key_files(files, search_matches)

    llm_result = OptionalLLMSummarizer().summarize(
        repo_name=repo.name,
        question=question,
        files=files,
        readme=readme,
        search_matches=search_matches,
    )
    if llm_result is not None:
        return _normalize_summary_result(llm_result, key_files)

    readme_summary = (
        _compact_text(readme, max_chars=600) if readme else "No README content was found."
    )
    summary = (
        f"Repository '{repo.name}' contains {len(files)} indexed files "
        "and is ready for modification planning. "
        f"The question is: {question}. "
        f"README signal: {readme_summary}"
    )

    modification_plan = _build_modification_plan(question, key_files)
    risk_notes = [
        "This prototype only plans changes; it does not edit files automatically.",
        "Review target files manually before applying any suggested modification.",
        "Add or update tests before changing behavior in shared modules.",
    ]
    suggestions = [
        "Start with README and project configuration files to confirm setup "
        "and runtime assumptions.",
        "Inspect the listed key files before changing behavior.",
        "Add focused tests around the requested behavior before making code changes.",
    ]

    if search_matches:
        suggestions.insert(
            1, "Review keyword matches because they are the most direct links to the question."
        )

    return {
        "repo_summary": summary,
        "key_files": key_files,
        "modification_plan": modification_plan,
        "risk_notes": risk_notes,
        "llm_used": False,
        "suggestions": suggestions,
    }


def _resolve_repo(repo_path: str) -> Path:
    try:
        return validate_directory(repo_path)
    except FileSystemSafetyError as exc:
        raise FileNotFoundError(
            f"Repository path does not exist or is not a directory: {repo_path}"
        ) from exc


def _resolve_inside_repo(repo: Path, file_path: str) -> Path:
    try:
        return validate_regular_file(repo, file_path).path
    except FileSystemSafetyError as exc:
        raise ValueError(str(exc)) from exc


def _safe_repo_entries(repo: Path) -> Iterator[WalkEntry]:
    try:
        yield from walk_tree_no_follow(repo, frozenset(IGNORED_DIRS))
    except FileSystemSafetyError as exc:
        raise ValueError(str(exc)) from exc


def _looks_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    return path.name.lower() in {"readme", "dockerfile", "makefile"}


def _read_text_safely(path: Path, max_chars: int) -> str:
    token = validate_regular_file(path.parent, path.name)
    text = read_verified_bytes(token).decode("utf-8", errors="ignore")
    return text[:max_chars]


def _select_key_files(files: list[str], search_matches: list[dict[str, Any]]) -> list[str]:
    weighted: list[str] = []
    for match in search_matches:
        file_path = str(match.get("file_path", ""))
        if file_path and file_path not in weighted:
            weighted.append(file_path)

    priority_names = ("README", "pyproject.toml", "requirements.txt", "main.py", "app.py")
    for file_path in files:
        name = Path(file_path).name
        if (
            any(name.startswith(priority) or name == priority for priority in priority_names)
            and file_path not in weighted
        ):
            weighted.append(file_path)

    for file_path in files[:10]:
        if file_path not in weighted:
            weighted.append(file_path)

    return weighted[:10]


def _build_modification_plan(question: str, key_files: list[str]) -> list[dict[str, Any]]:
    primary_files = key_files[:3] or ["README.md"]
    test_files = [file_path for file_path in key_files if "test" in file_path.lower()]
    if not test_files:
        test_files = ["tests/"]

    return [
        {
            "title": "Confirm existing behavior",
            "target_files": primary_files,
            "action": "Read the key files and map the current request flow before editing.",
            "reason": (
                f"The question '{question}' should be grounded in the current implementation first."
            ),
        },
        {
            "title": "Design the smallest code change",
            "target_files": primary_files,
            "action": "Identify the minimal functions or modules that need updates.",
            "reason": (
                "Keeping the change narrow lowers regression risk and preserves "
                "the existing architecture."
            ),
        },
        {
            "title": "Add verification coverage",
            "target_files": test_files,
            "action": "Add or update focused tests for the planned behavior.",
            "reason": "Tests make the modification plan executable and easier to review.",
        },
    ]


def _normalize_summary_result(
    result: dict[str, Any], fallback_key_files: list[str]
) -> dict[str, Any]:
    return {
        "repo_summary": str(result.get("repo_summary") or "LLM summary was unavailable."),
        "key_files": _string_list(result.get("key_files")) or fallback_key_files,
        "modification_plan": _modification_plan_list(
            result.get("modification_plan"), fallback_key_files
        ),
        "risk_notes": _string_list(result.get("risk_notes"))
        or ["LLM output should be reviewed before applying any code changes."],
        "llm_used": bool(result.get("llm_used")),
        "suggestions": _string_list(result.get("suggestions"))
        or ["Review key files and tests before making changes."],
    }


def _modification_plan_list(value: Any, fallback_key_files: list[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return _build_modification_plan("requested change", fallback_key_files)

    steps: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        steps.append(
            {
                "title": str(item.get("title") or "Review planned change"),
                "target_files": _string_list(item.get("target_files")) or fallback_key_files[:3],
                "action": str(item.get("action") or "Inspect the target files."),
                "reason": str(
                    item.get("reason") or "The step supports safer modification planning."
                ),
            }
        )

    fallback_steps = _build_modification_plan("requested change", fallback_key_files)
    while len(steps) < 3:
        steps.append(fallback_steps[len(steps)])
    return steps


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _compact_text(text: str | None, max_chars: int) -> str:
    if not text:
        return ""
    compacted = " ".join(text.split())
    if len(compacted) <= max_chars:
        return compacted
    return compacted[: max_chars - 3] + "..."
