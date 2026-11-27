"""Marketplace policy resolution follows the selected adapter, not a host name."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from update_hosts.base import UpdateHostError
from update_hosts.claude_code import ClaudeCodeUpdateAdapter
from update_hosts.codex import CodexUpdateAdapter
from version_update import UpdateError, locate_formal_policy


class AlternateMarketplaceAdapter(CodexUpdateAdapter):
    """Synthetic format exercises the extension seam without a native client."""

    host_id = "future-host"
    manifest_directory = ".future-plugin"
    marketplace_manifest_directory = ".future-plugin"


class UpdateMarketplaceContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.adapter = AlternateMarketplaceAdapter(str, lambda path: json.loads(path.read_text()))

    def fixture(self, source):
        plugin = self.root / source
        plugin.mkdir(parents=True, exist_ok=True)
        for root in (self.root, plugin):
            (root / '.future-plugin').mkdir(exist_ok=True)
        (plugin / '.future-plugin/plugin.json').write_text(json.dumps({'version': '1.0.0'}))
        (self.root / '.future-plugin/marketplace.json').write_text(json.dumps({
            'name': 'helixharness-marketplace',
            'plugins': [{'name': 'helix', 'source': source, 'version': '1.0.0'}],
        }))
        reference = plugin / 'reference'
        reference.mkdir()
        policy = reference / 'version-policy.json'
        policy.write_text(json.dumps({
            'schema_version': '1.0', 'plugin': 'helix', 'marketplace': 'helixharness-marketplace',
            'latest_version': '1.0.0', 'minimum_supported_version': None,
            'force_update_below': None, 'force_update_message': None,
            'releases': [{'version': '1.0.0', 'date': '2026-11-18',
                          'update_type': 'recommended', 'title': 'Synthetic release',
                          'changes': ['Synthetic marketplace contract'], 'restart_required': True}],
        }))
        return policy

    def test_alternate_format_supports_combined_repository(self):
        expected = self.fixture('packages/plugin')
        with patch('version_update._adapter', return_value=self.adapter):
            actual = locate_formal_policy('future-host', snapshot_root=self.root)
        self.assertEqual(actual, expected)
        self.assertFalse((self.root / '.claude-plugin').exists())

    def test_alternate_format_supports_standalone_plugin(self):
        expected = self.fixture('.')
        with patch('version_update._adapter', return_value=self.adapter):
            self.assertEqual(locate_formal_policy('future-host', snapshot_root=self.root), expected)

    def test_existing_hosts_keep_the_compatible_marketplace_format(self):
        for cls in (ClaudeCodeUpdateAdapter, CodexUpdateAdapter):
            adapter = cls(str, lambda path: {})
            self.assertEqual(adapter.marketplace_manifest_path(self.root),
                             self.root / '.claude-plugin/marketplace.json')

    def test_metadata_directory_cannot_escape_the_selected_marketplace(self):
        for directory in ('../outside', '/outside'):
            with self.subTest(directory=directory):
                self.adapter.marketplace_manifest_directory = directory
                with self.assertRaises(UpdateHostError):
                    self.adapter.marketplace_manifest_path(self.root)

    def test_metadata_symlink_cannot_escape_the_selected_marketplace(self):
        outside = self.root / 'outside'
        outside.mkdir()
        source = self.root / 'source'
        source.mkdir()
        (source / '.future-plugin').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(UpdateHostError):
            self.adapter.marketplace_manifest_path(source)

    def test_policy_still_rejects_source_escape_and_version_mismatch(self):
        self.fixture('plugin')
        marketplace = self.root / '.future-plugin/marketplace.json'
        original = json.loads(marketplace.read_text())
        for field, value in (('source', '../outside'), ('version', '2.0.0')):
            payload = json.loads(json.dumps(original))
            payload['plugins'][0][field] = value
            marketplace.write_text(json.dumps(payload))
            with self.subTest(field=field), patch('version_update._adapter', return_value=self.adapter):
                with self.assertRaises(UpdateError):
                    locate_formal_policy('future-host', snapshot_root=self.root)
