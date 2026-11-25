"""Synthetic regression evidence for bounded hooks and isolated shutdown cleanup."""
import importlib.util
import io
import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import telemetry_consent_state as consent
import telemetry_async
import telemetry_ledger
import metrics
import telemetry_consent
from telemetry_hosts.cursor import record_hook, _journal_lock
import test_cursor_telemetry as telemetry_fixtures
from test_cursor_hooks import synthetic_python

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location("synthetic_" + name, ROOT / "hosts/cursor" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CursorHookBudgetTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cursor-budget-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_permission_is_flushed_before_optional_collection(self):
        entry = load("hook_entry")
        class Output(io.StringIO):
            flushed = False
            def flush(self):
                self.flushed = True
        for event in ("beforeSubmitPrompt", "subagentStart"):
            stream = Output()
            def record(*args, **kwargs):
                self.assertTrue(stream.flushed)
            with patch.object(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": event}))), redirect_stdout(stream), patch.object(entry, "record_hook", side_effect=record):
                self.assertEqual(0, entry.main())

    def test_git_timeout_does_not_switch_to_local_project_identity(self):
        with patch.object(consent.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 0.01)):
            with self.assertRaises(consent.ConsentError):
                consent.project_fingerprint(self.root, deadline=time.monotonic() + 0.1)

    def test_default_git_timeout_remains_compatible(self):
        with patch.object(consent.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            self.assertTrue(consent.project_fingerprint(self.root).startswith("prj_"))

    @unittest.skipIf(os.name == "nt", "POSIX lock fault injection")
    def test_existing_consent_lock_has_bounded_wait(self):
        import fcntl
        cache = self.root / "cache.json"
        lock = cache.with_suffix(".json.lock")
        with lock.open("w") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            started = time.monotonic()
            with self.assertRaises(consent.ConsentError):
                with consent._cache_lock(cache, deadline=started + 0.08):
                    self.fail("acquired another writer's lock")
            self.assertLess(time.monotonic() - started, 0.5)

    @unittest.skipIf(os.name == "nt", "POSIX lock fault injection")
    def test_first_cache_anchor_has_bounded_wait(self):
        import fcntl
        cache = self.root / "cursor" / "helixharness" / "cache.json"
        fd = os.open(self.root, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(consent.ConsentError):
                with consent._cache_lock(cache, deadline=time.monotonic() + 0.05):
                    self.fail("acquired first-writer anchor")
            self.assertFalse(cache.parent.exists())
        finally:
            os.close(fd)

    @unittest.skipIf(os.name == "nt", "POSIX first-writer transition")
    def test_anchor_to_new_lock_transition_keeps_original_deadline(self):
        import fcntl
        cache = self.root / "cursor/helixharness/cache.json"
        cache.parent.mkdir(parents=True)
        anchor = os.open(self.root, os.O_RDONLY)
        fcntl.flock(anchor, fcntl.LOCK_EX)
        outcomes = []
        deadlines = []
        owners = []
        acquire = consent._acquire_read_lock
        budget = 0.2
        def observed(fd, deadline):
            deadlines.append(deadline)
            if len(deadlines) == 1:
                # Publish and hold the new lock file, then hand the anchor over, all
                # before the first wait begins. A main-thread transition raced the hook
                # deadline on a busy runner and lost, so the second attempt never
                # happened and the reused deadline was never observed.
                owner = cache.with_suffix(".json.lock").open("w")
                fcntl.flock(owner, fcntl.LOCK_EX)
                owners.append(owner)
                fcntl.flock(anchor, fcntl.LOCK_UN)
            return acquire(fd, deadline)
        begin = time.monotonic()
        def reader():
            try:
                with consent._cache_lock(cache, deadline=begin + budget):
                    outcomes.append("unexpected acquisition")
            except consent.ConsentError:
                outcomes.append("deadline")
        try:
            with patch.object(consent, "_acquire_read_lock", observed):
                thread = threading.Thread(target=reader)
                thread.start()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(["deadline"], outcomes)
            self.assertEqual(2, len(deadlines))
            self.assertEqual(1, len(set(deadlines)))
            # The wait must stop at the hook budget; the slack absorbs thread
            # scheduling, which this case does not measure.
            self.assertLess(time.monotonic() - begin, budget + 0.4)
        finally:
            for owner in owners:
                owner.close()
            os.close(anchor)

    def test_windows_lock_preserves_default_timeout_and_tighter_hook_deadline(self):
        modes = []
        def locked(fd, mode, size):
            modes.append(mode)
            raise OSError("synthetic contention")
        windows = SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, locking=locked)
        with patch.object(consent.os, "name", "nt"), patch.object(consent, "msvcrt", windows, create=True):
            with self.assertRaises(consent.ConsentDeadlineExceeded):
                consent._acquire_read_lock(7, time.monotonic() + 0.02)
            self.assertTrue(modes)
            self.assertEqual({2}, set(modes))
            modes.clear()
            with patch.object(consent, "CACHE_LOCK_TIMEOUT_SECONDS", 0.02):
                with self.assertRaisesRegex(consent.ConsentError, "consent cache lock timed out"):
                    consent._acquire_read_lock(7, None)
            self.assertTrue(modes)
            self.assertEqual({2}, set(modes))

    @unittest.skipIf(os.name == "nt", "POSIX executable fault injection")
    def test_real_slow_git_process_obeys_remaining_budget(self):
        executable = self.root / "git"
        executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(2)\n")
        executable.chmod(0o755)
        begin = time.monotonic()
        with patch.dict(os.environ, {"PATH": str(self.root)}), self.assertRaises(consent.ConsentError):
            consent.project_fingerprint(self.root, deadline=begin + 0.08)
        self.assertLess(time.monotonic() - begin, 0.5)

    def test_strict_outbox_cleanup_preserves_unrelated_corruption(self):
        outbox = telemetry_async.Outbox(self.root / "telemetry")
        outbox.outbox.mkdir(parents=True)
        corrupt = outbox.outbox / "foreign.json.gz"
        corrupt.write_bytes(b"synthetic broken gzip")
        self.assertEqual(0, outbox.purge_repo(self.root, platform="cursor", strict_scope=True))
        self.assertEqual(b"synthetic broken gzip", corrupt.read_bytes())
        self.assertFalse(outbox.quarantine.exists())

    def test_strict_outbox_preserves_corrupt_deflate_without_raising(self):
        outbox = telemetry_async.Outbox(self.root / "telemetry")
        outbox.outbox.mkdir(parents=True)
        corrupt = outbox.outbox / "foreign.json.gz"
        original = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07" + b"\x00" * 12
        corrupt.write_bytes(original)
        self.assertEqual(0, outbox.purge_repo(self.root, platform="cursor", strict_scope=True))
        self.assertEqual(original, corrupt.read_bytes())

    def test_strict_ledger_cleanup_preserves_unknown_platform(self):
        stages = self.root / ".helixharness/telemetry-v2/stages"
        stages.mkdir(parents=True)
        unknown = stages / "entry-unknown.json"
        unknown.write_text("{}")
        self.assertEqual(0, telemetry_ledger.purge_private(self.root, platform="cursor", strict_scope=True))
        self.assertTrue(unknown.exists())

    def test_strict_ledger_cleanup_preserves_conflicting_platforms(self):
        stages = self.root / ".helixharness/telemetry-v2/stages"
        stages.mkdir(parents=True)
        ambiguous = stages / "entry-conflicting.json"
        ambiguous.write_text(json.dumps({"platform": "cursor", "entry": {"platform": "codex"}}))
        self.assertEqual(0, telemetry_ledger.purge_private(self.root, platform="cursor", strict_scope=True))
        self.assertTrue(ambiguous.exists())

    def test_session_end_dispatches_only_isolated_cleanup(self):
        entry = load("hook_entry")
        payload = {"hook_event_name": "sessionEnd", "conversation_id": "synthetic", "workspace_roots": [str(self.root)], "prompt": "not-forwarded"}
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), patch.object(entry, "_spawn_cleanup") as spawn, patch("lifecycle_service.handle_event") as old:
            self.assertEqual(0, entry.main())
            spawn.assert_called_once_with([str(self.root.resolve())])
            old.assert_not_called()

    def test_cleanup_deduplicates_without_global_worker_or_scheduler(self):
        worker = load("cleanup_worker")
        private = self.root / ".helixharness/telemetry-v2"
        private.mkdir(parents=True)
        with worker.cleanup_lease(self.root) as first:
            self.assertTrue(first)
            with worker.cleanup_lease(self.root) as second:
                self.assertFalse(second)
        with worker.cleanup_lease(self.root) as released:
            self.assertTrue(released)
        self.assertEqual(["cursor-cleanup.lock"], [p.name for p in private.iterdir()])

    def test_strict_cleanup_removes_only_owned_data_and_preserves_malformed_active(self):
        root = self.root / ".helixharness/telemetry-v2"
        active = root / "lite-active/demo/propose.json"
        active.parent.mkdir(parents=True)
        active.write_text(json.dumps({"version": 2, "extra": "keep", "active": {
            "cursor": {"entry_id": "entry-cursor", "platform": "cursor"},
            "codex": {"entry_id": "entry-codex", "platform": "codex"},
            "unknown": ["synthetic malformed entry"]}}))
        corrupt = active.with_name("broken.json")
        corrupt.write_text("{")
        outbox = telemetry_async.Outbox(self.root / "outbox-home")
        for directory in (outbox.outbox, outbox.quarantine):
            directory.mkdir(parents=True)
            for name, platform, repo in (("owned", "cursor", self.root), ("foreign", "codex", self.root), ("other", "cursor", self.root / "other")):
                (directory / (name + ".json.gz")).write_bytes(gzip.compress(json.dumps({"platform": platform, "repo": str(repo.resolve())}).encode()))
        with patch.dict(os.environ, {"HELIXHARNESS_TELEMETRY_HOME": str(outbox.home)}):
            metrics.purge_private_states(self.root, "cursor", strict_scope=True)
        self.assertEqual("{", corrupt.read_text())
        value = json.loads(active.read_text())
        self.assertEqual({"codex", "unknown"}, set(value["active"]))
        self.assertEqual("keep", value["extra"])
        for directory in (outbox.outbox, outbox.quarantine):
            self.assertFalse((directory / "owned.json.gz").exists())
            self.assertTrue((directory / "foreign.json.gz").exists())
            self.assertTrue((directory / "other.json.gz").exists())

    def test_legacy_cleanup_defaults_keep_their_contract(self):
        stages = self.root / ".helixharness/telemetry-v2/stages"
        stages.mkdir(parents=True)
        unknown = stages / "entry-unknown.json"
        unknown.write_text("{}")
        self.assertEqual(1, telemetry_ledger.purge_private(self.root, platform="codex"))
        outbox = telemetry_async.Outbox(self.root / "home")
        outbox.outbox.mkdir(parents=True)
        corrupt = outbox.outbox / "corrupt.json.gz"
        corrupt.write_bytes(b"invalid")
        outbox.purge_repo(self.root, platform="claude-code")
        self.assertFalse(corrupt.exists())
        self.assertTrue((outbox.quarantine / corrupt.name).exists())


class CursorHookBudgetIntegrationTest(unittest.TestCase):
    # Reuse synthetic setup, not its test methods or any real host data.
    setUp = telemetry_fixtures.CursorTelemetryTest.setUp
    consent = telemetry_fixtures.CursorTelemetryTest.consent
    start = telemetry_fixtures.CursorTelemetryTest.start

    def test_same_epoch_deadline_path_still_records_normal_event(self):
        self.consent()
        started = self.start()
        self.assertEqual(1, record_hook({**self.payload, "hook_event_name": "afterAgentResponse", "text": "Synthetic answer"}, deadline=time.monotonic() + 1.5))
        source = Path(telemetry_ledger.load_locator(self.repo, started["entry_id"])["session_log_path"])
        self.assertEqual(2, len(source.read_text().splitlines()))

    def test_epoch_change_between_snapshots_never_appends_to_old_source(self):
        self.consent()
        started = self.start()
        source = Path(telemetry_ledger.load_locator(self.repo, started["entry_id"])["session_log_path"])
        original = source.read_bytes()
        snapshot = consent.consent_snapshot
        calls = []
        @contextmanager
        def changing(*args, **kwargs):
            calls.append(1)
            with snapshot(*args, **kwargs) as value:
                yield value
            if len(calls) == 1:
                self.consent()
        with patch.object(consent, "consent_snapshot", changing):
            self.assertEqual(0, record_hook({**self.payload, "hook_event_name": "afterAgentResponse", "text": "Not recorded"}, deadline=time.monotonic() + 1.5))
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(2, len(calls))

    def test_revoke_between_snapshots_does_not_recreate_private_source(self):
        self.consent()
        started = self.start()
        source = Path(telemetry_ledger.load_locator(self.repo, started["entry_id"])["session_log_path"])
        snapshot = consent.consent_snapshot
        calls = []
        @contextmanager
        def revoking(*args, **kwargs):
            calls.append(1)
            with snapshot(*args, **kwargs) as value:
                yield value
            if len(calls) == 1:
                self.consent("disabled")
        with patch.object(consent, "consent_snapshot", revoking):
            self.assertEqual(0, record_hook({**self.payload, "hook_event_name": "afterAgentResponse"}, deadline=time.monotonic() + 1.5))
        self.assertFalse(source.exists())

    @unittest.skipIf(os.name == "nt", "POSIX native bootstrap and lock fault injection")
    def test_native_bootstrap_flushes_and_exits_while_consent_writer_stays_locked(self):
        import fcntl
        import select
        self.consent()
        started = self.start()
        source = Path(telemetry_ledger.load_locator(self.repo, started["entry_id"])["session_log_path"])
        original = source.read_bytes()
        synthetic_python(self.root)
        env = {**self.env, "PATH": str(self.root) + os.pathsep + os.environ["PATH"]}
        lock = self.adapter.consent_cache_path().with_suffix(".json.lock")
        with lock.open("r+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            for event, expected in (("beforeSubmitPrompt", {"continue": True}), ("subagentStart", {"permission": "allow"})):
                begin = time.monotonic()
                process = subprocess.Popen(["sh", str(ROOT / "hosts/cursor/hook_bootstrap.sh"), event], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    process.stdin.write(json.dumps({**self.payload, "hook_event_name": event}))
                    process.stdin.close()
                    process.stdin = None
                    ready, _, _ = select.select([process.stdout], [], [], 1.0)
                    self.assertTrue(ready, "permission was buffered behind consent lock")
                    response = json.loads(process.stdout.readline())
                    if event == "beforeSubmitPrompt":
                        context = response.pop("additional_context")
                        self.assertIn("HELIXHARNESS_CURSOR_CONVERSATION_ID=" + self.payload["conversation_id"], context)
                        self.assertLessEqual(len(context.encode()), 8192)
                    self.assertEqual(expected, response)
                    remaining, stderr = process.communicate(timeout=2.5)
                    self.assertEqual("", remaining)
                    self.assertEqual(0, process.returncode, stderr)
                    self.assertLess(time.monotonic() - begin, 3.0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
        self.assertEqual(original, source.read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX journal contention")
    def test_journal_contention_consumes_remaining_budget(self):
        self.consent()
        self.start()
        begin = time.monotonic()
        with _journal_lock(self.repo):
            with self.assertRaises(ValueError):
                record_hook({**self.payload, "hook_event_name": "afterAgentResponse"}, deadline=begin + 0.15)
        self.assertLess(time.monotonic() - begin, 0.5)

    def test_multiple_workspaces_do_not_reset_shared_budget(self):
        payload = {**self.payload, "hook_event_name": "afterAgentResponse", "workspace_roots": [str(self.root / str(i)) for i in range(20)]}
        payload.pop("cwd")
        calls = []
        def slow_root(value, *, deadline):
            calls.append(deadline)
            time.sleep(min(0.04, max(0.0, deadline - time.monotonic())))
            return Path(value)
        begin = time.monotonic()
        with patch.object(consent, "_repo_root", slow_root), self.assertRaises(ValueError):
            record_hook(payload, deadline=begin + 0.1)
        self.assertLess(time.monotonic() - begin, 0.4)
        self.assertLess(len(calls), 5)
        self.assertEqual(1, len(set(calls)))

    def test_cleanup_skips_newly_enabled_authorization(self):
        self.consent()
        self.start()
        worker = load("cleanup_worker")
        with patch.object(metrics, "purge_private_states") as purge, patch.object(telemetry_async, "remove_scheduler") as remove:
            self.assertFalse(worker.cleanup_repository(self.repo))
            purge.assert_not_called()
            remove.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX cross-thread independent lock descriptors")
    def test_cleanup_holds_authorization_until_deletion_finishes(self):
        self.consent("disabled")
        worker = load("cleanup_worker")
        cache = self.adapter.consent_cache_path()
        entered = threading.Event()
        attempted = threading.Event()
        enabled = threading.Event()
        errors = []
        def enable():
            entered.wait(2)
            attempted.set()
            try:
                telemetry_consent.set_mode(self.repo, "cursor", cache, "project", "enabled", consent_source="explicit-command", user_confirmed=True)
                enabled.set()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=enable)
        thread.start()
        def purge(*args, **kwargs):
            self.assertEqual({"strict_scope": True}, kwargs)
            entered.set()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(enabled.wait(0.1), "enable writer passed the cleanup snapshot")
        try:
            with patch.object(metrics, "purge_private_states", side_effect=purge), patch.object(telemetry_async, "remove_scheduler") as remove, patch.object(telemetry_async, "ensure_runtime_ready") as install:
                self.assertTrue(worker.cleanup_repository(self.repo))
                remove.assert_not_called()
                install.assert_not_called()
        finally:
            entered.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(enabled.is_set())

    def test_session_end_returns_before_busy_cleanup_finishes(self):
        self.consent("disabled")
        worker = load("cleanup_worker")
        entry = load("hook_entry")
        processes = []
        original_popen = subprocess.Popen
        def tracking(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process
        with worker.cleanup_lease(self.repo), patch.object(subprocess, "Popen", tracking), patch.object(sys, "stdin", io.StringIO(json.dumps({**self.payload, "hook_event_name": "sessionEnd"}))):
            begin = time.monotonic()
            self.assertEqual(0, entry.main())
            self.assertLess(time.monotonic() - begin, 0.5)
        for process in processes:
            self.assertEqual(0, process.wait(timeout=3))


if __name__ == "__main__":
    unittest.main()
