"""Host Kit 声明的项目入口与设置文件契约。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import eval_json

from . import HostContractError, HostManifest


def _relative_path(value: Any, label: str) -> str:
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise HostContractError(f"{label} 必须是安全的项目相对路径")
    return path.as_posix()


@dataclass(frozen=True)
class ProjectConfig:
    """一个宿主需要生成或合并的项目配置表面。"""

    entry_file: str
    entry_template: str
    workspace_entry_template: str
    settings_file: str | None


def load_project_config(manifest: HostManifest) -> ProjectConfig:
    path = manifest.resolve_resource_adapter("project_config")
    try:
        payload = eval_json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HostContractError(f"无法读取项目配置适配器: {manifest.host_id}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "entry_file",
        "entry_template",
        "workspace_entry_template",
        "settings_file",
    }:
        raise HostContractError(f"项目配置适配器字段无效: {manifest.host_id}")
    settings = payload["settings_file"]
    return ProjectConfig(
        entry_file=_relative_path(payload["entry_file"], "entry_file"),
        entry_template=_relative_path(payload["entry_template"], "entry_template"),
        workspace_entry_template=_relative_path(
            payload["workspace_entry_template"], "workspace_entry_template"
        ),
        settings_file=(
            _relative_path(settings, "settings_file") if settings is not None else None
        ),
    )
