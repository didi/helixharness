import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import version_update as updates
from update_hosts.cursor import CursorUpdateAdapter
from update_hosts.base import UpdateHostError


class NativeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.root=self.home/'plugins/cache/helixharness-marketplace/helix'/('a'*40)
        (self.root/'.cursor-plugin').mkdir(parents=True)
        (self.root/'.cursor-plugin/plugin.json').write_text(json.dumps({'name':'helix','version':'1.0.0'}))
        self.adapter=CursorUpdateAdapter(updates._version_text,updates._read_json,home=self.home)
        self.cache=self.home/'old-cache';self.cache.write_bytes(b'old cache')
        self.auth=self.home/'old-auth';self.auth.write_bytes(b'old authorization')

    def test_native_check_never_reads_old_state(self):
        with patch.object(updates,'_adapter',return_value=self.adapter):
            result=updates.check_for_update('cursor',self.home,self.cache,plugin_root=self.root,authorization_path=self.auth)
        self.assertEqual(result['current_version'],'1.0.0')
        self.assertEqual(result['next_action'],'continue_with_warning')
        self.assertEqual(result['update_status'],'unknown')
        self.assertNotIn('pending_installation',result)
        self.assertEqual(self.auth.read_bytes(),b'old authorization')

    def test_install_stops_before_authorization_and_lock(self):
        with patch.object(updates,'_adapter',return_value=self.adapter):
            with self.assertRaises(updates.UpdateError):updates.install('cursor',self.cache,self.auth,'fake',plugin_root=self.root)
        self.assertEqual(self.auth.read_bytes(),b'old authorization')
        self.assertFalse(self.auth.with_name('old-auth.install').exists())

    def test_every_cli_subcommand_stops_before_state_paths(self):
        cases=[['check'],['resolve'],['refresh','--policy-file','unused','--current-version','1.0.0','--scope','local'],['skip','--target-version','1.1.0'],['prepare','--scope','local'],['decide','--challenge-id','old','--action','install'],['cancel','--challenge-id','old'],['install','--challenge-id','old']]
        # Use an allowed parser action without assuming its spelling.
        cases[5][-1]=next(iter(updates.VALID_UPDATE_ACTIONS))
        for args in cases:
            with self.subTest(command=args[0]),patch.object(updates,'PLUGIN_ROOT',self.root),patch.object(updates,'guard_source'),patch.object(updates,'_adapter',return_value=self.adapter),patch.object(updates,'_cache_path',side_effect=AssertionError('cache touched')),patch.object(updates,'_authorization_path',side_effect=AssertionError('authorization touched')),patch.object(sys,'argv',['version_update.py','--platform','cursor',*args]),contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(updates.main(),0 if args[0]=='check' else 2)
                self.assertEqual(json.loads(output.getvalue())['reason'],'installation_update_not_supported')

    def test_other_roots_do_not_change_local_policy(self):
        self.assertIsNone(self.adapter.entry_policy(self.home/'plugins/local/helix'))
        self.assertIsNone(self.adapter.entry_policy(self.home/'other'))

    def test_invalid_native_manifest_not_adopted(self):
        (self.root/'.cursor-plugin/plugin.json').write_text('{"name":"other","version":"1.0.0"}')
        with self.assertRaises(UpdateHostError):self.adapter.entry_policy(self.root)

    def test_unknown_native_cache_never_falls_back_to_local_update(self):
        for root in (self.root.parent/'1.0.0', self.home/'plugins/cache/other/helix'/('b'*40)):
            with self.subTest(root=root), patch.object(updates, '_adapter', return_value=self.adapter):
                with self.assertRaises(UpdateHostError):
                    updates.check_for_update('cursor', self.home, self.cache,
                                             plugin_root=root, authorization_path=self.auth)
        self.assertEqual(self.cache.read_bytes(), b'old cache')
        self.assertEqual(self.auth.read_bytes(), b'old authorization')
