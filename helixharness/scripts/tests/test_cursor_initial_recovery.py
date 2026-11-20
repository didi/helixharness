"""Initial-install failure recovery against isolated, synthetic Git distributions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_cursor_managed_update as fixtures
from update_hosts import cursor
from update_hosts.base import UpdateHostError


@unittest.skipUnless(shutil.which("git") and sys.platform in {"darwin", "linux"}, "Git and exclusive rename required")
class CursorInitialRecoveryTests(unittest.TestCase):
    setUp = fixtures.CursorManagedUpdateTests.setUp
    git = fixtures.CursorManagedUpdateTests.git
    publish = fixtures.CursorManagedUpdateTests.publish
    initialize = fixtures.CursorManagedUpdateTests.initialize
    installed_payload = fixtures.CursorManagedUpdateTests.installed_payload

    @property
    def pending(self):
        return self.adapter.registration_path.with_name("managed-install.pending.json")

    def assert_installed(self):
        installation = self.adapter.inspect_installation(self.root, self.adapter.install_root, self.runner)
        self.assertEqual(installation["current_version"], "1.0.0")
        self.assertEqual(self.installed_payload(), "old")
        self.assertFalse(self.pending.exists())
        self.assertEqual(list(self.adapter.registration_path.parent.glob("initial-*")), [])

    def crash(self, point):
        code = """
import os, sys
sys.path.insert(0, sys.argv[2])
import update_hosts.cursor as cursor
from version_update import _version_text, _read_json
from update_hosts.base import BoundedCommandRunner
adapter = cursor.CursorUpdateAdapter(_version_text, _read_json)
point = sys.argv[3]
move = adapter._move_initial_candidate
def interrupted_move(staged):
    if point == 'before-rename':
        os._exit(86)
    move(staged)
    if point == 'after-rename':
        os._exit(86)
adapter._move_initial_candidate = interrupted_move
rename = adapter._initial_rename_exclusive
def interrupted_rename(source, destination):
    rename(source, destination)
    if point == 'registration-published' and destination == adapter.registration_path:
        os._exit(86)
adapter._initial_rename_exclusive = interrupted_rename
publish = adapter._publish_initial_registration
def interrupted_publish(registration, staged):
    publish(registration, staged)
    if point == 'after-registration':
        os._exit(86)
adapter._publish_initial_registration = interrupted_publish
adapter.initialize(sys.argv[1], 'main', BoundedCommandRunner(), user_confirmed=True)
"""
        environment = {**os.environ, "HELIXHARNESS_CURSOR_HOME": str(self.home)}
        result = subprocess.run([sys.executable, "-I", "-c", code, str(self.source), str(Path(__file__).resolve().parents[1]), point], env=environment, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        self.assertTrue(self.pending.exists())

    def permission_failure(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("real directory permissions require an unprivileged process")
        directory = self.adapter.install_root.parent
        directory.mkdir(parents=True)
        directory.chmod(0o555)
        try:
            with self.assertRaises(OSError):
                self.initialize()
        finally:
            directory.chmod(0o755)
        self.assertFalse(self.adapter.install_root.exists())
        self.assertTrue(self.pending.exists())
        self.assertTrue(Path(cursor._json(self.pending)["staged_root"]).is_dir())

    def test_real_permission_failure_can_recover(self):
        self.permission_failure()
        self.assertTrue(self.adapter.recover_initial(user_confirmed=True)["recovered"])
        self.assert_installed()

    def test_real_permission_failure_can_retry_same_install(self):
        self.permission_failure()
        self.assertTrue(self.initialize()["recovered"])
        self.assert_installed()

    def test_process_exit_before_rename_can_recover(self):
        self.crash("before-rename")
        self.assertFalse(self.adapter.install_root.exists())
        self.adapter.recover_initial(user_confirmed=True)
        self.assert_installed()

    def test_process_exit_after_rename_can_recover(self):
        self.crash("after-rename")
        self.assertTrue(self.adapter.install_root.exists())
        self.adapter.recover_initial(user_confirmed=True)
        self.assert_installed()

    def test_process_exit_after_registration_can_retry(self):
        self.crash("after-registration")
        self.assertTrue(self.adapter.registration_path.exists())
        self.initialize()
        self.assert_installed()

    def test_process_exit_after_registration_publish_before_sync_can_recover(self):
        self.crash("registration-published")
        self.assertTrue(self.adapter.registration_path.exists())
        self.adapter.recover_initial(user_confirmed=True)
        self.assert_installed()

    def test_retry_keeps_authorized_commit_when_source_branch_advances(self):
        self.crash("before-rename")
        approved = cursor._json(self.pending)["receipt"]["commit"]
        self.publish("1.1.0", "later unapproved branch state")
        with patch.object(self.adapter, "_snapshot", side_effect=AssertionError("recovery must use the pinned candidate")):
            result = self.initialize()
        self.assertEqual(result["commit"], approved)
        self.assert_installed()

    def test_registration_directory_fsync_failure_retains_recoverable_state(self):
        fsync = os.fsync
        def fail_registration_sync(descriptor):
            parent = self.adapter.registration_path.parent
            if self.adapter.registration_path.exists() and os.fstat(descriptor).st_ino == parent.stat().st_ino:
                raise OSError("synthetic registration directory fsync failure")
            fsync(descriptor)
        with patch("update_hosts.cursor.os.fsync", side_effect=fail_registration_sync), self.assertRaises(OSError):
            self.initialize()
        self.assertTrue(self.adapter.registration_path.exists())
        self.assertTrue(self.adapter.install_root.exists())
        self.assertTrue(self.pending.exists())
        self.adapter.recover_initial(user_confirmed=True)
        self.assert_installed()

    def test_pending_directory_fsync_failure_retains_candidate_for_retry(self):
        fsync = os.fsync
        def fail_pending_sync(descriptor):
            parent = self.adapter.registration_path.parent
            if self.pending.exists() and os.fstat(descriptor).st_ino == parent.stat().st_ino:
                raise OSError("synthetic pending directory fsync failure")
            fsync(descriptor)
        with patch("update_hosts.cursor.os.fsync", side_effect=fail_pending_sync), self.assertRaises(OSError):
            self.initialize()
        self.assertTrue(self.pending.exists())
        self.initialize()
        self.assert_installed()

    def test_cleanup_sync_failure_after_durable_registration_is_committed(self):
        fsync = os.fsync
        def fail_cleanup_sync(descriptor):
            parent = self.adapter.registration_path.parent
            if self.adapter.registration_path.exists() and not self.pending.exists() and os.fstat(descriptor).st_ino == parent.stat().st_ino:
                raise OSError("synthetic cleanup directory fsync failure")
            fsync(descriptor)
        with patch("update_hosts.cursor.os.fsync", side_effect=fail_cleanup_sync):
            self.assertTrue(self.initialize()["installed"])
        self.assert_installed()

    def test_failed_copy_without_journal_removes_owned_staging_and_retries(self):
        copy = shutil.copytree
        def partial_copy(source, target, *args, **kwargs):
            copy(source, target, *args, **kwargs)
            raise OSError("synthetic copy failure")
        with patch("update_hosts.cursor.shutil.copytree", side_effect=partial_copy), self.assertRaises(OSError):
            self.initialize()
        self.assertFalse(self.pending.exists())
        self.assertEqual(list(self.adapter.registration_path.parent.glob("initial-*")), [])
        self.initialize()
        self.assert_installed()

    def test_failed_pending_publish_without_journal_cleans_staging_and_retries(self):
        replace = os.replace
        def fail_pending_replace(source, target):
            if target == self.pending:
                raise OSError("synthetic pending publication failure")
            replace(source, target)
        with patch("update_hosts.cursor.os.replace", side_effect=fail_pending_replace), self.assertRaises(OSError):
            self.initialize()
        self.assertFalse(self.pending.exists())
        self.assertEqual(list(self.adapter.registration_path.parent.glob("initial-*")), [])
        self.initialize()
        self.assert_installed()

    def test_unconfirmed_retry_and_recovery_leave_pending_state_untouched(self):
        self.crash("before-rename")
        journal = self.pending.read_bytes()
        staged = Path(cursor._json(self.pending)["staged_root"])
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=False)
        with self.assertRaises(UpdateHostError):
            self.adapter.initialize(str(self.source), "main", self.runner, user_confirmed=False)
        self.assertEqual(self.pending.read_bytes(), journal)
        self.assertTrue(staged.is_dir())
        self.assertFalse(self.adapter.install_root.exists())

    def test_retry_with_different_source_or_ref_is_rejected(self):
        self.crash("before-rename")
        journal = self.pending.read_bytes()
        for source, ref in ((str(self.source), "different-ref"), (str(self.root / "different source"), "main")):
            with self.subTest(source=source, ref=ref), self.assertRaises(UpdateHostError):
                self.adapter.initialize(source, ref, self.runner, user_confirmed=True)
        self.assertEqual(self.pending.read_bytes(), journal)
        self.assertFalse(self.adapter.install_root.exists())

    def test_recovery_rejects_modified_candidate_without_deleting_it(self):
        self.crash("before-rename")
        journal = self.pending.read_bytes()
        staged = Path(cursor._json(self.pending)["staged_root"])
        payload = staged / "skills/example/SKILL.md"
        payload.write_text("synthetic unapproved modification")
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)
        self.assertEqual(payload.read_text(), "synthetic unapproved modification")
        self.assertEqual(self.pending.read_bytes(), journal)
        self.assertFalse(self.adapter.install_root.exists())

    def test_recovery_rejects_receipt_changed_to_another_commit(self):
        self.crash("before-rename")
        staged = Path(cursor._json(self.pending)["staged_root"])
        receipt = cursor._json(staged / cursor.RECEIPT)
        receipt["commit"] = "0" * 40
        (staged / cursor.RECEIPT).write_text(json.dumps(receipt))
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)
        self.assertTrue(staged.is_dir())
        self.assertFalse(self.adapter.install_root.exists())

    def test_recovery_rejects_changed_registration_candidate_before_moving_plugin(self):
        self.crash("before-rename")
        staged = Path(cursor._json(self.pending)["staged_root"])
        metadata = staged.parent / "registration.json"
        metadata.write_text('{"synthetic": "foreign registration"}')
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)
        self.assertEqual(cursor._json(metadata), {"synthetic": "foreign registration"})
        self.assertTrue(staged.is_dir())
        self.assertFalse(self.adapter.install_root.exists())

    def test_recovery_rejects_journal_path_and_registration_tampering(self):
        self.crash("before-rename")
        original = cursor._json(self.pending)
        changed = dict(original)
        changed["staged_root"] = str(self.root / "unmanaged" / "plugin")
        self.pending.write_text(json.dumps(changed))
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)
        changed = {**original, "registration": {**original["registration"], "ref": "unapproved-ref"}}
        self.pending.write_text(json.dumps(changed))
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)
        self.assertTrue(Path(original["staged_root"]).is_dir())
        self.assertFalse(self.adapter.install_root.exists())

    def test_recovery_preserves_foreign_file_beside_staging_candidate(self):
        self.crash("before-rename")
        staged = Path(cursor._json(self.pending)["staged_root"])
        foreign = staged.parent / "user-notes.txt"
        foreign.write_text("synthetic user notes")
        with self.assertRaises(OSError):
            self.adapter.recover_initial(user_confirmed=True)
        self.assertEqual(foreign.read_text(), "synthetic user notes")
        self.assertTrue(self.pending.exists())
        foreign.unlink()
        self.adapter.recover_initial(user_confirmed=True)
        self.assert_installed()

    def test_recovery_preserves_unknown_registration_and_installation(self):
        self.crash("before-rename")
        registration = {"synthetic": "unknown registration"}
        self.adapter.registration_path.write_text(json.dumps(registration))
        self.adapter.install_root.mkdir()
        unknown = self.adapter.install_root / "user-notes.txt"
        unknown.write_text("synthetic user notes")
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)
        self.assertEqual(cursor._json(self.adapter.registration_path), registration)
        self.assertEqual(unknown.read_text(), "synthetic user notes")
        self.assertTrue(self.pending.exists())

    def test_initial_rename_does_not_replace_a_racing_empty_directory(self):
        move = self.adapter._move_initial_candidate
        def racing_move(staged):
            self.adapter.install_root.mkdir()
            move(staged)
        with patch.object(self.adapter, "_move_initial_candidate", side_effect=racing_move), self.assertRaises(OSError):
            self.initialize()
        self.assertEqual(list(self.adapter.install_root.iterdir()), [])
        self.assertFalse(self.adapter.registration_path.exists())
        self.assertTrue(self.pending.exists())
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)

    def test_registration_publish_does_not_replace_a_racing_foreign_registration(self):
        rename = self.adapter._initial_rename_exclusive
        foreign = {"synthetic": "foreign registration"}
        def racing_rename(source, destination):
            if destination == self.adapter.registration_path:
                destination.write_text(json.dumps(foreign))
            rename(source, destination)
        with patch.object(self.adapter, "_initial_rename_exclusive", side_effect=racing_rename), self.assertRaises(FileExistsError):
            self.initialize()
        self.assertEqual(cursor._json(self.adapter.registration_path), foreign)
        self.assertEqual(self.installed_payload(), "old")
        self.assertTrue(self.pending.exists())
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=True)
        self.assertEqual(cursor._json(self.adapter.registration_path), foreign)


if __name__ == "__main__":
    unittest.main()
