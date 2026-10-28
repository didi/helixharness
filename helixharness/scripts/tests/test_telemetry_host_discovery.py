import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import telemetry_hosts
from telemetry_hosts.codex import CodexAdapter

ROOT = Path(__file__).resolve().parents[2]


class TelemetryHostDiscoveryTest(unittest.TestCase):
    def tearDown(self):
        telemetry_hosts._host_registry.cache_clear()
        telemetry_hosts._adapter.cache_clear()

    def test_added_host_manifest_loads_adapter_without_central_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            shutil.copytree(
                ROOT, root, ignore=shutil.ignore_patterns("__pycache__", ".git")
            )
            new_host = root / "hosts/future-test"
            new_host.mkdir()
            manifest = json.loads((root / "hosts/codex/host.json").read_text())
            manifest.update(host_id="future-test", aliases=["future-alias"])
            manifest["adapters"]["telemetry"] = "python:fixture_telemetry_host:Adapter"
            (new_host / "host.json").write_text(json.dumps(manifest))
            module = types.ModuleType("fixture_telemetry_host")
            module.Adapter = type(
                "Adapter", (CodexAdapter,), {"host_id": "future-test"}
            )
            with (
                patch.object(telemetry_hosts, "PLUGIN_ROOT", root),
                patch.dict(sys.modules, {"fixture_telemetry_host": module}),
            ):
                self.tearDown()
                self.assertIn("future-test", telemetry_hosts.supported_host_ids())
                adapter = telemetry_hosts.get_host_adapter("future-alias")
                self.assertEqual("future-test", adapter.host_id)
                self.assertIsNone(telemetry_hosts.get_host_adapter("codex-model-name"))
                self.assertIsNone(telemetry_hosts.get_host_adapter("claude-codex"))
                self.assertIsNone(telemetry_hosts.get_host_adapter("unregistered"))


if __name__ == "__main__":
    unittest.main()
