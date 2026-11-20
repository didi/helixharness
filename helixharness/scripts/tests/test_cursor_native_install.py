import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import cursor_native_install as native
from update_hosts.base import UpdateHostError


class NativeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root/'project'; self.project.mkdir()
        self.tool = native.ProjectInstall(self.project, self.root/'home')
        self.binding = {'enabled':True,'gitUrl':'https://example.invalid/helix.git','gitRef':'a'*40,'gitPath':'./helixharness'}
        self.snapshot = {'binding':self.binding,'files':{}}

    def put(self, data):
        native.atomic(self.tool.settings, data)

    def test_merge_idempotence_restore_preserves_other_changes(self):
        self.put({'other':12,'plugins':{'another':True}})
        self.tool.configure(self.snapshot, True)
        self.tool.configure(self.snapshot, True)
        d=native.read(self.tool.settings); d['later']=42; self.put(d)
        self.tool.restore(True)
        self.assertEqual(native.read(self.tool.settings), {'other':12,'later':42,'plugins':{'another':True}})

    def test_project_alias_reuses_transaction_and_restores_original_settings(self):
        child = self.project / 'child'
        child.mkdir()
        alias = native.ProjectInstall(child / '..', self.tool.home)
        self.assertEqual(alias.project, self.tool.project)
        self.assertEqual(alias.state / 'lock', self.tool.state / 'lock')
        self.assertEqual(alias.receipt, self.tool.receipt)

        original = {'other': 12, 'plugins': {'another': True}}
        self.put(original)
        self.tool.configure(self.snapshot, True)
        alias.configure(self.snapshot, True)
        alias.restore(True)
        self.assertEqual(native.read(self.tool.settings), original)

    def test_new_file_restore_and_confirmation(self):
        with self.assertRaises(ValueError): self.tool.configure(self.snapshot)
        self.assertFalse(self.tool.settings.exists())
        self.tool.configure(self.snapshot, True); self.tool.restore(True)
        self.assertFalse(self.tool.settings.exists())

    def test_foreign_binding_needs_replace_and_is_restored(self):
        self.put({'plugins':{native.KEY:{'enabled':False}}})
        with self.assertRaises(ValueError): self.tool.configure(self.snapshot, True)
        self.tool.configure(self.snapshot, True, True); self.tool.restore(True)
        self.assertEqual(native.read(self.tool.settings)['plugins'][native.KEY], {'enabled':False})

    def test_external_change_blocks_restore(self):
        self.tool.configure(self.snapshot, True)
        self.put({'plugins':{native.KEY:{'enabled':False}},'other':12})
        with self.assertRaises(ValueError): self.tool.restore(True)
        self.assertEqual(native.read(self.tool.settings)['other'],12)

    def test_interrupted_publish_can_resume(self):
        original=native.atomic
        def fault(path,value):
            if path==self.tool.settings: raise OSError('synthetic interruption')
            return original(path,value)
        with patch.object(native,'atomic',side_effect=fault):
            with self.assertRaises(OSError):self.tool.configure(self.snapshot,True)
        self.tool.configure(self.snapshot,True)
        self.tool.restore(True)
        self.assertFalse(self.tool.settings.exists())

    def test_concurrent_change_not_overwritten(self):
        original=native.atomic
        def change(path,value):
            original(path,value)
            if path==self.tool.receipt:original(self.tool.settings,{'external':True})
        with patch.object(native,'atomic',side_effect=change):
            with self.assertRaises(ValueError): self.tool.configure(self.snapshot,True)
        self.assertEqual(native.read(self.tool.settings),{'external':True})

    def test_invalid_and_duplicate_json_preserved(self):
        self.tool.settings.parent.mkdir(parents=True)
        for raw in ('{"x":1,"x":2}', '// comment\n{}', '[]', '{"plugins":[]}'):
            self.tool.settings.write_text(raw)
            with self.assertRaises(ValueError):self.tool.configure(self.snapshot,True)
            self.assertEqual(self.tool.settings.read_text(),raw)

    def test_local_conflict_and_symlink_rejected(self):
        local=self.tool.home/'plugins/local/renamed/.cursor-plugin/plugin.json'
        native.atomic(local,{'name':'helix'})
        with self.assertRaises(ValueError):self.tool.configure(self.snapshot,True)
        self.assertFalse(self.tool.settings.exists())
        link=self.root/'link';link.symlink_to(self.project)
        with self.assertRaises(ValueError):native.ProjectInstall(link,self.tool.home)

    def test_payload_verification_never_claims_session_loaded(self):
        self.tool.configure(self.snapshot,True)
        root=self.tool.home/'plugins/cache/helixharness-marketplace/helix'/('a'*40)
        root.mkdir(parents=True);(root/'file').write_bytes(b'data')
        self.snapshot['files']={'file':hashlib.sha1(b'blob 4\0data').hexdigest()}
        self.assertEqual(self.tool.check(self.snapshot,root)['session_loaded'],'not_verified')
        (root/'file').write_bytes(b'changed')
        with self.assertRaises(ValueError):self.tool.check(self.snapshot,root)

    def test_source_validation(self):
        for source in ('https://example.invalid/helix.git','git@example.invalid:team/helix.git'):
            native.validate_source(source,'a'*40)
        for source in ('file:///tmp/repo','/tmp/repo','https://user:password@example.invalid/r','https://example.invalid/r?x=y','https://example.invalid/r#x'):
            with self.assertRaises(ValueError):native.validate_source(source,'a'*40)
        with self.assertRaises(ValueError):native.validate_source('https://example.invalid/r','main')

class SnapshotTests(unittest.TestCase):
    def runner(self, source='./helixharness', mode='100644', missing=False, actual_sha=None, object_type='commit'):
        from types import SimpleNamespace
        class Runner:
            def run(inner, argv, timeout):
                if 'rev-parse' in argv:
                    if argv[-1] == 'FETCH_HEAD^{commit}' and object_type != 'commit':
                        raise UpdateHostError('synthetic source is not a commit')
                    return SimpleNamespace(stdout=(actual_sha or 'a'*40)+'\n')
                if 'show' in argv:
                    value=({'name':'helixharness-marketplace','plugins':[{'name':'helix','source':source}]} if argv[-1].endswith('marketplace.json') else {'name':'helix','hooks':'./hosts/cursor/hooks.json'})
                    return SimpleNamespace(stdout=json.dumps(value))
                if 'ls-tree' in argv:
                    prefix='' if source=='.' else 'helixharness/'
                    files=['.cursor-plugin/plugin.json','hosts/cursor/hooks.json','hosts/cursor/host.json','scripts/host_runtime_cli.py']
                    for c in ('init','propose','apply','archive','ut','bug-fix','update','status','config'):files.extend([f'hosts/cursor/skills/helix-{c}/SKILL.md',f'commands/{c}.md'])
                    if missing:files.pop()
                    return SimpleNamespace(stdout=''.join(f'{mode} blob '+ 'b'*40+'\t'+prefix+n+'\0' for n in files))
                return SimpleNamespace(stdout='')
        return Runner()

    def test_root_and_standalone_layout(self):
        for source,expected in (('./helixharness','./helixharness'),('.','.')):
            with self.subTest(source=source):
                snapshot=native.source_snapshot('https://example.invalid/r','a'*40,self.runner(source))
                self.assertEqual(snapshot['binding']['gitPath'],expected)
                self.assertEqual(len(snapshot['files']),22)

    def test_bad_source_distribution_and_wrong_commit(self):
        for runner in (self.runner('../escape'),self.runner('/absolute'),self.runner(mode='120000'),self.runner(missing=True),self.runner(actual_sha='c'*40)):
            with self.assertRaises(ValueError):native.source_snapshot('https://example.invalid/r','a'*40,runner)

    def test_non_commit_sources_never_configure_project(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            home = project / 'cursor-home'
            argv = ['cursor_native_install.py', 'configure', '--project-root', str(project),
                    '--source', 'https://example.invalid/r', '--commit', 'a' * 40,
                    '--user-confirmed']
            for object_type in ('tree', 'blob'):
                with self.subTest(object_type=object_type):
                    with patch.object(sys, 'argv', argv), \
                         patch.dict(os.environ, {'HELIXHARNESS_CURSOR_HOME': str(home)}), \
                         patch.object(native, 'BoundedCommandRunner', return_value=self.runner(object_type=object_type)), \
                         patch.object(native.ProjectInstall, 'configure') as configure, \
                         contextlib.redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(native.main(), 2)
                        self.assertEqual(json.loads(output.getvalue())['status'], 'error')
                        configure.assert_not_called()
                    self.assertFalse((project / '.cursor/settings.json').exists())
                    self.assertFalse(home.exists())
