"""受支持版本更新宿主适配器的 Registry。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from host_runtime import HostContractError, HostRegistry

from .base import (
    INSTALL_TIMEOUT_SECONDS,
    MARKETPLACE,
    PLUGIN_ID,
    PLUGIN_NAME,
    BoundedCommandRunner,
    InstallStep,
    UpdateHostAdapter,
    UpdateHostError,
)
HOST_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_HOSTS_ROOT = PLUGIN_ROOT / "hosts"


def build_update_host_adapters(
    normalize_version: Callable[[Any], str],
    read_json: Callable[[Path], dict[str, Any]],
    *,
    hosts_root: Path = PACKAGED_HOSTS_ROOT,
) -> dict[str, UpdateHostAdapter]:
    registry = HostRegistry.discover(hosts_root)
    adapters: dict[str, UpdateHostAdapter] = {}
    for manifest in registry.manifests():
        if manifest.capability("plugin_update") == "unsupported":
            continue
        if manifest.adapter_reference("update") is None:
            raise HostContractError(
                f"宿主必须声明更新适配器: {manifest.host_id}"
            )
        adapter_type = manifest.load_python_adapter("update")
        try:
            adapter = adapter_type(normalize_version, read_json)
        except TypeError as exc:
            raise HostContractError(
                f"无法构造更新适配器: {manifest.host_id}"
            ) from exc
        if getattr(adapter, "host_id", None) != manifest.host_id:
            raise HostContractError(
                f"更新适配器 ID 与宿主清单不一致: {manifest.host_id}"
            )
        adapters[manifest.host_id] = adapter
    return adapters


def supported_update_host_ids() -> tuple[str, ...]:
    registry = HostRegistry.discover(PACKAGED_HOSTS_ROOT)
    return tuple(
        manifest.host_id
        for manifest in registry.manifests()
        if manifest.capability("plugin_update") != "unsupported"
        and manifest.adapter_reference("update") is not None
    )


def detect_update_host_id(explicit: str | None) -> str:
    registry = HostRegistry.discover(PACKAGED_HOSTS_ROOT)
    if explicit:
        host_id = str(explicit).strip().lower()
        if not HOST_ID_PATTERN.fullmatch(host_id):
            raise UpdateHostError(f"invalid platform: {host_id}")
        return registry.resolve_id(host_id) or host_id
    detected = registry.detect(os.environ)
    if detected is None:
        raise UpdateHostError("无法识别更新宿主")
    return detected


__all__ = [
    "INSTALL_TIMEOUT_SECONDS",
    "MARKETPLACE",
    "PLUGIN_ID",
    "PLUGIN_NAME",
    "BoundedCommandRunner",
    "InstallStep",
    "UpdateHostAdapter",
    "UpdateHostError",
    "build_update_host_adapters",
    "detect_update_host_id",
    "supported_update_host_ids",
]
