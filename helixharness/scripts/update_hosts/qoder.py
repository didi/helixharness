"""Qoder 版本与更新宿主适配器。

Qoder CLI 的插件市场命令与 JSON 结构与 Claude Code 同构，仅命令名、配置根与清单目录不同；
本适配器只复述这些显式差异，不做平台名称分支。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import eval_json

from .base import (
    INSPECT_TIMEOUT_SECONDS,
    MARKETPLACE,
    MARKETPLACE_TIMEOUT_SECONDS,
    PLUGIN_ID,
    PLUGIN_NAME,
    BoundedCommandRunner,
    InstallStep,
    UpdateHostAdapter,
    UpdateHostError,
    project_contains,
    same_path,
)


CLI_NAMES = ("qoderclicn", "qodercli")
LOCAL_SCOPES = frozenset({"project", "local"})


def _cli() -> str:
    """返回当前可用的 Qoder CLI 名称，不跨产品回退。"""

    return CLI_NAMES[0]


def detect_host(environment: Any) -> int | None:
    """只在存在 Qoder 运行时标识时返回匹配，禁止默认回退。"""

    markers = (
        "QODER_PLUGIN_ROOT",
        "QODER_SESSION_ID",
        "QODERCN_CONFIG_DIR",
        "QODER_CONFIG_DIR",
        "QODERCN_CLI_HOME",
    )
    return 100 if any(environment.get(name) for name in markers) else None


def _marketplace_manifest(plugin_root: Path) -> dict[str, Any]:
    try:
        data = eval_json.loads(
            (plugin_root / ".qoder-plugin" / "marketplace.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateHostError("Qoder 版本清单不可用") from exc
    if not isinstance(data, dict):
        raise UpdateHostError("Qoder 版本清单不可用")
    return data


def plugin_version(plugin_root: Path) -> str:
    """读取 Qoder marketplace 管理的 HelixHarness 版本。"""

    plugins = _marketplace_manifest(plugin_root).get("plugins")
    matches = [
        item
        for item in (plugins if isinstance(plugins, list) else [])
        if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
    ]
    version = matches[0].get("version") if len(matches) == 1 else None
    if not isinstance(version, str) or not version.strip():
        raise UpdateHostError("Qoder 版本清单不可用")
    return version.strip()


class QoderUpdateAdapter(UpdateHostAdapter):
    host_id = "qoder"
    supported_scopes = frozenset({"user", "project", "local"})
    manifest_directory = ".qoder-plugin"
    marketplace_manifest_relative = ".qoder-plugin/marketplace.json"

    def __init__(
        self,
        normalize_version: Callable[[Any], str],
        read_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        self._normalize_version = normalize_version
        self._read_json = read_json

    def state_home(self) -> Path:
        configured = os.environ.get("QODERCN_CONFIG_DIR") or os.environ.get("QODER_CONFIG_DIR")
        if configured:
            return Path(configured).expanduser()
        home = Path.home()
        for name in (".qoder-cn", ".qoder"):
            candidate = home / name
            if candidate.is_dir():
                return candidate
        return home / ".qoder-cn"

    def declared_version(self, plugin_root: Path) -> Any:
        plugins = _marketplace_manifest(plugin_root).get("plugins")
        entries = plugins if isinstance(plugins, list) else []
        matches = [
            item
            for item in entries
            if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
        ]
        if len(matches) != 1:
            raise UpdateHostError("Qoder marketplace 清单版本无效")
        return matches[0].get("version")

    def _plugin_list(
        self, runner: BoundedCommandRunner, plugin_list_data: Any
    ) -> list[dict[str, Any]]:
        data = (
            runner.run_json(
                [_cli(), "plugins", "list", "--json"],
                timeout=INSPECT_TIMEOUT_SECONDS,
            )
            if plugin_list_data is None
            else plugin_list_data
        )
        if not isinstance(data, list):
            raise UpdateHostError("invalid Qoder plugin list")
        return [item for item in data if isinstance(item, dict)]

    def inspect_installation(
        self,
        repo_root: Path,
        plugin_root: Path,
        runner: BoundedCommandRunner,
        plugin_list_data: Any = None,
    ) -> dict[str, Any]:
        data = self._plugin_list(runner, plugin_list_data)
        candidates = [
            item
            for item in data
            if item.get("id") == PLUGIN_ID and item.get("enabled") is True
        ]
        local_candidates = [
            item
            for item in candidates
            if item.get("scope") in LOCAL_SCOPES
            and project_contains(item.get("projectPath"), repo_root)
        ]
        if local_candidates:
            local_candidates.sort(
                key=lambda item: len(Path(str(item["projectPath"])).expanduser().parts),
                reverse=True,
            )
            deepest = len(
                Path(str(local_candidates[0]["projectPath"])).expanduser().parts
            )
            selected_group = [
                item
                for item in local_candidates
                if len(Path(str(item["projectPath"])).expanduser().parts) == deepest
            ]
        else:
            selected_group = [item for item in candidates if item.get("scope") == "user"]
        if len(selected_group) != 1:
            raise UpdateHostError("formal HelixHarness installation is missing or ambiguous")
        selected = selected_group[0]
        install_path = selected.get("installPath")
        if not install_path or not (
            same_path(install_path, plugin_root)
            or self._is_local_runtime_root(plugin_root, runner)
        ):
            raise UpdateHostError("executing HelixHarness is not the selected formal installation")
        version = self._normalize_version(selected.get("version"))
        if self._normalize_version(self.declared_version(plugin_root)) != version:
            raise UpdateHostError("formal installation version does not match the executing plugin")
        return {
            "platform": self.host_id,
            "plugin_id": PLUGIN_ID,
            "current_version": version,
            "current_scope": str(selected.get("scope")),
            "current_source": MARKETPLACE,
            "available_scopes": sorted(
                {
                    str(item.get("scope"))
                    for item in candidates
                    if item.get("scope") == "user"
                    or (
                        item.get("scope") in LOCAL_SCOPES
                        and project_contains(item.get("projectPath"), repo_root)
                    )
                }
            ),
        }

    def _is_local_runtime_root(
        self, plugin_root: Path, runner: BoundedCommandRunner
    ) -> bool:
        # 本地目录 marketplace 从注册源码运行，缓存 installPath 不代表执行根。
        data = runner.run_json(
            [_cli(), "plugins", "marketplace", "list", "--json"],
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
        matches = [
            item
            for item in (data if isinstance(data, list) else [])
            if isinstance(item, dict) and item.get("name") == MARKETPLACE
        ]
        if len(matches) != 1:
            return False
        source = matches[0].get("source")
        source_kind = source.get("source") if isinstance(source, dict) else None
        if source_kind not in {"directory", "local"}:
            return False
        location = matches[0].get("installLocation")
        if not isinstance(location, str) or not location.strip():
            return False
        root = Path(location).expanduser().resolve()
        try:
            manifest = self._read_json(root / ".qoder-plugin" / "marketplace.json")
        except UpdateHostError:
            return False
        entries = manifest.get("plugins")
        plugins = [
            item
            for item in (entries if isinstance(entries, list) else [])
            if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
        ]
        source_ref = plugins[0].get("source") if len(plugins) == 1 else None
        if isinstance(source_ref, dict):
            source_ref = source_ref.get("source")
        if not isinstance(source_ref, str) or not source_ref.strip():
            return False
        if Path(source_ref).is_absolute():
            return False
        runtime_root = (root / source_ref).resolve()
        if not runtime_root.is_relative_to(root):
            return False
        return same_path(runtime_root, plugin_root)

    def marketplace_root(
        self,
        runner: BoundedCommandRunner,
        marketplace_list_data: Any = None,
    ) -> Path:
        data = (
            runner.run_json(
                [_cli(), "plugins", "marketplace", "list", "--json"],
                timeout=INSPECT_TIMEOUT_SECONDS,
            )
            if marketplace_list_data is None
            else marketplace_list_data
        )
        marketplaces = data if isinstance(data, list) else []
        roots = [
            item.get("installLocation")
            for item in marketplaces
            if isinstance(item, dict) and item.get("name") == MARKETPLACE
        ]
        roots = [value for value in roots if value]
        if len(roots) != 1:
            raise UpdateHostError("formal HelixHarness marketplace is missing or ambiguous")
        return Path(str(roots[0])).expanduser().resolve()

    @contextmanager
    def refresh_marketplace(self, runner: BoundedCommandRunner) -> Iterator[Path]:
        runner.run(
            [_cli(), "plugins", "marketplace", "update", MARKETPLACE],
            timeout=MARKETPLACE_TIMEOUT_SECONDS,
        )
        yield self.marketplace_root(runner)

    def install_steps(
        self, scope: str, runner: BoundedCommandRunner
    ) -> tuple[InstallStep, ...]:
        self.validate_scope(scope)
        del runner
        return (
            InstallStep(
                step_id=f"update-{scope}",
                command=(_cli(), "plugins", "update", PLUGIN_ID, "--scope", scope),
            ),
        )
