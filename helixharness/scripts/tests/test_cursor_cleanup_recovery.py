"""Synthetic startup recovery and optional cleanup policy compatibility."""
import gzip
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
from unittest.mock import patch

import lifecycle_service
import metrics
import telemetry_async
import telemetry_consent
import telemetry_consent_state
import telemetry_ledger
from telemetry_hosts import get_host_adapter
from test_cursor_hook_budget import load
import test_cursor_telemetry as telemetry_fixtures


class CleanupPolicyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="cleanup-policy-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        env = patch.dict(os.environ, {
            "HELIXHARNESS_TELEMETRY_HOME": str(self.root / "outbox"),
            "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
        })
        env.start()
        self.addCleanup(env.stop)

    def active(self, repo, platform):
        target = repo / ".helixharness/telemetry-v2/lite-active/demo/propose.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"version": 2, "extra": "preserve", "active": {
            "owned": {"platform": platform, "entry_id": "owned"},
            "other": {"platform": "codex", "entry_id": "other"},
            "unknown": ["malformed synthetic state"],
        }}))
        return target

    def test_opt_in_is_generic_and_canonicalizes_alias_without_crossing_scope(self):
        repo = self.root / "repo"
        repo.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        active = self.active(repo, "synthetic-host")
        broken = active.with_name("broken.json")
        broken.write_text("{")
        box = telemetry_async.Outbox()
        for directory in (box.outbox, box.quarantine):
            directory.mkdir(parents=True)
            for name, host, target in (("owned", "synthetic-host", repo),
                                       ("other-host", "codex", repo),
                                       ("other-repo", "synthetic-host", self.root / "other")):
                (directory / (name + ".json.gz")).write_bytes(gzip.compress(
                    json.dumps({"platform": host, "repo": str(target)}).encode()))
        with patch.object(metrics, "get_host_adapter", return_value=SimpleNamespace(strict_private_cleanup=True)) as adapter:
            metrics.purge_private_states(alias, "synthetic-host")
        adapter.assert_called_once_with("synthetic-host")
        value = json.loads(active.read_text())
        self.assertEqual({"other", "unknown"}, set(value["active"]))
        self.assertEqual("preserve", value["extra"])
        self.assertEqual("{", broken.read_text())
        for directory in (box.outbox, box.quarantine):
            self.assertFalse((directory / "owned.json.gz").exists())
            self.assertTrue((directory / "other-host.json.gz").exists())
            self.assertTrue((directory / "other-repo.json.gz").exists())

    def test_only_explicit_true_changes_legacy_cleanup_behavior(self):
        policies = [None, SimpleNamespace(), SimpleNamespace(strict_private_cleanup=False),
                    SimpleNamespace(strict_private_cleanup="true"), SimpleNamespace(strict_private_cleanup=1)]
        for index, adapter in enumerate(policies):
            with self.subTest(index=index):
                repo = self.root / str(index)
                active = self.active(repo, "synthetic-host")
                with patch.object(metrics, "get_host_adapter", return_value=adapter):
                    metrics.purge_private_states(repo, "synthetic-host")
                # Legacy cleanup retains valid other-host entries, not malformed state.
                self.assertEqual({"other"}, set(json.loads(active.read_text())["active"]))

    def test_explicit_strict_cannot_be_disabled_by_adapter(self):
        active = self.active(self.root / "repo", "synthetic-host")
        with patch.object(metrics, "get_host_adapter", return_value=SimpleNamespace(strict_private_cleanup=False)):
            metrics.purge_private_states(self.root / "repo", "synthetic-host", strict_scope=True)
        self.assertEqual({"other", "unknown"}, set(json.loads(active.read_text())["active"]))

    def test_adapter_load_failure_happens_before_any_deletion(self):
        active = self.active(self.root / "repo", "synthetic-host")
        before = active.read_bytes()
        with patch.object(metrics, "get_host_adapter", side_effect=ValueError("synthetic broken adapter")):
            with self.assertRaisesRegex(ValueError, "synthetic broken adapter"):
                metrics.purge_private_states(self.root / "repo", "synthetic-host")
        self.assertEqual(before, active.read_bytes())

    def test_cursor_opts_in_and_original_hosts_keep_legacy_policy(self):
        self.assertIs(True, getattr(get_host_adapter("cursor"), "strict_private_cleanup", False))
        for host in ("claude-code", "codex"):
            self.assertIs(False, getattr(get_host_adapter(host), "strict_private_cleanup", False))

    def test_strict_cleanup_does_not_follow_managed_file_symlink(self):
        repo = self.root / "repo"
        active = self.active(repo, "synthetic-host")
        outside = self.root / "outside.json"
        outside.write_text(active.read_text())
        before = outside.read_bytes()
        active.unlink()
        active.symlink_to(outside)
        with patch.object(metrics, "get_host_adapter", return_value=SimpleNamespace(strict_private_cleanup=True)):
            metrics.purge_private_states(repo, "synthetic-host")
        self.assertEqual(before, outside.read_bytes())
        self.assertTrue(active.is_symlink())


class StartupCleanupDispatchTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="startup-cleanup-")
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name).resolve()
        self.entry = load("hook_entry")
        self.payload = {"hook_event_name": "sessionStart", "conversation_id": "synthetic-startup",
                        "workspace_roots": [str(self.repo), str(self.repo / ".")]}

    def invoke(self, payload, spawn):
        class Output(io.StringIO):
            flushed = False
            def flush(self):
                self.flushed = True
        output = Output()
        def dispatch(roots):
            self.assertTrue(output.flushed)
            self.assertEqual("synthetic-startup", json.loads(output.getvalue())["env"]["HELIXHARNESS_CURSOR_CONVERSATION_ID"])
            return spawn(roots)
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(output), patch.object(
            self.entry, "_spawn_cleanup", side_effect=dispatch
        ) as cleanup, patch.object(telemetry_consent_state, "_repo_root", return_value=self.repo), patch.object(
            lifecycle_service, "handle_event"
        ) as repair:
            self.assertEqual(0, self.entry.main())
        self.assertEqual(1, len(output.getvalue().splitlines()))
        return cleanup, repair

    def test_startup_flushes_then_dispatches_once_and_repairs(self):
        cleanup, repair = self.invoke(self.payload, lambda roots: None)
        cleanup.assert_called_once_with([str(self.repo)])
        repair.assert_called_once()

    def test_failed_dispatch_does_not_suppress_existing_repair(self):
        def unavailable(roots):
            raise OSError("synthetic spawn unavailable")
        cleanup, repair = self.invoke(self.payload, unavailable)
        cleanup.assert_called_once()
        repair.assert_called_once()

    def test_missing_identity_neither_dispatches_nor_guesses_repo(self):
        cleanup, repair = self.invoke({"hook_event_name": "sessionStart"}, lambda roots: None)
        cleanup.assert_not_called()
        repair.assert_not_called()


class StartupCleanupRecoveryTest(unittest.TestCase):
    setUp = telemetry_fixtures.CursorTelemetryTest.setUp
    consent = telemetry_fixtures.CursorTelemetryTest.consent
    start = telemetry_fixtures.CursorTelemetryTest.start

    def test_revocation_elsewhere_is_recovered_at_next_start_without_session_end(self):
        self.consent()
        started = self.start()
        source = Path(telemetry_ledger.load_locator(self.repo, started["entry_id"])["session_log_path"])
        private = self.repo / ".helixharness/telemetry-v2"
        retained = private / "lite-active/control/propose.json"
        retained.parent.mkdir(parents=True)
        retained.write_text(json.dumps({"active": {
            "codex": {"platform": "codex", "entry_id": "other"},
            "claude": {"platform": "claude-code", "entry_id": "other-2"},
            "unknown": ["do not discard"],
        }}))
        retained_bytes = retained.read_bytes()
        public = {p: p.read_bytes() for p in (self.repo / "docs/eval").rglob("*") if p.is_file()}
        other = self.root / "other-repo"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], env=self.env, check=True)
        telemetry_consent.set_mode(other, "cursor", self.adapter.consent_cache_path(), "user", "disabled",
                                   consent_source="explicit-command", user_confirmed=True)
        self.assertTrue(source.exists(), "closing consent elsewhere must not scan this repo")
        entry = load("hook_entry")
        processes = []
        popen = subprocess.Popen
        def tracking(*args, **kwargs):
            process = popen(*args, **kwargs)
            if any(str(value).endswith("cleanup_worker.py") for value in args[0]):
                processes.append(process)
            return process
        with patch.object(sys, "stdin", io.StringIO(json.dumps({**self.payload, "hook_event_name": "sessionStart"}))), redirect_stdout(io.StringIO()), patch.object(
            subprocess, "Popen", side_effect=tracking
        ), patch.object(lifecycle_service, "handle_event"), patch.object(telemetry_async, "remove_scheduler") as scheduler:
            self.assertEqual(0, entry.main())
        self.assertEqual(1, len(processes))
        self.assertEqual(0, processes[0].wait(timeout=10))
        scheduler.assert_not_called()
        self.assertFalse(source.exists())
        self.assertEqual(retained_bytes, retained.read_bytes())
        self.assertEqual(public, {p: p.read_bytes() for p in public})

    def test_active_cursor_consent_disable_preserves_unowned_records(self):
        self.consent()
        self.start()
        path = self.repo / ".helixharness/telemetry-v2/lite-active/control/propose.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"extra": "keep", "active": {
            "cursor": {"platform": "cursor", "entry_id": "synthetic"},
            "codex": {"platform": "codex", "entry_id": "synthetic-other"},
            "unknown": ["keep malformed"],
        }}))
        self.consent("disabled")
        value = json.loads(path.read_text())
        self.assertEqual({"codex", "unknown"}, set(value["active"]))
        self.assertEqual("keep", value["extra"])


if __name__ == "__main__":
    unittest.main()
