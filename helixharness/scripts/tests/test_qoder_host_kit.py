"""Qoder Host Kit contract: the seam that kept the update path from working."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from host_runtime import HostRegistry  # noqa: E402
from host_runtime.command_projection import load_command_projection  # noqa: E402
from host_runtime.project_config import load_project_config  # noqa: E402
from host_runtime.project_hooks import load_project_hooks  # noqa: E402
from update_hosts.qoder import (  # noqa: E402
    QoderUpdateAdapter,
    detect_host,
    plugin_version,
)


class QoderHostKitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = HostRegistry.discover(PLUGIN_ROOT / "hosts").get("qoder")

    def test_host_is_registered_with_preview_support(self):
        self.assertIsNotNone(self.manifest)
        self.assertEqual("stable", self.manifest.support_level)
        self.assertIsNotNone(self.manifest.evidence)
        self.assertTrue((PLUGIN_ROOT / self.manifest.evidence).is_file())

    def test_command_surface_is_skill_based(self):
        projection = load_command_projection(self.manifest)
        self.assertEqual("skill", projection.surface)
        self.assertEqual(self.manifest.capability("command_surface"), "emulated")
        for entry, contract in projection.entrypoints.values():
            self.assertTrue(entry.startswith("hosts/qoder/skills/"))
            self.assertTrue(contract.startswith("commands/"))

    def test_project_config_and_hooks_agree_on_settings_file(self):
        config = load_project_config(self.manifest)
        hooks = load_project_hooks(self.manifest)
        self.assertEqual("AGENTS.md", config.entry_file)
        self.assertEqual(".qoder/settings.json", config.settings_file)
        self.assertEqual(config.settings_file, hooks.settings_file)
        self.assertEqual("parent", hooks.owner)

    def test_update_adapter_declares_the_qoder_marketplace_manifest(self):
        # The shared update core reads the manifest path from the adapter; a
        # wrong or missing declaration silently reports "latest" forever.
        self.assertEqual(
            ".qoder-plugin/marketplace.json",
            QoderUpdateAdapter.marketplace_manifest_relative,
        )
        self.assertTrue(
            (PLUGIN_ROOT / QoderUpdateAdapter.marketplace_manifest_relative).is_file()
        )
        self.assertEqual("1.0.0", plugin_version(PLUGIN_ROOT))

    def test_detector_does_not_claim_other_hosts(self):
        self.assertEqual(100, detect_host({"QODER_PLUGIN_ROOT": "/x"}))
        self.assertIsNone(detect_host({"CLAUDE_PLUGIN_ROOT": "/x"}))
        self.assertIsNone(detect_host({"CODEX_THREAD_ID": "x"}))
        self.assertIsNone(detect_host({}))


if __name__ == "__main__":
    unittest.main()
