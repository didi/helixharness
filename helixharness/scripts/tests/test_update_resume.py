"""Interrupted updates must retain the approved target across later checks."""

from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import patch

import test_cursor_managed_update as fixtures
import version_update as updates
from update_hosts.base import UpdateHostError


@unittest.skipUnless(shutil.which("git") and sys.platform in {"darwin", "linux"}, "atomic Git installs required")
class UpdateResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CursorManagedUpdateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.initialize()
        self.approved_commit = self.fixture.publish("1.1.0", "approved")
        self.fixture.checked()
        self.challenge = self.fixture.authorize()

    def interrupt_after_exchange(self):
        fixture = self.fixture
        original = updates._atomic_write

        def interrupt(path, value):
            if path == fixture.authorization and value.get("completed_steps"):
                raise OSError("synthetic interruption after deployment")
            original(path, value)

        with patch.object(updates, "_atomic_write", side_effect=interrupt):
            with self.assertRaises(OSError):
                fixture.install(self.challenge)
        self.assertEqual(fixture.installed_payload(), "approved")

    def checked_with_resume(self):
        fixture = self.fixture
        return updates.check_for_update(
            "cursor", fixture.root, fixture.cache, plugin_root=fixture.adapter.install_root,
            force_refresh=True, runner=fixture.runner, authorization_path=fixture.authorization,
        )

    def test_resume_after_normal_check_refreshes_installed_version(self):
        self.interrupt_after_exchange()
        self.assertEqual(self.fixture.checked()["current_version"], "1.1.0")
        with patch("update_hosts.cursor._exchange", side_effect=AssertionError("must not deploy twice")):
            result = self.fixture.install(self.challenge)
        self.assertTrue(result["resumed"])
        self.assertTrue(result["authorization_consumed"])
        self.assertEqual(result["verified_commit"], self.approved_commit)

    def test_check_exposes_existing_approval_without_consuming_or_deploying(self):
        self.interrupt_after_exchange()
        before = self.fixture.authorization.read_bytes()
        with patch("update_hosts.cursor._exchange", side_effect=AssertionError("check must not install")):
            checked = self.checked_with_resume()
        self.assertEqual(checked["update_status"], "latest")
        self.assertEqual(checked["pending_installation"]["challenge_id"], self.challenge)
        self.assertEqual(checked["pending_installation"]["target_version"], "1.1.0")
        self.assertEqual(self.fixture.authorization.read_bytes(), before)

    def test_check_cli_returns_resume_handle_without_installing(self):
        self.interrupt_after_exchange()
        fixture = self.fixture
        arguments = ["version_update.py", "--platform", "cursor", "--repo-root", str(fixture.root),
                     "--cache-path", str(fixture.cache), "--authorization-path", str(fixture.authorization),
                     "check", "--force-refresh"]
        output = io.StringIO()
        # The full-bundle suite verifies actual installed scripts. Here main's
        # argument routing is exercised with the isolated fixture adapter/root.
        original_public = updates.check_for_update
        def checked(*args, **kwargs):
            kwargs["plugin_root"] = fixture.adapter.install_root
            return original_public(*args, **kwargs)
        with patch.object(sys, "argv", arguments), \
                patch.object(updates, "check_for_update", side_effect=checked), redirect_stdout(output):
            self.assertEqual(updates.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["pending_installation"]["challenge_id"], self.challenge)
        self.assertTrue(fixture.authorization.exists())

    def test_failed_exchange_can_resume_original_commit_after_new_release_check(self):
        with patch("update_hosts.cursor._exchange", side_effect=UpdateHostError("synthetic exchange failure")):
            with self.assertRaises(updates.UpdateError):
                self.fixture.install(self.challenge)
        self.fixture.publish("1.2.0", "not approved")
        self.assertEqual(self.checked_with_resume()["latest_version"], "1.2.0")
        result = self.fixture.install(self.challenge)
        self.assertEqual(result["verified_commit"], self.approved_commit)
        self.assertEqual(self.fixture.installed_payload(), "approved")

    def test_resume_does_not_require_mutable_cache_after_deployment(self):
        self.interrupt_after_exchange()
        self.fixture.cache.write_text("invalid cache")
        with patch.object(self.fixture.adapter, "refresh_marketplace", side_effect=UpdateHostError("offline")):
            checked = self.checked_with_resume()
        self.assertEqual(checked["pending_installation"]["challenge_id"], self.challenge)
        self.fixture.cache.unlink()
        self.assertTrue(self.fixture.install(self.challenge)["authorization_consumed"])

    def test_not_started_authorization_still_requires_matching_cache(self):
        self.fixture.publish("1.2.0", "not approved")
        checked = self.checked_with_resume()
        self.assertNotIn("pending_installation", checked)
        with self.assertRaises(updates.UpdateError):
            self.fixture.install(self.challenge)
        self.assertEqual(self.fixture.installed_payload(), "old")

    def test_snapshot_without_independent_transaction_cannot_bypass_cache(self):
        self.interrupt_after_exchange()
        transaction = self.fixture.home / "helixharness/transactions" / (self.challenge + ".json")
        transaction.unlink()
        self.assertNotIn("pending_installation", self.checked_with_resume())
        with self.assertRaises(updates.UpdateError):
            self.fixture.install(self.challenge)

    def test_old_authorization_without_snapshot_keeps_strict_cache_validation(self):
        self.interrupt_after_exchange()
        authorization = json.loads(self.fixture.authorization.read_text())
        authorization.pop("approved_releases")
        self.fixture.authorization.write_text(json.dumps(authorization))
        self.assertNotIn("pending_installation", self.checked_with_resume())
        with self.assertRaises(updates.UpdateError):
            self.fixture.install(self.challenge)

    def test_native_adapters_do_not_opt_into_cache_independent_install(self):
        self.interrupt_after_exchange()
        authorization = json.loads(self.fixture.authorization.read_text())
        for host in ("claude-code", "codex"):
            with self.subTest(host=host):
                self.assertFalse(updates._adapter(host).validate_install_resume(
                    authorization, plugin_root=self.fixture.adapter.install_root,
                ))
        self.fixture.checked()
        with patch.object(self.fixture.adapter, "validate_install_resume", return_value=False):
            with self.assertRaises(updates.UpdateError):
                self.fixture.install(self.challenge)

    def test_altered_approval_metadata_is_rejected_even_if_tree_matches(self):
        self.interrupt_after_exchange()
        authorization = json.loads(self.fixture.authorization.read_text())
        authorization["approved_releases"][0]["restart_required"] = False
        self.fixture.authorization.write_text(json.dumps(authorization))
        self.assertNotIn("pending_installation", self.checked_with_resume())
        with self.assertRaises(updates.UpdateError):
            self.fixture.install(self.challenge)

    def test_expired_approval_and_modified_registration_cannot_resume(self):
        self.interrupt_after_exchange()
        authorization = json.loads(self.fixture.authorization.read_text())
        expired = updates._parse_timestamp(authorization["expires_at"]) + timedelta(seconds=1)
        with self.assertRaisesRegex(updates.UpdateError, "expired"):
            updates.install("cursor", self.fixture.cache, self.fixture.authorization, self.challenge,
                            now=expired, repo_root=self.fixture.root,
                            plugin_root=self.fixture.adapter.install_root, runner=self.fixture.runner)
        registration = json.loads(self.fixture.adapter.registration_path.read_text())
        registration["ref"] = "changed"
        self.fixture.adapter.registration_path.write_text(json.dumps(registration))
        self.assertNotIn("pending_installation", self.checked_with_resume())
        with self.assertRaises(updates.UpdateError):
            self.fixture.install(self.challenge)


if __name__ == "__main__":
    unittest.main()
