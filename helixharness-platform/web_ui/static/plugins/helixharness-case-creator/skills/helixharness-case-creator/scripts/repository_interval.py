"""Deterministically verify a Scenario v2 repository interval."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Mapping


def _git(repository: Path, *arguments: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        raise ValueError(f"无法验证冻结仓库区间：git {' '.join(arguments[:2])} 失败")
    return result.stdout


def repository_definitions(manifest: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return single-repo or explicit multi-repo interval definitions."""

    if manifest.get("repository_mode") == "MULTI_REPOSITORY":
        workspace = manifest.get("workspace")
        definitions = workspace.get("repositories") if isinstance(workspace, dict) else None
        if not isinstance(definitions, list) or len(definitions) < 2 or any(
            not isinstance(item, dict) for item in definitions
        ):
            raise ValueError("MULTI_REPOSITORY case.json 缺少 workspace.repositories")
        return tuple(definitions)
    definition = manifest.get("repository")
    if not isinstance(definition, dict):
        raise ValueError("case.json 缺少 repository")
    return (definition,)


def parse_repositories(values: list[str] | None) -> dict[str, Path]:
    """Parse repeatable ``repository_id=/checkout`` CLI bindings."""

    repositories: dict[str, Path] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError("--repository 必须使用 repository_id=/checkout")
        repository_id, raw_path = value.split("=", 1)
        if not repository_id or not raw_path or repository_id in repositories:
            raise ValueError("--repository 必须使用唯一的 repository_id=/checkout")
        repositories[repository_id] = Path(raw_path)
    return repositories


def validate_repository_interval(
    manifest: dict[str, Any],
    repository: Mapping[str, Path] | None,
) -> None:
    """Verify all commits, ancestry and exact binary full-index diff hashes."""

    if manifest.get("case_kind") != "REPOSITORY_CASE":
        return
    if not isinstance(repository, Mapping) or not repository:
        raise ValueError("REPOSITORY_CASE 必须按 repository_id 提供本地仓库绑定")
    definitions = repository_definitions(manifest)
    bindings = repository
    for definition in definitions:
        repository_id = str(definition.get("repository_id", ""))
        local = bindings.get(repository_id)
        if local is None:
            raise ValueError(f"缺少本地仓库绑定：{repository_id}")
        _validate_definition(definition, local)


def _validate_definition(definition: dict[str, Any], repository: Path) -> None:
    local_repository = repository.expanduser().resolve()
    if local_repository.is_symlink() or not (local_repository / ".git").exists():
        raise ValueError(f"本地仓库不是可验证的 Git checkout：{local_repository}")
    base = str(definition.get("base_commit", ""))
    solution = str(definition.get("solution_commit", ""))
    expected_patch_hash = str(definition.get("solution_patch_sha256", ""))
    resolved_base = str(_git(local_repository, "rev-parse", "--verify", f"{base}^{{commit}}")).strip()
    resolved_solution = str(_git(local_repository, "rev-parse", "--verify", f"{solution}^{{commit}}")).strip()
    if resolved_base != base or resolved_solution != solution:
        raise ValueError("仓库区间必须使用精确 40 位 commit")
    _git(local_repository, "merge-base", "--is-ancestor", base, solution)
    patch = _git(local_repository, "diff", "--binary", "--full-index", f"{base}..{solution}", text=False)
    if not isinstance(patch, bytes) or hashlib.sha256(patch).hexdigest() != expected_patch_hash:
        raise ValueError("solution_patch_sha256 与冻结 commit 区间的真实 diff 不一致")
