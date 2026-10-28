#!/usr/bin/env python3
"""开发期/CI 只读校验 /helix:init 产物；业务命令不得调用。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from host_runtime import HostContractError, HostRegistry
from host_runtime.project_config import load_project_config
from project_hooks_cli import project_hooks
from workflow import WorkflowError
from workspace import WorkspaceError, load_registry


PLUGIN_ROOT = Path(__file__).resolve().parent.parent


SERVICE_COMMON_DIRS = (
    "docs/harness",
    "docs/knowledge",
    "docs/specs",
    "docs/specs/active",
    "docs/specs/completed",
    "docs/hooks",
)

SERVICE_COMMON_FILES = (
    "docs/harness/index.md",
    "docs/knowledge/index.md",
    "docs/knowledge/project-context.md",
)

WORKSPACE_DIRS = (
    "docs/specs/active",
    "docs/specs/completed",
)

WORKSPACE_FILES = (
    "helixharness.workspace.yaml",
    "docs/knowledge/service-catalog.yaml",
    "docs/knowledge/service-graph.md",
    "docs/knowledge/integration-boundaries.md",
    "docs/knowledge/index.md",
)


def _result(check: str, path: str, status: str, detail: str) -> dict[str, str]:
    return {"check": check, "path": path, "status": status, "detail": detail}


def _check_file(root: Path, relative_path: str) -> dict[str, str]:
    target = root / relative_path
    if not target.is_file():
        return _result("nonempty_file", relative_path, "fail", "文件缺失或不是普通文件")
    try:
        if target.stat().st_size <= 0:
            return _result("nonempty_file", relative_path, "fail", "文件为空")
    except OSError as exc:
        return _result("nonempty_file", relative_path, "fail", f"无法读取文件状态: {exc}")
    return _result("nonempty_file", relative_path, "pass", "文件存在且非空")


def _check_dir(root: Path, relative_path: str) -> dict[str, str]:
    target = root / relative_path
    if not target.is_dir():
        return _result("directory", relative_path, "fail", "目录缺失或路径不是目录")
    return _result("directory", relative_path, "pass", "目录存在")


def _check_eval_gitignore(root: Path) -> dict[str, str]:
    relative_path = ".gitignore"
    target = root / relative_path
    if not target.is_file():
        return _result(
            "eval_gitignore",
            relative_path,
            "warn",
            "数据统计已开启，但缺少 .gitignore",
        )
    try:
        lines = {line.strip() for line in target.read_text(encoding="utf-8").splitlines()}
    except (OSError, UnicodeError) as exc:
        return _result("eval_gitignore", relative_path, "warn", f"无法读取 .gitignore: {exc}")
    required = {"!docs/eval/", "!docs/eval/**"}
    missing = sorted(required - lines)
    if missing:
        return _result(
            "eval_gitignore",
            relative_path,
            "warn",
            "缺少 eval 例外规则: " + ", ".join(missing),
        )
    return _result("eval_gitignore", relative_path, "pass", "eval 例外规则已存在")


def validate(
    repo_root: Path,
    mode: str,
    platform: str,
    telemetry_mode: str,
) -> dict[str, Any]:
    root = repo_root.expanduser().resolve()
    checks: list[dict[str, str]] = []

    registry = HostRegistry.discover(PLUGIN_ROOT / "hosts")
    manifest = registry.get(platform)
    if manifest is None:
        return {
            "status": "fail",
            "reason": "unsupported_host",
            "mode": mode,
            "platform": platform,
            "telemetry_mode": telemetry_mode,
            "repo_root": str(root),
            "project_entry": None,
            "checks": [],
            "errors": [
                _result("host", platform, "fail", "宿主未注册，且不会回退")
            ],
            "warnings": [],
        }
    try:
        project_config = load_project_config(manifest)
    except HostContractError as exc:
        return {
            "status": "fail",
            "reason": "invalid_host_contract",
            "mode": mode,
            "platform": manifest.host_id,
            "telemetry_mode": telemetry_mode,
            "repo_root": str(root),
            "project_entry": None,
            "checks": [],
            "errors": [_result("host", manifest.host_id, "fail", str(exc))],
            "warnings": [],
        }

    if not root.is_dir():
        checks.append(_result("repo_root", str(root), "fail", "仓库根路径不是目录"))
    else:
        checks.append(_check_file(root, project_config.entry_file))

        if mode == "workspace":
            checks.extend(_check_dir(root, path) for path in WORKSPACE_DIRS)
            checks.extend(_check_file(root, path) for path in WORKSPACE_FILES)
            try:
                repos = load_registry(root)
                checks.append(_result("workspace_registry", "helixharness.workspace.yaml", "pass", f"已解析 {len(repos)} 个仓库路径"))
            except (OSError, UnicodeError, WorkspaceError, WorkflowError) as exc:
                checks.append(_result("workspace_registry", "helixharness.workspace.yaml", "fail", str(exc)))
        else:
            checks.extend(_check_dir(root, path) for path in SERVICE_COMMON_DIRS)
            checks.extend(_check_file(root, path) for path in SERVICE_COMMON_FILES)
            try:
                project_hooks(root, manifest.host_id, "check")
                checks.append(_result("project_hooks", project_config.settings_file or "", "pass", "宿主 hooks 完整且无重复，或声明不支持"))
            except (OSError, ValueError) as exc:
                checks.append(_result("project_hooks", project_config.settings_file or "", "fail", str(exc)))
            if mode == "service":
                checks.append(_check_file(root, "docs/knowledge/service-meta.yaml"))
            if telemetry_mode == "enabled":
                checks.append(_check_dir(root, "docs/eval"))
                checks.append(_check_eval_gitignore(root))

    errors = [check for check in checks if check["status"] == "fail"]
    warnings = [check for check in checks if check["status"] == "warn"]
    return {
        "status": "pass" if not errors else "fail",
        "mode": mode,
        "platform": manifest.host_id,
        "telemetry_mode": telemetry_mode,
        "repo_root": str(root),
        "project_entry": project_config.entry_file,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("service", "frontend", "workspace"))
    parser.add_argument("--platform", required=True)
    parser.add_argument("--telemetry-mode", required=True, choices=("enabled", "disabled"))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = validate(args.repo_root, args.mode, args.platform, args.telemetry_mode)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
