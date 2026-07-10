from __future__ import annotations

from pathlib import Path
from typing import Any

from repopilot_lite.models import StepLog, TaskRecord, TaskResult, TaskStatus
from repopilot_lite.storage import Storage
from repopilot_lite.tools import ToolRegistry


class Executor:
    """Runs planned steps and stores observable logs for each step."""

    def __init__(self, registry: ToolRegistry, storage: Storage) -> None:
        self.registry = registry
        self.storage = storage

    def run(self, task: TaskRecord) -> TaskRecord:
        context: dict[str, Any] = {
            "files": [],
            "readme": None,
            "search_matches": [],
            "summary_result": None,
        }
        self.storage.transition_status(task, TaskStatus.RUNNING)

        try:
            for step in task.plan:
                self._log(task, step.name, "STARTED", step.description)
                output = self._run_step(task, step.name, step.args, context)
                self._log(task, step.name, "SUCCESS", "Step completed.", self._preview(output))

            result = context["summary_result"]
            if result is None:
                result = self.registry.call(
                    "summarize_repo",
                    repo_path=task.repo_path,
                    question=task.question,
                    files=context["files"],
                    readme=context["readme"],
                    search_matches=context["search_matches"],
                )
            task.result = TaskResult.model_validate(result)
            self.storage.transition_status(task, TaskStatus.SUCCESS)
        except Exception as exc:
            self._log(task, "executor", "FAILED", str(exc))
            self.storage.transition_status(
                task,
                TaskStatus.FAILED,
                error_code="EXECUTOR_FAILED",
                error_message=str(exc),
            )

        return self.storage.get_task(task.task_id) or task

    def _run_step(
        self,
        task: TaskRecord,
        step_name: str,
        args: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if step_name == "list_files":
            output = self.registry.call("list_files", repo_path=task.repo_path, **args)
            context["files"] = output["files"]
            return output

        if step_name == "read_readme":
            readme_path = self._find_readme(context["files"])
            if readme_path is None:
                return {"file_path": None, "content": None, "message": "README not found."}
            output = self.registry.call("read_file", repo_path=task.repo_path, file_path=readme_path)
            context["readme"] = output["content"]
            return output

        if step_name == "search_keywords":
            output = self._search_with_retry(task, args)
            context["search_matches"] = output["matches"]
            return output

        if step_name == "summarize":
            output = self.registry.call(
                "summarize_repo",
                repo_path=task.repo_path,
                question=task.question,
                files=context["files"],
                readme=context["readme"],
                search_matches=context["search_matches"],
            )
            context["summary_result"] = output
            return output

        raise ValueError(f"Unknown plan step: {step_name}")

    def _search_with_retry(self, task: TaskRecord, args: dict[str, Any]) -> dict[str, Any]:
        keywords = list(args.get("keywords", []))
        max_retries = 2
        output = self.registry.call("search_text", repo_path=task.repo_path, keywords=keywords)
        attempts = [{"keywords": output.get("keywords", keywords), "count": output.get("count", 0)}]

        for retry_number in range(1, max_retries + 1):
            if output.get("matches"):
                break

            keywords = self._broaden_keywords(task.question, keywords, retry_number)
            self._log(
                task,
                "search_keywords",
                "RETRY",
                "No matches found; retrying with broader keywords.",
                {"retry": retry_number, "keywords": keywords},
            )
            output = self.registry.call("search_text", repo_path=task.repo_path, keywords=keywords)
            attempts.append({"keywords": output.get("keywords", keywords), "count": output.get("count", 0)})

        output["attempts"] = attempts
        output["retries"] = len(attempts) - 1
        return output

    def _log(
        self,
        task: TaskRecord,
        step: str,
        status: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.storage.add_log(
            StepLog(
                task_id=task.task_id,
                step=step,
                status=status,
                message=message,
                data=data or {},
            )
        )

    @staticmethod
    def _find_readme(files: list[str]) -> str | None:
        for file_path in files:
            name = Path(file_path).name.lower()
            if name == "readme" or name.startswith("readme."):
                return file_path
        return None

    @staticmethod
    def _broaden_keywords(question: str, previous_keywords: list[str], retry_number: int) -> list[str]:
        broad_terms = ["api", "task", "repo", "file", "test", "readme"]
        if retry_number == 2:
            broad_terms.extend(["main", "config", "storage", "tool", "executor", "planner"])

        words = list(previous_keywords)
        for raw_word in question.replace("_", " ").replace("-", " ").split():
            word = "".join(char for char in raw_word.lower() if char.isalnum())
            if len(word) >= 4 and word not in words:
                words.append(word)
        for term in broad_terms:
            if term not in words:
                words.append(term)
        return words[:12]

    @staticmethod
    def _preview(output: dict[str, Any]) -> dict[str, Any]:
        preview = dict(output)
        if "content" in preview and isinstance(preview["content"], str):
            preview["content"] = preview["content"][:500]
        if "files" in preview and isinstance(preview["files"], list):
            preview["files"] = preview["files"][:20]
        if "matches" in preview and isinstance(preview["matches"], list):
            preview["matches"] = preview["matches"][:10]
        return preview
