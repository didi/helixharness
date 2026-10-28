"""Deterministic Base/Solution calibration for published repository Scenarios."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.scenario_execution_support import (
    ScenarioExecutionSupportError,
    apply_hidden_test_patch,
    apply_workspace_fixture_patch,
    apply_workspace_preconditions,
    export_git_tree,
    frozen_command_environment,
    git_file,
    json_object,
    run_frozen_command,
    safe_relative_path,
    safe_workspace_path,
    snapshot_file,
)
from bench.scenario_storage import ScenarioStore
from bench.scenario_workspace import ScenarioRepository, ScenarioWorkspace, scenario_workspace


class ScenarioCalibrationError(RuntimeError):
    """Raised when a Scenario cannot be calibrated against its frozen repository."""


@dataclass(frozen=True)
class ScenarioCalibrationTest:
    """Base/Solution result for one frozen F2P or P2P command."""

    check_id: str
    kind: str
    base_status: str
    solution_status: str
    base_duration_ms: int
    solution_duration_ms: int


@dataclass(frozen=True)
class ScenarioCalibrationResult:
    """Auditable deterministic calibration result for one exact Scenario version."""

    case_id: str
    source_hash: str
    status: str
    solution_patch_sha256_verified: bool
    source_evidence_verified: bool
    tests: tuple[ScenarioCalibrationTest, ...]
    failures: tuple[str, ...]


class ScenarioCalibrationService:
    """Calibrate only REPOSITORY_CASE sources using frozen Commit facts."""

    def __init__(
        self,
        store: ScenarioStore,
        *,
        repositories: dict[str, Path],
        wall_timeout_seconds: int = 1_800,
        disk_budget_mb: int = 10_240,
        gradle_heap_mb: int = 4_096,
    ) -> None:
        self.store = store
        self.repositories = {name: path.resolve() for name, path in repositories.items()}
        self.wall_timeout_seconds = max(1, wall_timeout_seconds)
        self.disk_budget_bytes = max(0, disk_budget_mb) * 1_024 * 1_024
        self.gradle_heap_mb = gradle_heap_mb

    def calibrate(self, case_id: str) -> ScenarioCalibrationResult:
        """Verify patch, Gold provenance, and frozen F2P/P2P behavior."""

        deadline = time.monotonic() + self.wall_timeout_seconds
        scenario = self.store.get(case_id)
        if scenario is None:
            raise ScenarioCalibrationError("Published Scenario not found")
        snapshot = json.loads(scenario.snapshot_json)
        if not isinstance(snapshot, dict):  # pragma: no cover - protected on publication
            raise ScenarioCalibrationError("Published Scenario snapshot is invalid")
        manifest = json_object(snapshot_file(snapshot, "case.json"), label="Scenario manifest")
        if manifest.get("case_kind") != "REPOSITORY_CASE":
            raise ScenarioCalibrationError("Only REPOSITORY_CASE can receive formal calibration")
        self._require_declared_runtime(manifest)
        try:
            case_workspace = scenario_workspace(manifest)
        except ValueError as exc:
            raise ScenarioCalibrationError(str(exc)) from exc
        repositories: dict[str, Path] = {}
        for member in case_workspace.repositories:
            repository = self.repositories.get(member.repository_id)
            if repository is None:
                raise ScenarioCalibrationError(f"Repository resolver is missing: {member.repository_id}")
            repositories[member.repository_id] = repository
        gold_path = str(manifest.get("gold_path", ""))
        gold = json_object(snapshot_file(snapshot, gold_path), label="Scenario Gold")
        failures: list[str] = []

        patch_verified = True
        for member in case_workspace.repositories:
            patch = subprocess.run(
                (
                    "git",
                    "-C",
                    str(repositories[member.repository_id]),
                    "diff",
                    "--binary",
                    "--full-index",
                    member.base_commit,
                    member.solution_commit,
                ),
                check=False,
                capture_output=True,
            )
            member_verified = (
                patch.returncode == 0
                and hashlib.sha256(patch.stdout).hexdigest() == member.solution_patch_sha256
            )
            patch_verified = patch_verified and member_verified
            if not member_verified:
                failures.append(
                    f"Solution patch SHA-256 does not match the frozen interval: {member.repository_id}"
                )

        source_verified = self._verify_source_evidence(
            gold,
            repositories,
            primary_repository_id=case_workspace.primary_repository_id,
        )
        if not source_verified:
            failures.append("Repository source evidence Hash or Commit does not match")
        hidden_test_patches = self._hidden_test_patches(snapshot, manifest, case_workspace)

        tests: list[ScenarioCalibrationTest] = []
        try:
            with tempfile.TemporaryDirectory(prefix="scenario-calibration-") as temporary:
                runtime_root = Path(temporary)
                base_workspace = runtime_root / "base"
                solution_workspace = runtime_root / "solution"
                if case_workspace.is_multi_repository:
                    base_workspace.mkdir()
                    solution_workspace.mkdir()
                for member in case_workspace.repositories:
                    repository = repositories[member.repository_id]
                    base_target = self._repository_workspace(base_workspace, member)
                    solution_target = self._repository_workspace(solution_workspace, member)
                    export_git_tree(repository, member.base_commit, base_target, materialize_safe_symlinks=True)
                    export_git_tree(repository, member.solution_commit, solution_target, materialize_safe_symlinks=True)
                    self._restore_base_test_files(
                        repository,
                        base_commit=member.base_commit,
                        solution_commit=member.solution_commit,
                        base_workspace=base_target,
                        solution_workspace=solution_target,
                    )
                self._materialize_workspace_files(snapshot, manifest, base_workspace)
                self._materialize_workspace_files(snapshot, manifest, solution_workspace)
                apply_workspace_preconditions(base_workspace, manifest)
                apply_workspace_preconditions(solution_workspace, manifest)
                if case_workspace.is_multi_repository and manifest.get("workspace_fixture_patch") is not None:
                    raise ScenarioCalibrationError("MULTI_REPOSITORY does not support single-repo workspace_fixture_patch")
                if not case_workspace.is_multi_repository:
                    primary = case_workspace.repositories[0]
                    repository = repositories[primary.repository_id]
                    apply_workspace_fixture_patch(
                        base_workspace,
                        manifest,
                        snapshot,
                        repository=repository,
                        merge_root=runtime_root / "base-workspace-fixture-merge",
                    )
                    apply_workspace_fixture_patch(
                        solution_workspace,
                        manifest,
                        snapshot,
                        repository=repository,
                        merge_root=runtime_root / "solution-workspace-fixture-merge",
                    )
                self._require_within_resource_policy(runtime_root, deadline=deadline)
                for position, (member, contract, hidden_test_patch) in enumerate(hidden_test_patches, start=1):
                    repository = repositories[member.repository_id]
                    protected_paths = contract.get("protected_paths")
                    if not isinstance(protected_paths, list) or any(
                        not isinstance(path, str) for path in protected_paths
                    ):
                        raise ScenarioCalibrationError("Scenario hidden test protected paths are invalid")
                    for side, root in (("base", base_workspace), ("solution", solution_workspace)):
                        apply_hidden_test_patch(
                            self._repository_workspace(root, member),
                            hidden_test_patch,
                            protected_paths=protected_paths,
                            repository=repository,
                            base_commit=member.base_commit,
                            merge_root=runtime_root / f"{side}-hidden-test-merge-{position}",
                        )
                for position, test in enumerate(_object_list(gold.get("execution_tests", [])), start=1):
                    with tempfile.TemporaryDirectory(
                        prefix=f"command-{position}-",
                        dir=runtime_root,
                    ) as command_temporary:
                        command_root = Path(command_temporary)
                        base_runtime = command_root / "base"
                        solution_runtime = command_root / "solution"
                        base_command_workspace = base_runtime / "workspace"
                        solution_command_workspace = solution_runtime / "workspace"
                        shutil.copytree(base_workspace, base_command_workspace)
                        shutil.copytree(solution_workspace, solution_command_workspace)
                        base_status, base_duration = self._run_test(
                            test,
                            workspace=base_command_workspace,
                            runtime_root=base_runtime,
                            deadline=deadline,
                        )
                        solution_status, solution_duration = self._run_test(
                            test,
                            workspace=solution_command_workspace,
                            runtime_root=solution_runtime,
                            deadline=deadline,
                        )
                        self._require_within_resource_policy(runtime_root, deadline=deadline)
                    kind = str(test.get("kind", ""))
                    expected = (
                        base_status == "FAIL" and solution_status == "PASS"
                        if kind == "FAIL_TO_PASS"
                        else base_status == "PASS" and solution_status == "PASS"
                    )
                    if not expected:
                        failures.append(f"{test.get('check_id')} does not satisfy {kind} Base/Solution behavior")
                    tests.append(
                        ScenarioCalibrationTest(
                            check_id=str(test.get("check_id", "")),
                            kind=kind,
                            base_status=base_status,
                            solution_status=solution_status,
                            base_duration_ms=base_duration,
                            solution_duration_ms=solution_duration,
                        )
                    )
        except ScenarioExecutionSupportError as exc:
            raise ScenarioCalibrationError(str(exc)) from exc

        status = "CALIBRATED" if not failures else "FAILED"
        self.store.record_calibration(case_id, scenario.source_hash, state=status)
        return ScenarioCalibrationResult(
            case_id=case_id,
            source_hash=scenario.source_hash,
            status=status,
            solution_patch_sha256_verified=patch_verified,
            source_evidence_verified=source_verified,
            tests=tuple(tests),
            failures=tuple(failures),
        )

    @staticmethod
    def _require_declared_runtime(manifest: dict[str, Any]) -> None:
        """Stop before calibration when a declared Java toolchain is unavailable."""

        runtime = manifest.get("runtime")
        if not isinstance(runtime, dict) or str(runtime.get("language", "")).upper() != "JAVA":
            return
        java_home_env = runtime.get("java_home_env")
        if not isinstance(java_home_env, str) or not java_home_env:
            raise ScenarioCalibrationError("Java Scenario runtime must declare java_home_env")
        java_home = os.environ.get(java_home_env)
        if not java_home:
            raise ScenarioCalibrationError(f"Java Scenario runtime is unavailable: {java_home_env}")
        java_bin = Path(java_home) / "bin"
        if any(not os.access(java_bin / executable, os.X_OK) for executable in ("java", "javac")):
            raise ScenarioCalibrationError(f"Java Scenario runtime is incomplete: {java_home_env}")

    @staticmethod
    def _hidden_test_patches(
        snapshot: dict[str, object],
        manifest: dict[str, Any],
        workspace: ScenarioWorkspace,
    ) -> tuple[tuple[ScenarioRepository, dict[str, Any], bytes], ...]:
        raw_contracts = manifest.get("hidden_test_patches")
        if raw_contracts is None:
            single_patch = manifest.get("hidden_test_patch")
            raw_contracts = [] if single_patch is None else [single_patch]
        if not isinstance(raw_contracts, list) or any(not isinstance(item, dict) for item in raw_contracts):
            raise ScenarioCalibrationError("Scenario hidden test patch contract is invalid")
        members = {member.repository_id: member for member in workspace.repositories}
        patches: list[tuple[ScenarioRepository, dict[str, Any], bytes]] = []
        for contract in raw_contracts:
            repository_id = contract.get("repository_id", workspace.primary_repository_id)
            member = members.get(str(repository_id))
            path = contract.get("path")
            expected = contract.get("sha256")
            if member is None or not isinstance(path, str) or not path or not isinstance(expected, str):
                raise ScenarioCalibrationError("Scenario hidden test patch contract is invalid")
            content = snapshot_file(snapshot, path)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ScenarioCalibrationError("Scenario hidden test patch Hash does not match")
            patches.append((member, contract, content))
        return tuple(patches)

    @staticmethod
    def _repository_workspace(workspace: Path, member: ScenarioRepository) -> Path:
        return workspace if member.mount_path == "." else workspace / member.mount_path

    @staticmethod
    def _materialize_workspace_files(
        snapshot: dict[str, object], manifest: dict[str, Any], workspace: Path
    ) -> None:
        prefix_value = manifest.get("workspace_path")
        if not isinstance(prefix_value, str) or not prefix_value:
            return
        prefix = safe_relative_path(prefix_value, label="Scenario workspace_path")
        files = snapshot.get("files")
        if not isinstance(files, dict):
            raise ScenarioCalibrationError("Scenario files are invalid")
        for name, item in sorted(files.items()):
            if not isinstance(item, dict) or item.get("visibility") != "EXECUTION":
                continue
            path = safe_relative_path(name, label="Scenario execution path")
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            target = safe_workspace_path(workspace, relative, label="Scenario execution path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(snapshot_file(snapshot, name))

    @staticmethod
    def _restore_base_test_files(
        repository: Path,
        *,
        base_commit: str,
        solution_commit: str,
        base_workspace: Path,
        solution_workspace: Path,
    ) -> None:
        """Keep Solution calibration independent from historical test changes."""

        changed = subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                base_commit,
                solution_commit,
                "--",
            ),
            check=False,
            capture_output=True,
        )
        if changed.returncode != 0:
            raise ScenarioCalibrationError("Unable to isolate Solution calibration test fixtures")
        for raw_path in changed.stdout.decode("utf-8", errors="strict").split("\0"):
            if not raw_path:
                continue
            relative = safe_relative_path(raw_path, label="Solution changed path")
            if not _is_test_path(Path(relative.as_posix())):
                continue
            base_path = safe_workspace_path(base_workspace, relative, label="Base test path")
            solution_path = safe_workspace_path(solution_workspace, relative, label="Solution test path")
            if base_path.is_file():
                solution_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(base_path, solution_path)
            elif solution_path.is_file():
                solution_path.unlink()
            elif solution_path.exists() or solution_path.is_symlink():
                raise ScenarioCalibrationError("Solution test path has an unsupported shape")

    @staticmethod
    def _verify_source_evidence(
        gold: dict[str, Any],
        repositories: dict[str, Path],
        *,
        primary_repository_id: str,
    ) -> bool:
        evidence = gold.get("source_evidence")
        if not isinstance(evidence, list) or not evidence:
            return False
        for item in evidence:
            if not isinstance(item, dict):
                return False
            if "line_start" in item:
                continue
            repository = repositories.get(str(item.get("repository_id", primary_repository_id)))
            if repository is None:
                return False
            try:
                content = git_file(repository, str(item.get("commit", "")), str(item.get("path", "")))
            except ScenarioExecutionSupportError:
                return False
            if hashlib.sha256(content).hexdigest() != item.get("sha256"):
                return False
        return True

    def _run_test(
        self,
        test: dict[str, Any],
        *,
        workspace: Path,
        runtime_root: Path,
        deadline: float,
    ) -> tuple[str, int]:
        argv = test.get("argv")
        timeout = test.get("timeout_seconds")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(argument, str) or not argument for argument in argv)
            or not isinstance(timeout, int)
        ):
            raise ScenarioCalibrationError("Frozen execution test is invalid")
        remaining_seconds = int(deadline - time.monotonic())
        if remaining_seconds < 1:
            raise ScenarioCalibrationError("Case calibration exceeded its workload timeout")
        started = time.monotonic()
        completed = run_frozen_command(
            argv,
            cwd=safe_workspace_path(
                workspace,
                safe_relative_path(test.get("working_directory", "."), label="execution test working_directory"),
                label="execution test working_directory",
            ),
            environment=frozen_command_environment(
                runtime_root,
                workspace=workspace,
                gradle_heap_mb=self.gradle_heap_mb,
            ),
            timeout_seconds=min(timeout, remaining_seconds),
        )
        status = "PASS" if completed.returncode == 0 else "FAIL"
        return status, round((time.monotonic() - started) * 1000)

    def _require_within_resource_policy(self, runtime_root: Path, *, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise ScenarioCalibrationError("Case calibration exceeded its workload timeout")
        used_bytes = sum(
            path.stat().st_size
            for path in runtime_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        if used_bytes > self.disk_budget_bytes:
            raise ScenarioCalibrationError("Case calibration exceeded its workload disk budget")


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ScenarioCalibrationError("Scenario Gold collection is invalid")
    return value


def _is_test_path(path: Path) -> bool:
    test_directories = {
        "test",
        "tests",
        "testing",
        "__tests__",
        "testfixtures",
        "test-fixtures",
        "test_fixtures",
    }
    return any(part.lower() in test_directories for part in path.parts[:-1])
