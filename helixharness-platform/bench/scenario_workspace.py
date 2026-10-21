"""Normalized single- and multi-repository contracts for Scenario v2 Cases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

SINGLE_REPOSITORY = "SINGLE_REPOSITORY"
MULTI_REPOSITORY = "MULTI_REPOSITORY"
REPOSITORY_MODES = frozenset({SINGLE_REPOSITORY, MULTI_REPOSITORY})
EMPTY_GIT_DIFF_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@dataclass(frozen=True)
class ScenarioRepository:
    """One frozen Git member of a Scenario workspace."""

    repository_id: str
    mount_path: str
    impact: str
    base_commit: str
    solution_commit: str
    solution_patch_sha256: str
    primary: bool = False


@dataclass(frozen=True)
class ScenarioWorkspace:
    """Execution topology derived without rewriting frozen Case bytes."""

    repository_mode: str
    primary_repository_id: str
    repositories: tuple[ScenarioRepository, ...]

    @property
    def is_multi_repository(self) -> bool:
        return self.repository_mode == MULTI_REPOSITORY


def scenario_workspace(manifest: dict[str, Any]) -> ScenarioWorkspace:
    """Normalize a single-repo ``repository`` or an explicit multi-repo workspace."""

    mode = manifest.get("repository_mode")
    workspace = manifest.get("workspace")
    if mode is None and isinstance(manifest.get("repository"), dict):
        repository = _repository(manifest["repository"], mount_path=".", impact="WRITE", primary=True)
        return ScenarioWorkspace(SINGLE_REPOSITORY, repository.repository_id, (repository,))
    if mode == SINGLE_REPOSITORY:
        if workspace is not None:
            raise ValueError("SINGLE_REPOSITORY Case must not declare workspace")
        repository = _repository(manifest.get("repository"), mount_path=".", impact="WRITE", primary=True)
        return ScenarioWorkspace(SINGLE_REPOSITORY, repository.repository_id, (repository,))
    if mode != MULTI_REPOSITORY or not isinstance(workspace, dict):
        raise ValueError("REPOSITORY_CASE must declare a single repository or a supported repository_mode")
    if manifest.get("repository") is not None:
        raise ValueError("MULTI_REPOSITORY Case must not declare single repository")
    raw_repositories = workspace.get("repositories")
    primary_id = workspace.get("primary_repository_id")
    if not isinstance(raw_repositories, list) or len(raw_repositories) < 2:
        raise ValueError("MULTI_REPOSITORY workspace requires at least two repositories")
    if not isinstance(primary_id, str) or not primary_id:
        raise ValueError("MULTI_REPOSITORY workspace requires primary_repository_id")
    repositories = tuple(
        _repository(
            value,
            mount_path=None,
            impact=None,
            primary=isinstance(value, dict) and value.get("repository_id") == primary_id,
        )
        for value in raw_repositories
    )
    repository_ids = [item.repository_id for item in repositories]
    mount_paths = [item.mount_path for item in repositories]
    if len(set(repository_ids)) != len(repository_ids):
        raise ValueError("MULTI_REPOSITORY repository_id values must be unique")
    if len(set(mount_paths)) != len(mount_paths):
        raise ValueError("MULTI_REPOSITORY mount_path values must be unique")
    if "." in mount_paths:
        raise ValueError("MULTI_REPOSITORY repositories must use distinct child mount_path values")
    if primary_id not in repository_ids:
        raise ValueError("primary_repository_id must name a workspace repository")
    if not any(item.impact == "WRITE" for item in repositories):
        raise ValueError("MULTI_REPOSITORY workspace requires at least one WRITE repository")
    for item in repositories:
        if item.impact == "READ" and (
            item.base_commit != item.solution_commit
            or item.solution_patch_sha256 != EMPTY_GIT_DIFF_SHA256
        ):
            raise ValueError(
                "READ repository must freeze an unchanged base/solution interval: "
                f"{item.repository_id}"
            )
    return ScenarioWorkspace(MULTI_REPOSITORY, primary_id, repositories)


def _repository(
    value: object,
    *,
    mount_path: str | None,
    impact: str | None,
    primary: bool,
) -> ScenarioRepository:
    if not isinstance(value, dict):
        raise ValueError("Scenario repository contract must be an object")
    repository_id = value.get("repository_id")
    resolved_mount = mount_path if mount_path is not None else value.get("mount_path")
    resolved_impact = impact if impact is not None else value.get("impact")
    if not isinstance(repository_id, str) or not repository_id:
        raise ValueError("Scenario repository_id must be non-empty text")
    if not isinstance(resolved_mount, str) or not resolved_mount:
        raise ValueError("Scenario repository mount_path must be non-empty text")
    path = PurePosixPath(resolved_mount)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != resolved_mount
        or (resolved_mount != "." and len(path.parts) != 1)
    ):
        raise ValueError(f"Scenario repository mount_path is unsafe: {resolved_mount}")
    if resolved_impact not in {"WRITE", "READ"}:
        raise ValueError("Scenario repository impact must be WRITE or READ")
    return ScenarioRepository(
        repository_id=repository_id,
        mount_path=resolved_mount,
        impact=resolved_impact,
        base_commit=str(value.get("base_commit", "")),
        solution_commit=str(value.get("solution_commit", "")),
        solution_patch_sha256=str(value.get("solution_patch_sha256", "")),
        primary=primary,
    )
