"""Claude Code version-update host adapter."""

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


def detect_host(environment: Any) -> int | None:
    """只在存在 Claude Code 运行时标识时返回匹配，禁止默认回退。"""

    markers = (
        "CLAUDE_CODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_PLUGIN_ROOT",
    )
    return 100 if any(environment.get(name) for name in markers) else None


def plugin_version(plugin_root: Path) -> str:
    """读取 Claude Code marketplace 管理的 HelixHarness 版本。"""

    try:
        data = eval_json.loads(
            (plugin_root / ".claude-plugin" / "marketplace.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateHostError("Claude Code 版本清单不可用") from exc
    plugins = data.get("plugins") if isinstance(data, dict) else None
    matches = [
        item
        for item in (plugins if isinstance(plugins, list) else [])
        if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
    ]
    version = matches[0].get("version") if len(matches) == 1 else None
    if not isinstance(version, str) or not version.strip():
        raise UpdateHostError("Claude Code 版本清单不可用")
    return version.strip()


class ClaudeCodeUpdateAdapter(UpdateHostAdapter):
    host_id = "claude-code"
    supported_scopes = frozenset({"user", "project", "all"})
    aggregate_scope_members = {"all": frozenset({"user", "project"})}
    manifest_directory = ".claude-plugin"

    def __init__(self, normalize_version: Callable[[Any], str], read_json: Callable[[Path], dict[str, Any]]):
        self._normalize_version = normalize_version
        self._read_json = read_json

    def state_home(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()

    def inspect_installation(
        self,
        repo_root: Path,
        plugin_root: Path,
        runner: BoundedCommandRunner,
        plugin_list_data: Any = None,
    ) -> dict[str, Any]:
        data = (
            runner.run_json(
                ["claude", "plugin", "list", "--json"],
                timeout=INSPECT_TIMEOUT_SECONDS,
            )
            if plugin_list_data is None
            else plugin_list_data
        )
        if not isinstance(data, list):
            raise UpdateHostError("invalid Claude Code plugin list")
        candidates = [
            item
            for item in data
            if isinstance(item, dict)
            and item.get("id") == PLUGIN_ID
            and item.get("enabled") is True
        ]
        project_candidates = [
            item
            for item in candidates
            if item.get("scope") == "project"
            and project_contains(item.get("projectPath"), repo_root)
        ]
        if project_candidates:
            project_candidates.sort(
                key=lambda item: len(Path(str(item["projectPath"])).expanduser().parts),
                reverse=True,
            )
            deepest = len(Path(str(project_candidates[0]["projectPath"])).expanduser().parts)
            selected_group = [
                item
                for item in project_candidates
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
                        item.get("scope") == "project"
                        and project_contains(item.get("projectPath"), repo_root)
                    )
                }
            ),
        }

    def _is_local_runtime_root(
        self, plugin_root: Path, runner: BoundedCommandRunner,
    ) -> bool:
        # Local directory marketplaces run from their registered source even
        # though `plugin list` reports an installPath in the managed cache.
        data = runner.run_json(
            ["claude", "plugin", "marketplace", "list", "--json"],
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
        matches = [item for item in (data if isinstance(data, list) else [])
                   if isinstance(item, dict) and item.get("name") == MARKETPLACE]
        if len(matches) != 1 or matches[0].get("source") != "directory":
            return False
        location = matches[0].get("installLocation")
        if not isinstance(location, str) or not location.strip():
            return False
        root = Path(location).expanduser().resolve()
        manifest = self._read_json(root / ".claude-plugin/marketplace.json")
        entries = manifest.get("plugins")
        plugins = [item for item in (entries if isinstance(entries, list) else [])
                   if isinstance(item, dict) and item.get("name") == PLUGIN_NAME]
        source = plugins[0].get("source") if len(plugins) == 1 else None
        if not isinstance(source, str) or not source.strip() or Path(source).is_absolute():
            return False
        runtime_root = (root / source).resolve()
        if not runtime_root.is_relative_to(root):
            return False
        return same_path(runtime_root, plugin_root)

    def declared_version(self, plugin_root: Path) -> Any:
        marketplace = self._read_json(plugin_root / ".claude-plugin" / "marketplace.json")
        plugins = marketplace.get("plugins") if isinstance(marketplace.get("plugins"), list) else []
        matches = [
            item
            for item in plugins
            if isinstance(item, dict) and item.get("name") == PLUGIN_NAME
        ]
        if len(matches) != 1:
            raise UpdateHostError("formal HelixHarness marketplace manifest is invalid for version")
        return matches[0].get("version")

    def marketplace_root(
        self,
        runner: BoundedCommandRunner,
        marketplace_list_data: Any = None,
    ) -> Path:
        data = (
            runner.run_json(
                ["claude", "plugin", "marketplace", "list", "--json"],
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
            ["claude", "plugin", "marketplace", "update", MARKETPLACE],
            timeout=MARKETPLACE_TIMEOUT_SECONDS,
        )
        yield self.marketplace_root(runner)

    def install_steps(
        self, scope: str, runner: BoundedCommandRunner
    ) -> tuple[InstallStep, ...]:
        self.validate_scope(scope)
        del runner
        scopes = ("user", "project") if scope == "all" else (scope,)
        return tuple(
            InstallStep(
                step_id=f"update-{item}",
                command=("claude", "plugin", "update", PLUGIN_ID, "--scope", item),
            )
            for item in scopes
        )
