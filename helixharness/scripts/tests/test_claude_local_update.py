"""Verify formal local Claude installations without trusting arbitrary copies."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from update_hosts.base import UpdateHostError
from update_hosts.claude_code import ClaudeCodeUpdateAdapter


class ClaudeLocalInstallationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "marketplace"
        (self.source / ".claude-plugin").mkdir(parents=True)
        self.manifest = {"plugins": [{"name": "helix", "source": ".", "version": "1.0.0"}]}
        self.write_manifest()
        self.adapter = ClaudeCodeUpdateAdapter(str, lambda path: json.loads(path.read_text()))
        self.runner = Mock()
        self.marketplace = {"name": "helixharness-marketplace", "source": "directory",
                            "installLocation": str(self.source)}
        self.runner.run_json.return_value = [self.marketplace]
        self.installed = [{"id": "helix@helixharness-marketplace", "enabled": True,
                           "scope": "user", "version": "1.0.0", "installPath": str(self.root / "cache")}]

    def write_manifest(self):
        (self.source / ".claude-plugin/marketplace.json").write_text(json.dumps(self.manifest))

    def inspect(self, root=None):
        return self.adapter.inspect_installation(self.root, root or self.source, self.runner, self.installed)

    def test_registered_directory_source_is_a_formal_runtime_root(self):
        result = self.inspect()
        self.assertEqual(result["current_version"], "1.0.0")
        self.assertEqual(result["current_scope"], "user")

    def test_exact_install_path_does_not_require_source_fallback(self):
        self.installed[0]["installPath"] = str(self.source)
        self.inspect()
        self.runner.run_json.assert_not_called()

    def test_unrelated_copy_or_nonlocal_marketplace_is_rejected(self):
        with self.assertRaises(UpdateHostError):
            self.inspect(self.root / "unrelated-copy")
        self.marketplace["source"] = "git"
        with self.assertRaises(UpdateHostError):
            self.inspect()

    def test_source_cannot_escape_the_marketplace(self):
        for source in ("../outside", str(self.root / "outside")):
            self.manifest["plugins"][0]["source"] = source
            self.write_manifest()
            with self.subTest(source=source), self.assertRaises(UpdateHostError):
                self.inspect(self.root / "outside")

    def test_missing_or_ambiguous_registration_is_rejected(self):
        for listings in ([], [self.marketplace, self.marketplace]):
            self.runner.run_json.return_value = listings
            with self.subTest(listings=listings), self.assertRaises(UpdateHostError):
                self.inspect()

    def test_source_version_must_match_the_selected_installation(self):
        self.installed[0]["version"] = "0.9.0"
        with self.assertRaisesRegex(UpdateHostError, "version does not match"):
            self.inspect()

    def test_disabled_or_wrong_plugin_id_is_not_a_formal_installation(self):
        self.installed[0]["enabled"] = False
        with self.assertRaises(UpdateHostError):
            self.inspect()
        self.installed[0].update(enabled=True, id="helix@another-marketplace")
        with self.assertRaises(UpdateHostError):
            self.inspect()


if __name__ == "__main__":
    unittest.main()
