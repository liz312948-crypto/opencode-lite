from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

import repopilot_lite.runners as runners_module
from repopilot_lite.models import CommandSpec
from repopilot_lite.runners import (
    CommandPolicyError,
    CommandRunner,
    TestRunner,
    _WindowsJob,
)


def test_command_runner_captures_stdout_and_stderr(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = CommandSpec(
        argv=[
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ],
        cwd=str(workspace),
        timeout_seconds=5,
    )

    result = CommandRunner(workspace).run(spec)

    assert result.exit_code == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.timed_out is False


def test_command_runner_rejects_cwd_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = CommandSpec(
        argv=[sys.executable, "-c", "print('unsafe')"],
        cwd=str(tmp_path),
        timeout_seconds=5,
    )

    with pytest.raises(CommandPolicyError, match="inside"):
        CommandRunner(workspace).run(spec)


def test_command_runner_marks_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = CommandSpec(
        argv=[sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=str(workspace),
        timeout_seconds=1,
    )

    result = CommandRunner(workspace).run(spec)

    assert result.exit_code is None
    assert result.timed_out is True
    assert "timed out" in result.stderr


def test_command_runner_truncates_large_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = CommandSpec(
        argv=[sys.executable, "-c", "print('x' * 200)"],
        cwd=str(workspace),
        timeout_seconds=5,
    )

    result = CommandRunner(workspace, max_output_chars=80).run(spec)

    assert result.output_truncated is True
    assert len(result.stdout) <= 80
    assert result.stdout_bytes_discarded > 0


def test_command_runner_timeout_terminates_descendant_processes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ready = workspace / "descendant-ready.txt"
    sentinel = workspace / "descendant-survived.txt"
    child_code = (
        "import pathlib, time; "
        f"pathlib.Path({str(ready)!r}).write_text('ready', encoding='utf-8'); "
        "time.sleep(1.5); "
        f"pathlib.Path({str(sentinel)!r}).write_text('alive', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {json.dumps(child_code)}]); "
        "time.sleep(10)"
    )
    spec = CommandSpec(
        argv=[sys.executable, "-c", parent_code],
        cwd=str(workspace),
        timeout_seconds=1,
    )

    result = CommandRunner(workspace).run(spec)
    time.sleep(1.0)

    assert result.timed_out is True
    assert result.process_tree_terminated is True, result.termination_error
    assert result.termination_error is None
    assert ready.read_text(encoding="utf-8") == "ready"
    assert not sentinel.exists()


def test_command_runner_cleans_descendants_after_root_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ready = workspace / "background-ready.txt"
    sentinel = workspace / "background-survived.txt"
    child_code = (
        "import pathlib, time; "
        f"pathlib.Path({str(ready)!r}).write_text('ready', encoding='utf-8'); "
        "time.sleep(1.0); "
        f"pathlib.Path({str(sentinel)!r}).write_text('alive', encoding='utf-8')"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"ready = pathlib.Path({str(ready)!r}); "
        f"subprocess.Popen([sys.executable, '-c', {json.dumps(child_code)}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "deadline = time.time() + 2; "
        "exec(\"while not ready.exists() and time.time() < deadline:\\n "
        "   time.sleep(0.01)\"); "
        "sys.exit(0 if ready.exists() else 2)"
    )
    spec = CommandSpec(
        argv=[sys.executable, "-c", parent_code],
        cwd=str(workspace),
        timeout_seconds=5,
    )

    result = CommandRunner(workspace).run(spec)
    time.sleep(1.2)

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.termination_error is None
    assert ready.read_text(encoding="utf-8") == "ready"
    assert not sentinel.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object policy")
def test_command_runner_fails_closed_when_job_setup_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "command-ran.txt"
    monkeypatch.setattr(
        _WindowsJob,
        "create",
        classmethod(lambda cls: (None, "Job setup unavailable.")),
    )
    spec = CommandSpec(
        argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
        cwd=str(workspace),
        timeout_seconds=5,
    )

    result = CommandRunner(workspace).run(spec)

    assert result.exit_code is None
    assert result.timed_out is False
    assert "Job setup unavailable" in result.stderr
    assert not marker.exists()


def test_posix_cleanup_escalates_when_parent_exits_before_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    group_active = True
    sent_signals: list[int] = []
    clock = 0.0

    class FakeProcess:
        pid = 321
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            assert self.returncode is not None
            return self.returncode

    process = FakeProcess()

    def fake_kill_group(process_id: int, requested_signal: int) -> None:
        nonlocal group_active
        assert process_id == process.pid
        if requested_signal == 0:
            if group_active:
                return
            raise ProcessLookupError
        sent_signals.append(requested_signal)
        if requested_signal == runners_module.signal.SIGTERM:
            process.returncode = -requested_signal
        else:
            group_active = False

    def fake_clock() -> float:
        nonlocal clock
        clock += 0.6
        return clock

    monkeypatch.setattr(runners_module.os, "killpg", fake_kill_group, raising=False)
    monkeypatch.setattr(runners_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(runners_module, "perf_counter", fake_clock)
    monkeypatch.setattr(runners_module, "sleep", lambda seconds: None)

    succeeded, error = CommandRunner(workspace)._terminate_posix_group(process)  # type: ignore[arg-type]

    assert succeeded is True
    assert error is None
    assert sent_signals == [
        runners_module.signal.SIGTERM,
        getattr(runners_module.signal, "SIGKILL", runners_module.signal.SIGTERM),
    ]


def test_test_runner_rejects_arbitrary_python(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(CommandPolicyError, match="python -m pytest"):
        TestRunner().select_command(
            workspace,
            [sys.executable, "-c", "print('not allowed')"],
            5,
        )


def test_test_runner_rejects_untrusted_python_executable_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_python = workspace / ("python.exe" if os.name == "nt" else "python")
    fake_python.write_text("not the configured interpreter", encoding="utf-8")

    with pytest.raises(CommandPolicyError, match="configured Python interpreter"):
        TestRunner().select_command(
            workspace,
            [str(fake_python), "-m", "pytest", "-q"],
            5,
        )


@pytest.mark.parametrize("unsafe_path", ["../../outside", "C:\\outside", "/outside"])
def test_test_runner_rejects_path_arguments_outside_workspace(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(CommandPolicyError, match="outside"):
        TestRunner().select_command(
            workspace,
            [sys.executable, "-m", "pytest", f"--basetemp={unsafe_path}"],
            5,
        )


def test_test_runner_detects_pytest_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tests").mkdir()
    (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    spec = TestRunner().select_command(workspace, None, 30)

    assert Path(spec.argv[0]).name.lower().startswith("python")
    assert spec.argv[1:3] == ["-m", "pytest"]
    assert spec.timeout_seconds == 30
