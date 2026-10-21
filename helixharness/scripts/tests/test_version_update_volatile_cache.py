#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from update_hosts import BoundedCommandRunner, UpdateHostError
from update_hosts.codex import CodexUpdateAdapter
from version_update import check_for_update


VERSION = "1.1.0"
class FailingRefreshRunner(BoundedCommandRunner):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, command, *, timeout):
        self.calls += 1
        raise UpdateHostError("offline")


class VersionUpdateVolatileCacheTest(unittest.TestCase):
    def setUpPlugin(self, root: Path) -> None:
        for directory in (
            ".codex-plugin",
            ".claude-plugin",
            "commands",
            "reference",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / ".codex-plugin/plugin.json").write_text(
            json.dumps({"version": VERSION}), encoding="utf-8"
        )
        (root / "commands/apply.md").write_text("apply\n", encoding="utf-8")
        (root / ".claude-plugin/marketplace.json").write_text(
            json.dumps(
                {
                    "name": "helixharness-marketplace",
                    "plugins": [
                        {"name": "helix", "source": ".", "version": VERSION}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (root / "reference/version-policy.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "plugin": "helix",
                    "marketplace": "helixharness-marketplace",
                    "latest_version": VERSION,
                    "minimum_supported_version": "1.0.0",
                    "force_update_below": "1.0.0",
                    "force_update_message": "upgrade",
                    "releases": [
                        {
                            "version": VERSION,
                            "date": "2026-08-17",
                            "update_type": "recommended",
                            "title": "test",
                            "changes": ["test"],
                            "restart_required": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def pluginList(self, root: Path):
        return {
            "installed": [
                {
                    "pluginId": "helix@helixharness-marketplace",
                    "installed": True,
                    "enabled": True,
                    "version": VERSION,
                    "source": {"path": str(root)},
                }
            ]
        }

    def test_unwritable_primary_uses_volatile_policy_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin = base / "plugin"
            plugin.mkdir()
            self.setUpPlugin(plugin)
            blocker = base / "blocked-parent"
            blocker.write_text("not a directory", encoding="utf-8")
            primary = blocker / "cache.json"
            volatile = base / "volatile/cache.json"
            now = datetime(2026, 8, 17, tzinfo=timezone.utc)
            kwargs = dict(
                plugin_root=plugin,
                plugin_list_data=self.pluginList(plugin),
                marketplace_list_data={
                    "marketplaces": [
                        {"name": "helixharness-marketplace", "root": str(plugin)}
                    ]
                },
                volatile_cache_path=volatile,
                now=now,
            )

            first = check_for_update("codex", plugin, primary, **kwargs)
            second = check_for_update("codex", plugin, primary, **kwargs)

            self.assertEqual("volatile-cache-fallback", first["decision_source"])
            self.assertEqual("volatile-cache", second["decision_source"])
            self.assertTrue(volatile.is_file())

    def test_offline_failure_is_negative_cached_for_the_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin = base / "plugin"
            plugin.mkdir()
            self.setUpPlugin(plugin)
            runner = FailingRefreshRunner()
            negative = base / "volatile/negative.json"
            kwargs = dict(
                plugin_root=plugin,
                plugin_list_data=self.pluginList(plugin),
                volatile_cache_path=base / "volatile/cache.json",
                negative_cache_path=negative,
                now=datetime(2026, 8, 17, tzinfo=timezone.utc),
                runner=runner,
            )

            first = check_for_update("codex", plugin, base / "primary.json", **kwargs)
            second = check_for_update("codex", plugin, base / "primary.json", **kwargs)

            self.assertEqual("WARN:helixharness_version_check_unavailable", first["warning"])
            self.assertEqual("negative-cache", second["decision_source"])
            self.assertEqual(1, runner.calls)
            persisted = negative.read_text(encoding="utf-8")
            self.assertNotIn("offline", persisted)
            self.assertIn("marketplace_refresh_failed", persisted)
            self.assertNotIn("bundle_digest", persisted)

    def test_positive_cache_remains_valid_when_same_version_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin = base / "plugin"
            plugin.mkdir()
            self.setUpPlugin(plugin)
            volatile = base / "volatile/cache.json"
            blocker = base / "blocked-parent"
            blocker.write_text("not a directory", encoding="utf-8")
            primary = blocker / "cache.json"
            kwargs = dict(
                plugin_root=plugin,
                plugin_list_data=self.pluginList(plugin),
                marketplace_list_data={
                    "marketplaces": [
                        {"name": "helixharness-marketplace", "root": str(plugin)}
                    ]
                },
                volatile_cache_path=volatile,
                now=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )
            first = check_for_update("codex", plugin, primary, **kwargs)
            (plugin / "commands/apply.md").write_text("changed\n", encoding="utf-8")

            second = check_for_update("codex", plugin, primary, **kwargs)

            self.assertEqual("volatile-cache-fallback", first["decision_source"])
            self.assertEqual("volatile-cache", second["decision_source"])
            self.assertNotIn("bundle_digest", second)

    def test_retired_cache_field_invalidates_cache_and_forces_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin = base / "plugin"
            plugin.mkdir()
            self.setUpPlugin(plugin)
            volatile = base / "volatile/cache.json"
            blocker = base / "blocked-parent"
            blocker.write_text("not a directory", encoding="utf-8")
            primary = blocker / "cache.json"
            kwargs = dict(
                plugin_root=plugin,
                plugin_list_data=self.pluginList(plugin),
                marketplace_list_data={
                    "marketplaces": [
                        {"name": "helixharness-marketplace", "root": str(plugin)}
                    ]
                },
                volatile_cache_path=volatile,
                now=datetime(2026, 8, 17, tzinfo=timezone.utc),
            )
            check_for_update("codex", plugin, primary, **kwargs)
            cached = json.loads(volatile.read_text(encoding="utf-8"))
            cached["bundle_digest"] = {"legacy": True}
            volatile.write_text(json.dumps(cached), encoding="utf-8")

            result = check_for_update("codex", plugin, primary, **kwargs)

            self.assertEqual("volatile-cache-fallback", result["decision_source"])
            self.assertNotIn("bundle_digest", result)

    def test_codex_adapter_owns_deterministic_volatile_paths(self) -> None:
        adapter = CodexUpdateAdapter(lambda value: str(value), lambda path: {})
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary) / "plugin"
            first = adapter.volatile_check_cache_paths(plugin)
            second = adapter.volatile_check_cache_paths(plugin)

        self.assertEqual(first, second)
        self.assertTrue(first[0].name == "version-check-cache.json")
        self.assertTrue(first[1].name == "version-check-negative.json")
        self.assertNotIn(str(plugin), str(first[0]))


if __name__ == "__main__":
    unittest.main()
