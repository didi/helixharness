"""Host API 1.1+ 的业务命令投影契约。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import eval_json

from . import HostContractError, HostManifest


LITE_ONLY_COMMANDS = frozenset({"propose", "apply", "archive", "bug-fix"})
WORKFLOW_COMMANDS = frozenset({"init", "propose", "apply", "archive", "bug-fix", "ut", "update", "status", "config"})
PROJECTION_SURFACES = frozenset({"native-command", "skill"})
CANONICAL_COMMAND_MARKER = "HelixHarness 公共命令唯一契约"


def canonical_contract_path(plugin_root: Path, command: str) -> str:
    """返回 command 当前唯一的公共业务契约路径。"""

    if command not in WORKFLOW_COMMANDS:
        raise HostContractError(f"未知 workflow command: {command}")
    command_relative = f"commands/{command}.md"
    command_path = plugin_root / command_relative
    if not command_path.is_file():
        raise HostContractError(f"公共命令契约不存在: {command}")
    if CANONICAL_COMMAND_MARKER not in command_path.read_text(encoding="utf-8"):
        raise HostContractError(f"公共命令契约缺少唯一契约标记: {command}")
    return command_relative


def _relative_path(value: Any, label: str) -> str:
    raw = str(value or "").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise HostContractError(f"{label} 必须是安全的插件相对路径")
    return path.as_posix()


def _require_file(manifest: HostManifest, relative: str, label: str) -> None:
    plugin_root = manifest.root.parent.parent.resolve()
    resolved = (plugin_root / relative).resolve()
    try:
        resolved.relative_to(plugin_root)
    except ValueError as exc:
        raise HostContractError(f"{label} 越过插件根目录") from exc
    if not resolved.is_file():
        raise HostContractError(f"{label} 不存在: {relative}")


@dataclass(frozen=True)
class CommandProjection:
    """一个宿主对全部公共 workflow 的公开入口投影。"""

    surface: str
    entrypoints: dict[str, tuple[str, str]]


def load_command_projection(manifest: HostManifest) -> CommandProjection:
    path = manifest.resolve_resource_adapter("command_projection")
    try:
        payload = eval_json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HostContractError(f"无法读取命令投影: {manifest.host_id}") from exc
    allowed_fields = {
        "projection_version",
        "surface",
        "entrypoints",
    }
    if not isinstance(payload, dict) or set(payload) - allowed_fields:
        raise HostContractError(f"命令投影字段无效: {manifest.host_id}")
    if payload.get("projection_version") != "1.0":
        raise HostContractError(f"命令投影版本无效: {manifest.host_id}")
    surface = str(payload.get("surface") or "")
    if surface not in PROJECTION_SURFACES:
        raise HostContractError(f"命令投影 surface 无效: {manifest.host_id}")
    if surface == "native-command" and manifest.capability("command_surface") != "native":
        raise HostContractError(f"命令投影与 command_surface 能力冲突: {manifest.host_id}")

    raw_entrypoints = payload.get("entrypoints")
    if not isinstance(raw_entrypoints, dict) or set(raw_entrypoints) != WORKFLOW_COMMANDS:
        raise HostContractError(f"命令投影未完整覆盖公共 workflow: {manifest.host_id}")
    entrypoints: dict[str, tuple[str, str]] = {}
    for command in sorted(WORKFLOW_COMMANDS):
        pair = raw_entrypoints[command]
        if not isinstance(pair, list) or len(pair) != 2:
            raise HostContractError(f"命令投影入口结构无效: {manifest.host_id}.{command}")
        entry = _relative_path(pair[0], f"{manifest.host_id}.{command}.entry")
        contract = _relative_path(pair[1], f"{manifest.host_id}.{command}.contract")
        expected_contract = f"commands/{command}.md"
        if contract != expected_contract:
            raise HostContractError(
                f"命令投影 contract 不匹配: {manifest.host_id}.{command}"
            )
        _require_file(manifest, entry, f"{manifest.host_id}.{command}.entry")
        _require_file(manifest, contract, f"{manifest.host_id}.{command}.contract")
        plugin_root = manifest.root.parent.parent.resolve()
        canonical_contract = canonical_contract_path(plugin_root, command)
        if contract != canonical_contract:
            raise HostContractError(
                f"命令投影未使用唯一公共契约: {manifest.host_id}.{command}"
            )
        entrypoints[command] = (entry, contract)

    return CommandProjection(
        surface=surface,
        entrypoints=entrypoints,
    )


__all__ = [
    "LITE_ONLY_COMMANDS",
    "PROJECTION_SURFACES",
    "WORKFLOW_COMMANDS",
    "CANONICAL_COMMAND_MARKER",
    "canonical_contract_path",
    "CommandProjection",
    "load_command_projection",
]
