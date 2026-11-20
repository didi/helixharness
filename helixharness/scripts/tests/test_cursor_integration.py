"""Synthetic CLI integration across host binding, scopes and workflow evidence.

These tests execute shipped Python entrypoints; they do not emulate a native
Cursor model or claim IDE/CLI acceptance.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow
import validate_init_outputs
from host_runtime import HostRegistry
from host_runtime.command_projection import load_command_projection
from project_hooks_cli import project_hooks
from workflow_router import route

PLUGIN = Path(__file__).resolve().parents[2]


class CursorIntegrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='helix-cursor-tests-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'application with spaces'
        self.repo.mkdir()
        self.env = dict(os.environ)
        self.env.pop('HELIXHARNESS_BOUND_PLUGIN_ROOT', None)
        self.env.pop('HELIXHARNESS_CURSOR_CONVERSATION_ID', None)
        self.env.pop('HELIXHARNESS_CURSOR_WORKSPACE_ROOTS', None)
        self.env.update({
            'HELIXHARNESS_TELEMETRY_HOME': str(self.root / 'telemetry'),
            'HELIXHARNESS_FEEDBACK_HOME': str(self.root / 'feedback'),
            'HELIXHARNESS_CURSOR_HOME': str(self.root / 'cursor'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
            'CODEX_HOME': str(self.root / 'codex'),
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
            'GIT_CONFIG_VALUE_0': '/dev/null',
        })

    def cli(self, script, *args, env=None, code=0):
        process = subprocess.run(
            [sys.executable, str(PLUGIN / 'scripts' / script), *map(str, args)],
            env=self.env if env is None else env, cwd=self.repo, text=True,
            capture_output=True, timeout=20,
        )
        self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def init_git(self):
        subprocess.run(['git', 'init', '-q', str(self.repo)], env=self.env, check=True,
                       capture_output=True, text=True, timeout=10)

    def test_init_hook_merge_preserves_native_installation_settings(self):
        settings = self.repo / '.cursor/settings.json'
        settings.parent.mkdir()
        baseline = json.dumps({'plugins': {'helixharness-marketplace/helix': {
            'enabled': True, 'gitUrl': 'https://example.invalid/release.git',
            'gitRef': 'a' * 40, 'gitPath': './helixharness'}}, 'unrelated': True})
        settings.write_text(baseline)
        for host, filename in (('claude', 'settings.json'), ('codex', 'hooks.json')):
            target = self.repo / ('.' + host) / filename
            target.parent.mkdir()
            target.write_text('{"sentinel":true}')
        project_hooks(self.repo, 'cursor', 'merge')
        project_hooks(self.repo, 'cursor', 'merge')
        self.assertEqual(settings.read_text(), baseline)
        self.assertEqual((self.repo / '.claude/settings.json').read_text(), '{"sentinel":true}')
        self.assertEqual((self.repo / '.codex/hooks.json').read_text(), '{"sentinel":true}')

    def test_explicit_cursor_binding_wins_over_other_host_environment(self):
        env = {**self.env, 'CODEX_THREAD_ID': 'synthetic-codex',
               'CLAUDE_SESSION_ID': 'synthetic-claude', 'HELIXHARNESS_PLATFORM': 'codex'}
        result = self.cli('host_runtime_cli.py', 'resolve', '--platform', 'cursor',
                          '--expected-plugin-root', PLUGIN, env=env)
        self.assertEqual(result['host_id'], 'cursor')
        self.assertEqual(result['host_api_version'], '1.2')
        self.assertEqual(result['plugin_root'], str(PLUGIN))
        self.assertEqual(result['resources']['runtime'], 'hosts/cursor/runtime.md')

    def test_unknown_host_does_not_fall_back_to_installed_hosts(self):
        result = self.cli('host_runtime_cli.py', 'resolve', '--platform', 'cursor-unknown', code=2)
        self.assertEqual(result['reason'], 'unsupported_host')
        self.assertEqual(self.snapshot(), {})

    def test_foreign_session_source_cannot_be_overridden_by_an_entry_argument(self):
        env = {**self.env, 'HELIXHARNESS_BOUND_PLUGIN_ROOT': str(self.root / 'other-plugin')}
        before = self.snapshot()
        result = self.cli('host_runtime_cli.py', 'resolve', '--platform', 'cursor',
                          '--expected-plugin-root', PLUGIN, env=env, code=2)
        self.assertEqual(result['reason'], 'plugin_source_mismatch')
        self.assertEqual(self.snapshot(), before)

    def test_relocated_cursor_bundle_resolves_without_other_host_kits(self):
        manifest = HostRegistry.discover(PLUGIN / 'hosts').require('cursor')
        projection = load_command_projection(manifest)
        resources = {'hosts/cursor/host.json', 'scripts/host_runtime_cli.py',
                     'scripts/source_binding.py', 'scripts/eval_json.py'}
        resources.update(str(p.relative_to(PLUGIN)) for p in (PLUGIN / 'scripts/host_runtime').glob('*.py'))
        resources.update(str(manifest.resolve_resource_adapter(key).relative_to(PLUGIN))
                         for key in manifest.adapters if manifest.adapter_reference(key).kind == 'resource')
        resources.update(path for pair in projection.entrypoints.values() for path in pair)
        for layout in ('combined', 'standalone'):
            with self.subTest(layout=layout):
                base = self.root / layout
                target = base / 'helixharness' if layout == 'combined' else base
                for relative in resources:
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(PLUGIN / relative, destination)
                source_market = PLUGIN.parent if layout == 'combined' else PLUGIN
                marketplace = base / '.cursor-plugin/marketplace.json'
                marketplace.parent.mkdir(exist_ok=True)
                shutil.copyfile(source_market / '.cursor-plugin/marketplace.json', marketplace)
                source = json.loads(marketplace.read_text())['plugins'][0]['source']
                self.assertEqual((base / source).resolve(), target)
                process = subprocess.run(
                    [sys.executable, str(target / 'scripts/host_runtime_cli.py'), 'resolve',
                     '--platform', 'cursor', '--expected-plugin-root', str(target)],
                    env=self.env, cwd=self.repo, text=True, capture_output=True, timeout=20)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                result = json.loads(process.stdout)
                self.assertEqual(result['host_id'], 'cursor')
                self.assertEqual(result['plugin_root'], str(target))
                self.assertEqual({p.name for p in (target / 'hosts').iterdir()}, {'cursor', 'shared'})

    def test_single_service_and_frontend_init_outputs_are_checked(self):
        for mode in ('service', 'frontend'):
            with self.subTest(mode=mode):
                repo = self.repo / mode
                repo.mkdir()
                for relative in validate_init_outputs.SERVICE_COMMON_DIRS:
                    (repo / relative).mkdir(parents=True, exist_ok=True)
                for relative in (*validate_init_outputs.SERVICE_COMMON_FILES, 'AGENTS.md',
                                 'docs/knowledge/service-meta.yaml'):
                    (repo / relative).write_text('Synthetic initialized project\n')
                (repo / 'AGENTS.md').write_text((PLUGIN / 'templates/project-entry.md.tpl').read_text().replace(
                    '{{PROJECT_CONTEXT}}', 'Synthetic project context.'))
                failed = validate_init_outputs.validate(repo, mode, 'cursor', 'disabled')
                self.assertEqual(failed['status'], 'fail')
                self.assertTrue(any(e['check'] == 'project_hooks' for e in failed['errors']))
                project_hooks(repo, 'cursor', 'merge')
                ready = validate_init_outputs.validate(repo, mode, 'cursor', 'disabled')
                self.assertEqual(ready['status'], 'pass', ready)
                self.assertFalse((repo / '.claude').exists())
                self.assertFalse((repo / '.codex').exists())

    def test_workspace_root_does_not_require_or_create_child_hooks(self):
        for relative in validate_init_outputs.WORKSPACE_DIRS:
            (self.repo / relative).mkdir(parents=True, exist_ok=True)
        for relative in (*validate_init_outputs.WORKSPACE_FILES, 'AGENTS.md'):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('Synthetic workspace\n')
        (self.repo / 'AGENTS.md').write_text((PLUGIN / 'templates/workspace-entry.md.tpl').read_text())
        for name in ('api', 'web'):
            (self.repo / name).mkdir()
        (self.repo / 'helixharness.workspace.yaml').write_text(
            'version: 1\nrepos:\n  api:\n    path: ./api\n  web:\n    path: ./web\n')
        before = self.snapshot()
        result = validate_init_outputs.validate(self.repo, 'workspace', 'cursor', 'disabled')
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.repo / '.cursor').exists())
        for name in ('api', 'web'):
            self.assertFalse((self.repo / name / '.cursor').exists())

    def test_loop_resumes_shared_state_for_single_and_workspace(self):
        for scope in ('single', 'workspace'):
            repo = self.repo / scope
            repo.mkdir()
            if scope == 'workspace':
                (repo / 'helixharness.workspace.yaml').write_text('repos: []\n')
            for state, expected in (('planning', 'propose'), ('ready', 'apply'), ('applying', 'apply')):
                with self.subTest(scope=scope, state=state):
                    feature = repo / 'docs/specs/active/demo'
                    feature.mkdir(parents=True, exist_ok=True)
                    (feature / 'spec.md').write_text(
                        f'---\nfeature: demo\nexecution_scope: {scope}\nstatus: {state}\n---\nSynthetic\n')
                    (feature / 'tasks.md').write_text('---\nfeature: demo\n---\nSynthetic tasks\n')
                    before = self.snapshot()
                    result = self.cli('workflow_router.py', '--repo-root', repo, 'route',
                                      '--command', 'propose', '--arguments', 'demo --loop')
                    self.assertEqual(result['workflow'], f'workflows/{scope}/{expected}.md')
                    self.assertEqual(before, self.snapshot())

    def test_status_and_config_are_read_only_and_expose_cursor_consent(self):
        self.init_git()
        before = self.snapshot()
        result = self.cli('user_config.py', '--repo-root', self.repo, '--platform', 'cursor')
        self.assertTrue(result['read_only'])
        self.assertEqual(result['telemetry']['effective_mode'], 'disabled')
        self.assertNotEqual(result['telemetry'].get('reason'), 'unsupported_host')
        self.assertTrue(result['telemetry']['allowed_actions'])
        self.cli('project_status.py', '--repo-root', self.repo)
        self.assertEqual(before, self.snapshot())

    def test_disabled_telemetry_records_no_usage_or_ledger(self):
        self.init_git()
        for action in ('start', 'finish'):
            extra = ('--trigger', 'user_command') if action == 'start' else ('--status', 'completed')
            result = self.cli('metrics.py', '--repo-root', self.repo, '--platform', 'cursor',
                              action, '--feature', 'demo', '--stage', 'apply', *extra)
            self.assertEqual(result['reason'], 'telemetry_disabled')
        self.assertFalse((self.repo / 'docs/eval').exists())
        self.assertFalse(list(self.repo.rglob('*.jsonl')))
        self.assertFalse((self.root / 'claude').exists())
        self.assertFalse((self.root / 'codex').exists())

    def test_unregistered_update_cannot_adopt_or_modify_an_installation(self):
        before = self.snapshot()
        result = self.cli('version_update.py', '--repo-root', self.repo,
                          '--platform', 'cursor', 'check')
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['warning'], 'WARN:helixharness_version_unknown')
        self.assertEqual(before, self.snapshot())

    def test_consent_enable_requires_confirmation_and_missing_identity_is_not_guessed(self):
        self.init_git()
        before = self.snapshot()
        refused = self.cli('user_config.py', '--repo-root', self.repo, '--platform', 'cursor',
                           '--telemetry', 'enabled', '--scope', 'project', code=2)
        self.assertIn('explicit user', refused['error'])
        self.assertEqual(before, self.snapshot())
        notice = self.cli('user_config.py', '--repo-root', self.repo, '--platform', 'cursor',
                          '--telemetry', 'enabled', '--scope', 'project', '--user-confirmed')
        self.assertTrue(notice['notice_required'])
        self.assertFalse(notice['change']['updated'])
        self.assertEqual(before, self.snapshot())
        consent = self.cli('user_config.py', '--repo-root', self.repo, '--platform', 'cursor',
                           '--telemetry', 'enabled', '--scope', 'project', '--user-confirmed',
                           '--telemetry-notice-shown', notice['notice']['sha256'])
        self.assertEqual(consent['telemetry']['effective_mode'], 'enabled')
        result = self.cli('metrics.py', '--repo-root', self.repo, '--platform', 'cursor',
                          'start', '--feature', 'demo', '--stage', 'apply', '--trigger', 'user_command')
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['reason'], 'cursor_session_context_unavailable')
        self.assertFalse((self.repo / 'docs/eval').exists())
        self.assertFalse(list(self.repo.rglob('*.jsonl')))

    def test_cross_host_archive_keeps_baseline_and_rejects_stale_evidence(self):
        # Reuse the existing synthetic artifact factory, never a developer checkout.
        with patch.dict(os.environ, {k: v for k, v in self.env.items() if k.startswith('GIT_CONFIG_')}):
            fixture = test_workflow.LiteWorkflowCliTest()
            fixture.setUp()
            self.addCleanup(fixture.tearDown)
            fixture.write_spec(status='implemented')
            fixture.write_tasks(status='completed', rows='| T-01 | Synthetic task | AC-01 | — | completed |')
            fixture.write_evidence(body='Synthetic checks and two independent approvals.\n')
            baseline = fixture.repo / '.helixharness/workflow/checkout/apply-state.json'
            before = baseline.read_bytes()
            for host in ('claude-code', 'codex', 'cursor'):
                self.cli('host_runtime_cli.py', 'resolve', '--platform', host)
                code, result = route(fixture.repo, command='propose', arguments='checkout --loop')
                self.assertEqual(code, 0, result)
                self.assertEqual(result['stage'], 'archive')
                self.assertEqual(baseline.read_bytes(), before)
            spec = fixture.feature_dir / 'spec.md'
            spec.write_text(spec.read_text() + '\nUnreviewed scope change.\n')
            blocked = fixture.run_cli('archive-preflight', '--feature', fixture.feature, check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertEqual(baseline.read_bytes(), before)
