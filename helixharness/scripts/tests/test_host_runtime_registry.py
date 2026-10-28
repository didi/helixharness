#!/usr/bin/env python3
"""Host Registry regressions migrated from 2.2.9 to the current Host API."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from host_runtime import HostContractError, HostRegistry

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def write_host(root: Path, directory: str, payload: dict[str, object]) -> None:
    target = root / directory
    target.mkdir(parents=True, exist_ok=True)
    (target / "host.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    plugin_root = root.parent.resolve()
    for raw_reference in payload.get("adapters", {}).values():
        reference = str(raw_reference)
        if not reference.startswith("resource:"):
            continue
        resource = (plugin_root / reference.split(":", 1)[1]).resolve()
        try:
            resource.relative_to(plugin_root)
        except ValueError:
            continue
        resource.parent.mkdir(parents=True, exist_ok=True)
        if not resource.exists():
            content = "{}" if resource.suffix == ".json" else "测试资源\n"
            if resource.name == "command-projection.json":
                commands = ("init", "propose", "apply", "archive", "bug-fix", "ut", "update", "status", "config")
                entries = {}
                for command in commands:
                    relative = f"commands/{command}.md"
                    command_path = plugin_root / relative
                    command_path.parent.mkdir(exist_ok=True)
                    command_path.write_text("HelixHarness 公共命令唯一契约\n")
                    entries[command] = [relative, relative]
                content = json.dumps({"projection_version": "1.0", "surface": "native-command", "entrypoints": entries})
            elif resource.name == "project-hooks.json":
                content = json.dumps({"project_hooks_version": "1.0", "owner": "parent", "settings_file": None, "merge_strategy": "unsupported", "dedupe_key": None, "payload": None})
            elif resource.name == "project.json":
                content = json.dumps({"entry_file": "AGENTS.md", "entry_template": "templates/project-entry.md.tpl", "workspace_entry_template": "templates/workspace-entry.md.tpl", "settings_file": None})
            resource.write_text(content, encoding="utf-8")


def manifest(host_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "host_id": host_id,
        "host_api_version": "1.1",
        "aliases": [],
        "capabilities": {
            "skill_discovery": "native",
            "command_surface": "native",
            "session_start": "unsupported",
            "subagents": "emulated",
        },
        "adapters": {
            "command_projection": f"resource:hosts/{host_id}/command-projection.json",
            "installer": f"resource:hosts/{host_id}/installer.md",
            "project_hooks": f"resource:hosts/{host_id}/project-hooks.json",
            "distribution": f"resource:hosts/{host_id}/distribution.json",
            "lifecycle": f"resource:hosts/{host_id}/lifecycle.md",
            "project_config": f"resource:hosts/{host_id}/project.json",
            "runtime": f"resource:hosts/{host_id}/runtime.md",
            "subagent_protocol": f"resource:hosts/{host_id}/subagent.md",
            "telemetry_protocol": f"resource:hosts/{host_id}/telemetry.md",
            "ut": f"resource:hosts/{host_id}/ut.md",
            "workflow_transition": f"resource:hosts/{host_id}/workflow-transition.md",
        },
    }
    capability_overrides = overrides.pop("capabilities", None)
    adapter_overrides = overrides.pop("adapters", None)
    payload.update(overrides)
    if capability_overrides is not None:
        payload["capabilities"].update(capability_overrides)
    if adapter_overrides is not None:
        payload["adapters"].update(adapter_overrides)
    return payload

class HostRuntimeRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.hosts_root = Path(self.temp_dir.name) / "hosts"
        self.hosts_root.mkdir()

    def test_discovers_new_host_from_directory_without_central_registration(self) -> None:
        write_host(self.hosts_root, "claude-code", manifest("claude-code"))
        write_host(
            self.hosts_root,
            "future-harness",
            manifest("future-harness", aliases=["future"]),
        )

        registry = HostRegistry.discover(self.hosts_root)

        self.assertEqual(registry.supported_ids(), ("claude-code", "future-harness"))
        self.assertEqual(registry.resolve_id("future"), "future-harness")
        self.assertEqual(
            registry.require("future-harness").capability("subagents"), "emulated"
        )


    def test_rejects_unknown_capability_level(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest("broken", capabilities={"subagents": "sometimes"}),
        )

        with self.assertRaisesRegex(HostContractError, "能力等级无效"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_missing_required_business_resource_adapter(self) -> None:
        payload = manifest("broken")
        del payload["adapters"]["runtime"]
        write_host(self.hosts_root, "broken", payload)

        with self.assertRaisesRegex(HostContractError, "缺少必需资源适配器"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_enabled_capability_without_its_adapter(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest(
                "broken",
                capabilities={"telemetry": "native"},
                adapters={
                    "distribution": "resource:hosts/broken/distribution.json",
                },
            ),
        )

        with self.assertRaisesRegex(HostContractError, "能力缺少适配器"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_enabled_capability_with_wrong_adapter_kind(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest(
                "broken",
                capabilities={"telemetry": "native"},
                adapters={
                    "telemetry": "resource:hosts/broken/telemetry.md",
                    "version": "python:pathlib:Path",
                },
            ),
        )

        with self.assertRaisesRegex(HostContractError, "能力适配器类型无效"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_declared_resource_that_does_not_exist(self) -> None:
        target = self.hosts_root / "broken"
        target.mkdir(parents=True)
        (target / "host.json").write_text(
            json.dumps(manifest("broken"), ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(HostContractError, "适配器资源不存在"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_unknown_manifest_fields(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest("broken", unexpected_platform_switch="claude-code"),
        )

        with self.assertRaisesRegex(HostContractError, "包含未知字段"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_support_evidence_path_outside_plugin_root(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest(
                "broken",
                support={
                    "level": "stable",
                    "display_name": "Broken",
                    "evidence": "../outside.json",
                    "limitations": [],
                },
            ),
        )

        with self.assertRaisesRegex(HostContractError, "E2E 证据引用无效"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_incomplete_explicit_support_declaration(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest(
                "broken",
                support={
                    "level": "preview",
                    "display_name": "Broken",
                },
            ),
        )

        with self.assertRaisesRegex(HostContractError, "支持声明缺少字段"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_duplicate_aliases_across_hosts(self) -> None:
        write_host(
            self.hosts_root,
            "first",
            manifest("first", aliases=["shared"]),
        )
        write_host(
            self.hosts_root,
            "second",
            manifest("second", aliases=["shared"]),
        )

        with self.assertRaisesRegex(HostContractError, "宿主别名重复"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_directory_and_host_id_mismatch(self) -> None:
        write_host(self.hosts_root, "directory-name", manifest("different-name"))

        with self.assertRaisesRegex(HostContractError, "必须与目录名一致"):
            HostRegistry.discover(self.hosts_root)

    def test_unknown_host_does_not_fall_back(self) -> None:
        write_host(self.hosts_root, "claude-code", manifest("claude-code"))
        registry = HostRegistry.discover(self.hosts_root)

        self.assertIsNone(registry.get("unknown-host"))
        self.assertIsNone(registry.resolve_id("unknown-host"))


    def test_resolves_python_and_resource_adapters_from_manifest(self) -> None:
        resource = self.hosts_root / "future" / "distribution.json"
        resource.parent.mkdir(parents=True)
        resource.write_text("{}", encoding="utf-8")
        write_host(
            self.hosts_root,
            "future",
            manifest(
                "future",
                adapters={
                    "distribution": "resource:hosts/future/distribution.json",
                    "fixture": "python:pathlib:Path",
                },
            ),
        )

        host = HostRegistry.discover(self.hosts_root).require("future")

        self.assertIs(host.load_python_adapter("fixture"), Path)
        self.assertEqual(host.resolve_resource_adapter("distribution"), resource.resolve())

    def test_rejects_adapter_resource_that_escapes_plugin_root(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest(
                "broken",
                adapters={"distribution": "resource:../outside.json"},
            ),
        )

        with self.assertRaisesRegex(HostContractError, "越过插件根目录"):
            HostRegistry.discover(self.hosts_root)

    def test_rejects_unknown_adapter_reference_scheme(self) -> None:
        write_host(
            self.hosts_root,
            "broken",
            manifest("broken", adapters={"runtime": "magic:somewhere"}),
        )

        with self.assertRaisesRegex(HostContractError, "适配器引用协议"):
            HostRegistry.discover(self.hosts_root)







if __name__ == "__main__":
    unittest.main()
