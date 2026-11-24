"""Pinned, consent-driven checkout updates, using synthetic local Git sources."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from update_hosts.base import BoundedCommandRunner, UpdateHostError
from update_hosts.opencode import OpenCodeUpdateAdapter, plugin_version
import version_update as updates


class OpenCodeUpdateTest(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args], text=True).strip()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-q", "-b", "main")
        self.git(self.source, "config", "user.name", "Synthetic")
        self.git(self.source, "config", "user.email", "synthetic@example.invalid")
        plugin = self.source / "helixharness"
        plugin.mkdir()
        def release(version):
            marketplace = {"name": "helixharness-marketplace", "plugins": [{"name": "helix", "source": "./helixharness", "version": version}]}
            policy = {"schema_version": "1.0", "plugin": "helix", "marketplace": "helixharness-marketplace", "latest_version": version,
                "minimum_supported_version": "1.0.0", "force_update_below": None, "force_update_message": None,
                "releases": [{"version": value, "date": "2026-11-23", "update_type": "recommended", "title": "Synthetic", "changes": ["Synthetic"], "restart_required": True}
                             for value in ([version, "1.0.0"] if version != "1.0.0" else [version])]}
            (self.source / ".claude-plugin").mkdir(exist_ok=True)
            (self.source / ".claude-plugin/marketplace.json").write_text(json.dumps(marketplace))
            (plugin / "reference").mkdir(exist_ok=True)
            (plugin / "reference/version-policy.json").write_text(json.dumps(policy))
        release("1.0.0")
        (plugin / "package.json").write_text(json.dumps({"name": "helixharness-opencode", "version": "1.0.0", "exports": {".": "./opencode/index.ts"}}))
        (plugin / "opencode").mkdir()
        (plugin / "opencode/index.ts").write_text("export default {}")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "synthetic A")
        self.checkout = self.root / "installed checkout"
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "clone", "-q", str(self.source), str(self.checkout)], check=True)
        self.plugin = self.checkout / "helixharness"
        self.old = self.git(self.checkout, "rev-parse", "HEAD")
        (plugin / "package.json").write_text(json.dumps({"name": "helixharness-opencode", "version": "1.0.1", "exports": {".": "./opencode/index.ts"}}))
        release("1.0.1")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "synthetic B")
        self.new = self.git(self.source, "rev-parse", "HEAD")
        self.adapter = OpenCodeUpdateAdapter(str, lambda p: json.loads(p.read_text()), home=self.root / "state")
        self.runner = BoundedCommandRunner()

    def test_entire_authorization_chain_and_single_consumption(self):
        cache = self.root / "state/cache.json"
        authorization = self.root / "state/authorization.json"
        with patch.dict(updates._HOST_ADAPTERS, {"opencode": self.adapter}):
            checked = updates.check_for_update("opencode", self.root, cache, plugin_root=self.plugin, runner=self.runner, force_refresh=True)
            self.assertEqual(checked["update_status"], "recommended", checked)
            prepared = updates.prepare_authorization("opencode", "checkout", cache, authorization)
            challenge = prepared["challenge_id"]
            with self.assertRaises(updates.UpdateError):
                updates.install("opencode", cache, authorization, challenge, plugin_root=self.plugin, runner=self.runner)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old)
            with self.assertRaises(updates.UpdateError):
                updates.apply_update_choice(cache, authorization, challenge, "upgrade_now", False)
            updates.apply_update_choice(cache, authorization, challenge, "upgrade_now", True)
            installed = updates.install("opencode", cache, authorization, challenge, plugin_root=self.plugin, runner=self.runner)
            self.assertTrue(installed["authorization_consumed"])
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.new)
            with self.assertRaises(updates.UpdateError):
                updates.install("opencode", cache, authorization, challenge, plugin_root=self.plugin, runner=self.runner)

    def test_target_version_mismatch_is_rejected_before_mutation(self):
        binding = self.binding()
        with self.assertRaises(UpdateHostError):
            self.adapter.execute_install_step(self.adapter.install_steps("checkout", self.runner)[0], self.runner,
                authorization={"state": "installing", "update_binding": binding, "target_version": "9.0.0"}, plugin_root=self.plugin)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old)

    def binding(self):
        installation = self.adapter.inspect_installation(self.root, self.plugin, self.runner)
        with self.adapter.refresh_marketplace(self.runner) as snapshot:
            self.assertEqual(plugin_version(snapshot / "helixharness"), "1.0.1")
            return self.adapter.update_binding(snapshot, installation, self.runner)

    def test_check_does_not_change_head_or_worktree(self):
        binding = self.binding()
        self.assertEqual(binding["target_commit"], self.new)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old)
        self.assertEqual(plugin_version(self.plugin), "1.0.0")

    def test_authorized_target_is_pinned_even_if_source_advances(self):
        binding = self.binding()
        (self.source / "later.txt").write_text("synthetic later")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "synthetic C")
        auth = {"state": "installing", "scope": "checkout", "update_binding": binding, "target_version": "1.0.1"}
        step = self.adapter.install_steps("checkout", self.runner)[0]
        self.adapter.execute_install_step(step, self.runner, authorization=auth, plugin_root=self.plugin)
        self.adapter.execute_install_step(step, self.runner, authorization=auth, plugin_root=self.plugin)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.new)
        self.assertFalse((self.checkout / "later.txt").exists())
        self.assertEqual(self.adapter.verify_installation(auth, self.runner, plugin_root=self.plugin)["target_commit"], self.new)

    def test_dirty_checkout_blocks_without_overwriting(self):
        binding = self.binding()
        (self.checkout / "local.txt").write_text("keep")
        with self.assertRaises(UpdateHostError):
            self.adapter.execute_install_step(self.adapter.install_steps("checkout", self.runner)[0], self.runner,
                authorization={"state": "installing", "update_binding": binding}, plugin_root=self.plugin)
        self.assertEqual((self.checkout / "local.txt").read_text(), "keep")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.old)

    def test_detached_and_no_upstream_are_rejected(self):
        self.git(self.checkout, "checkout", "--detach", "-q")
        with self.assertRaises(UpdateHostError):
            self.adapter.inspect_installation(self.root, self.plugin, self.runner)
        self.git(self.checkout, "checkout", "-q", "main")
        self.git(self.checkout, "branch", "--unset-upstream")
        with self.assertRaises(UpdateHostError):
            self.adapter.inspect_installation(self.root, self.plugin, self.runner)

    def test_divergence_is_rejected_and_git_hooks_never_run(self):
        marker = self.root / "hook-must-not-run"
        hook = self.checkout / ".git/hooks/post-merge"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        binding = self.binding()
        self.adapter.execute_install_step(self.adapter.install_steps("checkout", self.runner)[0], self.runner,
            authorization={"state": "installing", "update_binding": binding, "target_version": "1.0.1"}, plugin_root=self.plugin)
        self.assertFalse(marker.exists())
        # Two descendants of B now diverge; all commits stay inside this fixture.
        (self.checkout / "local-branch.txt").write_text("synthetic local commit")
        self.git(self.checkout, "add", ".")
        self.git(self.checkout, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "synthetic local")
        head = self.git(self.checkout, "rev-parse", "HEAD")
        (self.source / "upstream-branch.txt").write_text("synthetic upstream commit")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "synthetic upstream")
        with self.assertRaises(UpdateHostError):
            self.binding()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)

    def test_malformed_binding_and_unconfirmed_execution_are_rejected(self):
        binding = self.binding()
        for changed in ({"plugin_relative": "../foreign"}, {"target_commit": "main"}, {"checkout_root": "relative"}):
            with self.subTest(changed=changed), self.assertRaises(UpdateHostError):
                self.adapter.validate_update_binding({**binding, **changed})
        with self.assertRaises(UpdateHostError):
            self.adapter.execute_install_step(self.adapter.install_steps("checkout", self.runner)[0], self.runner,
                authorization={"state": "prepared", "update_binding": binding}, plugin_root=self.plugin)

    def test_dirty_inspection_is_rejected(self):
        (self.plugin / "package.json").write_text("local edits")
        with self.assertRaises(UpdateHostError):
            self.adapter.inspect_installation(self.root, self.plugin, self.runner)
