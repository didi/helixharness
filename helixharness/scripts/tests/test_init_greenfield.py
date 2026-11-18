from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[2]
SCRIPT = PLUGIN / 'scripts/init_greenfield.py'
STATE = 'docs/.helixharness-init-state.json'


class GreenfieldInitTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='helix project ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, relative, content='demo\n'):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)

    def invoke(self, action, *, exit_code=0):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), '--repo-root', str(self.root), action],
            capture_output=True, text=True,
        )
        self.assertEqual(exit_code, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_only_git_readme_docs_and_ignored_dependencies_are_greenfield(self):
        self.git('init', '-q')
        self.assertEqual('empty', self.invoke('probe')['projectType'])
        self.write('README.md')
        self.write('docs/overview.md')
        self.write('docs/notes.txt')
        self.write('.gitignore', '.cache/\n')
        self.write('.cache/generated.py')
        self.write('node_modules/dep/index.js')
        self.assertEqual('empty', self.invoke('probe')['projectType'])
        self.assertFalse((self.root / STATE).exists())

    def test_existing_source_and_build_metadata_are_not_greenfield(self):
        self.git('init', '-q')
        for name in ('index.html', 'bin/run.sh', 'docs/migrations/00.sql', 'pom.xml',
                     'pnpm-workspace.yaml', 'docs/requirements.txt', 'lib/unknown-source.foo'):
            with self.subTest(name=name):
                self.write(name)
                self.assertFalse(self.invoke('probe')['empty'])
                (self.root / name).unlink()

    def test_consent_cleanup_does_not_make_empty_repository_nonempty(self):
        self.git('init', '-q')
        self.assertTrue(self.invoke('probe')['empty'])
        env = {**os.environ,
               'HELIXHARNESS_TELEMETRY_HOME': str(self.root / '.git/test-telemetry'),
               'HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER': '1'}
        for platform in ('codex', 'claude-code'):
            with self.subTest(platform=platform):
                command = [sys.executable, str(PLUGIN / 'scripts/telemetry_consent.py'),
                           '--repo-root', str(self.root), '--platform', platform,
                           '--cache-path', str(self.root / f'.git/test-consent-{platform}.json')]
                for action in (['resolve'], ['choose', '--action', 'keep_disabled', '--user-confirmed']):
                    result = subprocess.run(command + action, env=env, capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, result.stderr or result.stdout)
                    self.assertEqual('disabled', json.loads(result.stdout)['effective_mode'])
                self.assertTrue((self.root / '.helixharness/telemetry-v2/ledger.lock').is_file())
                self.assertTrue(self.invoke('probe')['empty'])
                self.assertEqual('pending', self.invoke('prepare')['bootstrapStatus'])
                self.assertFalse((self.root / 'docs/eval').exists())

    def test_tracked_source_in_private_runtime_directory_is_not_hidden(self):
        self.git('init', '-q')
        self.write('.helixharness/custom.py')
        self.git('add', '.helixharness/custom.py')
        self.assertFalse(self.invoke('probe')['empty'])

    def test_tracked_code_in_dependency_dir_counts_even_if_ignored(self):
        self.git('init', '-q')
        self.write('.gitignore', 'vendor/\n')
        self.write('vendor/app.py')
        self.git('add', '-f', 'vendor/app.py')
        self.assertFalse(self.invoke('probe')['empty'])

    def test_non_git_and_nested_path_stop_without_writing(self):
        self.assertEqual('init_git_repository_required', self.invoke('probe', exit_code=2)['reason'])
        self.git('init', '-q')
        self.root = self.root / 'nested'
        self.root.mkdir()
        self.assertEqual('init_git_root_required', self.invoke('probe', exit_code=2)['reason'])
        self.assertFalse((self.root / STATE).exists())

    def test_final_state_records_no_scans_and_is_idempotent(self):
        self.git('init', '-q')
        self.assertEqual('pending', self.invoke('prepare')['bootstrapStatus'])
        self.assertEqual('init_required_artifact_missing', self.invoke('complete', exit_code=2)['reason'])
        self.write_bootstrap()
        state = self.invoke('complete')
        self.assertEqual('empty', state['projectType'])
        self.assertEqual([], state['scanPlan'])
        self.assertEqual('skipped', state['scanStatus'])
        self.assertEqual('completed', state['bootstrapStatus'])
        self.assertIs(type(state['completedAt']), int)
        self.assertEqual(state, json.loads((self.root / STATE).read_text(encoding='utf-8')))
        self.assertEqual(state, self.invoke('complete'))
        self.assertTrue(self.invoke('probe')['empty'])

    def test_prepare_does_not_overwrite_state_for_a_nonempty_project(self):
        self.git('init', '-q')
        self.write('app.py')
        self.write(STATE, '{"projectType":"backend"}')
        self.assertEqual('init_not_greenfield', self.invoke('prepare', exit_code=2)['reason'])
        self.assertEqual({'projectType': 'backend'}, json.loads((self.root / STATE).read_text(encoding='utf-8')))

    def write_bootstrap(self):
        self.write('docs/harness/index.md')
        self.write('docs/knowledge/index.md', '[Context](project-context.md)\n')
        self.write('docs/knowledge/project-context.md', '- **project-mode**: greenfield\n')
        for directory in ('docs/specs/active', 'docs/specs/completed', 'docs/hooks'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def test_verify_outputs_requires_finished_state_and_real_host_hooks(self):
        from project_hooks_cli import project_hooks
        from validate_init_outputs import validate

        self.git('init', '-q')
        self.invoke('prepare')
        self.write_bootstrap()
        for platform, entry in (('codex', 'AGENTS.md'), ('claude-code', 'CLAUDE.md')):
            with self.subTest(platform=platform):
                self.write(entry)
                project_hooks(self.root, platform, 'merge')
                self.assertEqual('fail', validate(self.root, 'greenfield', platform, 'disabled')['status'])
        self.invoke('complete')
        for platform in ('codex', 'claude-code'):
            with self.subTest(platform=platform):
                self.assertEqual('pass', validate(self.root, 'greenfield', platform, 'disabled')['status'])
        self.assertTrue(self.invoke('probe')['empty'])

    def test_bad_scan_state_is_not_marked_complete(self):
        self.git('init', '-q')
        state = self.invoke('prepare')
        state['scanPlan'] = ['scan-tech']
        self.write(STATE, json.dumps(state))
        self.assertEqual('init_state_update_failed', self.invoke('complete', exit_code=2)['reason'])
        self.assertEqual(state, json.loads((self.root / STATE).read_text(encoding='utf-8')))

    def test_complete_accepts_context_in_standard_knowledge_index_table(self):
        self.git('init', '-q')
        self.invoke('prepare')
        self.write_bootstrap()
        for entry in ('project-context.md', '`project-context.md`', './project-context.md',
                      '[Context](project-context.md)'):
            with self.subTest(entry=entry):
                self.write('docs/knowledge/index.md',
                           '| 文件/目录 | 定位 | 加载时机 |\n| --- | --- | --- |\n'
                           f'| {entry} | 项目上下文 | propose/apply 必读 |\n')
                self.assertEqual('completed', self.invoke('complete')['bootstrapStatus'])
        for entry in ('archive/project-context.md', 'project-context.md.bak'):
            with self.subTest(invalid_entry=entry):
                self.write('docs/knowledge/index.md', f'| {entry} | 项目上下文 | 必读 |\n')
                self.assertEqual('init_required_artifact_missing',
                                 self.invoke('complete', exit_code=2)['reason'])
