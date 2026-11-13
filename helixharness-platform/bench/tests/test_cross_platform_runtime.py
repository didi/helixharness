"""Portable execution contracts, including real Windows Job Object cleanup."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bench.process_runtime import kill_group, spawn_group
from bench.scenario_execution_support import (
    ScenarioExecutionSupportError,
    frozen_command_environment,
    run_frozen_command,
    safe_relative_path,
)


@pytest.mark.parametrize("value", [r"C:\outside", "C:/outside", r"..\outside", "file:stream", r"\\server\share"])
def test_windows_paths_cannot_escape_frozen_posix_manifest(value):
    with pytest.raises(ScenarioExecutionSupportError):
        safe_relative_path(value, label="fixture")


def test_frozen_python_command_uses_current_runtime_with_unicode_path(tmp_path):
    workspace = tmp_path / "space 中文"
    workspace.mkdir()
    result = run_frozen_command(["python3", "-c", "from pathlib import Path; Path('result.txt').write_text('ok')"],
                                cwd=workspace, environment=frozen_command_environment(tmp_path), timeout_seconds=10)
    assert result.returncode == 0
    assert (workspace / "result.txt").read_text() == "ok"


def test_isolated_windows_homes_and_system_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setenv("SECRET_FIXTURE", "must-not-forward")
    environment = frozen_command_environment(tmp_path)
    assert environment["SystemRoot"] == "C:\\Windows"
    assert environment["TEMP"] == environment["TMP"] == environment["TMPDIR"]
    assert environment["USERPROFILE"] == environment["HOME"]
    assert "SECRET_FIXTURE" not in environment


@pytest.mark.skipif(os.name != "nt", reason="native Windows batch wrapper execution")
def test_gradle_wrapper_resolves_bat_and_keeps_its_exit_code(tmp_path):
    (tmp_path / "gradlew.bat").write_bytes(b"@echo off\r\necho ran>result.txt\r\nexit /b 7\r\n")
    result = run_frozen_command(["./gradlew", "test"], cwd=tmp_path,
                                environment=frozen_command_environment(tmp_path / "runtime"), timeout_seconds=10)
    assert result.returncode == 7
    assert (tmp_path / "result.txt").read_text().strip() == "ran"


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job Object integration")
def test_job_cleanup_reaps_descendant_after_leader_exits(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    child = "import time; from pathlib import Path; p=Path(" + repr(str(heartbeat)) + ");\nwhile True: p.write_text(str(time.time())); time.sleep(.05)"
    parent = f"import subprocess,sys,time; time.sleep(.3); subprocess.Popen([sys.executable, '-c', {child!r}])"
    process = spawn_group([sys.executable, "-c", parent], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert heartbeat.exists()
        kill_group(process.pid)
        time.sleep(.2)
        modified = heartbeat.stat().st_mtime_ns
        time.sleep(.3)
        assert heartbeat.stat().st_mtime_ns == modified
    finally:
        if process.poll() is None:
            kill_group(process.pid)
            process.wait(timeout=10)


def test_standalone_plugin_and_platform_process_helpers_are_identical():
    root = Path(__file__).resolve().parents[3]
    assert (root / "helixharness/scripts/process_runtime.py").read_bytes() == (root / "helixharness-platform/bench/process_runtime.py").read_bytes()
