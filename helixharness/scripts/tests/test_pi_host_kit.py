#!/usr/bin/env python3
"""Pi Host Kit: projection, package manifest and lifecycle binding."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ROOT.parent
PUBLIC_COMMANDS = {
    "init", "propose", "apply", "archive", "bug-fix", "ut", "update", "status", "config"
}

sys.path.insert(0, str(ROOT / "scripts"))

import pi_skill_projection  # noqa: E402
from host_runtime import HostRegistry  # noqa: E402


class PiProjectionTest(unittest.TestCase):
    def test_command_projection_is_current(self):
        pi_skill_projection.check(ROOT)

    def test_projection_covers_exact_public_command_set(self):
        skills = {
            path.parent.name for path in (ROOT / "hosts/pi/skills").glob("*/SKILL.md")
        }
        self.assertEqual(skills, {f"helix-{name}" for name in PUBLIC_COMMANDS})


class PiHostKitTest(unittest.TestCase):
    def test_manifest_binds_pi_without_pretending_missing_capabilities(self):
        manifest = HostRegistry.discover(ROOT / "hosts").get("pi-coding-agent")
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest.host_id, "pi")
        self.assertEqual(manifest.capability("skill_discovery"), "native")
        self.assertEqual(manifest.capability("session_start"), "native")
        self.assertEqual(manifest.capability("subagents"), "unsupported")
        self.assertEqual(manifest.capability("telemetry"), "unsupported")
        self.assertEqual(manifest.capability("plugin_update"), "unsupported")

    def test_session_start_binds_this_host_kit(self):
        with tempfile.TemporaryDirectory() as cwd:
            process = subprocess.run(
                [sys.executable, str(ROOT / "hosts/pi/hook_entry.py")],
                input=json.dumps(
                    {"hook_event_name": "SessionStart", "cwd": cwd, "session_id": "fixture"}
                ),
                text=True,
                capture_output=True,
                timeout=30,
            )
        self.assertEqual(0, process.returncode, process.stderr)
        context = json.loads(process.stdout)["hookSpecificOutput"]["additionalContext"]
        binding = json.loads(context.split(": ", 1)[1].split("\n", 1)[0])
        self.assertEqual(binding["canonical_host_id"], "pi")
        self.assertEqual(binding["host_manifest"], str(ROOT / "hosts/pi/host.json"))


class PiPackageTest(unittest.TestCase):
    def test_package_manifest_loads_existing_resources(self):
        manifest = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertIn("pi-package", manifest["keywords"])
        for relative in [*manifest["pi"]["extensions"], *manifest["pi"]["skills"]]:
            with self.subTest(resource=relative):
                self.assertTrue((REPO_ROOT / relative).exists())

    def test_package_version_matches_the_version_policy(self):
        manifest = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
        policy = json.loads(
            (ROOT / "reference/version-policy.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], policy["latest_version"])

    def test_extension_forwards_lifecycle_and_registers_every_command(self):
        source = (ROOT / "hosts/pi/extension.ts").read_text(encoding="utf-8")
        for event in ('"session_start"', '"session_shutdown"', '"before_agent_start"'):
            self.assertIn(event, source)
        self.assertIn('path.join(root, "hosts", "pi", "hook_entry.py")', source)
        block = source.split("const COMMANDS: Record<string, string> = {", 1)[1]
        block = block.split("\n};", 1)[0]
        keys = {
            line.strip().split(":", 1)[0].strip().strip('"')
            for line in block.splitlines()
            if ":" in line
        }
        self.assertEqual(keys, PUBLIC_COMMANDS)


if __name__ == "__main__":
    unittest.main()
