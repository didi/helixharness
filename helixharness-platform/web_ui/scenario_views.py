"""Shared user-facing projections for workflow-neutral evaluation Scenarios."""

from __future__ import annotations

import json
import re
import subprocess
from base64 import b64decode
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from bench.case_difficulty import (
    UNASSESSED_DIFFICULTY,
    CaseDifficultyAssessment,
    assess_case_difficulty,
)
from bench.scenario_storage import PublishedScenario
from bench.scenario_workspace import MULTI_REPOSITORY, scenario_workspace

_DIFFICULTY_LABELS = {
    "EASY": "简单",
    "MEDIUM": "中等",
    "HARD": "困难",
    "UNASSESSED": "待评估",
}

_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")


def _display_text(value: object) -> str:
    """Remove source-format heading markers without changing frozen Case data."""

    return _MARKDOWN_HEADING_RE.sub("", str(value or "")).strip()


def _display_requirement_text(value: object, requirement_id: object) -> str:
    """Remove a duplicated leading requirement ID from its display statement."""

    text = _display_text(value)
    identifier = str(requirement_id or "").strip()
    if not identifier:
        return text
    return re.sub(
        rf"^{re.escape(identifier)}(?:\s*[:：]\s*|\s+)",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()


def snapshot_file_text(snapshot: dict[str, object], path: object) -> str:
    """Decode one UTF-8 file from an immutable Case snapshot."""

    if not isinstance(path, str) or not path:
        return ""
    files = snapshot.get("files")
    payload = files.get(path) if isinstance(files, dict) else None
    encoded = payload.get("content_base64") if isinstance(payload, dict) else None
    if not isinstance(encoded, str):
        return ""
    try:
        return b64decode(encoded).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""


def snapshot_json(snapshot: dict[str, object], path: object) -> dict[str, object]:
    """Decode one JSON object from an immutable Case snapshot."""

    try:
        value = json.loads(snapshot_file_text(snapshot, path))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=1_024)
def _cached_difficulty(
    repository_path: str,
    base_commit: str,
    solution_commit: str,
) -> CaseDifficultyAssessment:
    """Reuse immutable Git interval measurements across Case list requests."""

    try:
        return assess_case_difficulty(
            Path(repository_path),
            base_commit,
            solution_commit,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return UNASSESSED_DIFFICULTY


def _scenario_difficulty(
    manifest: dict[str, object],
    repositories: Mapping[str, Path] | None,
) -> CaseDifficultyAssessment:
    if repositories is None:
        return UNASSESSED_DIFFICULTY
    try:
        workspace = scenario_workspace(manifest)
    except ValueError:
        return UNASSESSED_DIFFICULTY
    assessments: list[CaseDifficultyAssessment] = []
    for repository in workspace.repositories:
        repository_path = repositories.get(repository.repository_id)
        if repository_path is None or not repository_path.is_dir():
            return UNASSESSED_DIFFICULTY
        assessments.append(
            _cached_difficulty(
                str(repository_path.resolve()),
                repository.base_commit,
                repository.solution_commit,
            )
        )
    assessed = [item for item in assessments if item.level != "UNASSESSED"]
    rank = {"EASY": 1, "MEDIUM": 2, "HARD": 3}
    return max(assessed, key=lambda item: rank.get(item.level, 0)) if assessed else UNASSESSED_DIFFICULTY


def scenario_case_view(
    item: PublishedScenario,
    *,
    repositories: Mapping[str, Path] | None = None,
) -> dict[str, object]:
    """Project one immutable Scenario into the canonical Chinese Case view."""

    try:
        snapshot = json.loads(item.snapshot_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        snapshot = {}
    manifest = snapshot_json(snapshot, "case.json")
    gold = snapshot_json(snapshot, manifest.get("gold_path"))
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    try:
        workspace = scenario_workspace(manifest) if item.case_kind == "REPOSITORY_CASE" else None
    except ValueError:
        workspace = None
    difficulty = _scenario_difficulty(manifest, repositories)
    runtime_parts = [str(runtime.get("language") or "未声明语言")]
    if runtime.get("java_major"):
        runtime_parts.append(f"Java {runtime['java_major']}")
    elif runtime.get("python_version"):
        runtime_parts.append(f"Python {runtime['python_version']}")
    if runtime.get("build_tool"):
        runtime_parts.append(str(runtime["build_tool"]))
    case_kind = item.case_kind
    calibration_state = item.calibration_state
    selectable = case_kind != "REPOSITORY_CASE" or calibration_state == "CALIBRATED"
    calibration_label = (
        "无需校准"
        if case_kind != "REPOSITORY_CASE"
        else "已校准"
        if calibration_state == "CALIBRATED"
        else "校准中"
    )
    initial_request = snapshot_file_text(snapshot, manifest.get("initial_request_path")).strip()
    return {
        "case_id": item.case_id,
        "title": item.title,
        "source_hash": item.source_hash,
        "published_at": item.published_at,
        "publication_status": item.publication_status,
        "case_kind": case_kind,
        "case_kind_label": "真实仓库场景" if case_kind == "REPOSITORY_CASE" else "开发验证场景",
        "calibration_state": calibration_state,
        "calibration_label": calibration_label,
        "readiness": "AVAILABLE" if selectable else "PENDING",
        "readiness_label": "可评测" if selectable else "待确定性校准",
        "recent_calibration_result": "最近一次校准未通过" if calibration_state == "FAILED" else "",
        "selectable": selectable,
        "repository_id": workspace.primary_repository_id if workspace else "内置工作区",
        "repository_ids": [repository.repository_id for repository in workspace.repositories] if workspace else [],
        "repository_count": len(workspace.repositories) if workspace else 0,
        "repository_mode": workspace.repository_mode if workspace else "SINGLE_REPOSITORY",
        "repository_mode_label": (
            f"多仓 · {len(workspace.repositories)}"
            if workspace and workspace.repository_mode == MULTI_REPOSITORY
            else "单仓"
        ),
        "runtime_label": " · ".join(runtime_parts),
        "difficulty": difficulty.level,
        "difficulty_label": _DIFFICULTY_LABELS[difficulty.level],
        "difficulty_evidence": difficulty.evidence_text if difficulty.level != "UNASSESSED" else "",
        "request_preview": " ".join(initial_request.split())[:180],
        "requirements_count": len(gold.get("requirements", []))
        if isinstance(gold.get("requirements"), list)
        else 0,
        "source_evidence_count": len(gold.get("source_evidence", []))
        if isinstance(gold.get("source_evidence"), list)
        else 0,
        "execution_tests_count": len(gold.get("execution_tests", []))
        if isinstance(gold.get("execution_tests"), list)
        else 0,
        "solution_checks_count": len(gold.get("solution_checks", []))
        if isinstance(gold.get("solution_checks"), list)
        else 0,
        "detail_url": f"/framework-evaluation/cases/{item.case_id}",
    }


def scenario_case_detail_view(
    item: PublishedScenario,
    *,
    repositories: Mapping[str, Path] | None = None,
) -> dict[str, object]:
    """Project one v2 Scenario snapshot into a readable Case archive."""

    summary = scenario_case_view(item, repositories=repositories)
    try:
        snapshot = json.loads(item.snapshot_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        snapshot = {}
    manifest = snapshot_json(snapshot, "case.json")
    gold = snapshot_json(snapshot, manifest.get("gold_path"))
    clarification = snapshot_json(snapshot, manifest.get("clarification_policy_path"))
    provenance = snapshot_json(snapshot, manifest.get("provenance_path"))
    repository = manifest.get("repository") if isinstance(manifest.get("repository"), dict) else {}
    try:
        workspace = scenario_workspace(manifest) if item.case_kind == "REPOSITORY_CASE" else None
    except ValueError:
        workspace = None
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    hidden_test_patch = (
        manifest.get("hidden_test_patch")
        if isinstance(manifest.get("hidden_test_patch"), dict)
        else {}
    )
    hidden_test_patches = (
        [item for item in manifest.get("hidden_test_patches", []) if isinstance(item, dict)]
        if isinstance(manifest.get("hidden_test_patches"), list)
        else ([hidden_test_patch] if hidden_test_patch else [])
    )
    raw_files = snapshot.get("files") if isinstance(snapshot, dict) else {}
    files = [
        {
            "path": str(path),
            "visibility": str(payload.get("visibility") or "CONTROL"),
            "sha256": str(payload.get("sha256") or ""),
        }
        for path, payload in sorted(raw_files.items())
        if isinstance(payload, dict)
    ] if isinstance(raw_files, dict) else []
    raw_requirements = (
        gold.get("requirements", []) if isinstance(gold.get("requirements"), list) else []
    )
    requirements = [
        {
            **requirement,
            "display_statement": _display_requirement_text(
                requirement.get("statement"),
                requirement.get("requirement_id"),
            ),
        }
        for requirement in raw_requirements
        if isinstance(requirement, dict)
    ]
    source_evidence = (
        gold.get("source_evidence", [])
        if isinstance(gold.get("source_evidence"), list)
        else []
    )
    execution_tests = (
        gold.get("execution_tests", [])
        if isinstance(gold.get("execution_tests"), list)
        else []
    )
    solution_checks = (
        gold.get("solution_checks", [])
        if isinstance(gold.get("solution_checks"), list)
        else []
    )
    facts = clarification.get("facts", []) if isinstance(clarification.get("facts"), list) else []
    contract_files = [
        {
            "label": "Case 定义",
            "path": "case.json",
            "version": str(manifest.get("schema_version") or ""),
            "purpose": "仓库、运行环境和各评测资产的总入口",
        },
        {
            "label": "评测目标",
            "path": str(manifest.get("initial_request_path") or ""),
            "version": "",
            "purpose": "提供给执行模型的业务任务说明",
        },
        {
            "label": "验收标准",
            "path": str(manifest.get("gold_path") or ""),
            "version": str(gold.get("schema_version") or ""),
            "purpose": "需求、来源证据和校验项之间的冻结关系",
        },
        {
            "label": "澄清答案",
            "path": str(manifest.get("clarification_policy_path") or ""),
            "version": str(clarification.get("schema_version") or ""),
            "purpose": "执行中允许直接使用的预置事实和澄清边界",
        },
        {
            "label": "来源记录",
            "path": str(manifest.get("provenance_path") or ""),
            "version": str(provenance.get("schema_version") or ""),
            "purpose": "记录 Case 由哪些历史材料整理而来，仅用于审计",
        },
        *[
            {
                "label": (
                    f"隐藏校验补丁 · {item.get('repository_id') or summary['repository_id']}"
                    if workspace and workspace.is_multi_repository
                    else "隐藏校验补丁"
                ),
                "path": str(item.get("path") or ""),
                "version": "",
                "purpose": "仅由平台在评测时加载的隐藏校验资产",
            }
            for item in hidden_test_patches
        ],
    ]
    return {
        **summary,
        "manifest": manifest,
        "gold": gold,
        "clarification": clarification,
        "provenance": provenance,
        "repository": repository,
        "repositories": [
            {
                "repository_id": member.repository_id,
                "mount_path": member.mount_path,
                "impact": member.impact,
                "base_commit": member.base_commit,
                "solution_commit": member.solution_commit,
                "solution_patch_sha256": member.solution_patch_sha256,
                "primary": member.primary,
            }
            for member in (workspace.repositories if workspace else ())
        ],
        "runtime": runtime,
        "hidden_test_patch": hidden_test_patch,
        "hidden_test_patches": hidden_test_patches,
        "initial_request": snapshot_file_text(snapshot, manifest.get("initial_request_path")),
        "execution_intent": str(manifest.get("execution_intent") or "FLOW_THROUGH"),
        "requirements": requirements,
        "source_evidence": source_evidence,
        "execution_tests": execution_tests,
        "solution_checks": solution_checks,
        "verification_count": len(execution_tests) + len(solution_checks),
        "facts": facts,
        "contract_files": [item for item in contract_files if item["path"]],
        "source_paths": {
            "任务说明": str(manifest.get("initial_request_path") or ""),
            "业务标准": str(manifest.get("gold_path") or ""),
            "澄清策略": str(manifest.get("clarification_policy_path") or ""),
            "来源追溯": str(manifest.get("provenance_path") or ""),
        },
        "schema_versions": {
            "Case 契约": str(manifest.get("schema_version") or ""),
            "业务标准": str(gold.get("schema_version") or ""),
            "澄清策略": str(clarification.get("schema_version") or ""),
            "来源追溯": str(provenance.get("schema_version") or ""),
            "冻结快照": str(snapshot.get("schema_version") or ""),
        },
        "files": files,
    }
