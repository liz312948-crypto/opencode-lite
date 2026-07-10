from __future__ import annotations

import sys
from pathlib import Path

import pytest

from repopilot_lite.models import CommandSpec
from repopilot_lite.runners import CommandPolicyError, CommandRunner, TestRunner


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


def test_test_runner_rejects_arbitrary_python(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(CommandPolicyError, match="python -m pytest"):
        TestRunner().select_command(
            workspace,
            [sys.executable, "-c", "print('not allowed')"],
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
