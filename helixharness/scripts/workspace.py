#!/usr/bin/env python3
"""Deterministic guards and coordinator plans for federated Lite workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

try:
    import workflow as lifecycle
    from workflow import (
        WorkflowError,
        ReviewScopeDrift,
        archive_preflight,
        completed_archive_preflight,
        feature_name,
        markdown_table_rows,
        read_regular,
        spec_contract,
        spec_receipt,
        validate_tasks,
    )
except ImportError:  # pragma: no cover
    from scripts import workflow as lifecycle
    from scripts.workflow import (
        WorkflowError,
        ReviewScopeDrift,
        archive_preflight,
        completed_archive_preflight,
        feature_name,
        markdown_table_rows,
        read_regular,
        spec_contract,
        spec_receipt,
        validate_tasks,
    )


REPO_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EMPTY_LIST_VALUES = {"", "-", "—", "none", "None", "N/A"}
OVERVIEW_HEADINGS = (
    "背景、目标与范围",
    "总体架构",
    "仓库职责与依赖",
    "跨仓接口与数据契约",
    "端到端业务流程",
    "状态、数据归属与一致性",
    "兼容、发布与回滚",
    "稳定性、安全与可观测性",
    "跨仓验收标准",
    "Repo Handoff",
    "已确认决策与非目标",
)


class WorkspaceError(ValueError):
    """Raised when a Lite workspace violates its public contract."""


def workspace_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceError("workspace root is not a directory")
    marker = root / "helixharness.workspace.yaml"
    if marker.is_symlink() or not marker.is_file():
        raise WorkspaceError("helixharness.workspace.yaml is missing or unsafe")
    return root


def _path_without_symlinks(base: Path | None, value: Path, label: str) -> Path:
    """Resolve an explicit path while rejecting every traversed symlink."""
    if value.is_absolute():
        current = Path(value.anchor)
        parts = value.parts[1:]
    else:
        if base is None:
            raise WorkspaceError(f"{label} has no base directory")
        current = base
        parts = value.parts
    for part in parts:
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise WorkspaceError(f"{label} traverses a symlink")
    return current


def _yaml_scalar(value: str) -> str:
    value = value.split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_registry(root: str | Path) -> dict[str, Path]:
    """Load the bounded repos.<key>.path contract without a YAML dependency."""
    root = workspace_root(root)
    text = read_regular(root / "helixharness.workspace.yaml", "helixharness.workspace.yaml")
    repos: dict[str, Path] = {}
    seen_keys: set[str] = set()
    in_repos = False
    current: str | None = None
    current_path: str | None = None

    def finish_current() -> None:
        nonlocal current, current_path
        if current is None:
            return
        if current_path is None:
            raise WorkspaceError(f"repo {current} has no path")
        raw_path = Path(current_path).expanduser()
        candidate = _path_without_symlinks(
            None if raw_path.is_absolute() else root,
            raw_path,
            f"repo {current} path",
        )
        target = candidate.resolve()
        if not target.is_dir():
            raise WorkspaceError(f"repo {current} path is missing or unsafe")
        if target == root:
            raise WorkspaceError(
                f"repo {current} path cannot be the workspace root"
            )
        if target in repos.values():
            raise WorkspaceError(f"repo {current} duplicates another repo path")
        repos[current] = target
        current = None
        current_path = None

    for raw in text.splitlines():
        if not in_repos:
            if re.fullmatch(r"repos:\s*(?:#.*)?", raw):
                in_repos = True
            continue
        if raw and not raw.startswith((" ", "\t", "#")):
            finish_current()
            break
        key_match = re.match(r"^  ([^\s:#][^:]*):\s*(?:#.*)?$", raw)
        if key_match:
            finish_current()
            key = _yaml_scalar(key_match.group(1))
            if not REPO_KEY.fullmatch(key):
                raise WorkspaceError(f"invalid repo key {key or '<empty>'}")
            if key in seen_keys:
                raise WorkspaceError(f"duplicate repo key {key}")
            seen_keys.add(key)
            current = key
            continue
        path_match = re.match(r"^    path:\s*(.*?)\s*$", raw)
        if path_match and current is not None:
            if current_path is not None:
                raise WorkspaceError(f"repo {current} has duplicate path")
            current_path = _yaml_scalar(path_match.group(1))
            if not current_path:
                raise WorkspaceError(f"repo {current} has empty path")
    else:
        if in_repos:
            finish_current()

    if not in_repos:
        raise WorkspaceError("helixharness.workspace.yaml has no repos mapping")
    if not repos:
        raise WorkspaceError("helixharness.workspace.yaml has no repos")
    return repos


def _feature_directory(root: Path, feature: str, state: str = "active") -> Path:
    directory = _path_without_symlinks(
        root,
        Path("docs") / "specs" / state / feature_name(feature),
        f"workspace {state} feature directory",
    )
    if not directory.is_dir():
        raise WorkspaceError(
            f"workspace {state} feature directory is missing or unsafe"
        )
    return directory


def _list_values(value: str, *, pattern: str, label: str) -> list[str]:
    if value in EMPTY_LIST_VALUES:
        return []
    values = re.findall(pattern, value)
    residue = re.sub(pattern, "", value)
    if re.sub(r"[\s,，、;；]", "", residue):
        raise WorkspaceError(f"{label} is invalid")
    return values


def _validate_repo_dag(repositories: list[dict[str, Any]]) -> None:
    names = {str(item["repo"]) for item in repositories}
    dependencies = {
        str(item["repo"]): [str(value) for value in item["depends_on"]]
        for item in repositories
    }
    for repo, required in dependencies.items():
        if repo in required:
            raise WorkspaceError(f"repo {repo} cannot depend on itself")
        unknown = sorted(set(required) - names)
        if unknown:
            raise WorkspaceError(
                f"repo {repo} depends on unknown repos: {', '.join(unknown)}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(repo: str) -> None:
        if repo in visiting:
            raise WorkspaceError("workspace repository dependency graph has a cycle")
        if repo in visited:
            return
        visiting.add(repo)
        for dependency in dependencies[repo]:
            visit(dependency)
        visiting.remove(repo)
        visited.add(repo)

    for repo in sorted(names):
        visit(repo)


def validate_workspace_overview(
    root: str | Path,
    feature: str,
    stage: str,
    *,
    state: str = "active",
) -> dict[str, Any]:
    """Validate the one-file Workspace contract and return its static repo DAG."""
    root = workspace_root(root)
    registry = load_registry(root)
    directory = _feature_directory(root, feature, state)
    try:
        entries = sorted(path.name for path in directory.iterdir())
    except OSError as exc:
        raise WorkspaceError("workspace feature directory is unreadable") from exc
    if entries != ["spec.md"]:
        raise WorkspaceError(
            "Workspace Lite feature directory must contain only spec.md"
        )

    spec_text = read_regular(directory / "spec.md", "workspace spec.md")
    spec = spec_contract(spec_text)
    expected = {
        "feature": feature,
        "execution_scope": "workspace",
        "testing_seam_confirmed": "true",
    }
    for field, value in expected.items():
        if spec.get(field) != value:
            raise WorkspaceError(f"workspace spec.md has invalid {field}")
    statuses = {
        "propose": {"planning", "ready"},
        "apply": {"ready", "applying", "implemented"},
        "archive": {"implemented", "completed"},
        "completed": {"completed"},
    }
    if stage not in statuses:
        raise WorkspaceError("workspace validation stage is invalid")
    if spec.get("status") not in statuses[stage]:
        raise WorkspaceError(f"workspace spec.md status is invalid for {stage}")

    for heading in OVERVIEW_HEADINGS:
        if not re.search(
            rf"(?m)^##[ \t]+(?:\d+\.[ \t]+)?{re.escape(heading)}[ \t]*$",
            spec_text,
        ):
            raise WorkspaceError(
                f"workspace spec.md is missing required section {heading}"
            )
    mermaid_blocks = re.findall(
        r"```mermaid\s*\n(.*?)```", spec_text, flags=re.DOTALL | re.IGNORECASE
    )
    if not any(
        re.search(r"(?m)^\s*(?:flowchart|graph)\b", block)
        for block in mermaid_blocks
    ):
        raise WorkspaceError(
            "workspace spec.md is missing a Mermaid architecture diagram"
        )
    if not any(
        re.search(r"(?m)^\s*sequenceDiagram\b", block)
        for block in mermaid_blocks
    ):
        raise WorkspaceError(
            "workspace spec.md is missing a Mermaid sequence diagram"
        )
    contract_ids = sorted(
        set(re.findall(r"(?m)^###[ \t]+(C-[0-9]+)\b", spec_text))
    )
    if not contract_ids:
        raise WorkspaceError("workspace spec.md has no cross-repo contract IDs")

    try:
        _, rows = markdown_table_rows(
            spec_text,
            label="workspace spec.md",
            table_name="repository responsibility",
            required_header={
                "repo",
                "mode",
                "responsibility",
                "depends on",
                "contract refs",
            },
        )
    except WorkflowError as exc:
        raise WorkspaceError(str(exc)) from exc
    repositories: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        repo = row["repo"]
        if repo not in registry:
            raise WorkspaceError(
                f"workspace repository {repo or '<empty>'} "
                "is not in helixharness.workspace.yaml"
            )
        if repo in seen:
            raise WorkspaceError(f"workspace repository {repo} is duplicated")
        seen.add(repo)
        mode = row["mode"]
        if mode not in {"write", "read"}:
            raise WorkspaceError(f"workspace repository {repo} has invalid mode")
        if not row["responsibility"]:
            raise WorkspaceError(
                f"workspace repository {repo} has empty responsibility"
            )
        depends_on = _list_values(
            row["depends on"],
            pattern=r"[A-Za-z0-9][A-Za-z0-9._-]*",
            label=f"repo {repo} dependencies",
        )
        refs = _list_values(
            row["contract refs"],
            pattern=r"C-[0-9]+",
            label=f"repo {repo} contract refs",
        )
        if not refs or not set(refs).issubset(contract_ids):
            raise WorkspaceError(
                f"repo {repo} has unknown or empty contract refs"
            )
        if not re.search(
            rf"(?m)^###[ \t]+{re.escape(repo)}[ \t]*$", spec_text
        ):
            raise WorkspaceError(
                f"workspace spec.md has no Repo Handoff for {repo}"
            )
        repositories.append(
            {
                "contract_refs": refs,
                "depends_on": depends_on,
                "mode": mode,
                "path": str(registry[repo]),
                "repo": repo,
                "responsibility": row["responsibility"],
            }
        )
    _validate_repo_dag(repositories)
    return {
        "feature": feature,
        "repositories": repositories,
        "repos": [str(item["repo"]) for item in repositories],
        "spec_receipt_sha256": spec_receipt(spec_text),
        "stage": stage,
        "status": spec.get("status"),
    }


def _execution_batches(repositories: list[dict[str, Any]]) -> list[list[str]]:
    remaining = {
        str(item["repo"]): set(item["depends_on"]) for item in repositories
    }
    batches: list[list[str]] = []
    completed: set[str] = set()
    order = [str(item["repo"]) for item in repositories]
    while remaining:
        batch = [
            repo
            for repo in order
            if repo in remaining and remaining[repo] <= completed
        ]
        if not batch:
            raise WorkspaceError(
                "workspace repository dependency graph has a cycle"
            )
        batches.append(batch)
        completed.update(batch)
        for repo in batch:
            remaining.pop(repo)
    return batches


def _repo_job_id(command: str, repo: str) -> str:
    """Return a stable Codex-safe identity without trusting registry spelling."""
    prefix = f"workspace_{command}_"
    digest = hashlib.sha256(repo.encode("ascii")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "_", repo.lower()).strip("_") or "repo"
    slug = slug[: 64 - len(prefix) - len(digest) - 1].rstrip("_") or "repo"
    return f"{prefix}{slug}_{digest}"


def _validate_repo_identity(
    repo_root: Path,
    feature: str,
    expected_repo: str,
    workspace_receipt: str,
    *,
    state: str = "active",
) -> None:
    if state not in {"active", "completed"}:
        raise WorkspaceError("repo identity state is invalid")
    directory = _path_without_symlinks(
        repo_root,
        Path("docs") / "specs" / state / feature_name(feature),
        f"repo {expected_repo} {state} feature directory",
    )
    spec = spec_contract(
        read_regular(directory / "spec.md", f"repo {expected_repo} spec.md")
    )
    if spec.get("execution_scope") != "single":
        raise WorkspaceError(
            f"repo {expected_repo} spec.md must declare execution_scope: single"
        )
    if spec.get("workspace_repo") != expected_repo:
        raise WorkspaceError(
            f"repo {expected_repo} spec.md has invalid workspace_repo"
        )
    if spec.get("workspace_contract_sha256") != workspace_receipt:
        raise WorkspaceError(
            f"repo {expected_repo} spec.md workspace contract receipt is stale"
        )


def workspace_revision_context(
    repo_root: Path, feature: str, source: str
) -> dict[str, Any] | None:
    """Recognize only an explicit, registry-owned Workspace source for this child."""
    source_path = Path(os.path.abspath(repo_root / Path(source).expanduser()))
    if tuple(source_path.parts[-5:]) != (
        "docs", "specs", "active", feature_name(feature), "spec.md"
    ):
        return None
    root = source_path.parents[4]
    if not os.path.lexists(root / "helixharness.workspace.yaml"):
        return None
    overview = validate_workspace_overview(root, feature, "propose")
    owners = [
        item for item in overview["repositories"]
        if Path(str(item["path"])) == repo_root.resolve()
    ]
    if len(owners) != 1:
        raise WorkspaceError("Workspace source does not own this repository")
    owner = str(owners[0]["repo"])
    completed = _path_without_symlinks(
        repo_root, Path("docs/specs/completed") / feature, "completed child feature"
    )
    if os.path.lexists(completed):
        raise WorkspaceError("Workspace revision cannot reopen an archived child")
    directory = _path_without_symlinks(
        repo_root, Path("docs/specs/active") / feature, "active child feature"
    )
    if not (directory / "spec.md").exists():
        return None
    spec = spec_contract(read_regular(directory / "spec.md", "spec.md"))
    if (
        spec.get("feature") != feature
        or spec.get("execution_scope") != "single"
        or spec.get("workspace_repo") != owner
        or not re.fullmatch(r"[0-9a-f]{64}", spec.get("workspace_contract_sha256", ""))
    ):
        raise WorkspaceError("Workspace revision child identity is invalid")
    if spec.get("status") not in {"planning", "ready", "applying", "implemented"}:
        raise WorkspaceError("Workspace revision child stage is invalid")
    receipt = str(overview["spec_receipt_sha256"])
    key = hashlib.sha256(
        f"workspace-contract-revision\n{feature}\n{owner}\n{receipt}".encode("utf-8")
    ).hexdigest()
    repair = lifecycle._repair_state(repo_root, feature)
    unfinished = repair is not None and repair["status"] == "in_progress"
    if unfinished and repair["repair_key"] != key:
        raise WorkspaceError("another child revision is already in progress")
    apply_path = lifecycle._state_path(repo_root, feature, "apply-state.json")
    required = (
        spec["workspace_contract_sha256"] != receipt
        or unfinished
        or (spec["status"] == "planning" and os.path.lexists(apply_path))
    )
    return {
        "required": required,
        "workspace_root": str(root),
        "repo": owner,
        "source": str(source_path),
        "source_sha256": receipt,
        "repair_key": key,
        "repair_kind": "contract_amendment",
        "child_status": spec["status"],
    }


def begin_repo_revision(
    root: str | Path, feature: str, repo: str, expected_workspace_receipt: str
) -> dict[str, Any]:
    """Lock a child revision using the current root contract, preserving Apply state."""
    root = workspace_root(root)
    registry = load_registry(root)
    if repo not in registry:
        raise WorkspaceError("Workspace revision repository is not registered")
    source = root / "docs/specs/active" / feature_name(feature) / "spec.md"
    context = workspace_revision_context(registry[repo], feature, str(source))
    if context is None or context["source_sha256"] != expected_workspace_receipt:
        raise WorkspaceError("Workspace revision source receipt changed")
    old_repair = lifecycle._repair_state(registry[repo], feature)
    if not context["required"] and (
        old_repair is None or old_repair["repair_key"] != context["repair_key"]
    ):
        raise WorkspaceError("Workspace contract is already bound")
    facts = lifecycle._repair_preflight(registry[repo], feature)
    result = lifecycle.begin_repair(
        registry[repo], feature,
        repair_key=context["repair_key"],
        source_sha256=context["source_sha256"],
        repair_kind="contract_amendment",
        expected_spec_receipt=facts["spec_receipt_sha256"],
        expected_tasks_receipt=facts["tasks_receipt_sha256"] or "absent",
        workspace_contract={"repo": repo, "sha256": context["source_sha256"]},
    )
    return {**context, **result}


def workspace_execution_plan(
    root: str | Path, feature: str, command: str, *, commit_option: str | None = None,
    allow_review_choice: bool = False
) -> dict[str, Any]:
    """Build jobs that invoke each repo's complete Single Lite command."""
    if command not in {"propose", "apply", "archive"}:
        raise WorkspaceError("workspace command is invalid")
    if commit_option not in {None, "--commit", "--no-commit"}:
        raise WorkspaceError("archive commit option is invalid")
    if commit_option is not None and command != "archive":
        raise WorkspaceError("commit options require archive")
    if allow_review_choice and command != "archive":
        raise WorkspaceError("review choice requires archive")
    overview = validate_workspace_overview(root, feature, command)
    resolved_root = Path(str(root)).resolve()
    workspace_feature_root = (
        resolved_root
        / "docs"
        / "specs"
        / "active"
        / feature_name(feature)
    )
    plugin_root = Path(__file__).resolve().parents[1]
    quoted_feature = shlex.quote(feature)
    repo_results: dict[str, Any] = {}
    jobs: list[dict[str, Any]] = []
    for item in overview["repositories"]:
        repo = str(item["repo"])
        repo_root = Path(str(item["path"]))
        revision = None
        if command == "propose":
            revision = workspace_revision_context(
                repo_root, feature, str(workspace_feature_root / "spec.md")
            )
            if revision is not None and not revision["required"] and revision["child_status"] != "planning":
                repo_results[repo] = validate_tasks(repo_root, feature, "apply")
                continue
        if command == "apply":
            try:
                _validate_repo_identity(
                    repo_root,
                    feature,
                    repo,
                    str(overview["spec_receipt_sha256"]),
                )
                repo_results[repo] = validate_tasks(repo_root, feature, "apply")
            except (WorkflowError, OSError) as exc:
                raise WorkspaceError(
                    f"repo {repo} Single Lite artifacts are invalid: {exc}"
                ) from exc
        elif command == "archive":
            active = (
                repo_root / "docs" / "specs" / "active" / feature_name(feature)
            )
            completed = (
                repo_root
                / "docs"
                / "specs"
                / "completed"
                / feature_name(feature)
            )
            try:
                if (
                    active.is_dir()
                    and not active.is_symlink()
                    and not os.path.lexists(completed)
                ):
                    _validate_repo_identity(
                        repo_root,
                        feature,
                        repo,
                        str(overview["spec_receipt_sha256"]),
                    )
                    try:
                        repo_results[repo] = archive_preflight(
                            repo_root, feature, allow_review_acceptance=allow_review_choice
                        )
                    except ReviewScopeDrift as exc:
                        if not allow_review_choice or exc.finalizing or overview["status"] == "completed":
                            raise
                        repo_results[repo] = exc.choice
                elif (
                    completed.is_dir()
                    and not completed.is_symlink()
                    and not os.path.lexists(active)
                ):
                    _validate_repo_identity(
                        repo_root,
                        feature,
                        repo,
                        str(overview["spec_receipt_sha256"]),
                        state="completed",
                    )
                    repo_results[repo] = completed_archive_preflight(
                        repo_root, feature
                    )
                else:
                    raise WorkflowError(
                        "active/completed feature location is ambiguous"
                    )
            except (WorkflowError, OSError) as exc:
                raise WorkspaceError(
                    f"repo {repo} Single Lite archive is invalid: {exc}"
                ) from exc
        arguments = shlex.join([feature, *([commit_option] if commit_option else [])])
        completion_check: dict[str, Any]
        if command == "propose":
            completion_check = {
                "command": "validate-tasks",
                "stage": "apply" if revision is not None and revision["required"] else "propose",
            }
        elif command == "apply":
            completion_check = {"command": "archive-preflight"}
        else:
            completion_check = {
                "command": "archive-code-scope",
                "completed": True,
            }
        artifact_state = "completed" if command == "archive" else "active"
        artifact_root = (
            repo_root
            / "docs"
            / "specs"
            / artifact_state
            / feature_name(feature)
        )
        expected_artifacts = [
            str(artifact_root / name)
            for name in ("spec.md", "tasks.md", "verify-report.md")
            if command != "propose" or name != "verify-report.md"
        ]
        job_id = _repo_job_id(command, repo)
        job: dict[str, Any] = {
            "arguments": arguments,
            "command": f"/helix:{command} {arguments}",
            "completion_check": completion_check,
            "cwd": str(repo_root),
            "delegation_policy": "may_delegate" if command == "apply" else "leaf",
            "depends_on": list(item["depends_on"]),
            "expected_artifacts": expected_artifacts,
            "expected_execution_scope": "single",
            "expected_result": {
                "job_id": job_id,
                "repo": repo,
                "stage": command,
                "status": "completed",
                "workflow": f"workflows/single/{command}.md",
            },
            "expected_workflow": f"workflows/single/{command}.md",
            "forbidden_scope": [
                str(workspace_feature_root),
                *[
                    str(Path(str(other["path"])))
                    for other in overview["repositories"]
                    if str(other["repo"]) != repo
                ],
            ],
            "interaction_mode": "host_native",
            "job_id": job_id,
            "mode": str(item["mode"]),
            "ownership_root": str(repo_root),
            "prompt_source": f"commands/{command}.md",
            "read_scope": [
                str(plugin_root),
                str(resolved_root / "helixharness.workspace.yaml"),
                str(workspace_feature_root / "spec.md"),
                str(repo_root),
                *[
                    str(Path(str(dependency["path"])))
                    for dependency in overview["repositories"]
                    if command in {"apply", "archive"}
                    and dependency["repo"] in item["depends_on"]
                ],
            ],
            "repo": repo,
            "stage": command,
            "unavailable_fallback": "serial_inline_same_command_contract",
            "validation": completion_check,
            "write_scope": [str(repo_root)],
        }
        if command == "propose":
            if revision is not None and revision["required"]:
                job["workspace_revision"] = revision
            source = (
                resolved_root
                / "docs"
                / "specs"
                / "active"
                / feature_name(feature)
                / "spec.md"
            )
            arguments = (
                f"{quoted_feature} {shlex.quote(str(source))}"
            )
            job.update(
                {
                    "arguments": arguments,
                    "command": f"/helix:propose {arguments}",
                    "source": str(source),
                    "workspace_contract_sha256": str(
                        overview["spec_receipt_sha256"]
                    ),
                }
            )
        jobs.append(job)
    batches = _execution_batches(overview["repositories"])
    if command == "archive":
        batches = list(reversed(batches))
    return {
        **overview,
        "batches": batches,
        "command": command,
        "jobs": jobs,
        "repo_results": repo_results,
    }


def workspace_archive_preflight(
    root: str | Path, feature: str, *, allow_review_choice: bool = False
) -> dict[str, Any]:
    root = workspace_root(root)
    completed = _path_without_symlinks(
        root,
        Path("docs") / "specs" / "completed" / feature_name(feature),
        "workspace completed feature destination",
    )
    if not completed.parent.is_dir():
        raise WorkspaceError(
            "workspace completed feature parent is missing or unsafe"
        )
    if os.path.lexists(completed):
        raise WorkspaceError("workspace completed feature already exists")
    result = workspace_execution_plan(
        root, feature, "archive", allow_review_choice=allow_review_choice
    )
    pending = sorted(repo for repo, check in result["repo_results"].items()
                     if check.get("decision_needed") == "review_scope_changed")
    return {
        **result,
        "archive_state": (
            "ready" if result["status"] == "implemented" else "finalizing"
        ),
        "ready": not pending,
        "pending_review_repos": pending,
        **({"decision_needed": "review_scope_changed"} if pending else {}),
    }


def workspace_completed_preflight(
    root: str | Path, feature: str
) -> dict[str, Any]:
    root = workspace_root(root)
    active = _path_without_symlinks(
        root,
        Path("docs") / "specs" / "active" / feature_name(feature),
        "workspace active feature location",
    )
    if os.path.lexists(active):
        raise WorkspaceError("workspace active feature still exists")
    overview = validate_workspace_overview(
        root, feature, "completed", state="completed"
    )
    repo_results: dict[str, Any] = {}
    for item in overview["repositories"]:
        repo = str(item["repo"])
        try:
            _validate_repo_identity(
                Path(str(item["path"])),
                feature,
                repo,
                str(overview["spec_receipt_sha256"]),
                state="completed",
            )
            repo_results[repo] = completed_archive_preflight(
                Path(str(item["path"])), feature
            )
        except WorkflowError as exc:
            raise WorkspaceError(
                f"repo {repo} completed Single Lite archive is invalid: {exc}"
            ) from exc
    return {
        **overview,
        "archive_state": "completed",
        "ready": True,
        "repo_results": repo_results,
    }


def _command_validate(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if args.stage == "propose":
        return validate_workspace_overview(root, args.feature, "propose")
    if args.stage == "apply":
        return workspace_execution_plan(root, args.feature, "apply")
    if args.stage == "archive":
        return workspace_archive_preflight(root, args.feature)
    return workspace_completed_preflight(root, args.feature)


def _command_plan(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    return workspace_execution_plan(
        root, args.feature, args.command_name, commit_option=args.commit_option,
        allow_review_choice=args.review_choice,
    )


def _command_archive(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    return workspace_archive_preflight(root, args.feature, allow_review_choice=args.review_choice)


def _command_completed(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    return workspace_completed_preflight(root, args.feature)


def _command_begin_revision(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    return begin_repo_revision(root, args.feature, args.repo, args.expected_workspace_receipt)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--workspace-root", required=True)
    commands = root.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("workspace-validate")
    validate.add_argument("--feature", required=True)
    validate.add_argument(
        "--stage",
        choices=("propose", "apply", "archive", "completed"),
        required=True,
    )
    validate.set_defaults(handler=_command_validate)

    plan = commands.add_parser("workspace-plan")
    plan.add_argument("--feature", required=True)
    plan.add_argument(
        "--command",
        dest="command_name",
        choices=("propose", "apply", "archive"),
        required=True,
    )
    commit = plan.add_mutually_exclusive_group()
    commit.add_argument("--commit", dest="commit_option", action="store_const", const="--commit")
    commit.add_argument("--no-commit", dest="commit_option", action="store_const", const="--no-commit")
    plan.add_argument("--review-choice", action="store_true")
    plan.set_defaults(handler=_command_plan)

    revision = commands.add_parser("workspace-begin-revision")
    revision.add_argument("--feature", required=True)
    revision.add_argument("--repo", required=True)
    revision.add_argument("--expected-workspace-receipt", required=True)
    revision.set_defaults(handler=_command_begin_revision)

    archive = commands.add_parser("workspace-archive-preflight")
    archive.add_argument("--feature", required=True)
    archive.add_argument("--review-choice", action="store_true")
    archive.set_defaults(handler=_command_archive)

    completed = commands.add_parser("workspace-completed-preflight")
    completed.add_argument("--feature", required=True)
    completed.set_defaults(handler=_command_completed)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        payload = args.handler(workspace_root(args.workspace_root), args)
    except (WorkspaceError, WorkflowError) as exc:
        print(f"invalid Lite workspace: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
