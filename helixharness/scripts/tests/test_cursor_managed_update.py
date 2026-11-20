"""Synthetic Git distributions only; never touch a real Cursor profile or plugin."""

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

import version_update as updates
from update_hosts.base import BoundedCommandRunner, UpdateHostError
from update_hosts.cursor import CursorUpdateAdapter, RECEIPT, _json, plugin_version


@unittest.skipUnless(shutil.which("git") and sys.platform in {"darwin", "linux"}, "Git and atomic directory exchange required")
class CursorManagedUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="helix-cursor-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "synthetic git source"
        self.source.mkdir()
        self.home = self.root / "synthetic cursor"
        self.adapter = CursorUpdateAdapter(updates._version_text, updates._read_json, home=self.home)
        self.runner = BoundedCommandRunner()
        self.environment = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.registry = patch.dict(updates._HOST_ADAPTERS, {"cursor": self.adapter})
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.platforms = patch.object(updates, "SUPPORTED_PLATFORMS", updates.SUPPORTED_PLATFORMS | {"cursor"})
        self.platforms.start()
        self.addCleanup(self.platforms.stop)
        self.git("init", "--template=", "-b", "main")
        self.publish("1.0.0", "old")
        self.cache = self.home / "helixharness/check.json"
        self.authorization = self.home / "helixharness/authorization.json"

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "-C", str(self.source), *arguments], capture_output=True, text=True, check=True, timeout=10)
        return result.stdout.strip()

    def publish(self, version, payload, *, standalone=False):
        plugin = self.source if standalone else self.source / "helixharness"
        plugin.mkdir(exist_ok=True)
        manifest = {"name": "helix", "version": version, "skills": "./hosts/cursor/skills/", "hooks": "./hosts/cursor/hooks.json"}
        marketplace = {"name": "helixharness-marketplace", "plugins": [{"name": "helix", "source": "." if standalone else "./helixharness", "version": version}]}
        policy = {"schema_version": "1.0", "plugin": "helix", "marketplace": "helixharness-marketplace", "latest_version": version, "minimum_supported_version": "1.0.0", "force_update_below": None, "force_update_message": None, "releases": [{"version": v, "date": "2026-11-18", "update_type": "recommended", "title": "Synthetic release " + v, "changes": ["Synthetic change"], "restart_required": True} for v in ([version, "1.0.0"] if version != "1.0.0" else [version])]}
        files = {plugin / ".cursor-plugin/plugin.json": json.dumps(manifest), self.source / ".cursor-plugin/marketplace.json": json.dumps(marketplace), plugin / "reference/version-policy.json": json.dumps(policy), plugin / "skills/example/SKILL.md": payload, plugin / "scripts/example.py": "# Synthetic distribution\n"}
        entries = {}
        for command in ("init", "propose", "apply", "archive", "ut", "bug-fix", "update", "status", "config"):
            entry = f"hosts/cursor/skills/helix-{command}/SKILL.md"
            canonical = f"commands/{command}.md"
            entries[command] = [entry, canonical]
            files[plugin / entry] = "Synthetic Cursor entry"
            files[plugin / canonical] = "Synthetic canonical command"
        files[plugin / "hosts/cursor/command-projection.json"] = json.dumps({"entrypoints": entries})
        files[plugin / "hosts/cursor/host.json"] = json.dumps({"host_id": "cursor", "host_api_version": "1.2", "adapters": {"runtime": "resource:hosts/cursor/runtime.md", "update": "python:update_hosts.cursor:CursorUpdateAdapter"}})
        files[plugin / "hosts/cursor/runtime.md"] = "Synthetic runtime"
        files[plugin / "hosts/cursor/hooks.json"] = '{"version":1,"hooks":{}}'
        files[plugin / "scripts/update_hosts/cursor.py"] = "# Synthetic candidate code; never executed\n"
        if not standalone:
            files[plugin / ".cursor-plugin/marketplace.json"] = json.dumps({"name": "helixharness-marketplace", "plugins": [{"name": "helix", "source": ".", "version": version}]})
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic " + version)
        return self.git("rev-parse", "HEAD")

    def initialize(self):
        return self.adapter.initialize(str(self.source), "main", self.runner, user_confirmed=True)

    def checked(self):
        return updates.check_for_update("cursor", self.root, self.cache, plugin_root=self.adapter.install_root, force_refresh=True, runner=self.runner)

    def authorize(self):
        prepared = updates.prepare_authorization("cursor", "local", self.cache, self.authorization)
        updates.apply_update_choice(self.cache, self.authorization, prepared["challenge_id"], "upgrade_now", True)
        return prepared["challenge_id"]

    def install(self, challenge):
        return updates.install("cursor", self.cache, self.authorization, challenge, repo_root=self.root, plugin_root=self.adapter.install_root, runner=self.runner)

    def installed_payload(self):
        return (self.adapter.install_root / "skills/example/SKILL.md").read_text()

    def test_check_prepare_and_confirmation_do_not_change_installation(self):
        self.initialize()
        before = self.adapter.inspect_installation(self.root, self.adapter.install_root, self.runner)
        commit = self.publish("1.1.0", "new")
        result = self.checked()
        self.assertEqual(result["update_status"], "recommended")
        self.assertEqual(result["update_binding"]["target_commit"], commit)
        prepared = updates.prepare_authorization("cursor", "local", self.cache, self.authorization)
        self.assertIn("非原生市场更新", prepared["prompt"]["message"])
        self.assertIn(commit, prepared["prompt"]["message"])
        self.assertEqual(before, self.adapter.inspect_installation(self.root, self.adapter.install_root, self.runner))
        with self.assertRaises(updates.UpdateError):
            self.install(prepared["challenge_id"])
        with self.assertRaises(updates.UpdateError):
            updates.apply_update_choice(self.cache, self.authorization, prepared["challenge_id"], "upgrade_now", False)
        self.assertEqual(self.installed_payload(), "old")

    def test_authorized_atomic_update_verifies_target_keeps_rollback_and_consumes_once(self):
        self.initialize()
        commit = self.publish("1.1.0", "new")
        self.checked()
        challenge = self.authorize()
        result = self.install(challenge)
        self.assertEqual(result["verified_commit"], commit)
        self.assertEqual(result["verified_version"], "1.1.0")
        self.assertEqual(result["commands"], [])  # No invented native Cursor CLI.
        self.assertTrue(result["authorization_consumed"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(self.installed_payload(), "new")
        self.assertFalse(self.adapter.install_root.is_symlink())
        self.assertEqual((self.home / "helixharness/rollback" / challenge / "skills/example/SKILL.md").read_text(), "old")
        with self.assertRaises(updates.UpdateError):
            self.install(challenge)

    def test_failed_post_install_validation_rolls_back_and_retry_succeeds(self):
        self.initialize()
        self.publish("1.1.0", "new")
        self.checked()
        challenge = self.authorize()
        with patch.object(self.adapter, "_verify_target", side_effect=UpdateHostError("synthetic validation failure")):
            with self.assertRaises(updates.UpdateError):
                self.install(challenge)
        self.assertEqual(self.installed_payload(), "old")
        self.assertEqual(plugin_version(self.adapter.install_root), "1.0.0")
        self.assertFalse((self.home / "helixharness/rollback" / challenge).exists())
        self.assertTrue(self.install(challenge)["resumed"])
        self.assertEqual(self.installed_payload(), "new")

    def test_exchange_failure_keeps_old_installation_and_authorization_for_retry(self):
        self.initialize()
        self.publish("1.1.0", "new")
        self.checked()
        challenge = self.authorize()
        with patch("update_hosts.cursor._exchange", side_effect=UpdateHostError("synthetic exchange failure")):
            with self.assertRaises(updates.UpdateError):
                self.install(challenge)
        self.assertEqual(self.installed_payload(), "old")
        self.assertTrue(self.authorization.exists())
        self.install(challenge)

    def test_resume_after_exchange_before_journal_write_does_not_repeat_deployment(self):
        self.initialize()
        self.publish("1.1.0", "new")
        self.checked()
        challenge = self.authorize()
        original = updates._atomic_write
        def interrupted(path, data):
            if path == self.authorization and data.get("completed_steps"):
                raise OSError("synthetic interruption after atomic deployment")
            original(path, data)
        with patch.object(updates, "_atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.install(challenge)
        self.assertEqual(self.installed_payload(), "new")
        with patch("update_hosts.cursor._exchange", side_effect=AssertionError("must not deploy twice")):
            self.assertTrue(self.install(challenge)["resumed"])

    def test_registration_replacement_rejected_at_prepare_and_install(self):
        self.initialize()
        self.publish("1.1.0", "new")
        self.checked()
        original = self.adapter.registration_path.read_bytes()
        registration = _json(self.adapter.registration_path)
        registration["ref"] = "different-ref"
        self.adapter.registration_path.write_text(json.dumps(registration))
        with self.assertRaises(updates.UpdateError):
            updates.prepare_authorization("cursor", "local", self.cache, self.authorization)
        self.adapter.registration_path.write_bytes(original)
        challenge = self.authorize()
        self.adapter.registration_path.write_text(json.dumps(registration))
        with self.assertRaises(updates.UpdateError):
            self.install(challenge)
        self.assertEqual(self.installed_payload(), "old")

    def test_approved_commit_is_pinned_when_branch_advances_with_same_version(self):
        self.initialize()
        approved = self.publish("1.1.0", "approved tree")
        self.checked()
        challenge = self.authorize()
        self.publish("1.1.0", "different tree with same version")
        result = self.install(challenge)
        self.assertEqual(result["verified_commit"], approved)
        self.assertEqual(self.installed_payload(), "approved tree")

    def test_same_version_modified_install_tree_and_cache_binding_are_rejected(self):
        self.initialize()
        self.publish("1.1.0", "new")
        self.checked()
        challenge = self.authorize()
        installed = self.adapter.install_root / "skills/example/SKILL.md"
        installed.write_text("unexpected local modification")
        with self.assertRaises(updates.UpdateError):
            self.install(challenge)
        installed.write_text("old")
        cache = json.loads(self.cache.read_text())
        cache["update_binding"]["target_tree"] = "0" * 64
        self.cache.write_text(json.dumps(cache))
        with self.assertRaises(updates.UpdateError):
            self.install(challenge)
        self.assertEqual(self.installed_payload(), "old")

    def test_distribution_excludes_credentials_runtime_output_and_untracked_files(self):
        plugin = self.source / "helixharness"
        for relative in (".env", ".env.production", ".cursor/settings.json", "skills/example/output/private.txt", "scripts/__pycache__/private.pyc", "skills/example/temp/session.json"):
            path = plugin / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SYNTHETIC-PRIVATE")
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic excluded files")
        (plugin / "skills/example/untracked.md").write_text("SYNTHETIC-UNTRACKED")
        self.initialize()
        copied = [p for p in self.adapter.install_root.rglob("*") if p.is_file()]
        self.assertFalse(any(b"SYNTHETIC-PRIVATE" in p.read_bytes() or b"SYNTHETIC-UNTRACKED" in p.read_bytes() for p in copied))
        self.assertFalse((self.adapter.install_root / ".git").exists())

    def test_ordinary_local_and_marketplace_cache_are_never_adopted(self):
        self.adapter.install_root.mkdir(parents=True)
        (self.adapter.install_root / "unmanaged.txt").write_text("synthetic unmanaged")
        with self.assertRaises(UpdateHostError):
            self.initialize()
        with self.assertRaises(UpdateHostError):
            self.adapter.inspect_installation(self.root, self.adapter.install_root, self.runner)
        self.assertEqual((self.adapter.install_root / "unmanaged.txt").read_text(), "synthetic unmanaged")

    def test_symlink_install_path_and_git_distribution_symlinks_are_rejected(self):
        elsewhere = self.root / "unmanaged cache"
        elsewhere.mkdir()
        self.adapter.install_root.parent.mkdir(parents=True)
        self.adapter.install_root.symlink_to(elsewhere)
        with self.assertRaises(UpdateHostError):
            self.initialize()
        self.adapter.install_root.unlink()
        link = self.source / "helixharness/skills/example/linked.md"
        link.symlink_to("../../../.env")
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic unsafe link")
        with self.assertRaises(UpdateHostError):
            self.initialize()
        self.assertFalse(self.adapter.install_root.exists())

    def test_installer_cli_requires_confirmation_and_installs_only_synthetic_home(self):
        script = Path(__file__).resolve().parents[1] / "cursor_managed_install.py"
        command = [sys.executable, str(script), "install", "--source", str(self.source), "--ref", "main"]
        environment = {**os.environ, "HELIXHARNESS_CURSOR_HOME": str(self.home)}
        denied = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(denied.returncode, 2, denied.stderr)
        self.assertFalse(self.adapter.install_root.exists())
        installed = subprocess.run([*command, "--user-confirmed"], env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertEqual(json.loads(installed.stdout)["plugin_root"], str(self.adapter.install_root))

    def test_standalone_git_marketplace_layout(self):
        # A fresh synthetic repository exercises source='.' independently.
        self.source = self.root / "standalone git"
        self.source.mkdir()
        self.git("init", "--template=", "-b", "main")
        self.publish("1.0.0", "standalone", standalone=True)
        self.initialize()
        self.assertEqual(self.installed_payload(), "standalone")
        self.publish("1.1.0", "standalone new", standalone=True)
        self.checked()
        self.install(self.authorize())
        self.assertEqual(self.installed_payload(), "standalone new")

    def test_git_sources_reject_embedded_credentials_query_and_fragment(self):
        for source in ("https://user:secret@example.invalid/repo.git", "ssh://user:secret@example.invalid/repo.git", "https://example.invalid/repo.git?token=synthetic", "ssh://git@example.invalid/repo.git#synthetic", "https://user@example.invalid/repo.git"):
            with self.subTest(source=source), self.assertRaises(UpdateHostError):
                self.adapter._source(source, "main")
        self.adapter._source("ssh://git@example.invalid/repo.git", "main")

    def test_missing_manifest_resource_fails_before_installation(self):
        hooks = self.source / "helixharness/hosts/cursor/hooks.json"
        hooks.unlink()
        self.git("add", ".")
        self.git("commit", "-m", "Synthetic missing resource")
        with self.assertRaises(UpdateHostError):
            self.initialize()
        self.assertFalse(self.adapter.install_root.exists())

    def test_unmanaged_extra_files_are_not_silently_moved_out_of_active_install(self):
        self.initialize()
        self.publish("1.1.0", "new")
        self.checked()
        challenge = self.authorize()
        extra = self.adapter.install_root / "user-notes.txt"
        extra.write_text("Synthetic user-owned notes")
        with self.assertRaises(updates.UpdateError):
            self.install(challenge)
        self.assertEqual(extra.read_text(), "Synthetic user-owned notes")
        self.assertEqual(self.installed_payload(), "old")

    def test_initial_registration_recovers_after_real_process_exit_between_writes(self):
        code = """
import os, sys
sys.path.insert(0, sys.argv[2])
import update_hosts.cursor as cursor
from version_update import _version_text, _read_json
from update_hosts.base import BoundedCommandRunner
adapter = cursor.CursorUpdateAdapter(_version_text, _read_json)
def interrupted(registration, staged):
    os._exit(86)
adapter._publish_initial_registration = interrupted
adapter.initialize(sys.argv[1], 'main', BoundedCommandRunner(), user_confirmed=True)
"""
        environment = {**os.environ, "HELIXHARNESS_CURSOR_HOME": str(self.home)}
        crashed = subprocess.run([sys.executable, "-I", "-c", code, str(self.source), str(Path(__file__).resolve().parents[1])], env=environment, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(crashed.returncode, 86, crashed.stderr)
        self.assertTrue(self.adapter.install_root.exists())
        self.assertFalse(self.adapter.registration_path.exists())
        with self.assertRaises(UpdateHostError):
            self.adapter.recover_initial(user_confirmed=False)
        receipt = self.adapter.recover_initial(user_confirmed=True)
        self.assertTrue(receipt["recovered"])
        self.assertEqual(self.adapter.inspect_installation(self.root, self.adapter.install_root, self.runner)["current_version"], "1.0.0")
        self.assertFalse(self.adapter.registration_path.with_name("managed-install.pending.json").exists())

    def test_annotated_tag_is_peeled_to_its_commit_for_receipt_and_policy_binding(self):
        commit = self.git("rev-parse", "HEAD")
        self.git("tag", "-a", "synthetic-release", "-m", "Synthetic annotated tag")
        self.assertNotEqual(self.git("rev-parse", "synthetic-release"), commit)
        result = self.adapter.initialize(str(self.source), "synthetic-release", self.runner, user_confirmed=True)
        self.assertEqual(result["commit"], commit)
        self.assertEqual(_json(self.adapter.install_root / RECEIPT)["commit"], commit)
        checked = self.checked()
        self.assertEqual(checked["update_binding"]["target_commit"], commit)

    def test_tree_only_ref_is_rejected_without_creating_an_installation(self):
        tree = self.git("rev-parse", "HEAD^{tree}")
        self.git("tag", "synthetic-tree-only", tree)
        with self.assertRaises(UpdateHostError):
            self.adapter.initialize(str(self.source), "synthetic-tree-only", self.runner, user_confirmed=True)
        self.assertFalse(self.adapter.install_root.exists())
        self.assertFalse(self.adapter.registration_path.exists())


if __name__ == "__main__":
    unittest.main()
