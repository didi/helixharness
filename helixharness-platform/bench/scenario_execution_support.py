"""Credential-safe filesystem helpers shared by Scenario calibration and execution."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from bench.gradle_runtime import configure_evaluator_gradle_home
from bench.worker_processes import kill_group, register_group


class ScenarioExecutionSupportError(RuntimeError):
    """Raised when frozen bytes or Git boundaries cannot be materialized safely."""


_HELIXHARNESS_INITIALIZATION_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    "helixharness.workspace.yaml",
    "docs/.helixharness-init-state.json",
    "docs/eval",
    "docs/harness",
    "docs/hooks",
    "docs/knowledge",
    "docs/specs",
    ".codex/hooks.json",
)


@dataclass(frozen=True)
class FrozenCommandResult:
    """Minimal result from one credential-safe isolated command."""

    returncode: int | None
    timed_out: bool


def snapshot_file(snapshot: dict[str, object], name: str) -> bytes:
    """Decode one frozen file and verify its SHA-256 before use."""

    files = snapshot.get("files")
    item = files.get(name) if isinstance(files, dict) else None
    if not isinstance(item, dict):
        raise ScenarioExecutionSupportError(f"Frozen Scenario file is missing: {name}")
    encoded = item.get("content_base64")
    expected = item.get("sha256")
    if not isinstance(encoded, str) or not isinstance(expected, str):
        raise ScenarioExecutionSupportError(f"Frozen Scenario file metadata is invalid: {name}")
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ScenarioExecutionSupportError(f"Frozen Scenario file is not valid Base64: {name}") from exc
    if hashlib.sha256(content).hexdigest() != expected:
        raise ScenarioExecutionSupportError(f"Frozen Scenario file Hash mismatch: {name}")
    return content


def safe_relative_path(value: object, *, label: str) -> PurePosixPath:
    """Require a non-environment relative POSIX path."""

    if not isinstance(value, str) or not value:
        raise ScenarioExecutionSupportError(f"{label} must be a non-empty relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or any(part == ".env" or part.startswith(".env.") for part in path.parts)
    ):
        raise ScenarioExecutionSupportError(f"{label} is unsafe: {value}")
    return path


def _is_environment_path(value: str) -> bool:
    """Return whether an archive member is a dotenv file that must not be exported."""

    path = PurePosixPath(value)
    return any(part == ".env" or part.startswith(".env.") for part in path.parts)


def assert_safe_workspace_tree(root: Path) -> None:
    """Reject links and special files before privileged verification or evidence reads."""

    root = root.resolve()
    if not root.is_dir():
        raise ScenarioExecutionSupportError(f"Scenario workspace is unavailable: {root}")
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        for name in (*directories, *files):
            path = Path(current) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ScenarioExecutionSupportError(f"Scenario workspace contains a symbolic link: {path.relative_to(root)}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ScenarioExecutionSupportError(f"Scenario workspace contains a special file: {path.relative_to(root)}")


def safe_workspace_path(root: Path, relative: PurePosixPath, *, label: str) -> Path:
    """Resolve a workspace-relative path while rejecting every symbolic-link component."""

    root = root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise ScenarioExecutionSupportError(f"{label} crosses a symbolic link: {relative}")
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive after component checks
        raise ScenarioExecutionSupportError(f"{label} escapes the Scenario workspace: {relative}") from exc
    return resolved


def apply_workspace_preconditions(workspace: Path, manifest: dict[str, Any]) -> tuple[str, ...]:
    """Materialize the Case-owned initial HelixHarness state without arbitrary path deletion."""

    policy = manifest.get("workspace_preconditions")
    if policy is None:
        return ()
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schema_version", "helixharness_initialization", "init_mode"}
        or policy.get("schema_version") != "framework-eval-workspace-preconditions.v1"
        or policy.get("helixharness_initialization") != "ABSENT"
        or policy.get("init_mode") not in {"SERVICE", "FRONTEND", "WORKSPACE"}
    ):
        raise ScenarioExecutionSupportError("Scenario workspace preconditions are unsupported")
    workspace = workspace.resolve()
    removed: list[str] = []
    for value in _HELIXHARNESS_INITIALIZATION_PATHS:
        relative = safe_relative_path(value, label="HelixHarness initialization path")
        target = safe_workspace_path(workspace, relative, label="HelixHarness initialization path")
        if target.is_symlink():
            raise ScenarioExecutionSupportError(
                f"HelixHarness initialization path is a symbolic link: {value}"
            )
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(value)
        elif target.exists():
            if not target.is_file():
                raise ScenarioExecutionSupportError(
                    f"HelixHarness initialization path is not a regular file: {value}"
                )
            target.unlink()
            removed.append(value)
    assert_safe_workspace_tree(workspace)
    return tuple(removed)


def export_git_tree(
    repository: Path,
    commit: str,
    target: Path,
    *,
    materialize_safe_symlinks: bool = False,
) -> None:
    """Export one exact Git tree without checkout hooks or path traversal."""

    repository = repository.resolve()
    if not (repository / ".git").exists():
        raise ScenarioExecutionSupportError(f"Repository is not a Git checkout: {repository}")
    if target.exists():
        raise ScenarioExecutionSupportError(f"Git export target already exists: {target}")
    completed = subprocess.run(
        ("git", "-C", str(repository), "archive", "--format=tar", commit),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ScenarioExecutionSupportError(f"Git Commit cannot be exported: {commit}")
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
        deferred_symlinks: list[tuple[PurePosixPath, PurePosixPath]] = []
        for member in archive.getmembers():
            member_name = member.name.rstrip("/")
            if _is_environment_path(member_name):
                continue
            relative = safe_relative_path(member_name, label="Git archive path")
            destination = target.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym() and materialize_safe_symlinks:
                link_target = _archive_link_target(relative, member.linkname)
                deferred_symlinks.append((relative, link_target))
                continue
            if not member.isfile():
                raise ScenarioExecutionSupportError("Git archive contains a non-regular entry")
            source = archive.extractfile(member)
            if source is None:  # pragma: no cover - tarfile contract
                raise ScenarioExecutionSupportError("Git archive member cannot be read")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read())
            destination.chmod(member.mode & 0o777)
        _materialize_archive_symlinks(target, deferred_symlinks)


def _materialize_archive_symlinks(
    root: Path,
    links: Sequence[tuple[PurePosixPath, PurePosixPath]],
) -> None:
    """Copy safe internal Git links as regular files/directories, rejecting cycles."""

    link_targets = dict(links)
    if len(link_targets) != len(links):
        raise ScenarioExecutionSupportError("Git archive contains duplicate symbolic links")
    visiting: set[PurePosixPath] = set()
    materialized: set[PurePosixPath] = set()

    def materialize(relative: PurePosixPath) -> None:
        if relative in materialized:
            return
        if relative in visiting:
            raise ScenarioExecutionSupportError("Git archive link target is unavailable or cyclic")
        visiting.add(relative)
        link_target = link_targets[relative]
        dependencies = sorted(
            (
                candidate
                for candidate in link_targets
                if _path_contains(link_target, candidate) or _path_contains(candidate, link_target)
            ),
            key=lambda item: item.as_posix(),
        )
        for dependency in dependencies:
            materialize(dependency)

        source = root.joinpath(*link_target.parts)
        destination = root.joinpath(*relative.parts)
        if destination.exists() or destination.is_symlink():
            raise ScenarioExecutionSupportError("Git archive symbolic link collides with an exported path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, destination)
        elif source.is_dir():
            shutil.copytree(source, destination)
        else:
            raise ScenarioExecutionSupportError("Git archive link target is unavailable or cyclic")
        visiting.remove(relative)
        materialized.add(relative)

    for relative in sorted(link_targets, key=lambda item: item.as_posix()):
        materialize(relative)


def _path_contains(parent: PurePosixPath, child: PurePosixPath) -> bool:
    """Return whether one safe archive path contains another, including equality."""

    return child == parent or parent in child.parents


def apply_hidden_test_patch(
    workspace: Path,
    patch: bytes,
    *,
    protected_paths: Sequence[str],
    repository: Path,
    base_commit: str,
    merge_root: Path,
) -> str:
    """Inject hidden tests directly or via an isolated Base/Candidate union merge."""

    workspace = workspace.resolve()
    if _apply_patch_direct(workspace, patch):
        return "DIRECT"
    protected = {safe_relative_path(value, label="Hidden test protected path") for value in protected_paths}
    if not protected or len(protected) != len(tuple(protected_paths)):
        raise ScenarioExecutionSupportError("Hidden test protected paths are missing or duplicated")
    merge_root = merge_root.resolve()
    if merge_root.exists():
        raise ScenarioExecutionSupportError("Hidden test merge workspace already exists")
    baseline = merge_root / "baseline"
    instrumented = merge_root / "instrumented"
    export_git_tree(repository, base_commit, baseline, materialize_safe_symlinks=True)
    shutil.copytree(baseline, instrumented)
    if not _apply_patch_direct(instrumented, patch):
        raise ScenarioExecutionSupportError("Frozen hidden test patch does not apply to its Base Commit")
    baseline_files = _tree_file_states(baseline)
    instrumented_files = _tree_file_states(instrumented)
    changed = {
        relative
        for relative in baseline_files.keys() | instrumented_files.keys()
        if baseline_files.get(relative) != instrumented_files.get(relative)
    }
    if not changed or any(
        not any(_path_contains(prefix, relative) for prefix in protected)
        for relative in changed
    ):
        raise ScenarioExecutionSupportError("Frozen hidden test patch changed undeclared protected paths")
    for relative in sorted(changed, key=lambda item: item.as_posix()):
        _merge_protected_file(
            workspace=safe_workspace_path(workspace, relative, label="Hidden test protected path"),
            baseline=baseline.joinpath(*relative.parts),
            instrumented=instrumented.joinpath(*relative.parts),
        )
    assert_safe_workspace_tree(workspace)
    return "THREE_WAY_UNION"


def apply_workspace_fixture_patch(
    workspace: Path,
    manifest: dict[str, Any],
    snapshot: dict[str, object],
    *,
    repository: Path,
    merge_root: Path,
) -> str:
    """Apply one Case-owned build fixture before the Agent starts working."""

    contract = manifest.get("workspace_fixture_patch")
    if contract is None:
        return "NONE"
    if not isinstance(contract, dict):
        raise ScenarioExecutionSupportError("Workspace fixture patch contract is invalid")
    path = contract.get("path")
    expected = contract.get("sha256")
    protected_paths = contract.get("protected_paths")
    repository_contract = manifest.get("repository")
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(expected, str)
        or not isinstance(protected_paths, list)
        or any(not isinstance(item, str) for item in protected_paths)
        or not isinstance(repository_contract, dict)
    ):
        raise ScenarioExecutionSupportError("Workspace fixture patch contract is invalid")
    patch = snapshot_file(snapshot, path)
    if hashlib.sha256(patch).hexdigest() != expected:
        raise ScenarioExecutionSupportError("Workspace fixture patch Hash does not match")
    protected = {
        safe_relative_path(value, label="Workspace fixture protected path")
        for value in protected_paths
    }
    if not protected or len(protected) != len(protected_paths):
        raise ScenarioExecutionSupportError(
            "Workspace fixture protected paths are missing or duplicated"
        )
    validation_root = merge_root.parent / f"{merge_root.name}-validation"
    if validation_root.exists():
        raise ScenarioExecutionSupportError("Workspace fixture validation workspace already exists")
    baseline = validation_root / "baseline"
    instrumented = validation_root / "instrumented"
    try:
        export_git_tree(
            repository,
            str(repository_contract.get("base_commit", "")),
            baseline,
            materialize_safe_symlinks=True,
        )
        shutil.copytree(baseline, instrumented)
        if not _apply_patch_direct(instrumented, patch):
            raise ScenarioExecutionSupportError(
                "Frozen workspace fixture patch does not apply to its Base Commit"
            )
        baseline_files = _tree_file_states(baseline)
        instrumented_files = _tree_file_states(instrumented)
        changed = {
            relative
            for relative in baseline_files.keys() | instrumented_files.keys()
            if baseline_files.get(relative) != instrumented_files.get(relative)
        }
        if changed != protected:
            raise ScenarioExecutionSupportError(
                "Frozen workspace fixture patch changed undeclared protected paths"
            )
    finally:
        shutil.rmtree(validation_root, ignore_errors=True)
    return apply_hidden_test_patch(
        workspace,
        patch,
        protected_paths=protected_paths,
        repository=repository,
        base_commit=str(repository_contract.get("base_commit", "")),
        merge_root=merge_root,
    )


def _apply_patch_direct(workspace: Path, patch: bytes) -> bool:
    # Exported workspaces have no .git; an enclosing checkout would make Git
    # interpret patch paths relative to that checkout and silently skip them.
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        environment.pop(name, None)
    environment["GIT_CEILING_DIRECTORIES"] = str(workspace.resolve().parent)
    for check_only in (True, False):
        command = ["git", "apply", "--whitespace=nowarn", "--recount"]
        if check_only:
            command.append("--check")
        command.append("-")
        completed = subprocess.run(
            command,
            cwd=workspace,
            input=patch,
            check=False,
            capture_output=True,
            env=environment,
        )
        if completed.returncode != 0:
            if check_only:
                return False
            raise ScenarioExecutionSupportError("Frozen hidden test patch changed after preflight")
    return True


def _tree_file_states(root: Path) -> dict[PurePosixPath, tuple[str, int]]:
    assert_safe_workspace_tree(root)
    return {
        PurePosixPath(path.relative_to(root).as_posix()): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _merge_protected_file(*, workspace: Path, baseline: Path, instrumented: Path) -> None:
    base_exists = baseline.is_file()
    instrumented_exists = instrumented.is_file()
    workspace_exists = workspace.is_file()
    if not base_exists and instrumented_exists:
        if workspace_exists and workspace.read_bytes() != instrumented.read_bytes():
            raise ScenarioExecutionSupportError("Candidate collided with a hidden test file")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(instrumented, workspace)
        return
    if base_exists and not instrumented_exists:
        if workspace_exists and workspace.read_bytes() != baseline.read_bytes():
            raise ScenarioExecutionSupportError("Candidate changed a file removed by the hidden test patch")
        if workspace_exists:
            workspace.unlink()
        return
    if not base_exists or not instrumented_exists or not workspace_exists:
        raise ScenarioExecutionSupportError("Hidden test protected file shape is incompatible with Candidate")
    if workspace.read_bytes() == baseline.read_bytes():
        shutil.copy2(instrumented, workspace)
        return
    if workspace.read_bytes() == instrumented.read_bytes():
        return
    merged = subprocess.run(
        ("git", "merge-file", "--union", str(workspace), str(baseline), str(instrumented)),
        check=False,
        capture_output=True,
    )
    if merged.returncode != 0:
        raise ScenarioExecutionSupportError("Candidate and hidden test instrumentation cannot be safely merged")


def _archive_link_target(link_path: PurePosixPath, raw_target: str) -> PurePosixPath:
    target = PurePosixPath(raw_target)
    if target.is_absolute():
        raise ScenarioExecutionSupportError("Git archive link target escapes the repository")
    parts: list[str] = []
    for part in (*link_path.parent.parts, *target.parts):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ScenarioExecutionSupportError("Git archive link target escapes the repository")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise ScenarioExecutionSupportError("Git archive link target is invalid")
    return safe_relative_path(PurePosixPath(*parts).as_posix(), label="Git archive link target")


def frozen_command_environment(
    runtime_root: Path,
    *,
    workspace: Path | None = None,
    gradle_home: Path | None = None,
    gradle_heap_mb: int | None = None,
) -> dict[str, str]:
    """Return a minimal command environment that never forwards provider secrets."""

    environment = {
        key: os.environ[key]
        for key in ("JAVA_HOME", "LANG", "LC_ALL")
        if key in os.environ
    }
    executable_directory = str(Path(sys.executable).parent)
    inherited_path = os.environ.get("PATH", "")
    environment["PATH"] = (
        executable_directory
        if not inherited_path
        else os.pathsep.join((executable_directory, inherited_path))
    )
    if workspace is not None:
        environment["PYTHONPATH"] = str(workspace.resolve())
    home = runtime_root / "home"
    temporary = runtime_root / "tmp"
    resolved_gradle_home = gradle_home or runtime_root / "gradle-user-home"
    cache_home = runtime_root / "cache"
    for directory in (home, temporary, resolved_gradle_home, cache_home):
        directory.mkdir(parents=True, exist_ok=True)
    gradle_environment = (
        {"HELIXHARNESS_PLATFORM_EVALUATION_GRADLE_MAX_HEAP_MB": str(gradle_heap_mb)}
        if gradle_heap_mb is not None
        else None
    )
    configure_evaluator_gradle_home(resolved_gradle_home, environment=gradle_environment)
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "GRADLE_USER_HOME": str(resolved_gradle_home),
            "XDG_CACHE_HOME": str(cache_home),
            "CI": "true",
        }
    )
    return environment


def run_frozen_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> FrozenCommandResult:
    """Run one command in its own process group and fail closed on timeout."""

    try:
        process = subprocess.Popen(
            tuple(argv),
            cwd=cwd,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return FrozenCommandResult(returncode=None, timed_out=False)
    registration = None
    try:
        registration = register_group(process.pid)
        return FrozenCommandResult(
            returncode=process.wait(timeout=timeout_seconds),
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        return FrozenCommandResult(returncode=None, timed_out=True)
    finally:
        kill_group(process.pid)
        process.wait()
        if registration is not None:
            registration.unlink(missing_ok=True)


def git_file(repository: Path, commit: str, relative_path: str) -> bytes:
    """Read one regular file from an exact Commit without checking it out."""

    safe_relative_path(relative_path, label="Repository evidence path")
    completed = subprocess.run(
        ("git", "-C", str(repository.resolve()), "show", f"{commit}:{relative_path}"),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise ScenarioExecutionSupportError(f"Repository evidence is unavailable: {commit}:{relative_path}")
    return completed.stdout


def json_object(content: bytes, *, label: str) -> dict[str, Any]:
    """Parse one frozen UTF-8 JSON object."""

    import json

    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ScenarioExecutionSupportError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ScenarioExecutionSupportError(f"{label} must contain an object")
    return value
