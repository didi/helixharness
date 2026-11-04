#!/usr/bin/env python3
"""OpenCode Host Kit and plugin package contracts."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from host_runtime import HostRegistry
from host_runtime.command_projection import load_command_projection
from host_runtime.project_config import load_project_config
from host_runtime.project_hooks import load_project_hooks
from telemetry_hosts import get_host_adapter, normalize_host_id
from update_hosts.opencode import detect_host

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HOST_ROOT = PLUGIN_ROOT / "hosts" / "opencode"
COMMANDS = {
    "init",
    "propose",
    "apply",
    "archive",
    "bug-fix",
    "ut",
    "update",
    "status",
    "config",
}


class OpenCodeHostKitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = HostRegistry.discover(PLUGIN_ROOT / "hosts")
        self.manifest = self.registry.require("opencode")

    def test_manifest_is_discovered_with_expected_capabilities(self) -> None:
        self.assertEqual(self.manifest.host_id, "opencode")
        self.assertEqual(self.manifest.host_api_version, "1.1")
        self.assertEqual(self.manifest.support_level, "preview")
        self.assertEqual(self.manifest.display_name, "OpenCode")

        expected = {
            "command_surface": "native",
            "skill_discovery": "native",
            "session_start": "emulated",
            "stop_hook": "unsupported",
            "failure_hook": "unsupported",
            "command_lifecycle": "native",
            "workflow_transition": "emulated",
            "subagents": "native",
            "unit_test_workflow": "native",
            "task_tracking": "unsupported",
            "project_hooks": "unsupported",
            "session_events": "unsupported",
            "telemetry": "unsupported",
            "plugin_install": "native",
            "plugin_update": "unsupported",
            "session_end": "unsupported",
        }
        self.assertEqual(self.manifest.capabilities, expected)

    def test_command_projection_is_the_canonical_native_surface(self) -> None:
        projection = load_command_projection(self.manifest)
        self.assertEqual(projection.surface, "native-command")
        self.assertEqual(set(projection.entrypoints), COMMANDS)
        for command, (entry, contract) in projection.entrypoints.items():
            self.assertEqual(entry, f"commands/{command}.md")
            self.assertEqual(contract, f"commands/{command}.md")
            text = (PLUGIN_ROOT / contract).read_text(encoding="utf-8")
            self.assertIn("HelixHarness 公共命令唯一契约", text)

    def test_project_config_uses_agents_md_without_settings(self) -> None:
        project = load_project_config(self.manifest)
        self.assertEqual(project.entry_file, "AGENTS.md")
        self.assertIsNone(project.settings_file)
        self.assertEqual(project.entry_template, "templates/project-entry.md.tpl")
        self.assertEqual(
            project.workspace_entry_template,
            "templates/workspace-entry.md.tpl",
        )

    def test_project_hooks_are_explicitly_unsupported(self) -> None:
        hooks = load_project_hooks(self.manifest)
        self.assertEqual(hooks.merge_strategy, "unsupported")
        self.assertIsNone(hooks.settings_file)
        self.assertIsNone(hooks.dedupe_key)
        self.assertIsNone(hooks.payload)

    def test_all_required_resources_resolve_to_files(self) -> None:
        for name in (
            "command_projection",
            "distribution",
            "installer",
            "lifecycle",
            "project_config",
            "project_hooks",
            "runtime",
            "subagent_protocol",
            "telemetry_protocol",
            "ut",
            "workflow_transition",
        ):
            resource = self.manifest.resolve_resource_adapter(name)
            self.assertTrue(resource.is_file(), name)
            self.assertTrue(resource.is_relative_to(PLUGIN_ROOT), name)
        self.assertEqual(
            self.manifest.resolve_resource_adapter("distribution"),
            (PLUGIN_ROOT / "package.json").resolve(),
        )
        self.assertEqual(
            self.manifest.resolve_resource_adapter("telemetry_protocol"),
            (PLUGIN_ROOT / "hosts/shared/telemetry.md").resolve(),
        )

    def test_package_manifest_exports_the_opencode_plugin(self) -> None:
        package = json.loads((PLUGIN_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["type"], "module")
        self.assertEqual(package["exports"]["."], "./opencode/index.ts")
        self.assertIn("@opencode/plugin", package["devDependencies"])
        self.assertEqual(package.get("dependencies", {}), {})
        self.assertTrue((PLUGIN_ROOT / "opencode/index.ts").is_file())
        self.assertTrue((PLUGIN_ROOT / "opencode/core.ts").is_file())
        self.assertTrue((PLUGIN_ROOT / "opencode/tests/core.test.ts").is_file())
        self.assertTrue((PLUGIN_ROOT / "opencode/tests/plugin.test.ts").is_file())
        self.assertTrue((PLUGIN_ROOT / "opencode/tests/e2e.test.ts").is_file())

    def test_plugin_source_registers_prefixed_surfaces_and_host_context(self) -> None:
        core = (PLUGIN_ROOT / "opencode/core.ts").read_text(encoding="utf-8")
        self.assertIn("helix:${command.name}", core)
        self.assertIn("id: `helix-${directory}`", core)
        self.assertIn("id: `helix-${agentName}`", core)
        self.assertIn("WORKER_AGENT_ID", core)
        self.assertIn('action: "subagent"', core)
        self.assertIn('effect: "deny"', core)
        self.assertIn("canonical_host_id", core)
        self.assertIn("resources.subagent_protocol", core)
        self.assertIn('ctx.tool.hook("execute.after"', core)
        self.assertIn("session.prompt", core)

    def test_detector_requires_explicit_opencode_markers(self) -> None:
        self.assertEqual(detect_host({"OPENCODE_SESSION_ID": "ses_x"}), 100)
        self.assertEqual(detect_host({"OPENCODE": "1"}), 80)
        self.assertIsNone(detect_host({}))
        self.assertIsNone(detect_host({"CODEX_THREAD_ID": "thread"}))

    def test_telemetry_and_update_are_declared_unsupported(self) -> None:
        self.assertEqual(normalize_host_id("opencode"), "opencode")
        self.assertIsNone(get_host_adapter("opencode"))

        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(PLUGIN_ROOT / "scripts/version_update.py"),
                    "--platform",
                    "opencode",
                    "--repo-root",
                    directory,
                    "check",
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=PLUGIN_ROOT / "scripts",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "unsupported")
        self.assertEqual(payload["reason"], "unsupported_host")
        self.assertEqual(payload["next_action"], "continue_silently")

    def test_host_context_cli_resolves_the_installed_plugin(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "scripts/host_runtime_cli.py"),
                "resolve",
                "--platform",
                "opencode",
                "--expected-plugin-root",
                str(PLUGIN_ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=PLUGIN_ROOT / "scripts",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["host_id"], "opencode")
        self.assertEqual(payload["plugin_root"], str(PLUGIN_ROOT.resolve()))
        self.assertEqual(payload["capabilities"]["telemetry"], "unsupported")
        self.assertEqual(
            payload["resources"]["runtime"], "hosts/opencode/runtime.md"
        )

    def test_host_kit_does_not_duplicate_public_business_contracts(self) -> None:
        self.assertFalse((HOST_ROOT / "commands").exists())
        self.assertFalse((HOST_ROOT / "skills").exists())
        self.assertFalse((HOST_ROOT / "hooks.json").exists())
        self.assertEqual(
            {path.stem for path in (PLUGIN_ROOT / "commands").glob("*.md")},
            COMMANDS,
        )


if __name__ == "__main__":
    unittest.main()
