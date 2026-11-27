"""Cursor update outcome checks, including the two Codex simulation failures.

Synthetic Git releases and directories exercise the real managed adapter.
Fault injection models deployment failures; no native Cursor client is started.
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from unittest.mock import patch

import test_cursor_managed_update as fixtures
import version_update as updates
from update_hosts import cursor


@unittest.skipUnless(shutil.which("git") and sys.platform in {"darwin", "linux"}, "Git and atomic rename required")
class CursorUpdateScenarioTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CursorManagedUpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.initialize()
        self.approved_commit = self.fixture.publish("1.1.0", "approved payload")
        self.fixture.checked()

    def assert_original_install(self):
        self.assertEqual(self.fixture.installed_payload(), "old")
        self.assertEqual(cursor.plugin_version(self.fixture.adapter.install_root), "1.0.0")
        inspected = self.fixture.adapter.inspect_installation(
            self.fixture.root, self.fixture.adapter.install_root, self.fixture.runner,
        )
        self.assertEqual(inspected["current_version"], "1.0.0")

    def assert_retry_succeeds(self, challenge):
        result = self.fixture.install(challenge)
        self.assertTrue(result["resumed"])
        self.assertTrue(result["authorization_consumed"])
        self.assertEqual(result["verified_commit"], self.approved_commit)
        self.assertEqual(result["verified_version"], "1.1.0")
        self.assertEqual(self.fixture.installed_payload(), "approved payload")

    def test_source_advances_after_approval_but_installs_exact_approved_release(self):
        challenge = self.fixture.authorize()
        later = self.fixture.publish("1.2.0", "unapproved newer payload")
        result = self.fixture.install(challenge)
        receipt = cursor._json(self.fixture.adapter.install_root / cursor.RECEIPT)
        self.assertNotEqual(later, self.approved_commit)
        self.assertEqual(result["verified_commit"], self.approved_commit)
        self.assertEqual(receipt["commit"], self.approved_commit)
        self.assertEqual(receipt["version"], "1.1.0")
        self.assertEqual(self.fixture.installed_payload(), "approved payload")
        self.assertEqual(result["target_version"], result["verified_version"])
        self.assertFalse(self.fixture.authorization.exists())

    def test_exchange_returns_without_installing_is_detected_and_retryable(self):
        challenge = self.fixture.authorize()
        # Model a successful function return that did not change the filesystem.
        # The production verification must discover the old version itself.
        with patch("update_hosts.cursor._exchange", return_value=None):
            with self.assertRaises(updates.UpdateError):
                self.fixture.install(challenge)
        self.assert_original_install()
        journal = json.loads(self.fixture.authorization.read_text())
        self.assertEqual(journal["state"], "installing")
        self.assertEqual(journal["completed_steps"], [])
        self.assertFalse((self.fixture.home / "helixharness/rollback" / challenge).exists())
        self.assert_retry_succeeds(challenge)

    def test_wrong_deployed_contents_are_verified_rolled_back_and_retryable(self):
        challenge = self.fixture.authorize()
        exchange = cursor._exchange
        calls = []

        def changed_deployment(left, right):
            exchange(left, right)
            calls.append((left, right))
            if len(calls) == 1:
                (left / "skills/example/SKILL.md").write_text("synthetic unapproved tree")

        with patch("update_hosts.cursor._exchange", side_effect=changed_deployment):
            with self.assertRaises(updates.UpdateError):
                self.fixture.install(challenge)
        self.assertEqual(len(calls), 2)  # Deployment followed by restoration.
        self.assert_original_install()
        self.assertTrue(self.fixture.authorization.exists())
        self.assert_retry_succeeds(challenge)

    def test_final_verification_rejects_receipt_changed_after_successful_step(self):
        challenge = self.fixture.authorize()
        execute = self.fixture.adapter.execute_install_step

        def change_receipt(*args, **kwargs):
            execute(*args, **kwargs)
            path = self.fixture.adapter.install_root / cursor.RECEIPT
            receipt = cursor._json(path)
            receipt["commit"] = "0" * 40
            path.write_text(json.dumps(receipt))

        with patch.object(self.fixture.adapter, "execute_install_step", side_effect=change_receipt):
            with self.assertRaisesRegex(updates.UpdateError, "verification failed"):
                self.fixture.install(challenge)
        self.assertTrue(self.fixture.authorization.exists())
        self.assertEqual(json.loads(self.fixture.authorization.read_text())["state"], "installing")
        self.assertEqual(self.fixture.installed_payload(), "approved payload")
        self.assertEqual(
            (self.fixture.home / "helixharness/rollback" / challenge / "skills/example/SKILL.md").read_text(),
            "old",
        )
        # An externally changed live receipt is preserved for inspection, never
        # silently overwritten or declared a successful installation.
        self.assertEqual(cursor._json(self.fixture.adapter.install_root / cursor.RECEIPT)["commit"], "0" * 40)

    def test_skip_and_cancel_leave_registered_install_unchanged(self):
        for action in ("skip", "cancel"):
            with self.subTest(action=action):
                prepared = updates.prepare_authorization(
                    "cursor", "local", self.fixture.cache, self.fixture.authorization,
                )
                challenge = prepared["challenge_id"]
                with patch("update_hosts.cursor._exchange", side_effect=AssertionError("decline must not deploy")):
                    if action == "skip":
                        result = updates.apply_update_choice(
                            self.fixture.cache, self.fixture.authorization, challenge, "skip_update", True,
                        )
                        self.assertTrue(result["authorization_cancelled"])
                    else:
                        self.assertTrue(updates.cancel_authorization(self.fixture.authorization, challenge)["cancelled"])
                self.assertFalse(self.fixture.authorization.exists())
                self.assert_original_install()
                self.assertFalse((self.fixture.home / "helixharness/transactions").exists())


if __name__ == "__main__":
    unittest.main()
