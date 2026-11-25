"""Config's enable checkpoint cannot write consent before notice acknowledgment.

These CLI tests verify the program guard, not that a model actually displayed
the notice in a native UI. All repositories and host directories are synthetic.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import env_sandbox
import telemetry_consent
import user_config


class UserConfigNoticeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='helix-config-notice-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / 'synthetic repo'
        self.repo.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {
            **env_sandbox.home_vars(self.home), 'CODEX_HOME': str(self.home / 'codex'),
            'CLAUDE_CONFIG_DIR': str(self.home / 'claude'),
            'HELIXHARNESS_CURSOR_HOME': str(self.home / 'cursor'),
            'HELIXHARNESS_TELEMETRY_HOME': str(self.home / 'telemetry'),
            'HELIXHARNESS_FEEDBACK_HOME': str(self.home / 'feedback'),
            'HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER': '1',
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
            'GIT_CONFIG_VALUE_0': os.devnull,
        })
        patch.start()
        self.addCleanup(patch.stop)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)

    def files(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def run_config(self, *args, platform='cursor'):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = user_config.main(['--repo-root', str(self.repo), '--platform', platform, *args])
        return code, json.loads(output.getvalue())

    def enable(self, *, scope='project', platform='cursor', confirmed=True, digest=None):
        args = ['--telemetry', 'enabled', '--scope', scope]
        if confirmed:
            args.append('--user-confirmed')
        if digest is not None:
            args.extend(['--telemetry-notice-shown', digest])
        return self.run_config(*args, platform=platform)

    def test_first_enable_is_read_only_and_returns_exact_canonical_notice_on_all_hosts(self):
        # Compare against the documented fixed text, independent of the CLI parser.
        protocol = user_config.EVAL_PROTOCOL.read_text()
        expected = protocol.split('<!-- helixharness-eval-data-notice-v1:start -->')[1]
        expected = expected.split('```text\n', 1)[1].split('\n```', 1)[0]
        before = self.files()
        for host in ('cursor', 'codex', 'claude-code'):
            for scope in ('project', 'user'):
                with self.subTest(host=host, scope=scope), mock.patch.object(
                    telemetry_consent, 'set_mode', side_effect=AssertionError('notice preparation must not write'),
                ), mock.patch.object(
                    telemetry_consent, '_purge_current_repo_private_state',
                    side_effect=AssertionError('notice preparation must not purge'),
                ):
                    code, result = self.enable(scope=scope, platform=host)
                    self.assertEqual(code, 0, result)
                    self.assertTrue(result['read_only'])
                    self.assertTrue(result['notice_required'])
                    self.assertEqual(result['next_action'], 'display_notice_before_enable')
                    self.assertEqual(result['change'], {'updated': False, 'reason': 'telemetry_notice_required'})
                    self.assertEqual(result['notice']['text'], expected)
                    self.assertEqual(result['notice']['sha256'], hashlib.sha256(expected.encode()).hexdigest())
                    self.assertEqual(result['telemetry']['effective_mode'], 'disabled')
                    self.assertTrue(result['telemetry']['prompt_due'])
                    self.assertFalse(result['request_complete'])
                    self.assertFalse(result['continuation']['new_user_confirmation_required'])
                    self.assertEqual(before, self.files())

    def test_current_notice_acknowledgment_allows_authorized_change_for_each_host(self):
        for host in ('cursor', 'codex', 'claude-code'):
            with self.subTest(host=host):
                code, prepared = self.enable(platform=host)
                self.assertEqual(code, 0)
                code, applied = self.enable(platform=host, digest=prepared['notice']['sha256'])
                self.assertEqual(code, 0, applied)
                self.assertFalse(applied['read_only'])
                self.assertTrue(applied['change']['updated'])
                self.assertEqual(applied['telemetry']['effective_mode'], 'enabled')
                self.assertEqual(applied['telemetry']['effective_scope'], 'project')
        self.assertFalse((self.repo / 'docs/eval').exists())
        self.assertFalse((self.home / 'telemetry').exists())
        self.assertFalse((self.home / 'feedback').exists())

    def test_prepared_enable_exposes_a_same_request_continuation_on_every_host_and_scope(self):
        for host in ('cursor', 'codex', 'claude-code'):
            for scope in ('user', 'project'):
                with self.subTest(host=host, scope=scope):
                    original = ['--telemetry', 'enabled', '--scope', scope, '--user-confirmed']
                    before = self.files()
                    code, prepared = self.run_config(*original, platform=host)
                    self.assertEqual(code, 0, prepared)
                    self.assertFalse(prepared['request_complete'])
                    self.assertEqual(before, self.files())
                    continuation = prepared['continuation']
                    self.assertEqual(continuation['after'], 'display_notice_to_user')
                    self.assertEqual(continuation['action'], 'repeat_same_request')
                    self.assertTrue(continuation['same_turn'])
                    self.assertFalse(continuation['new_user_confirmation_required'])
                    self.assertEqual(continuation['append_arguments'],
                                     ['--telemetry-notice-shown', prepared['notice']['sha256']])
                    # Simulate a caller that has displayed the notice. The original
                    # explicit authorization and scope are reused, not expanded.
                    code, applied = self.run_config(*original, *continuation['append_arguments'], platform=host)
                    self.assertEqual(code, 0, applied)
                    self.assertTrue(applied['request_complete'])
                    self.assertNotIn('continuation', applied)
                    self.assertTrue(applied['change']['updated'])
                    _, readback = self.run_config(platform=host)
                    self.assertEqual(readback['telemetry']['effective_mode'], 'enabled')
                    self.assertEqual(readback['telemetry']['effective_scope'], scope)
                    # A new enable request still prepares its notice, even when
                    # the setting is already enabled; it must not change state.
                    before = self.files()
                    _, again = self.run_config(*original, platform=host)
                    self.assertFalse(again['request_complete'])
                    self.assertEqual(again['telemetry']['effective_mode'], 'enabled')
                    self.assertEqual(before, self.files())

    def test_queries_disable_and_endpoint_changes_do_not_request_continuation(self):
        for args in [(), ('--telemetry', 'disabled', '--scope', 'project', '--user-confirmed'),
                     ('--report-endpoint', 'http://127.0.0.1:12345/report', '--user-confirmed'),
                     ('--reset-report-endpoint', '--user-confirmed')]:
            with self.subTest(args=args), mock.patch.object(
                user_config, 'telemetry_enable_notice', side_effect=AssertionError('must not load notice'),
            ):
                code, result = self.run_config(*args)
                self.assertEqual(code, 0, result)
                self.assertTrue(result['request_complete'])
                self.assertNotIn('continuation', result)

    def test_notice_digest_does_not_replace_explicit_authorization(self):
        digest = user_config.telemetry_enable_notice()['sha256']
        before = self.files()
        for supplied in (None, digest):
            code, result = self.enable(confirmed=False, digest=supplied)
            self.assertEqual(code, 2, result)
            self.assertIn('explicit user', result['error'])
        self.assertEqual(before, self.files())

    def test_all_hosts_keep_scope_precedence_read_only_and_endpoint_reset_contracts(self):
        for host in ('cursor', 'codex', 'claude-code'):
            with self.subTest(host=host):
                for scope in ('user', 'project'):
                    _, prepared = self.enable(platform=host, scope=scope)
                    code, changed = self.enable(platform=host, scope=scope, digest=prepared['notice']['sha256'])
                    self.assertEqual(code, 0, changed)
                    self.assertTrue(changed['change']['updated'])
                    self.assertEqual(changed['telemetry']['effective_mode'], 'enabled')
                    self.assertEqual(changed['telemetry']['effective_scope'], scope)
                code, disabled = self.run_config('--telemetry', 'disabled', '--scope', 'user',
                                                 '--user-confirmed', platform=host)
                self.assertEqual(code, 0, disabled)
                self.assertEqual(disabled['telemetry']['effective_scope'], 'user')
                self.assertEqual(disabled['telemetry']['effective_mode'], 'disabled')
                _, prepared = self.enable(platform=host)
                code, overridden = self.enable(platform=host, digest=prepared['notice']['sha256'])
                self.assertEqual(code, 0, overridden)
                self.assertTrue(overridden['request_complete'])
                self.assertNotIn('continuation', overridden)
                self.assertFalse(overridden['change']['updated'])
                self.assertEqual(overridden['change']['warning'], 'WARN:telemetry_user_scope_disabled')
                code, disabled = self.run_config('--telemetry', 'disabled', '--scope', 'project',
                                                 '--user-confirmed', platform=host)
                self.assertEqual(code, 0, disabled)
                self.assertTrue(disabled['change']['updated'])
                before = self.files()
                code, viewed = self.run_config(platform=host)
                self.assertEqual(code, 0, viewed)
                self.assertTrue(viewed['read_only'])
                self.assertEqual(before, self.files())
                for channel in ('report', 'feedback'):
                    code, changed = self.run_config(f'--{channel}-endpoint',
                                                    f'http://127.0.0.1:12345/{channel}',
                                                    '--user-confirmed', platform=host)
                    self.assertEqual(code, 0, changed)
                    self.assertTrue(changed['change']['updated'])
                    self.assertEqual(changed[channel]['source'], 'user')
                    code, reset = self.run_config(f'--reset-{channel}-endpoint', '--user-confirmed', platform=host)
                    self.assertEqual(code, 0, reset)
                    self.assertTrue(reset['change']['updated'])
                    self.assertNotEqual(reset[channel]['source'], 'user')

    def test_incorrect_and_changed_notice_digests_do_not_enable(self):
        _, prepared = self.enable()
        old_digest = prepared['notice']['sha256']
        revised = self.root / 'revised-protocol.md'
        revised.write_text(user_config.EVAL_PROTOCOL.read_text().replace('数据统计说明：默认关闭。', '数据统计说明：默认关闭。新增合成测试说明。'))
        before = self.files()
        for digest in ('', 'bad-value', '0' * 64):
            code, result = self.enable(digest=digest)
            self.assertEqual(code, 2, result)
            self.assertEqual(before, self.files())
        with mock.patch.object(user_config, 'EVAL_PROTOCOL', revised):
            code, result = self.enable(digest=old_digest)
            self.assertEqual(code, 2, result)
            self.assertEqual(before, self.files())
            _, current = self.enable()
            self.assertNotEqual(old_digest, current['notice']['sha256'])
            self.assertEqual(before, self.files())

    def test_missing_or_malformed_canonical_notice_never_enables(self):
        broken = self.root / 'broken.md'
        marker = 'helixharness-eval-data-notice-v1'
        values = ['', f'<!-- {marker}:start -->\nplain text\n<!-- {marker}:end -->',
                  f'<!-- {marker}:start -->\n```text\n\n```\n<!-- {marker}:end -->',
                  f'<!-- {marker}:end -->\n<!-- {marker}:start -->',
                  user_config.EVAL_PROTOCOL.read_text() * 2]
        for text in values:
            with self.subTest(format=text[:30]):
                broken.write_text(text)
                before = self.files()
                with mock.patch.object(user_config, 'EVAL_PROTOCOL', broken):
                    code, result = self.enable()
                self.assertEqual(code, 2, result)
                self.assertEqual(before, self.files())
        with mock.patch.object(user_config, 'EVAL_PROTOCOL', self.root / 'absent.md'):
            code, _ = self.enable()
        self.assertEqual(code, 2)

    def test_disable_endpoint_and_read_only_do_not_depend_on_notice_loading(self):
        with mock.patch.object(user_config, 'telemetry_enable_notice', side_effect=AssertionError('must not load notice')):
            code, viewed = self.run_config()
            self.assertEqual(code, 0, viewed)
            code, disabled = self.run_config('--telemetry', 'disabled', '--scope', 'project', '--user-confirmed')
            self.assertEqual(code, 0, disabled)
            self.assertTrue(disabled['change']['updated'])
            code, endpoint = self.run_config('--report-endpoint', 'http://127.0.0.1:12345/report', '--user-confirmed')
            self.assertEqual(code, 0, endpoint)
            self.assertTrue(endpoint['change']['updated'])

    def test_notice_marker_cannot_be_used_on_unrelated_actions(self):
        digest = user_config.telemetry_enable_notice()['sha256']
        before = self.files()
        for args in [(), ('--telemetry', 'disabled', '--scope', 'project', '--user-confirmed'),
                     ('--report-endpoint', 'http://127.0.0.1:12345/report', '--user-confirmed')]:
            code, result = self.run_config(*args, '--telemetry-notice-shown', digest)
            self.assertEqual(code, 2, result)
            self.assertEqual(before, self.files())

    def test_unsupported_host_does_not_prepare_notice_or_write(self):
        before = self.files()
        code, result = self.enable(platform='synthetic-unknown-host')
        self.assertEqual(code, 2, result)
        self.assertNotIn('notice', result)
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
