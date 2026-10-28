from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bench.scenario_execution_support import (
    ScenarioExecutionSupportError,
    apply_hidden_test_patch,
    apply_workspace_fixture_patch,
    export_git_tree,
    frozen_command_environment,
    run_frozen_command,
)


def _snapshot_file(path: str, content: bytes) -> dict[str, object]:
    return {
        "files": {
            path: {
                "content_base64": base64.b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        }
    }


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_frozen_command_environment_exposes_current_virtualenv_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = frozen_command_environment(tmp_path, workspace=workspace)

    assert environment["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["TMPDIR"] == str(tmp_path / "tmp")
    assert environment["PYTHONPATH"] == str(workspace.resolve())
    assert environment["GRADLE_USER_HOME"] == str(tmp_path / "gradle-user-home")
    assert environment["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    gradle_properties = (tmp_path / "gradle-user-home/gradle.properties").read_text(encoding="utf-8")
    assert "org.gradle.jvmargs=-Xmx4096m" in gradle_properties
    assert "org.gradle.daemon=false" in gradle_properties


def test_frozen_command_environment_can_reuse_managed_gradle_home(tmp_path: Path) -> None:
    runtime_root = tmp_path / "command"
    managed_gradle_home = tmp_path / "runtime" / "gradle-user-home"

    environment = frozen_command_environment(
        runtime_root,
        gradle_home=managed_gradle_home,
    )

    assert environment["GRADLE_USER_HOME"] == str(managed_gradle_home)
    assert (managed_gradle_home / "gradle.properties").is_file()
    assert not (runtime_root / "gradle-user-home").exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group isolation requires POSIX")
def test_frozen_command_timeout_kills_the_entire_process_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )

    result = run_frozen_command(
        (sys.executable, "-c", script),
        cwd=tmp_path,
        environment=os.environ.copy(),
        timeout_seconds=1,
    )

    assert result.returncode is None
    assert result.timed_out is True
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out child process is still alive")


def test_git_export_materializes_only_safe_repository_internal_symlinks(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    (repository / "CLAUDE.md").write_text("trusted instructions\n", encoding="utf-8")
    bundled_skill = repository / ".agents" / "skills" / "helixharness"
    bundled_skill.mkdir(parents=True)
    (bundled_skill / "SKILL.md").write_text("safe directory link\n", encoding="utf-8")
    (repository / "AGENTS.md").symlink_to("CLAUDE.md")
    (repository / "skills").mkdir()
    (repository / "skills" / "helixharness").symlink_to("../.agents/skills/helixharness", target_is_directory=True)
    _git(repository, "add", "CLAUDE.md", "AGENTS.md", ".agents", "skills")
    _git(repository, "commit", "-m", "safe link")
    commit = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(ScenarioExecutionSupportError, match="non-regular"):
        export_git_tree(repository, commit, tmp_path / "strict-export")
    export_git_tree(
        repository,
        commit,
        tmp_path / "plugin-export",
        materialize_safe_symlinks=True,
    )

    exported = tmp_path / "plugin-export" / "AGENTS.md"
    assert exported.is_file()
    assert not exported.is_symlink()
    assert exported.read_text(encoding="utf-8") == "trusted instructions\n"
    exported_skill = tmp_path / "plugin-export" / "skills" / "helixharness"
    assert exported_skill.is_dir()
    assert not exported_skill.is_symlink()
    assert (exported_skill / "SKILL.md").read_text(encoding="utf-8") == "safe directory link\n"


def test_git_export_omits_dotenv_files_but_preserves_other_dotfiles(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    (repository / ".env").write_text("SECRET=do-not-export\n", encoding="utf-8")
    (repository / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (repository / ".gitignore").write_text("local-only\n", encoding="utf-8")
    nested = repository / "app"
    nested.mkdir()
    (nested / ".env.local").write_text("TOKEN=do-not-export\n", encoding="utf-8")
    (nested / "source.txt").write_text("safe\n", encoding="utf-8")
    _git(repository, "add", "-f", ".env", ".env.example", "app/.env.local")
    _git(repository, "add", ".gitignore", "app/source.txt")
    _git(repository, "commit", "-m", "tracked environment files")

    target = tmp_path / "export"
    export_git_tree(repository, _git(repository, "rev-parse", "HEAD"), target)

    assert not (target / ".env").exists()
    assert not (target / ".env.example").exists()
    assert not (target / "app/.env.local").exists()
    assert (target / ".gitignore").read_text(encoding="utf-8") == "local-only\n"
    assert (target / "app/source.txt").read_text(encoding="utf-8") == "safe\n"


def test_git_export_rejects_symlink_that_escapes_the_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    (repository / "unsafe").symlink_to("../outside")
    _git(repository, "add", "unsafe")
    _git(repository, "commit", "-m", "unsafe link")

    with pytest.raises(ScenarioExecutionSupportError, match="escapes"):
        export_git_tree(
            repository,
            _git(repository, "rev-parse", "HEAD"),
            tmp_path / "export",
            materialize_safe_symlinks=True,
        )


def test_git_export_rejects_internal_symlink_cycle(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    (repository / "first").symlink_to("second")
    (repository / "second").symlink_to("first")
    _git(repository, "add", "first", "second")
    _git(repository, "commit", "-m", "cyclic links")

    with pytest.raises(ScenarioExecutionSupportError, match="unavailable or cyclic"):
        export_git_tree(
            repository,
            _git(repository, "rev-parse", "HEAD"),
            tmp_path / "export",
            materialize_safe_symlinks=True,
        )


def test_git_export_rejects_directory_symlink_to_its_ancestor(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    directory = repository / "directory"
    directory.mkdir()
    (directory / "value.txt").write_text("value\n", encoding="utf-8")
    (directory / "loop").symlink_to("../directory", target_is_directory=True)
    _git(repository, "add", "directory")
    _git(repository, "commit", "-m", "directory cycle")

    with pytest.raises(ScenarioExecutionSupportError, match="unavailable or cyclic"):
        export_git_tree(
            repository,
            _git(repository, "rev-parse", "HEAD"),
            tmp_path / "export",
            materialize_safe_symlinks=True,
        )


def test_hidden_patch_materializes_inside_parent_git_repository(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    workspace = tmp_path / "output" / "verifier"
    workspace.mkdir(parents=True)
    patch = (
        b"diff --git a/check.py b/check.py\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/check.py\n"
        b"@@ -0,0 +1 @@\n"
        b"+raise SystemExit(0)\n"
    )

    mode = apply_hidden_test_patch(
        workspace,
        patch,
        protected_paths=("check.py",),
        repository=tmp_path,
        base_commit="unused-for-direct-injection",
        merge_root=tmp_path / "merge",
    )

    assert mode == "DIRECT"
    assert (workspace / "check.py").read_bytes() == b"raise SystemExit(0)\n"
    assert not (tmp_path / "check.py").exists()


def test_hidden_patch_uses_isolated_union_merge_when_candidate_changes_protected_build_file(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    build_file = repository / "build.gradle"
    build_file.write_text(
        "plugins { id 'java' }\n"
        "dependencies {\n"
        "    implementation 'example:base:1'\n"
        "}\n"
        "jar { enabled = true }\n",
        encoding="utf-8",
    )
    _git(repository, "add", "build.gradle")
    _git(repository, "commit", "-m", "base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    build_file.write_text(
        "plugins { id 'java' }\n"
        "dependencies {\n"
        "    implementation 'example:base:1'\n"
        "    testImplementation 'example:hidden:1'\n"
        "}\n",
        encoding="utf-8",
    )
    hidden_test = repository / "hidden" / "ContractTest.java"
    hidden_test.parent.mkdir()
    hidden_test.write_text("final class ContractTest {}\n", encoding="utf-8")
    _git(repository, "add", "--intent-to-add", "hidden/ContractTest.java")
    patch = subprocess.run(
        ("git", "-C", str(repository), "diff", "--binary", "--full-index", base_commit),
        check=True,
        capture_output=True,
    ).stdout
    workspace = tmp_path / "candidate"
    export_git_tree(repository, base_commit, workspace)
    (workspace / "build.gradle").write_text(
        "plugins { id 'java' }\n"
        "dependencies {\n"
        "    implementation 'example:base:1'\n"
        "    implementation 'example:candidate:1'\n"
        "}\n",
        encoding="utf-8",
    )

    mode = apply_hidden_test_patch(
        workspace,
        patch,
        protected_paths=("build.gradle", "hidden"),
        repository=repository,
        base_commit=base_commit,
        merge_root=tmp_path / "merge",
    )

    merged = (workspace / "build.gradle").read_text(encoding="utf-8")
    assert mode == "THREE_WAY_UNION"
    assert "example:candidate:1" in merged
    assert "example:hidden:1" in merged
    assert (workspace / "hidden" / "ContractTest.java").is_file()


def test_workspace_fixture_patch_is_applied_before_agent_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    build_file = repository / "build.gradle"
    build_file.write_text("plugins { id 'java' }\n", encoding="utf-8")
    _git(repository, "add", "build.gradle")
    _git(repository, "commit", "-m", "base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    build_file.write_text(
        "plugins { id 'java' }\nsourceSets.test.java.setSrcDirs(['src/helixharnessTest/java'])\n",
        encoding="utf-8",
    )
    patch = subprocess.run(
        ("git", "-C", str(repository), "diff", "--binary", "--full-index", base_commit),
        check=True,
        capture_output=True,
    ).stdout
    workspace = tmp_path / "workspace"
    export_git_tree(repository, base_commit, workspace)
    manifest = {
        "repository": {"base_commit": base_commit},
        "workspace_fixture_patch": {
            "path": "workspace-fixture.patch",
            "sha256": hashlib.sha256(patch).hexdigest(),
            "protected_paths": ["build.gradle"],
        },
    }

    mode = apply_workspace_fixture_patch(
        workspace,
        manifest,
        _snapshot_file("workspace-fixture.patch", patch),
        repository=repository,
        merge_root=tmp_path / "merge",
    )

    assert mode == "DIRECT"
    assert "src/helixharnessTest/java" in (workspace / "build.gradle").read_text(encoding="utf-8")


def test_workspace_fixture_patch_rejects_undeclared_changed_path(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    (repository / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "build.gradle", "README.md")
    _git(repository, "commit", "-m", "base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    (repository / "build.gradle").write_text("plugins { id 'java-library' }\n", encoding="utf-8")
    (repository / "README.md").write_text("changed\n", encoding="utf-8")
    patch = subprocess.run(
        ("git", "-C", str(repository), "diff", "--binary", "--full-index", base_commit),
        check=True,
        capture_output=True,
    ).stdout
    workspace = tmp_path / "workspace"
    export_git_tree(repository, base_commit, workspace)
    manifest = {
        "repository": {"base_commit": base_commit},
        "workspace_fixture_patch": {
            "path": "workspace-fixture.patch",
            "sha256": hashlib.sha256(patch).hexdigest(),
            "protected_paths": ["build.gradle"],
        },
    }

    with pytest.raises(ScenarioExecutionSupportError, match="undeclared protected paths"):
        apply_workspace_fixture_patch(
            workspace,
            manifest,
            _snapshot_file("workspace-fixture.patch", patch),
            repository=repository,
            merge_root=tmp_path / "merge",
        )
