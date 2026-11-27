"""High-frequency Cursor hooks must not load unused lifecycle/delivery work."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reporting_config
from host_runtime import HostRegistry
import env_sandbox
from telemetry_hosts.cursor import record_hook

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'hosts/cursor/hook_entry.py'


class CursorHookFastPathTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='cursor-fastpath-')
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.payload = {'conversation_id': 'synthetic-fastpath', 'workspace_roots': [str(self.home)],
                        'hook_event_name': 'postToolUse', 'tool_use_id': 'synthetic-call', 'tool_name': 'Shell'}
        self.env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1',
                    **env_sandbox.boot_vars(),
                    'HELIXHARNESS_TELEMETRY_HOME': str(self.home / 'telemetry'),
                    'HELIXHARNESS_CURSOR_HOME': str(self.home / 'cursor'),
                    'HELIXHARNESS_FEEDBACK_HOME': str(self.home / 'feedback'),
                    'HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER': '1',
                    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}

    def test_cold_non_lifecycle_events_work_without_lifecycle_or_delivery_imports(self):
        script = (
            'import importlib.abc,runpy,sys\n'
            'class RejectUnused(importlib.abc.MetaPathFinder):\n'
            ' def find_spec(self, fullname, path=None, target=None):\n'
            '  if fullname in {"host_runtime","lifecycle_service","telemetry_async","telemetry_consent_state"}:\n'
            '   raise ImportError("unused synchronous dependency: " + fullname)\n'
            'sys.meta_path.insert(0,RejectUnused())\n'
            f'runpy.run_path({str(ENTRY)!r},run_name="__main__")\n'
        )
        for event, expected in (('postToolUse', ''), ('postToolUseFailure', ''), ('afterAgentResponse', ''),
                                ('stop', ''), ('subagentStop', ''), ('beforeSubmitPrompt', {'continue': True}),
                                ('subagentStart', {'permission': 'allow'}), ('unsupportedNativeEvent', '')):
            with self.subTest(event=event):
                result = subprocess.run([sys.executable, '-c', script], input=json.dumps({**self.payload, 'hook_event_name': event}),
                                        text=True, capture_output=True, env=self.env, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
                actual = json.loads(result.stdout) if expected else result.stdout
                if event == 'beforeSubmitPrompt':
                    context = actual.pop('additional_context')
                    self.assertIn('HELIXHARNESS_CURSOR_CONVERSATION_ID=synthetic-fastpath', context)
                    # The roots travel as shlex-quoted JSON, so a Windows path is doubled.
                    self.assertIn(json.dumps(str(self.home))[1:-1], context)
                    self.assertLessEqual(len(context.encode()), 8192)
                self.assertEqual(expected, actual)
        self.assertEqual([], list(self.home.iterdir()))

    def test_unconfigured_record_does_not_import_delivery_or_consent(self):
        with patch.dict(os.environ, self.env, clear=True), patch.dict(
            sys.modules, {'telemetry_async': None, 'telemetry_consent_state': None}
        ):
            self.assertEqual(0, record_hook(self.payload))
        self.assertEqual([], list(self.home.iterdir()))

    def test_endpoint_resolution_matches_canonical_default_and_override_rules(self):
        endpoint = 'https://telemetry.example.invalid/events'
        cases = [
            ('missing', None, '', False),
            ('default', None, endpoint, True),
            ('valid-override', json.dumps({'endpoint': endpoint}), '', True),
            ('blank-falls-back', json.dumps({'endpoint': '  '}), endpoint, True),
            ('invalid-overrides-default', json.dumps({'endpoint': 'invalid'}), endpoint, False),
            ('credentials-rejected', json.dumps({'endpoint': 'https://name:password@example.invalid'}), '', False),
            ('malformed', '{', endpoint, False),
            ('non-object', '[]', endpoint, False),
            ('wrong-type', json.dumps({'endpoint': 9}), endpoint, False),
        ]
        telemetry = self.home / 'telemetry'
        telemetry.mkdir()
        config = telemetry / 'config.json'
        for name, content, default, ready in cases:
            with self.subTest(case=name):
                if content is None:
                    config.unlink(missing_ok=True)
                else:
                    config.write_text(content)
                heavy = SimpleNamespace(endpoint_skip=Mock(return_value={'status': 'skipped'}))
                with patch.dict(os.environ, self.env, clear=True), patch.object(reporting_config, 'DEFAULT_REPORT_ENDPOINT', default), patch.dict(
                    sys.modules, {'telemetry_async': heavy, 'telemetry_consent_state': SimpleNamespace()}
                ):
                    self.assertEqual(ready, reporting_config.endpoint_status(telemetry, default)['ready'])
                    self.assertEqual(0, record_hook(self.payload))
                self.assertEqual(int(ready), heavy.endpoint_skip.call_count)

    def test_invalid_identity_or_event_never_loads_delivery(self):
        payloads = [{}, {**self.payload, 'conversation_id': '../bad'},
                    {**self.payload, 'hook_event_name': 'unknown'}, {**self.payload, 'workspace_roots': []}]
        with patch.dict(os.environ, self.env, clear=True), patch.dict(
            sys.modules, {'telemetry_async': None, 'telemetry_consent_state': None}
        ), patch.object(reporting_config, 'endpoint_status', side_effect=AssertionError('invalid input must stop first')):
            for payload in payloads:
                with self.subTest(payload=payload):
                    self.assertEqual(0, record_hook(payload))

    def test_default_and_tilde_home_resolution_preserve_endpoint_selection(self):
        endpoint = 'https://telemetry.example.invalid/events'
        for override, relative in ((None, '.helixharness/telemetry'), ('~/configured', 'configured')):
            with self.subTest(override=override):
                home = self.home / relative
                home.mkdir(parents=True)
                (home / 'config.json').write_text(json.dumps({'endpoint': endpoint}))
                env = {**self.env, **env_sandbox.home_vars(self.home)}
                if override is None:
                    env.pop('HELIXHARNESS_TELEMETRY_HOME')
                else:
                    env['HELIXHARNESS_TELEMETRY_HOME'] = override
                heavy = SimpleNamespace(endpoint_skip=Mock(return_value={'status': 'skipped'}))
                with patch.dict(os.environ, env, clear=True), patch.dict(
                    sys.modules, {'telemetry_async': heavy, 'telemetry_consent_state': SimpleNamespace()}
                ):
                    self.assertEqual(0, record_hook(self.payload))
                heavy.endpoint_skip.assert_called_once_with()

    def test_session_start_still_resolves_bound_host_and_emits_context(self):
        spec = importlib.util.spec_from_file_location('synthetic_fastpath_entry', ENTRY)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        with patch.object(sys, 'stdin', io.StringIO('{"hook_event_name":"sessionStart"}')), patch.object(
            HostRegistry, 'discover', wraps=HostRegistry.discover
        ) as discover, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, entry.main())
        self.assertEqual(1, discover.call_count)
        result = json.loads(output.getvalue())
        self.assertEqual(str(ROOT), result['env']['HELIXHARNESS_BOUND_PLUGIN_ROOT'])
        self.assertIn('require &&' if os.name != 'nt' else 'runtime_preflight', result['additional_context'])


if __name__ == '__main__':
    unittest.main()
