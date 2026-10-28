"""Host API 1.1+ 的项目级 hook 合并契约。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import eval_json

from . import HostContractError, HostManifest
from .project_config import load_project_config


HOOK_OWNERS = frozenset({"bootstrap", "parent"})
HOOK_MERGE_STRATEGIES = frozenset({"json_append_dedup", "unsupported"})


def _optional_relative_path(value: Any, label: str) -> str | None:
    if value is None:
        return None
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise HostContractError(f"{label} 必须是安全的项目相对路径")
    return path.as_posix()


@dataclass(frozen=True)
class ProjectHooks:
    """宿主在 init 时怎样拥有和合并项目级 hooks。"""

    owner: str
    settings_file: str | None
    merge_strategy: str
    dedupe_key: str | None
    payload: dict[str, Any] | None


def load_project_hooks(manifest: HostManifest) -> ProjectHooks:
    path = manifest.resolve_resource_adapter("project_hooks")
    try:
        payload = eval_json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HostContractError(f"无法读取项目 hooks: {manifest.host_id}") from exc
    required_fields = {
        "project_hooks_version",
        "owner",
        "settings_file",
        "merge_strategy",
        "dedupe_key",
        "payload",
    }
    if not isinstance(payload, dict) or set(payload) != required_fields:
        raise HostContractError(f"项目 hooks 字段无效: {manifest.host_id}")
    if payload.get("project_hooks_version") != "1.0":
        raise HostContractError(f"项目 hooks 版本无效: {manifest.host_id}")

    owner = str(payload.get("owner") or "")
    strategy = str(payload.get("merge_strategy") or "")
    if owner not in HOOK_OWNERS:
        raise HostContractError(f"项目 hooks owner 无效: {manifest.host_id}")
    if strategy not in HOOK_MERGE_STRATEGIES:
        raise HostContractError(f"项目 hooks merge_strategy 无效: {manifest.host_id}")
    settings_file = _optional_relative_path(
        payload.get("settings_file"), f"{manifest.host_id}.project_hooks.settings_file"
    )
    project_config = load_project_config(manifest)
    if settings_file != project_config.settings_file:
        raise HostContractError(f"项目 hooks 与 project_config 不一致: {manifest.host_id}")

    raw_dedupe_key = payload.get("dedupe_key")
    dedupe_key = str(raw_dedupe_key or "").strip() or None
    raw_hook_payload = payload.get("payload")
    capability = manifest.capability("project_hooks")
    if strategy == "unsupported":
        if capability != "unsupported" or settings_file is not None:
            raise HostContractError(f"项目 hooks unsupported 声明冲突: {manifest.host_id}")
        if dedupe_key is not None or raw_hook_payload is not None:
            raise HostContractError(f"项目 hooks unsupported 不得携带 payload: {manifest.host_id}")
        hook_payload = None
    else:
        if capability == "unsupported" or settings_file is None:
            raise HostContractError(f"项目 hooks capability 声明冲突: {manifest.host_id}")
        if dedupe_key is None or not isinstance(raw_hook_payload, dict) or not raw_hook_payload:
            raise HostContractError(f"项目 hooks 合并参数无效: {manifest.host_id}")
        hook_payload = raw_hook_payload

    return ProjectHooks(
        owner=owner,
        settings_file=settings_file,
        merge_strategy=strategy,
        dedupe_key=dedupe_key,
        payload=hook_payload,
    )


__all__ = [
    "HOOK_MERGE_STRATEGIES",
    "HOOK_OWNERS",
    "ProjectHooks",
    "load_project_hooks",
]
