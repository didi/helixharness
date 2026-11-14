"""Exercise update consent and retry journals with synthetic host commands only."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from unittest.mock import Mock, patch

import version_update as update
from update_hosts.base import BoundedCommandRunner, CommandResult, InstallStep, UpdateHostError


class UpdateAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache.json'
        self.auth = self.root / 'authorization.json'
        self.now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        self.data = {
            'schema_version': '1.0', 'platform': 'codex', 'plugin_id': update.PLUGIN_ID,
            'marketplace': update.MARKETPLACE, 'checked_at': update._timestamp(self.now),
            'current_version': '1.0.0', 'current_scope': 'default',
            'current_source': update.MARKETPLACE, 'latest_version': '1.2.0',
            'minimum_supported_version': None, 'force_update_below': None,
            'update_status': 'recommended', 'required_reason': None,
            'releases': [{'version': v, 'date': '2026-11-14', 'update_type': 'recommended',
                          'title': 'Synthetic release', 'changes': ['Synthetic change'],
                          'restart_required': True} for v in ('1.1.0', '1.2.0')],
        }
        self.save_cache()
        self.steps = (InstallStep('refresh', ('synthetic-host', 'refresh')),
                      InstallStep('install', ('synthetic-host', 'install')))
        self.adapter = update._adapter('codex')
        self.adapter.install_steps = Mock(return_value=self.steps)
        self.runner = Mock(spec=BoundedCommandRunner)
        self.runner.run.return_value = CommandResult(0, '', '')
        self.installation = {'current_version': '1.0.0', 'current_scope': 'default',
                             'current_source': update.MARKETPLACE}
        for patcher in (patch('version_update._adapter', return_value=self.adapter),
                        patch('version_update.inspect_formal_installation', return_value=self.installation)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save_cache(self):
        self.cache.write_text(json.dumps(self.data), encoding='utf-8')

    def read_auth(self):
        return json.loads(self.auth.read_text(encoding='utf-8'))

    def prepare(self):
        return update.prepare_authorization('codex', 'default', self.cache, self.auth, self.now)

    def decide(self, challenge, action='upgrade_now', confirmed=True):
        return update.apply_update_choice(self.cache, self.auth, challenge['challenge_id'],
                                          action, confirmed, self.now)

    def authorized(self):
        challenge = self.prepare()
        self.decide(challenge)
        return challenge

    def install(self, challenge):
        return update.install('codex', self.cache, self.auth, challenge['challenge_id'],
                              self.now, repo_root=self.root, runner=self.runner)

    def cli(self, *arguments):
        argv = ['version_update.py', '--platform', 'codex', '--repo-root', str(self.root),
                '--cache-path', str(self.cache), '--authorization-path', str(self.auth), *arguments]
        with patch.object(sys, 'argv', argv), patch('version_update._now', return_value=self.now), \
             patch.dict('os.environ', {'HELIXHARNESS_BOUND_PLUGIN_ROOT': str(update.PLUGIN_ROOT)}), \
             patch('version_update.BoundedCommandRunner', return_value=self.runner), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = update.main()
        return code, json.loads(output.getvalue())

    def test_cli_prepare_confirm_and_install_consumes_the_same_challenge(self):
        code, challenge = self.cli('prepare', '--scope', 'default')
        self.assertEqual(0, code)
        code, authorized = self.cli('decide', '--challenge-id', challenge['challenge_id'],
                                    '--action', 'upgrade_now', '--user-confirmed')
        self.assertEqual((0, 'authorized'), (code, authorized['state']))
        code, result = self.cli('install', '--challenge-id', challenge['challenge_id'])
        self.assertEqual((0, True), (code, result['updated']))
        self.assertFalse(self.auth.exists())
        self.assertEqual(2, self.runner.run.call_count)

    def test_cli_reports_missing_confirmation_and_supports_cancel_and_resolve(self):
        _, challenge = self.cli('prepare', '--scope', 'default')
        code, error = self.cli('decide', '--challenge-id', challenge['challenge_id'], '--action', 'upgrade_now')
        self.assertEqual((2, 'error'), (code, error['status']))
        self.assertEqual('pending', self.read_auth()['state'])
        code, result = self.cli('cancel', '--challenge-id', challenge['challenge_id'])
        self.assertEqual((0, True), (code, result['cancelled']))
        code, result = self.cli('resolve')
        self.assertEqual((0, 'ready'), (code, result['state']))

    def test_cli_refresh_skip_and_check_use_only_synthetic_policy_and_runner(self):
        policy = {key: self.data[key] for key in ('schema_version', 'marketplace', 'latest_version', 'releases')}
        policy.update(plugin='helix', minimum_supported_version='1.0.0', force_update_below='1.0.0', force_update_message='Synthetic minimum')
        policy['releases'] = list(reversed(policy['releases']))
        path = self.root/'policy.json'
        path.write_text(json.dumps(policy), encoding='utf-8')
        code, result = self.cli('refresh', '--policy-file', str(path), '--current-version', '1.0.0', '--scope', 'default')
        self.assertEqual(0, code, result)
        self.assertEqual((0, 'recommended'), (code, result['update_status']))
        code, result = self.cli('skip', '--target-version', '1.2.0', '--source', 'interactive-user', '--user-confirmed')
        self.assertEqual((0, True), (code, result['skip_effective']))
        with patch('version_update.check_for_update', return_value={'state': 'synthetic-check'}) as check:
            code, result = self.cli('check', '--force-refresh')
        self.assertEqual((0, 'synthetic-check'), (code, result['state']))
        self.assertTrue(check.call_args.kwargs['force_refresh'])
        self.runner.run.assert_not_called()

    def test_prepare_requires_an_installable_update_and_valid_scope(self):
        for status in ('latest', 'current-newer'):
            with self.subTest(status=status):
                self.data.update(update_status=status, latest_version='1.0.0' if status == 'latest' else '0.9.0', releases=[])
                self.save_cache()
                with self.assertRaisesRegex(update.UpdateError, 'no installable'):
                    self.prepare()
        with self.assertRaisesRegex(update.UpdateError, 'scope'):
            update.prepare_authorization('codex', 'invalid', self.cache, self.auth, self.now)
        self.runner.run.assert_not_called()

    def test_pending_challenge_has_ttl_and_cannot_be_silently_replaced(self):
        challenge = self.prepare()
        self.assertEqual('pending', challenge['state'])
        self.assertEqual(['upgrade_now', 'skip_update'], [a['id'] for a in challenge['prompt']['allowed_actions']])
        expiry = update._parse_timestamp(challenge['expires_at'])
        self.assertEqual(timedelta(minutes=update.AUTH_TTL_MINUTES), expiry - self.now)
        with self.assertRaisesRegex(update.UpdateError, 'active update authorization'):
            self.prepare()
        self.assertEqual(challenge, self.read_auth())

    def test_expired_challenge_is_replaced_and_old_id_is_rejected(self):
        first = self.prepare()
        later = self.now + timedelta(minutes=update.AUTH_TTL_MINUTES + 1)
        second = update.prepare_authorization('codex', 'default', self.cache, self.auth, later)
        self.assertNotEqual(first['challenge_id'], second['challenge_id'])
        with self.assertRaisesRegex(update.UpdateError, 'challenge'):
            update.cancel_authorization(self.auth, first['challenge_id'], later)

    def test_no_confirmation_or_invalid_action_cannot_authorize_or_install(self):
        challenge = self.prepare()
        for action, confirmed in [('upgrade_now', False), ('invented', True)]:
            with self.subTest(action=action, confirmed=confirmed):
                with self.assertRaises(update.UpdateError):
                    self.decide(challenge, action, confirmed)
                self.assertEqual('pending', self.read_auth()['state'])
        with self.assertRaisesRegex(update.UpdateError, 'not been confirmed'):
            self.install(challenge)
        self.runner.run.assert_not_called()

    def test_recommended_skip_records_consent_and_removes_challenge(self):
        challenge = self.prepare()
        result = self.decide(challenge, 'skip_update')
        self.assertFalse(result['blocked'])
        self.assertTrue(result['skip_effective'])
        self.assertFalse(self.auth.exists())
        self.assertEqual('1.2.0', update._load_cache(self.cache)['skip']['version'])
        self.runner.run.assert_not_called()

    def test_required_skip_blocks_without_creating_skip_cache(self):
        self.data.update(update_status='required', required_reason='Synthetic minimum version')
        self.save_cache()
        challenge = self.prepare()
        self.assertEqual('required', challenge['prompt']['kind'])
        result = self.decide(challenge, 'skip_update')
        self.assertTrue(result['blocked'])
        self.assertEqual('BLOCKED:helixharness_update_required', result['block_reason'])
        self.assertNotIn('skip', update._load_cache(self.cache))
        self.assertFalse(self.auth.exists())

    def test_stale_skip_does_not_destroy_the_pending_challenge(self):
        challenge = self.prepare()
        self.data['latest_version'] = '1.3.0'
        self.save_cache()
        with self.assertRaisesRegex(update.UpdateError, 'current recommended'):
            self.decide(challenge, 'skip_update')
        self.assertEqual(challenge, self.read_auth())

    def test_expiry_boundary_and_tampered_confirmation_fail_closed(self):
        challenge = self.prepare()
        with self.assertRaisesRegex(update.UpdateError, 'expired'):
            update.cancel_authorization(self.auth, challenge['challenge_id'],
                                        self.now + timedelta(minutes=update.AUTH_TTL_MINUTES))
        for changes in ({'challenge_id': 'bad'}, {'completed_steps': ['refresh']},
                        {'schema_version': 'invalid'}, {'state': 'invented'},
                        {'unexpected': True}, {'expires_at': 'invalid'}):
            with self.subTest(changes=changes):
                self.auth.write_text(json.dumps({**challenge, **changes}), encoding='utf-8')
                with self.assertRaises(update.UpdateError):
                    self.decide(challenge)
                self.runner.run.assert_not_called()

    def test_future_or_extended_challenge_lifetime_is_rejected(self):
        challenge = self.prepare()
        for changes in ({'created_at': update._timestamp(self.now + timedelta(minutes=1)),
                         'expires_at': update._timestamp(self.now + timedelta(minutes=11))},
                        {'expires_at': update._timestamp(self.now + timedelta(days=30))}):
            with self.subTest(changes=changes):
                self.auth.write_text(json.dumps({**challenge, **changes}), encoding='utf-8')
                with self.assertRaisesRegex(update.UpdateError, 'lifetime'):
                    self.decide(challenge)

    def test_invalid_prompt_cannot_change_approved_actions(self):
        challenge = self.prepare()
        mutations = [dict(challenge['prompt'], kind='required'),
                     dict(challenge['prompt'], title=''),
                     dict(challenge['prompt'], allowed_actions=[])]
        for key, value in [('id', 'arbitrary-command'), ('recommended', 'yes'),
                           ('label', ''), ('description', ''), ('extra', 'bad')]:
            prompt = copy.deepcopy(challenge['prompt'])
            prompt['allowed_actions'][0][key] = value
            mutations.append(prompt)
        reversed_prompt = copy.deepcopy(challenge['prompt'])
        reversed_prompt['allowed_actions'].reverse()
        mutations.append(reversed_prompt)
        for prompt in mutations:
            with self.subTest(prompt=prompt):
                self.auth.write_text(json.dumps({**challenge, 'prompt': prompt}), encoding='utf-8')
                with self.assertRaises(update.UpdateError):
                    self.decide(challenge)
        self.runner.run.assert_not_called()

    def test_success_consumes_authorization_and_cannot_be_replayed(self):
        challenge = self.authorized()
        result = self.install(challenge)
        self.assertTrue(result['updated'])
        self.assertTrue(result['authorization_consumed'])
        self.assertTrue(result['restart_required'])
        self.assertEqual([list(s.command) for s in self.steps], result['commands'])
        self.assertEqual(['refresh', 'install'], result['completed_steps'])
        self.assertFalse(result['resumed'])
        self.assertFalse(self.auth.exists())
        with self.assertRaises(update.UpdateError):
            self.install(challenge)
        self.assertEqual(2, self.runner.run.call_count)

    def test_failed_step_is_journaled_and_retry_skips_completed_steps(self):
        challenge = self.authorized()
        self.runner.run.side_effect = [CommandResult(0, '', ''), UpdateHostError('synthetic failure')]
        with self.assertRaisesRegex(update.UpdateError, 'step failed: install'):
            self.install(challenge)
        journal = self.read_auth()
        self.assertEqual('installing', journal['state'])
        self.assertEqual(['refresh'], journal['completed_steps'])
        self.assertIn('synthetic failure', journal['last_error'])
        with self.assertRaisesRegex(update.UpdateError, 'cannot be cancelled'):
            update.cancel_authorization(self.auth, challenge['challenge_id'], self.now)
        self.runner.run.reset_mock(side_effect=True)
        result = self.install(challenge)
        self.assertTrue(result['resumed'])
        self.assertEqual([list(self.steps[1].command)], result['commands'])
        self.runner.run.assert_called_once_with(self.steps[1].command, timeout=update.INSTALL_TIMEOUT_SECONDS)
        self.assertFalse(self.auth.exists())

    def test_cache_and_live_installation_drift_prevent_host_execution(self):
        challenge = self.authorized()
        self.data['latest_version'] = '1.3.0'
        self.save_cache()
        with self.assertRaisesRegex(update.UpdateError, 'no longer matches cache'):
            self.install(challenge)
        self.data['latest_version'] = '1.2.0'
        self.save_cache()
        for key, value in [('current_version', '0.9.0'), ('current_scope', 'other'),
                           ('current_source', 'unrelated-marketplace')]:
            with self.subTest(key=key):
                original = self.installation[key]
                self.installation[key] = value
                with self.assertRaisesRegex(update.UpdateError, 'live formal installation'):
                    self.install(challenge)
                self.installation[key] = original
        self.runner.run.assert_not_called()
        self.assertEqual('authorized', self.read_auth()['state'])

    def test_resume_rejects_unknown_and_out_of_order_completed_steps(self):
        challenge = self.authorized()
        journal = self.read_auth()
        journal.update(state='installing', install_started_at=update._timestamp(self.now))
        for completed in (['unknown'], ['install']):
            with self.subTest(completed=completed):
                self.auth.write_text(json.dumps({**journal, 'completed_steps': completed}), encoding='utf-8')
                with self.assertRaises(update.UpdateError):
                    self.install(challenge)
                self.runner.run.assert_not_called()

    def test_install_rejects_empty_or_duplicate_step_plans(self):
        challenge = self.authorized()
        journal = self.read_auth()
        for steps in ((), (self.steps[0], self.steps[0])):
            with self.subTest(steps=steps):
                self.auth.write_text(json.dumps(journal), encoding='utf-8')
                self.adapter.install_steps.return_value = steps
                with self.assertRaisesRegex(update.UpdateError, 'step'):
                    self.install(challenge)
                self.runner.run.assert_not_called()
                self.assertEqual(journal, self.read_auth())

    def test_cancel_pending_or_authorized_challenge_and_reject_second_decision(self):
        challenge = self.authorized()
        with self.assertRaisesRegex(update.UpdateError, 'not pending'):
            self.decide(challenge)
        self.assertTrue(update.cancel_authorization(self.auth, challenge['challenge_id'], self.now)['cancelled'])
        self.assertFalse(self.auth.exists())
        pending = self.prepare()
        update.cancel_authorization(self.auth, pending['challenge_id'], self.now)
        self.assertFalse(self.auth.exists())

    def test_invalid_install_confirmation_and_journal_metadata_cannot_execute(self):
        challenge = self.authorized()
        journal = self.read_auth()
        for changes in ({'confirmation_source': 'automatic-agent'},
                        {'authorized_at': update._timestamp(self.now - timedelta(seconds=1))},
                        {'authorized_at': 'invalid'}, {'completed_steps': ['refresh', 'refresh']},
                        {'state': 'installing', 'install_started_at': 'invalid'},
                        {'state': 'installing', 'install_started_at': update._timestamp(self.now), 'last_error': ''}):
            with self.subTest(changes=changes):
                self.auth.write_text(json.dumps({**journal, **changes}), encoding='utf-8')
                with self.assertRaises(update.UpdateError):
                    self.install(challenge)
                self.runner.run.assert_not_called()

    def test_failed_skip_write_keeps_authorization_retryable(self):
        challenge = self.prepare()
        with patch('version_update._atomic_write', side_effect=OSError('synthetic disk full')):
            with self.assertRaises(OSError):
                self.decide(challenge, 'skip_update')
        self.assertEqual(challenge, self.read_auth())
        self.assertNotIn('skip', update._load_cache(self.cache))

    def test_concurrent_installers_execute_each_native_step_only_once(self):
        challenge = self.authorized()
        started = threading.Event()
        release = threading.Event()

        def run(command, **kwargs):
            if command == self.steps[0].command:
                started.set()
                self.assertTrue(release.wait(timeout=5))
            return CommandResult(0, '', '')

        self.runner.run.side_effect = run
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.install, challenge)
            self.assertTrue(started.wait(timeout=5))
            second = pool.submit(self.install, challenge)
            release.set()
            self.assertTrue(first.result(timeout=10)['updated'])
            with self.assertRaises(update.UpdateError):
                second.result(timeout=10)
        self.assertEqual([s.command for s in self.steps], [c.args[0] for c in self.runner.run.call_args_list])


if __name__ == '__main__':
    unittest.main()
