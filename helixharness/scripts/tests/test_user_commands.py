"""User-visible status/config behavior on synthetic repositories and homes."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import project_status
import reporting_config
import telemetry_consent
import user_config
import test_workspace
import test_workflow


class UserCommandsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.home = self.root / 'home'
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'CODEX_HOME': str(self.home / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.home / 'claude'),
            'HELIXHARNESS_TELEMETRY_HOME': str(self.home / 'telemetry'),
            'HELIXHARNESS_FEEDBACK_HOME': str(self.home / 'feedback'),
        })
        patch.start()
        self.addCleanup(patch.stop)

    def files(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def feature(self, name='demo', status='ready', rows=None, state='active'):
        directory = self.repo / 'docs/specs' / state / name
        directory.mkdir(parents=True)
        (directory / 'spec.md').write_text(
            f'---\nfeature: {name}\nexecution_scope: single\nstatus: {status}\n'
            'testing_seam_confirmed: true\n---\nSynthetic change.\n')
        if rows is not None:
            task_status = 'ready' if status == 'ready' else 'applying'
            (directory / 'tasks.md').write_text(
                f'---\nfeature: {name}\nstatus: {task_status}\n---\n'
                '| ID | Task | Blocked By | Status |\n| --- | --- | --- | --- |\n' + rows)
        return directory

    def run_config(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = user_config.main(['--repo-root', str(self.repo), '--platform', 'codex', *args])
        return code, json.loads(output.getvalue())

    def test_empty_status_and_config_show_do_not_create_files(self):
        before = self.files()
        self.assertTrue(project_status.snapshot(self.repo)['empty'])
        with mock.patch.object(telemetry_consent, '_purge_current_repo_private_state', side_effect=AssertionError('read must not purge')):
            code, result = self.run_config()
        self.assertEqual(code, 0)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['telemetry']['effective_mode'], 'disabled')
        self.assertEqual(before, self.files())

    def test_status_lists_multiple_features_and_preserves_bytes_and_mtime(self):
        self.feature(rows='| T-01 | First | — | pending |\n| T-02 | Second | T-01 | pending |\n')
        self.feature('second', 'planning')
        before = self.files()
        result = project_status.snapshot(self.repo)
        self.assertEqual([f['feature'] for f in result['features']], ['demo', 'second'])
        demo = result['features'][0]
        self.assertEqual(demo['tasks']['total'], 2)
        self.assertEqual(demo['next_task'], 'T-01')
        self.assertEqual(demo['next_command'], '/helix:apply demo')
        self.assertEqual(demo['issues'], [])
        self.assertEqual(result['features'][1]['next_command'], '/helix:propose second')
        self.assertEqual(before, self.files())

    def test_blocked_tasks_and_unverified_completion_are_not_success(self):
        self.feature(status='applying', rows='| T-01 | First | — | blocked |\n')
        result = project_status.snapshot(self.repo, 'demo')['features'][0]
        self.assertEqual(result['tasks']['blocked'], 1)
        self.assertTrue(result['issues'])
        self.feature('done', 'completed', '| T-01 | First | — | completed |\n', 'completed')
        done = project_status.snapshot(self.repo, 'done')['features'][0]
        self.assertNotEqual(done['status'], 'archived')
        self.assertTrue(done['issues'])
        self.assertIsNone(done['next_command'])

    def test_implemented_without_evidence_does_not_offer_archive_as_ready(self):
        self.feature(status='implemented', rows='| T-01 | First | — | completed |\n')
        result = project_status.snapshot(self.repo, 'demo')['features'][0]
        self.assertEqual(result['archive_check'], 'failed')
        self.assertIsNone(result['next_command'])
        self.assertTrue(result['issues'])

    def test_status_rejects_unsafe_paths_and_duplicate_locations(self):
        directory = self.feature(status='planning')
        external = self.root / 'external.md'
        external.write_text('must not read')
        (directory / 'spec.md').unlink()
        (directory / 'spec.md').symlink_to(external)
        self.assertIn('unsafe', project_status.snapshot(self.repo, 'demo')['features'][0]['issues'][0])
        self.feature(state='completed')
        self.assertIn('both', project_status.snapshot(self.repo, 'demo')['features'][0]['issues'][0])
        with self.assertRaises(ValueError):
            project_status.snapshot(self.repo, '../escape')

    def test_config_requires_explicit_authorization_and_scope(self):
        before = self.files()
        for args in [('--telemetry', 'enabled', '--scope', 'project'),
                     ('--telemetry', 'enabled', '--user-confirmed'),
                     ('--report-endpoint', 'https://example.test/report')]:
            code, result = self.run_config(*args)
            self.assertEqual(code, 2, result)
        self.assertEqual(before, self.files())

    def test_consent_changes_use_existing_scope_precedence_and_keep_view_read_only(self):
        for mode, scope in [('enabled', 'project'), ('disabled', 'user')]:
            code, result = self.run_config('--telemetry', mode, '--scope', scope, '--user-confirmed')
            self.assertEqual(code, 0, result)
            self.assertTrue(result['change']['updated'])
            self.assertEqual(result['telemetry']['effective_mode'], mode)
        code, result = self.run_config('--telemetry', 'enabled', '--scope', 'project', '--user-confirmed')
        self.assertEqual(code, 0, result)
        self.assertFalse(result['change']['updated'])
        self.assertEqual(result['change']['warning'], 'WARN:telemetry_user_scope_disabled')
        before = self.files()
        self.run_config()
        self.assertEqual(before, self.files())

    def test_endpoints_are_independent_merge_other_fields_and_reset_to_default(self):
        home = user_config.channel_home('telemetry')
        home.mkdir()
        target = home / 'config.json'
        target.write_text('{"custom": {"keep": true}}')
        user_config.set_endpoint('feedback', 'https://feedback.example/report', user_confirmed=True)
        user_config.set_endpoint('telemetry', 'https://stats.example/report', user_confirmed=True)
        self.assertEqual(json.loads(target.read_text())['custom'], {'keep': True})
        if os.name != 'nt':
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        with mock.patch.object(reporting_config, 'DEFAULT_REPORT_ENDPOINT', 'https://default.example/report'):
            result = user_config.set_endpoint('telemetry', None, user_confirmed=True)
        self.assertEqual(result['endpoint'], 'https://default.example/report')
        self.assertEqual(result['source'], 'published_default')
        self.assertNotIn('endpoint', json.loads(target.read_text()))
        self.assertEqual(user_config.describe_endpoint('feedback')['endpoint'], 'https://feedback.example/report')
        self.assertFalse((self.home / 'codex').exists())

    def test_bad_address_or_corrupt_file_is_not_overwritten_or_exposed(self):
        before = self.files()
        for endpoint in ['', 'file:///tmp/report', 'https://user:secret@example.test/report',
                         'https://example.test/report?key=secret', 'https://example.test/#part']:
            code, result = self.run_config('--report-endpoint', endpoint, '--user-confirmed')
            self.assertEqual(code, 2, result)
            self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(before, self.files())
        home = user_config.channel_home('telemetry')
        home.mkdir()
        target = home / 'config.json'
        target.write_text('{invalid synthetic config')
        before = self.files()
        code, result = self.run_config('--report-endpoint', 'https://example.test/report', '--user-confirmed')
        self.assertEqual(code, 2)
        self.assertEqual(before, self.files())

    def test_endpoint_symlink_is_not_followed(self):
        home = user_config.channel_home('telemetry')
        home.mkdir()
        external = self.root / 'external.json'
        external.write_text('{"keep": true}')
        (home / 'config.json').symlink_to(external)
        with self.assertRaises(user_config.ConfigError):
            user_config.set_endpoint('telemetry', 'https://example.test/report', user_confirmed=True)
        self.assertEqual(external.read_text(), '{"keep": true}')


class WorkspaceStatusTest(unittest.TestCase):
    def test_status_reads_synthetic_workspace_children_and_dependencies(self):
        fixture = test_workspace.LiteWorkspaceOverviewContractTest(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        fixture._write_overview(status='ready')
        result = project_status.snapshot(fixture.workspace, 'cross-repo')['features'][0]
        self.assertEqual(result['execution_scope'], 'workspace')
        self.assertEqual([child['repo'] for child in result['repositories']], ['api', 'web'])
        self.assertEqual(result['repositories'][1]['depends_on'], ['api'])
        self.assertTrue(result['issues'])  # Child specs have not been generated.
        for repo in ('api', 'web'):
            fixture._write_repo_feature(repo)
        result = project_status.snapshot(fixture.workspace, 'cross-repo')['features'][0]
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['repositories'][0]['tasks']['total'], 1)
        self.assertEqual(result['repositories'][1]['next_command'], '/helix:apply cross-repo')


class CompletedStatusTest(unittest.TestCase):
    def test_archive_ready_archived_and_drift_are_distinguished(self):
        fixture = test_workflow.LiteWorkflowCliTest(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.write_spec(status='implemented')
        fixture.write_tasks(status='completed', rows='| T-01 | First | AC-01 | — | completed |')
        fixture.write_evidence()
        before = {str(p): p.read_bytes() for p in fixture.repo.rglob('*') if p.is_file()}
        result = project_status.snapshot(fixture.repo, fixture.feature)['features'][0]
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['archive_check'], 'passed')
        self.assertEqual(result['status'], 'implemented')
        self.assertEqual(result['next_command'], '/helix:archive checkout')
        self.assertEqual(before, {str(p): p.read_bytes() for p in fixture.repo.rglob('*') if p.is_file()})
        spec = fixture.feature_dir / 'spec.md'
        spec.write_text(spec.read_text().replace('status: implemented', 'status: completed'))
        evidence = fixture.feature_dir / 'verify-report.md'
        evidence.write_text(evidence.read_text().replace('archive: pending', 'archive: completed'))
        completed = fixture.repo / 'docs/specs/completed' / fixture.feature
        completed.parent.mkdir(parents=True)
        fixture.feature_dir.rename(completed)
        result = project_status.snapshot(fixture.repo, fixture.feature)['features'][0]
        self.assertEqual(result['status'], 'archived')
        self.assertEqual(result['archive_check'], 'passed')
        evidence = completed / 'verify-report.md'
        evidence.write_text(evidence.read_text().replace('spec_verdict: APPROVE', 'spec_verdict: MUST_FIX'))
        result = project_status.snapshot(fixture.repo, fixture.feature)['features'][0]
        self.assertNotEqual(result['status'], 'archived')
        self.assertTrue(result['issues'])


if __name__ == '__main__':
    unittest.main()
