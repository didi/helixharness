"""Status remains observational across active, blocked and archived workflows."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import project_status
import test_workflow as workflow_fixture


class StatusObservationTest(unittest.TestCase):
    def setUp(self):
        # The shared fixture creates only a temporary synthetic Git history.
        # Never invoke a developer's global commit hooks from this regression.
        count = int(os.environ.get('GIT_CONFIG_COUNT', '0'))
        git_env = mock.patch.dict(os.environ, {
            'GIT_CONFIG_COUNT': str(count + 1),
            f'GIT_CONFIG_KEY_{count}': 'core.hooksPath',
            f'GIT_CONFIG_VALUE_{count}': os.devnull,
        })
        git_env.start()
        self.addCleanup(git_env.stop)
        self.fixture = workflow_fixture.LiteWorkflowCliTest()
        self.fixture.tempdir = tempfile.TemporaryDirectory()
        self.fixture.repo = Path(self.fixture.tempdir.name) / 'repo'
        self.fixture.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.fixture.repo)], check=True)
        subprocess.run([
            'git', '-C', str(self.fixture.repo), '-c', f'core.hooksPath={os.devnull}',
            '-c', 'user.email=test@example.invalid', '-c', 'user.name=Synthetic test',
            'commit', '--allow-empty', '-qm', 'synthetic baseline',
        ], check=True)
        self.fixture.feature = 'checkout'
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.repo

    def files(self):
        return {
            str(p.relative_to(self.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.repo.rglob('*') if p.is_file()
        }

    def observe(self, *args, **kwargs):
        before = self.files()
        result = project_status.snapshot(self.repo, *args, **kwargs)
        self.assertEqual(before, self.files(), 'Status changed repository or private state')
        self.assertTrue(result['read_only'])
        self.assertIn('not rerun', result['note'])
        return result

    def test_empty_repository_has_no_feature_or_invented_next_action(self):
        result = self.observe()
        self.assertTrue(result['empty'])
        self.assertEqual(result['features'], [])
        self.assertFalse((self.repo / '.helixharness').exists())

    def test_ready_status_does_not_bind_apply_or_start_execution(self):
        self.fixture.write_spec(status='ready')
        self.fixture.write_tasks(status='ready')
        item = self.observe()['features'][0]
        self.assertEqual(item['status'], 'ready')
        self.assertEqual(item['next_command'], '/helix:apply checkout')
        self.assertEqual(item['tasks']['pending'], 1)
        self.assertFalse((self.repo / '.helixharness').exists())

    def test_blocked_task_is_reported_without_repair_or_reset(self):
        self.fixture.write_spec(status='ready')
        self.fixture.write_tasks(status='ready', rows='| T-01 | Waiting for input | AC-01 | — | blocked |')
        item = self.observe()['features'][0]
        self.assertEqual(item['tasks']['blocked'], 1)
        self.assertEqual(item['blocked_tasks'][0]['id'], 'T-01')
        self.assertTrue(item['issues'])
        self.assertIn('unfinished work', ' '.join(item['issues']))

    def test_in_progress_task_is_not_reset_by_status(self):
        self.fixture.write_spec(status='applying')
        self.fixture.write_tasks(status='applying', rows='| T-01 | Partial implementation | AC-01 | — | in_progress |')
        self.fixture.write_apply_state()
        item = self.observe('checkout')['features'][0]
        self.assertEqual(item['tasks']['in_progress'], 1)
        self.assertEqual(len(item['in_progress_tasks']), 1)

    def test_missing_evidence_does_not_claim_archive_passed(self):
        self.fixture.write_spec(status='implemented')
        self.fixture.write_tasks(status='completed', rows='| T-01 | Completed implementation | AC-01 | — | completed |')
        self.fixture.write_apply_state()
        item = self.observe()['features'][0]
        self.assertEqual(item['archive_check'], 'failed')
        self.assertIsNone(item['next_command'])
        self.assertTrue(item['issues'])

    def test_completed_directory_requires_valid_evidence_and_review_seal(self):
        self.fixture.write_spec(status='implemented')
        self.fixture.write_tasks(status='completed', rows='| T-01 | Completed implementation | AC-01 | — | completed |')
        self.fixture.write_apply_state()
        workflow_fixture.write(self.repo / 'delivery.py', 'def value():\n    return 1\n')
        self.fixture.write_evidence()
        self.fixture.run_cli('authorize-knowledge', '--feature', 'checkout', '--paths-json', '[]')
        directory = self.fixture.feature_dir
        for name, before, after in (
            ('spec.md', 'status: implemented', 'status: completed'),
            ('verify-report.md', 'archive: pending', 'archive: completed'),
        ):
            p = directory / name
            p.write_text(p.read_text().replace(before, after))
        self.fixture.run_cli('finalize-preflight', '--feature', 'checkout')
        completed = self.repo / 'docs/specs/completed/checkout'
        completed.parent.mkdir(parents=True)
        directory.rename(completed)
        item = self.observe('checkout')['features'][0]
        self.assertEqual(item['status'], 'archived')
        self.assertEqual(item['archive_check'], 'passed')
        self.assertIsNone(item['next_command'])
        (completed / 'verify-report.md').unlink()
        item = self.observe(completed=True)['features'][0]
        self.assertNotEqual(item['status'], 'archived')
        self.assertEqual(item['archive_check'], 'failed')
        self.assertIsNone(item['next_command'])
