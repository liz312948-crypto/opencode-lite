from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Thread
from time import perf_counter
from typing import BinaryIO

from repopilot_lite.filesystem_safety import (
    FileSystemSafetyError,
    is_lexically_within,
    same_file,
    validate_directory,
)
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


@dataclass
class _BoundedCapture:
    limit: int
    content: bytearray = field(default_factory=bytearray)
    discarded: int = 0
    error: str | None = None

    def drain(self, stream: BinaryIO) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                remaining = max(0, self.limit - len(self.content))
                accepted = min(len(chunk), remaining)
                self.content.extend(chunk[:accepted])
                self.discarded += len(chunk) - accepted
        except (OSError, ValueError) as exc:
            self.error = str(exc)


class CommandRunner:
    """Runs one argv command with bounded output, time, cwd, and environment."""

    def __init__(
        self,
        workspace_root: str | Path,
        max_output_chars: int = 20_000,
        cleanup_grace_seconds: float = 3.0,
    ) -> None:
        try:
            self.workspace_root = validate_directory(workspace_root)
        except FileSystemSafetyError as exc:
            raise CommandPolicyError(str(exc)) from exc
        if max_output_chars < 32:
            raise CommandPolicyError("Command output limit must be at least 32 bytes.")
        self.max_output_bytes = max_output_chars
        self.cleanup_grace_seconds = cleanup_grace_seconds

    def run(self, spec: CommandSpec) -> CommandResult:
        cwd = self._validate_cwd(spec.cwd)
        environment = self._build_environment(spec.env_overrides)
        started_at = datetime.now(UTC)
        started = perf_counter()
        exit_code: int | None = None
        stdout_capture = _BoundedCapture(self.max_output_bytes)
        stderr_capture = _BoundedCapture(self.max_output_bytes)
        timed_out = False
        process_tree_terminated: bool | None = None
        termination_error: str | None = None
        process: subprocess.Popen[bytes] | None = None
        reader_threads: list[Thread] = []

        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=(
                    int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                    if os.name == "nt"
                    else 0
                ),
            )
            assert process.stdout is not None
            assert process.stderr is not None
            reader_threads = [
                Thread(
                    target=stdout_capture.drain,
                    args=(process.stdout,),
                    name="opencode-lite-stdout",
                    daemon=True,
                ),
                Thread(
                    target=stderr_capture.drain,
                    args=(process.stderr,),
                    name="opencode-lite-stderr",
                    daemon=True,
                ),
            ]
            for thread in reader_threads:
                thread.start()
            try:
                exit_code = process.wait(timeout=spec.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process_tree_terminated, termination_error = self._terminate_process_tree(process)
                exit_code = None
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            termination_error = str(exc)
        except OSError as exc:
            message = f"Command could not start: {exc}".encode("utf-8", errors="replace")
            stderr_capture.content.extend(message[: self.max_output_bytes])
            stderr_capture.discarded += max(0, len(message) - self.max_output_bytes)
        finally:
            if process is not None:
                for thread in reader_threads:
                    thread.join(timeout=self.cleanup_grace_seconds)
                alive_threads = [thread for thread in reader_threads if thread.is_alive()]
                if alive_threads:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
                    for thread in alive_threads:
                        thread.join(timeout=0.25)
                    if any(thread.is_alive() for thread in alive_threads):
                        process_tree_terminated = False
                        termination_error = termination_error or "Output pipes did not close."

        stdout = stdout_capture.content.decode("utf-8", errors="replace")
        stderr = stderr_capture.content.decode("utf-8", errors="replace")
        if timed_out:
            timeout_message = f"Command timed out after {spec.timeout_seconds} seconds."
            stderr = f"{stderr}\n{timeout_message}".strip()
        if termination_error:
            stderr = f"{stderr}\nProcess cleanup issue: {termination_error}".strip()
        stdout, stdout_truncated = self._truncate_decoded(stdout, stdout_capture.discarded)
        stderr, stderr_truncated = self._truncate_decoded(stderr, stderr_capture.discarded)

        duration_ms = round((perf_counter() - started) * 1000)
        finished_at = datetime.now(UTC)
        return CommandResult(
            argv=list(spec.argv),
            cwd=str(cwd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_truncated=stdout_truncated or stderr_truncated,
            process_tree_terminated=process_tree_terminated,
            termination_error=termination_error,
            stdout_bytes_discarded=stdout_capture.discarded,
            stderr_bytes_discarded=stderr_capture.discarded,
            duration_ms=duration_ms,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _validate_cwd(self, cwd_value: str) -> Path:
        cwd_path = Path(cwd_value).expanduser()
        if not cwd_path.is_absolute():
            cwd_path = self.workspace_root / cwd_path
        try:
            cwd = validate_directory(cwd_path)
        except FileSystemSafetyError as exc:
            raise CommandPolicyError(str(exc)) from exc
        if not is_lexically_within(cwd, self.workspace_root):
            raise CommandPolicyError("Command cwd must be inside the task workspace.")
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

    def _truncate_decoded(self, value: str, discarded: int) -> tuple[str, bool]:
        if discarded == 0 and len(value) <= self.max_output_bytes:
            return value, False
        suffix = "\n...[output truncated]"
        return value[: self.max_output_bytes - len(suffix)] + suffix, True

    def _terminate_process_tree(
        self,
        process: subprocess.Popen[bytes],
    ) -> tuple[bool, str | None]:
        if process.poll() is not None:
            return True, None
        if os.name == "nt":
            return self._terminate_windows_tree(process)
        return self._terminate_posix_group(process)

    def _terminate_posix_group(
        self,
        process: subprocess.Popen[bytes],
    ) -> tuple[bool, str | None]:
        errors: list[str] = []
        kill_group = getattr(os, "killpg", None)
        if kill_group is None:
            return False, "POSIX process-group termination is unavailable."
        try:
            kill_group(process.pid, signal.SIGTERM)
        except OSError as exc:
            errors.append(f"SIGTERM failed: {exc}")
        try:
            process.wait(timeout=min(1.0, self.cleanup_grace_seconds))
        except subprocess.TimeoutExpired:
            try:
                kill_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError as exc:
                errors.append(f"SIGKILL failed: {exc}")
            try:
                process.wait(timeout=self.cleanup_grace_seconds)
            except subprocess.TimeoutExpired:
                errors.append("Process group did not exit before cleanup deadline.")
        return process.poll() is not None and not errors, "; ".join(errors) or None

    def _terminate_windows_tree(
        self,
        process: subprocess.Popen[bytes],
    ) -> tuple[bool, str | None]:
        try:
            completed = subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.cleanup_grace_seconds,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            process.kill()
            return False, f"Windows process-tree termination failed: {exc}"
        try:
            process.wait(timeout=self.cleanup_grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            return False, "Windows process tree did not exit before cleanup deadline."
        if completed.returncode != 0:
            detail = (completed.stderr or "taskkill returned a failure status").strip()
            return False, detail[:500]
        return True, None


class TestRunner:
    """Selects one explicit, allowlisted test command and runs it once."""

    def select_command(
        self,
        workspace_path: str | Path,
        requested_argv: list[str] | None,
        timeout_seconds: int,
    ) -> CommandSpec:
        try:
            workspace = validate_directory(workspace_path)
        except FileSystemSafetyError as exc:
            raise CommandPolicyError(str(exc)) from exc
        argv = list(requested_argv) if requested_argv is not None else self._detect(workspace)
        argv = self._normalize_argv(argv)
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
    def _normalize_argv(argv: list[str]) -> list[str]:
        if not argv or any(not item.strip() for item in argv):
            raise CommandPolicyError("Test command must contain non-empty argv values.")

        executable = Path(argv[0]).name.lower()
        if executable in {"pytest", "pytest.exe"}:
            if TestRunner._has_path_syntax(argv[0]):
                raise CommandPolicyError("pytest must use the configured Python interpreter.")
            TestRunner._validate_argument_paths(argv[1:])
            return [sys.executable, "-m", "pytest", *argv[1:]]
        if executable in {"python", "python.exe", "python3", "python3.exe"}:
            if len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]:
                TestRunner._validate_python_executable(argv[0])
                TestRunner._validate_argument_paths(argv[3:])
                return [sys.executable, "-m", "pytest", *argv[3:]]
            raise CommandPolicyError("Python test commands must use 'python -m pytest'.")
        if executable in {"npm", "npm.cmd", "npm.exe"}:
            if len(argv) >= 2 and argv[1] == "test":
                TestRunner._validate_argument_paths(argv[2:])
                return [TestRunner._trusted_npm(argv[0]), *argv[1:]]
            raise CommandPolicyError("npm test commands must use 'npm test'.")
        raise CommandPolicyError(
            "Unsupported test command. Allowed commands are pytest, python -m pytest, and npm test."
        )

    @staticmethod
    def _validate_python_executable(executable: str) -> None:
        if not TestRunner._has_path_syntax(executable):
            return
        candidate = Path(executable).expanduser()
        try:
            if not same_file(candidate, Path(sys.executable)):
                raise CommandPolicyError(
                    "Python test commands must use the configured Python interpreter."
                )
        except OSError as exc:
            raise CommandPolicyError(
                "Python test executable could not be verified."
            ) from exc

    @staticmethod
    def _trusted_npm(executable: str) -> str:
        trusted = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if trusted is None:
            raise CommandPolicyError("A trusted npm executable is not available.")
        if TestRunner._has_path_syntax(executable):
            try:
                if not same_file(Path(executable).expanduser(), Path(trusted)):
                    raise CommandPolicyError(
                        "npm test commands must use the configured npm executable."
                    )
            except OSError as exc:
                raise CommandPolicyError("npm executable could not be verified.") from exc
        return trusted

    @staticmethod
    def _has_path_syntax(executable: str) -> bool:
        return (
            Path(executable).is_absolute()
            or "/" in executable
            or "\\" in executable
            or bool(re.match(r"^[A-Za-z]:", executable))
        )

    @staticmethod
    def _validate_argument_paths(arguments: list[str]) -> None:
        for argument in arguments:
            if "\x00" in argument:
                raise CommandPolicyError("Test command arguments cannot contain null bytes.")
            candidate = argument.split("=", maxsplit=1)[-1] if "=" in argument else argument
            normalized = candidate.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            has_windows_drive = bool(re.match(r"^[A-Za-z]:", candidate))
            if PurePosixPath(normalized).is_absolute() or has_windows_drive or ".." in parts:
                raise CommandPolicyError(
                    "Test command arguments cannot reference paths outside the workspace."
                )
