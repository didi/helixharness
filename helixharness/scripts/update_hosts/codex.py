"""Codex version-update host adapter."""

from __future__ import annotations

import hashlib
import os
import tempfile
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
    same_path,
)


def detect_host(environment: Any) -> int | None:
    """存在 Codex 稳定会话标记时优先识别为 Codex。"""

    return 100 if environment.get("CODEX_THREAD_ID") else None


def plugin_version(plugin_root: Path) -> str:
    """读取 Codex plugin manifest 中的 HelixHarness 版本。"""

    try:
        data = eval_json.loads(
            (plugin_root / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateHostError("Codex 版本清单不可用") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise UpdateHostError("Codex 版本清单不可用")
    return version.strip()


class CodexUpdateAdapter(UpdateHostAdapter):
    host_id = "codex"
    supported_scopes = frozenset({"default"})
    manifest_directory = ".codex-plugin"

    def __init__(
        self,
        normalize_version: Callable[[Any], str],
        read_json: Callable[[Path], dict[str, Any]],
    ) -> None:
        self._normalize_version = normalize_version
        self._read_json = read_json

    def state_home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()

    def volatile_check_cache_paths(
        self, plugin_root: Path
    ) -> tuple[Path | None, Path | None]:
        thread = os.environ.get("CODEX_THREAD_ID") or "process-independent"
        identity = hashlib.sha256(
            f"{thread}\0{plugin_root.resolve()}".encode("utf-8")
        ).hexdigest()[:24]
        root = (
            Path(tempfile.gettempdir())
            / "helixharness"
            / "codex-version-check"
            / identity
        )
        return root / "version-check-cache.json", root / "version-check-negative.json"

    def inspect_installation(
        self,
        repo_root: Path,
        plugin_root: Path,
        runner: BoundedCommandRunner,
        plugin_list_data: Any = None,
    ) -> dict[str, Any]:
        del repo_root
        data = (
            runner.run_json(
                ["codex", "plugin", "list", "--json", "--marketplace", MARKETPLACE],
                timeout=INSPECT_TIMEOUT_SECONDS,
            )
            if plugin_list_data is None
            else plugin_list_data
        )
        installed = data.get("installed") if isinstance(data, dict) else None
        if not isinstance(installed, list):
            raise UpdateHostError("invalid Codex plugin list")
        candidates = [
            item
            for item in installed
            if isinstance(item, dict)
            and item.get("pluginId") == PLUGIN_ID
            and item.get("installed") is True
            and item.get("enabled") is True
        ]
        if len(candidates) != 1:
            raise UpdateHostError("formal HelixHarness installation is missing or ambiguous")
        selected = candidates[0]
        raw_version = selected.get("version")
        version = self._normalize_version(raw_version)
        manifest = self._read_json(self.manifest_path(plugin_root))
        if self._normalize_version(manifest.get("version")) != version:
            raise UpdateHostError("formal installation version does not match the executing plugin")
        source = selected.get("source")
        install_path = source.get("path") if isinstance(source, dict) else None
        managed_cache_path = (
            self.state_home()
            / "plugins"
            / "cache"
            / MARKETPLACE
            / PLUGIN_NAME
            / str(raw_version).strip()
        )
        if not install_path or not (
            same_path(install_path, plugin_root)
            or same_path(managed_cache_path, plugin_root)
        ):
            raise UpdateHostError(
                "BLOCKED:helixharness_plugin_catalog_stale: executing HelixHarness "
                "is not the selected formal installation; fully restart Codex Desktop"
            )
        return {
            "platform": self.host_id,
            "plugin_id": PLUGIN_ID,
            "current_version": version,
            "current_scope": "default",
            "current_source": MARKETPLACE,
            "plugin_root": str(plugin_root.resolve()),
            "available_scopes": ["default"],
        }

    def marketplace_root(
        self,
        runner: BoundedCommandRunner,
        marketplace_list_data: Any = None,
    ) -> Path:
        data = (
            runner.run_json(
                ["codex", "plugin", "marketplace", "list", "--json"],
                timeout=INSPECT_TIMEOUT_SECONDS,
            )
            if marketplace_list_data is None
            else marketplace_list_data
        )
        marketplaces = data.get("marketplaces") if isinstance(data, dict) else None
        roots = [
            item.get("root")
            for item in (marketplaces if isinstance(marketplaces, list) else [])
            if isinstance(item, dict) and item.get("name") == MARKETPLACE
        ]
        roots = [value for value in roots if value]
        if len(roots) != 1:
            raise UpdateHostError("formal HelixHarness marketplace is missing or ambiguous")
        return Path(str(roots[0])).expanduser().resolve()

    @contextmanager
    def refresh_marketplace(self, runner: BoundedCommandRunner) -> Iterator[Path]:
        data = runner.run_json(
            ["codex", "plugin", "marketplace", "list", "--json"],
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
        marketplaces = data.get("marketplaces") if isinstance(data, dict) else None
        matches = [
            item for item in (marketplaces if isinstance(marketplaces, list) else [])
            if isinstance(item, dict) and item.get("name") == MARKETPLACE
        ]
        if len(matches) != 1:
            raise UpdateHostError("formal HelixHarness marketplace is missing or ambiguous")
        root = self.marketplace_root(runner, data)
        source = matches[0].get("marketplaceSource")
        source_type = source.get("sourceType") if isinstance(source, dict) else None
        if source_type == "local":
            yield root
            return
        if source_type != "git":
            raise UpdateHostError("unsupported HelixHarness marketplace source type")
        # Native `marketplace upgrade` also replaces installed caches. Inspect an
        # isolated snapshot instead, preserving the executing plugin until consent.
        # The CLI's list omits ref, and a fresh installation has no install
        # metadata file. Use its actual Git checkout: follow its selected branch,
        # while detached tag/commit installations remain pinned to that revision.
        url = runner.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            timeout=INSPECT_TIMEOUT_SECONDS,
        ).stdout.strip()
        ref = runner.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=INSPECT_TIMEOUT_SECONDS,
        ).stdout.strip()
        if ref == "HEAD":
            ref = runner.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                timeout=INSPECT_TIMEOUT_SECONDS,
            ).stdout.strip()
        if not url or url.startswith("-") or not ref or ref.startswith("-"):
            raise UpdateHostError("formal Codex marketplace Git source is invalid")
        with tempfile.TemporaryDirectory(prefix="helixharness-policy-") as temporary:
            snapshot = Path(temporary) / "marketplace"
            runner.run(
                ["git", "-c", "init.templateDir=", "init", "--quiet", str(snapshot)],
                timeout=MARKETPLACE_TIMEOUT_SECONDS,
            )
            runner.run(
                ["git", "-C", str(snapshot), "fetch", "--depth=1", "--", url, ref],
                timeout=MARKETPLACE_TIMEOUT_SECONDS,
            )
            runner.run(
                ["git", "-C", str(snapshot), "-c", "core.hooksPath=/dev/null",
                 "checkout", "--quiet", "--detach", "FETCH_HEAD"],
                timeout=MARKETPLACE_TIMEOUT_SECONDS,
            )
            yield snapshot.resolve()

    def install_steps(
        self, scope: str, runner: BoundedCommandRunner
    ) -> tuple[InstallStep, ...]:
        self.validate_scope(scope)
        data = runner.run_json(
            ["codex", "plugin", "marketplace", "list", "--json"],
            timeout=INSPECT_TIMEOUT_SECONDS,
        )
        marketplaces = data.get("marketplaces") if isinstance(data, dict) else None
        matches = [item for item in (marketplaces if isinstance(marketplaces, list) else [])
                   if isinstance(item, dict) and item.get("name") == MARKETPLACE]
        if len(matches) != 1:
            raise UpdateHostError("formal HelixHarness marketplace is missing or ambiguous")
        source = matches[0].get("marketplaceSource")
        kind = source.get("sourceType") if isinstance(source, dict) else None
        if kind not in {"local", "git"}:
            raise UpdateHostError("unsupported HelixHarness marketplace source type")
        refresh = (InstallStep(
            step_id="refresh-marketplace",
            command=("codex", "plugin", "marketplace", "upgrade", "--json", MARKETPLACE),
        ),) if kind == "git" else ()
        return refresh + (
            InstallStep(
                step_id="update-default",
                command=("codex", "plugin", "add", PLUGIN_ID, "--json"),
            ),
        )
