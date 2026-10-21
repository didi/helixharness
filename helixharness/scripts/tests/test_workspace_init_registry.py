from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import validate_init_outputs
import workspace


class WorkspaceInitRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for repo in ('service', 'helix'):
            (self.root / repo).mkdir()
        for path in validate_init_outputs.WORKSPACE_DIRS:
            (self.root / path).mkdir(parents=True, exist_ok=True)
        for path in (*validate_init_outputs.WORKSPACE_FILES, 'AGENTS.md'):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('Initialized workspace\n', encoding='utf-8')
        self.registry = self.root / 'helixharness.workspace.yaml'
        self.registry.write_text('''version: 1
repos:
  service:
    path: ./service
    helix:
      initialized: true
      service_meta: docs/knowledge/service-meta.yaml
      nested:
        path: ./not-a-repository
  helix:
    path: ./helix
    helix:
      initialized: true
      service_meta: docs/knowledge/service-meta.yaml
''', encoding='utf-8')

    def test_documented_nested_metadata_does_not_create_repositories(self) -> None:
        self.assertEqual(workspace.load_registry(self.root), {
            'service': self.root / 'service', 'helix': self.root / 'helix',
        })

    def test_workspace_init_accepts_usable_registry(self) -> None:
        report = validate_init_outputs.validate(self.root, 'workspace', 'codex', 'disabled')
        self.assertEqual(report['status'], 'pass', report)
        self.assertTrue(any(check['check'] == 'workspace_registry'
                            and check['status'] == 'pass' for check in report['checks']))

    def test_workspace_init_rejects_nonempty_but_unusable_registry(self) -> None:
        for text in ('version: 1\n', 'repos:\n  service:\n    path: ./missing\n'):
            with self.subTest(registry=text):
                self.registry.write_text(text, encoding='utf-8')
                report = validate_init_outputs.validate(self.root, 'workspace', 'codex', 'disabled')
                self.assertEqual(report['status'], 'fail', report)
                self.assertTrue(any(error['check'] == 'workspace_registry'
                                    for error in report['errors']))
