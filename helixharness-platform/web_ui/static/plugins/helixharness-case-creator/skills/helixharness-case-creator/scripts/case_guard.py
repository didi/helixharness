#!/usr/bin/env python3
"""Decide whether a staged Case is new, unchanged, or unsafe to update."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

try:
    import sqlite3
except ImportError:  # pragma: no cover - depends on the target Python build
    from pysqlite3 import dbapi2 as sqlite3

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

BLOCKING_DECISIONS = {
    "BLOCK_ACTIVE_CALIBRATION",
    "BLOCK_ACTIVE_RUN",
    "BLOCK_DELETED",
    "BLOCK_DUPLICATE_SOURCE",
    "BLOCK_EXISTING_SOURCE_UNPUBLISHED",
    "BLOCK_REPOSITORY_CONFLICT",
    "NO_PLATFORM_DATABASE",
    "PREFLIGHT_FAILED",
    "TARGET_STATE_UNVERIFIED",
    "UPDATE_REQUIRES_CONFIRMATION",
}
_REGISTRY_SCHEMA_VERSION = "framework-eval-repository-registry.v1"
_REPOSITORY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def _bootstrap(platform_root: Path) -> Path:
    root = platform_root.expanduser().resolve()
    if not (root / "bench" / "scenarios.py").is_file():
        raise ValueError(f"不是有效 HelixHarness Platform 根目录：{root}")
    sys.path.insert(0, str(root))
    return root


def _reject_symlinks(root: Path) -> None:
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError(f"Case Source 不允许符号链接：{root}")


def _load_with_local_repository(
    case_dir: Path,
    repository: Mapping[str, Path] | None,
) -> tuple[Any, str | None, dict[str, Any]]:
    from bench.scenarios import load_scenario_source  # noqa: PLC0415

    manifest_path = case_dir / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("case.json 顶层必须是对象")
    with tempfile.TemporaryDirectory(prefix="helixharness-case-guard-") as temporary_directory:
        temporary_case_dir = Path(temporary_directory) / case_dir.name
        shutil.copytree(case_dir, temporary_case_dir, symlinks=True)
        source = load_scenario_source(temporary_case_dir)
        from repository_interval import validate_repository_interval  # noqa: PLC0415

        validate_repository_interval(source.manifest, repository)
        source_hash = source.snapshot.source_hash
        return source, source_hash, manifest


def _duplicate_source_directories(source_root: Path, case_id: str) -> tuple[list[str], list[str]]:
    matches: list[str] = []
    errors: list[str] = []
    for manifest_path in sorted(source_root.glob("*/case.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"无法读取 {manifest_path}: {exc}")
            continue
        if isinstance(manifest, dict) and manifest.get("case_id") == case_id:
            matches.append(str(manifest_path.parent.resolve()))
    return matches, errors


def _read_repository_registry(registry_path: Path) -> dict[str, dict[str, object]]:
    if not registry_path.is_file() or not os.access(registry_path, os.R_OK):
        raise ValueError(f"repository registry snapshot 不存在或不可读：{registry_path}")
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"repository registry snapshot 无效：{registry_path}: {exc}") from exc
    schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
    if schema_version != _REGISTRY_SCHEMA_VERSION:
        raise ValueError(
            "repository registry snapshot schema_version 无效："
            f"期待 {_REGISTRY_SCHEMA_VERSION}，实际 {schema_version!r}"
        )
    repositories = payload.get("repositories") if isinstance(payload, dict) else None
    if not isinstance(repositories, dict):
        raise ValueError(f"repository registry snapshot 缺少 repositories 对象：{registry_path}")
    invalid_ids = [
        repository_id
        for repository_id in repositories
        if not isinstance(repository_id, str) or not _REPOSITORY_ID_PATTERN.fullmatch(repository_id)
    ]
    if invalid_ids:
        raise ValueError(f"repository registry snapshot 含无效 repository ID：{invalid_ids!r}")
    invalid_entries = [
        repository_id
        for repository_id, entry in repositories.items()
        if not isinstance(entry, dict)
        or not isinstance(entry.get("clone_url"), str)
        or not entry["clone_url"].strip()
    ]
    if invalid_entries:
        raise ValueError(
            "repository registry snapshot 含缺失或无效 clone_url 的条目："
            f"{', '.join(sorted(invalid_entries))}"
        )
    return repositories


def _registered_clone_url(registry: dict[str, dict[str, object]], repository_id: str) -> str | None:
    entry = registry.get(repository_id)
    clone_url = entry.get("clone_url") if isinstance(entry, dict) else None
    return clone_url if isinstance(clone_url, str) else None


def _read_platform_state(database_path: Path, case_id: str) -> dict[str, Any]:
    """Read the minimum Case state through an immutable SQLite connection."""

    if not database_path.is_file() or not os.access(database_path, os.R_OK):
        raise ValueError(f"平台 DB snapshot 不存在或不可读：{database_path}")
    database_uri = f"file:{quote(str(database_path.resolve()))}?mode=ro&immutable=1"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        existing = connection.execute(
            """
            SELECT source_hash, publication_status, calibration_state,
                   CASE
                       WHEN publication_status = 'PUBLISHED' AND calibration_state = 'CALIBRATED'
                       THEN 'FORMAL'
                       ELSE 'CALIBRATION_ONLY'
                   END AS eligibility
            FROM scenarios
            WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()
        active_runs = connection.execute(
            """
            SELECT DISTINCT scenario_run_cases.run_id
            FROM scenario_run_cases
            JOIN scenario_runs ON scenario_runs.run_id = scenario_run_cases.run_id
            WHERE scenario_run_cases.case_id = ?
              AND scenario_runs.status IN ('QUEUED', 'RUNNING')
            ORDER BY scenario_run_cases.run_id
            """,
            (case_id,),
        ).fetchall()
    return {
        "deleted": False,
        "existing": dict(existing) if existing is not None else None,
        "active_run_ids": [str(row[0]) for row in active_runs],
        "active_calibration_job_ids": [],
    }


def guard(
    platform_root: Path,
    case_dir: Path,
    repository: Mapping[str, Path] | None,
    *,
    update_existing: bool,
    database_path: Path | None = None,
    case_source_root: Path | None = None,
    repository_registry_path: Path | None = None,
) -> dict[str, object]:
    _bootstrap(platform_root)
    source_directory = case_dir.expanduser().resolve()
    _reject_symlinks(source_directory)
    source, candidate_hash, original_manifest = _load_with_local_repository(source_directory, repository)
    payload: dict[str, object] = {
        "schema_version": "helixharness-case-creator-guard.v2",
        "case_id": source.case_id,
        "candidate_source_hash": candidate_hash,
        "preflight_passed": source.preflight_passed,
    }
    if not source.preflight_passed:
        payload.update(decision="PREFLIGHT_FAILED", allowed=False)
        return payload

    resolved_source_root = (
        case_source_root
        or Path(os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CASE_SOURCE_ROOT", "bench/.cache/case-sources-v2"))
    ).expanduser().resolve()
    resolved_database = (
        database_path
        or Path(os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH", "bench/.cache/evaluation.db"))
    ).expanduser().resolve()
    resolved_registry = repository_registry_path.expanduser().resolve() if repository_registry_path else None
    payload["state_source"] = {
        "database": str(resolved_database),
        "case_source_root": str(resolved_source_root),
        "repository_registry": str(resolved_registry) if resolved_registry else None,
        "database_mode": "READ_ONLY_IMMUTABLE",
    }
    if not resolved_source_root.is_dir() or not os.access(resolved_source_root, os.R_OK | os.X_OK):
        payload.update(
            decision="TARGET_STATE_UNVERIFIED",
            allowed=False,
            reason=f"Case Source 根不存在、不是目录或不可读：{resolved_source_root}",
        )
        return payload
    if resolved_registry is None:
        payload.update(
            decision="TARGET_STATE_UNVERIFIED",
            allowed=False,
            reason="必须显式提供与 DB snapshot 同一目标状态的 repository registry snapshot",
        )
        return payload
    try:
        repository_registry = _read_repository_registry(resolved_registry)
    except ValueError as exc:
        payload.update(decision="TARGET_STATE_UNVERIFIED", allowed=False, reason=str(exc))
        return payload

    duplicate_directories, source_errors = _duplicate_source_directories(resolved_source_root, source.case_id)
    if source_errors:
        payload.update(
            decision="TARGET_STATE_UNVERIFIED",
            allowed=False,
            source_errors=source_errors,
        )
        return payload
    if len(duplicate_directories) > 1:
        payload.update(
            decision="BLOCK_DUPLICATE_SOURCE",
            allowed=False,
            source_directories=duplicate_directories,
        )
        return payload

    if not resolved_database.is_file() or not os.access(resolved_database, os.R_OK):
        payload.update(decision="NO_PLATFORM_DATABASE", allowed=False, database=str(resolved_database))
        return payload
    platform_state = _read_platform_state(resolved_database, source.case_id)
    if platform_state["deleted"]:
        payload.update(decision="BLOCK_DELETED", allowed=False)
        return payload
    from repository_interval import repository_definitions  # noqa: PLC0415

    repository_definitions_to_check = (
        repository_definitions(original_manifest)
        if original_manifest.get("case_kind") == "REPOSITORY_CASE"
        else ()
    )
    for repository_definition in repository_definitions_to_check:
        declared_id = repository_definition.get("repository_id")
        clone_url = repository_definition.get("clone_url")
        if isinstance(declared_id, str) and declared_id:
            registered_clone_url = _registered_clone_url(
                repository_registry,
                declared_id,
            )
            if (
                registered_clone_url is not None
                and isinstance(clone_url, str)
                and clone_url
                and registered_clone_url != clone_url
            ):
                payload.update(
                    decision="BLOCK_REPOSITORY_CONFLICT",
                    allowed=False,
                    registered_clone_url=registered_clone_url,
                    declared_clone_url=clone_url,
                )
                return payload

    existing = platform_state["existing"]
    if existing is None:
        if duplicate_directories:
            payload.update(
                decision="BLOCK_EXISTING_SOURCE_UNPUBLISHED",
                allowed=False,
                source_directories=duplicate_directories,
            )
            return payload
        payload.update(decision="READY_NEW", allowed=True)
        return payload
    payload.update(
        existing_source_hash=existing["source_hash"],
        publication_status=existing["publication_status"],
        calibration_state=existing["calibration_state"],
        eligibility=existing["eligibility"],
    )
    if existing["source_hash"] == candidate_hash:
        payload.update(decision="NO_CHANGE", allowed=True)
        return payload

    active_runs = platform_state["active_run_ids"]
    if active_runs:
        payload.update(decision="BLOCK_ACTIVE_RUN", allowed=False, active_run_ids=list(active_runs))
        return payload
    active_calibrations = platform_state["active_calibration_job_ids"]
    if active_calibrations:
        payload.update(
            decision="BLOCK_ACTIVE_CALIBRATION",
            allowed=False,
            active_calibration_job_ids=active_calibrations,
        )
        return payload

    if not update_existing:
        payload.update(decision="UPDATE_REQUIRES_CONFIRMATION", allowed=False, recalibration_required=True)
        return payload
    payload.update(decision="READY_UPDATE_RECALIBRATION", allowed=True, recalibration_required=True)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform-root", required=True, type=Path)
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument(
        "--repository",
        action="append",
        help="可重复的 repository_id=/checkout 仓库绑定",
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--case-source-root", required=True, type=Path)
    parser.add_argument("--repository-registry", required=True, type=Path)
    parser.add_argument("--update-existing", action="store_true")
    arguments = parser.parse_args()
    try:
        from repository_interval import parse_repositories

        repositories = parse_repositories(arguments.repository)
        result = guard(
            arguments.platform_root,
            arguments.case_dir,
            repositories or None,
            update_existing=arguments.update_existing,
            database_path=arguments.database,
            case_source_root=arguments.case_source_root,
            repository_registry_path=arguments.repository_registry,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, sqlite3.DatabaseError, ValueError) as exc:
        print(json.dumps({"allowed": False, "decision": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("decision") not in BLOCKING_DECISIONS else 1


if __name__ == "__main__":
    raise SystemExit(main())
