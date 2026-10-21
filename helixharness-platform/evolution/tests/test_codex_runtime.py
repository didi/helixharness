from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from evolution import codex_runtime
from evolution.candidate import CandidateBuildError, CodexCandidateRunner


def test_runtime_availability_requires_an_executable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("runtime", encoding="utf-8")
    monkeypatch.setattr(codex_runtime, "_bundled_codex_path", lambda: binary)

    assert codex_runtime.codex_runtime_available() is False

    binary.chmod(0o755)
    assert codex_runtime.resolve_codex_executable() == binary
    assert codex_runtime.codex_runtime_available() is True


def test_candidate_runner_uses_the_bundled_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bundled-codex"
    binary.write_text("runtime", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("evolution.candidate.resolve_codex_executable", lambda: binary)
    monkeypatch.setattr("evolution.candidate.subprocess.run", fake_run)

    CodexCandidateRunner(model="test-model").run(tmp_path, "test prompt")

    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == str(binary)
    assert "/Applications/ChatGPT.app" not in " ".join(command)
    assert captured["kwargs"] == {
        "input": "test prompt",
        "text": True,
        "capture_output": True,
        "check": False,
        "timeout": 900,
    }


def test_candidate_runner_passes_proxy_secret_only_via_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bundled-codex"
    binary.write_text("runtime", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("evolution.candidate.resolve_codex_executable", lambda: binary)
    monkeypatch.setattr("evolution.candidate.subprocess.run", fake_run)

    CodexCandidateRunner(
        model="test-model",
        api_key="secret-test-key",
        base_url="https://proxy.example/v1/",
    ).run(tmp_path, "test prompt")

    command = captured["command"]
    kwargs = captured["kwargs"]
    assert isinstance(command, list)
    assert "secret-test-key" not in " ".join(command)
    assert 'model_provider="evaluation_proxy"' in command
    assert 'model_providers.evaluation_proxy.base_url="https://proxy.example/v1"' in command
    assert isinstance(kwargs, dict)
    assert kwargs["env"]["OPENAI_API_KEY"] == "secret-test-key"
    assert "--output-last-message" in command


def test_candidate_runner_turns_timeout_into_a_build_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bundled-codex"
    binary.write_text("runtime", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("evolution.candidate.resolve_codex_executable", lambda: binary)

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("codex", 3)

    monkeypatch.setattr("evolution.candidate.subprocess.run", timeout)

    with pytest.raises(CandidateBuildError, match="timed out after 3 seconds"):
        CodexCandidateRunner(timeout_seconds=3).run(tmp_path, "test prompt")


def test_candidate_runner_uses_low_privilege_host_isolation_without_bwrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bundled-codex"
    binary.write_text("runtime", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    runner = CodexCandidateRunner(execution_user="nobody")
    runner._execution_identity = (65534, 65534)
    monkeypatch.setattr("evolution.candidate.resolve_codex_executable", lambda: binary)
    monkeypatch.setattr("evolution.candidate.shutil.which", lambda name: "/usr/bin/setpriv")
    monkeypatch.setattr(runner, "_runtime_home", lambda worktree, uid, gid: tmp_path)
    monkeypatch.setattr("evolution.candidate.subprocess.run", fake_run)

    runner.run(tmp_path, "test prompt")

    command = captured["command"]
    kwargs = captured["kwargs"]
    assert isinstance(command, list)
    assert command[:5] == [
        "/usr/bin/setpriv",
        "--reuid=65534",
        "--regid=65534",
        "--clear-groups",
        "--",
    ]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "workspace-write" not in command
    assert kwargs["env"]["HOME"] == str(tmp_path)


def test_candidate_prepare_grants_only_catalog_owned_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "candidate"
    (worktree / "commands").mkdir(parents=True)
    (worktree / "skills").mkdir()
    (worktree / "eval").mkdir()
    granted: list[Path] = []
    monkeypatch.setattr(
        "evolution.candidate.pwd.getpwnam",
        lambda name: SimpleNamespace(pw_uid=65534, pw_gid=65534),
    )
    monkeypatch.setattr("evolution.candidate.shutil.which", lambda name: "/usr/bin/setpriv")
    monkeypatch.setattr(
        "evolution.candidate._chown_tree",
        lambda path, uid, gid: granted.append(path),
    )

    runner = CodexCandidateRunner(execution_user="nobody")
    runner.prepare(worktree, ("commands/", "skills/"))

    assert granted == [worktree / "commands", worktree / "skills"]
    assert worktree / "eval" not in granted
