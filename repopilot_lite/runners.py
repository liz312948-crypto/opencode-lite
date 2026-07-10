from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from repopilot_lite.models import CommandResult, CommandSpec


INHERITED_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)
SENSITIVE_ENV_PARTS = ("KEY", "PASSWORD", "SECRET", "TOKEN")


class CommandPolicyError(ValueError):
    """Raised when a command or cwd is outside the explicit execution policy."""

    error_code = "COMMAND_POLICY_VIOLATION"


class CommandRunner:
    """Runs one argv command with bounded output, time, cwd, and environment."""

    def __init__(self, workspace_root: str | Path, max_output_chars: int = 20_000) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        if not self.workspace_root.exists() or not self.workspace_root.is_dir():
            raise CommandPolicyError(f"Workspace does not exist: {workspace_root}")
        self.max_output_chars = max_output_chars

    def run(self, spec: CommandSpec) -> CommandResult:
        cwd = self._validate_cwd(spec.cwd)
        environment = self._build_environment(spec.env_overrides)
        started_at = datetime.now(timezone.utc)
        started = perf_counter()
        exit_code: int | None
        stdout = ""
        stderr = ""
        timed_out = False

        try:
            completed = subprocess.run(
                spec.argv,
                cwd=str(cwd),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.timeout_seconds,
                shell=False,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            timed_out = True
            stdout = self._coerce_output(exc.stdout)
            stderr = self._coerce_output(exc.stderr)
            timeout_message = f"Command timed out after {spec.timeout_seconds} seconds."
            stderr = f"{stderr}\n{timeout_message}".strip()
        except OSError as exc:
            exit_code = None
            stderr = f"Command could not start: {exc}"

        duration_ms = round((perf_counter() - started) * 1000)
        finished_at = datetime.now(timezone.utc)
        stdout, stdout_truncated = self._truncate(stdout)
        stderr, stderr_truncated = self._truncate(stderr)
        return CommandResult(
            argv=list(spec.argv),
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_truncated=stdout_truncated or stderr_truncated,
            duration_ms=duration_ms,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _validate_cwd(self, cwd_value: str) -> Path:
        cwd_path = Path(cwd_value).expanduser()
        if not cwd_path.is_absolute():
            cwd_path = self.workspace_root / cwd_path
        cwd = cwd_path.resolve()
        if cwd != self.workspace_root and self.workspace_root not in cwd.parents:
            raise CommandPolicyError("Command cwd must be inside the task workspace.")
        if not cwd.exists() or not cwd.is_dir():
            raise CommandPolicyError(f"Command cwd does not exist: {cwd}")
        return cwd

    @staticmethod
    def _build_environment(overrides: dict[str, str]) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if key.upper() in INHERITED_ENV_KEYS
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"

        for key, value in overrides.items():
            normalized = key.upper()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise CommandPolicyError(f"Invalid environment variable name: {key}")
            if any(part in normalized for part in SENSITIVE_ENV_PARTS):
                raise CommandPolicyError(f"Sensitive environment override is not allowed: {key}")
            environment[key] = value
        return environment

    def _truncate(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.max_output_chars:
            return value, False
        suffix = "\n...[output truncated]"
        return value[: self.max_output_chars - len(suffix)] + suffix, True

    @staticmethod
    def _coerce_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value


class TestRunner:
    """Selects one explicit, allowlisted test command and runs it once."""

    def select_command(
        self,
        workspace_path: str | Path,
        requested_argv: list[str] | None,
        timeout_seconds: int,
    ) -> CommandSpec:
        workspace = Path(workspace_path).expanduser().resolve()
        argv = list(requested_argv) if requested_argv is not None else self._detect(workspace)
        self._validate_argv(argv)
        return CommandSpec(
            argv=argv,
            cwd=str(workspace),
            timeout_seconds=timeout_seconds,
        )

    def run_tests(
        self,
        workspace_path: str | Path,
        requested_argv: list[str] | None,
        timeout_seconds: int,
    ) -> tuple[CommandSpec, CommandResult]:
        spec = self.select_command(workspace_path, requested_argv, timeout_seconds)
        return spec, CommandRunner(workspace_path).run(spec)

    @staticmethod
    def _detect(workspace: Path) -> list[str]:
        python_markers = (
            workspace / "pyproject.toml",
            workspace / "pytest.ini",
            workspace / "setup.cfg",
        )
        if (workspace / "tests").is_dir() and any(marker.exists() for marker in python_markers):
            return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

        package_json = workspace / "package.json"
        if package_json.is_file():
            try:
                package_data = json.loads(package_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise CommandPolicyError("package.json could not be parsed safely.") from exc
            scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
            if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
                return ["npm", "test"]

        raise CommandPolicyError(
            "No safe test command was provided or detected. Submit test_command explicitly."
        )

    @staticmethod
    def _validate_argv(argv: list[str]) -> None:
        if not argv or any(not item.strip() for item in argv):
            raise CommandPolicyError("Test command must contain non-empty argv values.")

        executable = Path(argv[0]).name.lower()
        if executable in {"pytest", "pytest.exe"}:
            return
        if executable in {"python", "python.exe", "python3", "python3.exe"}:
            if len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]:
                return
            raise CommandPolicyError("Python test commands must use 'python -m pytest'.")
        if executable in {"npm", "npm.cmd", "npm.exe"}:
            if len(argv) >= 2 and argv[1] == "test":
                return
            raise CommandPolicyError("npm test commands must use 'npm test'.")
        raise CommandPolicyError(
            "Unsupported test command. Allowed commands are pytest, python -m pytest, and npm test."
        )
