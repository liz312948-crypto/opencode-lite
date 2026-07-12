from __future__ import annotations

from pathlib import Path

import pytest

from repopilot_lite.patching import PatchApplier, PatchApplyError, PatchValidationError

VALID_PATCH = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def test_patch_dry_run_and_apply_only_change_workspace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    original = "def add(a, b):\r\n    return a - b\r\n"
    _write_exact(source / "app.py", original)
    _write_exact(workspace / "app.py", original)
    sentinel = workspace / ".app.py.opencode-lite.tmp"
    sentinel.write_text("keep me", encoding="utf-8")
    applier = PatchApplier()

    assert applier.validate(workspace, VALID_PATCH) == ["app.py"]
    assert _read_exact(workspace / "app.py") == original

    modified_files = applier.apply(workspace, VALID_PATCH)

    assert modified_files == ["app.py"]
    assert _read_exact(source / "app.py") == original
    assert _read_exact(workspace / "app.py") == ("def add(a, b):\r\n    return a + b\r\n")
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert "return a + b" in applier.actual_diff(source, workspace, modified_files)


@pytest.mark.parametrize(
    "unsafe_path",
    ["../outside.py", "/absolute.py", "C:\\outside.py"],
)
def test_patch_rejects_unsafe_paths(tmp_path: Path, unsafe_path: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    patch = f"--- a/{unsafe_path}\n+++ b/{unsafe_path}\n@@ -1 +1 @@\n-old\n+new\n"

    with pytest.raises(PatchValidationError, match="safe relative path"):
        PatchApplier().validate(workspace, patch)


def test_patch_context_failure_does_not_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    invalid_patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 999
+VALUE = 2
"""

    with pytest.raises(PatchValidationError, match="does not match"):
        PatchApplier().apply(workspace, invalid_patch)

    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_multifile_commit_failure_restores_prior_replacements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.py"
    second = workspace / "second.py"
    first.write_text("VALUE = 1\n", encoding="utf-8")
    second.write_text("VALUE = 10\n", encoding="utf-8")
    patch = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-VALUE = 10
+VALUE = 20
"""
    original_replace = Path.replace
    committed_temporary_files = 0

    def interrupt_second_commit(source: Path, target: Path) -> Path:
        nonlocal committed_temporary_files
        if source.name.endswith(".opencode-lite.tmp"):
            committed_temporary_files += 1
            if committed_temporary_files == 2:
                raise OSError("simulated second-file commit failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", interrupt_second_commit)

    with pytest.raises(PatchApplyError) as captured:
        PatchApplier().apply(workspace, patch)

    error = captured.value
    assert error.attempted_files == ["first.py", "second.py"]
    assert error.replaced_files == ["first.py"]
    assert error.restored_files == ["first.py"]
    assert error.restore_errors == []
    assert first.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert second.read_text(encoding="utf-8") == "VALUE = 10\n"
    assert not list(workspace.glob("*.opencode-lite.tmp"))
    assert not list(workspace.glob("*.opencode-lite.backup"))


def test_post_replace_verification_failure_restores_and_records_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    applier = PatchApplier()
    monkeypatch.setattr(applier, "_read_text", lambda token: "unexpected content")

    with pytest.raises(PatchApplyError) as captured:
        applier.apply(workspace, patch)

    error = captured.value
    assert error.attempted_files == ["app.py"]
    assert error.replaced_files == ["app.py"]
    assert error.restored_files == ["app.py"]
    assert error.restore_errors == []
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not list(workspace.glob("*.opencode-lite.tmp"))
    assert not list(workspace.glob("*.opencode-lite.backup"))


def _write_exact(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _read_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()
