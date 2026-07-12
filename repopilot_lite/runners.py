from __future__ import annotations

import ctypes
import ctypes.wintypes
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
from time import perf_counter, sleep
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


@dataclass
class _WindowsJob:
    """Best-effort Windows Job Object that kills all assigned processes on close."""

    handle: int
    closed: bool = False

    @classmethod
    def create(cls) -> tuple[_WindowsJob | None, str | None]:
        if os.name != "nt":
            return None, None
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return None, "Windows Job Objects are unavailable."
        kernel32 = loader("kernel32", use_last_error=True)

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.wintypes.DWORD),
                ("SchedulingClass", ctypes.wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        create_job.restype = ctypes.wintypes.HANDLE
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
        ]
        set_information.restype = ctypes.wintypes.BOOL
        handle = create_job(None, None)
        if not handle:
            return None, cls._last_error("CreateJobObjectW failed")

        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not set_information(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = cls._last_error("SetInformationJobObject failed")
            cls._close_handle(int(handle))
            return None, error
        return cls(handle=int(handle)), None

    def assign_suspended(self, process: subprocess.Popen[bytes]) -> str | None:
        loader = getattr(ctypes, "WinDLL", None)
        process_handle = getattr(process, "_handle", None)
        if loader is None or process_handle is None:
            return "Windows process handle is unavailable."
        kernel32 = loader("kernel32", use_last_error=True)
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE]
        assign_process.restype = ctypes.wintypes.BOOL
        if not assign_process(
            ctypes.wintypes.HANDLE(self.handle),
            ctypes.wintypes.HANDLE(int(process_handle)),
        ):
            return self._last_error("AssignProcessToJobObject failed")
        return self._resume_primary_thread(process.pid)

    def terminate_and_wait(
        self,
        process: subprocess.Popen[bytes],
        timeout_seconds: float,
    ) -> tuple[bool, str | None]:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return False, "TerminateJobObject is unavailable."
        kernel32 = loader("kernel32", use_last_error=True)
        terminate_job = kernel32.TerminateJobObject
        terminate_job.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
        terminate_job.restype = ctypes.wintypes.BOOL
        if not terminate_job(ctypes.wintypes.HANDLE(self.handle), 1):
            return False, self._last_error("TerminateJobObject failed")

        deadline = perf_counter() + timeout_seconds
        while perf_counter() < deadline:
            active_processes, query_error = self._active_process_count()
            if query_error is not None:
                return False, query_error
            if active_processes == 0:
                try:
                    process.wait(timeout=max(0.05, deadline - perf_counter()))
                except subprocess.TimeoutExpired:
                    return False, "Root process did not exit after Job termination."
                return True, None
            sleep(0.02)
        return False, "Windows Job still contained active processes at cleanup deadline."

    def _active_process_count(self) -> tuple[int | None, str | None]:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return None, "QueryInformationJobObject is unavailable."
        kernel32 = loader("kernel32", use_last_error=True)

        class _BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", ctypes.wintypes.DWORD),
                ("TotalProcesses", ctypes.wintypes.DWORD),
                ("ActiveProcesses", ctypes.wintypes.DWORD),
                ("TotalTerminatedProcesses", ctypes.wintypes.DWORD),
            ]

        query_job = kernel32.QueryInformationJobObject
        query_job.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.c_void_p,
        ]
        query_job.restype = ctypes.wintypes.BOOL
        information = _BasicAccountingInformation()
        if not query_job(
            ctypes.wintypes.HANDLE(self.handle),
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            return None, self._last_error("QueryInformationJobObject failed")
        return int(information.ActiveProcesses), None

    @classmethod
    def _resume_primary_thread(cls, process_id: int) -> str | None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return "Windows thread APIs are unavailable."
        kernel32 = loader("kernel32", use_last_error=True)

        class _ThreadEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.wintypes.DWORD),
                ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ThreadID", ctypes.wintypes.DWORD),
                ("th32OwnerProcessID", ctypes.wintypes.DWORD),
                ("tpBasePri", ctypes.wintypes.LONG),
                ("tpDeltaPri", ctypes.wintypes.LONG),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
        create_snapshot.restype = ctypes.wintypes.HANDLE
        thread_first = kernel32.Thread32First
        thread_first.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        thread_first.restype = ctypes.wintypes.BOOL
        thread_next = kernel32.Thread32Next
        thread_next.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        ]
        thread_next.restype = ctypes.wintypes.BOOL
        open_thread = kernel32.OpenThread
        open_thread.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        open_thread.restype = ctypes.wintypes.HANDLE
        resume_thread = kernel32.ResumeThread
        resume_thread.argtypes = [ctypes.wintypes.HANDLE]
        resume_thread.restype = ctypes.wintypes.DWORD

        snapshot = create_snapshot(0x00000004, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if int(snapshot) == invalid_handle:
            return cls._last_error("CreateToolhelp32Snapshot failed")
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            found_thread_id: int | None = None
            has_entry = bool(thread_first(snapshot, ctypes.byref(entry)))
            while has_entry:
                if int(entry.th32OwnerProcessID) == process_id:
                    found_thread_id = int(entry.th32ThreadID)
                    break
                has_entry = bool(thread_next(snapshot, ctypes.byref(entry)))
            if found_thread_id is None:
                return "Suspended process primary thread could not be found."
        finally:
            cls._close_handle(int(snapshot))

        thread_handle = open_thread(0x0002, False, found_thread_id)
        if not thread_handle:
            return cls._last_error("OpenThread failed")
        try:
            if int(resume_thread(thread_handle)) == 0xFFFFFFFF:
                return cls._last_error("ResumeThread failed")
        finally:
            cls._close_handle(int(thread_handle))
        return None

    def close(self) -> None:
        if self.closed:
            return
        self._close_handle(self.handle)
        self.closed = True

    @staticmethod
    def _close_handle(handle: int) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            return
        kernel32 = loader("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.wintypes.HANDLE]
        close_handle.restype = ctypes.wintypes.BOOL
        close_handle(ctypes.wintypes.HANDLE(handle))

    @staticmethod
    def _last_error(operation: str) -> str:
        get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
        return f"{operation} (Windows error {get_last_error()})."


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
        windows_job: _WindowsJob | None = None
        windows_job_terminated = False

        try:
            windows_job, windows_job_error = _WindowsJob.create()
            if os.name == "nt" and windows_job is None:
                raise OSError(windows_job_error or "Windows Job Object setup failed.")
            creation_flags = 0
            if os.name == "nt":
                creation_flags = int(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ) | int(getattr(subprocess, "CREATE_SUSPENDED", 0x00000004))
            process = subprocess.Popen(
                spec.argv,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=creation_flags,
            )
            if windows_job is not None:
                assignment_error = windows_job.assign_suspended(process)
                if assignment_error is not None:
                    cleanup_error = self._kill_root_and_wait(process)
                    detail = (
                        f"{assignment_error} {cleanup_error or ''}".strip()
                    )
                    raise OSError(detail)
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
                if windows_job is not None:
                    process_tree_terminated, termination_error = (
                        windows_job.terminate_and_wait(
                            process,
                            self.cleanup_grace_seconds,
                        )
                    )
                    windows_job_terminated = True
                else:
                    process_tree_terminated, termination_error = (
                        self._terminate_process_tree(process)
                    )
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
                if windows_job is not None and not windows_job_terminated:
                    cleanup_succeeded, cleanup_error = windows_job.terminate_and_wait(
                        process,
                        self.cleanup_grace_seconds,
                    )
                    if not cleanup_succeeded:
                        process_tree_terminated = False
                        termination_error = termination_error or cleanup_error
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
            if windows_job is not None:
                windows_job.close()

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
        if os.name == "nt":
            return False, "Windows process cleanup requires an assigned Job Object."
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

    def _kill_root_and_wait(
        self,
        process: subprocess.Popen[bytes],
    ) -> str | None:
        try:
            process.kill()
            process.wait(timeout=self.cleanup_grace_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Root process cleanup failed: {exc}"
        return None


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
