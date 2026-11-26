"""Synthetic OpenCode stage telemetry: explicit identity, consent, no transcripts."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metrics
import telemetry_consent
import telemetry_collector
from telemetry_hosts import get_host_adapter
from telemetry_hosts.opencode import OpenCodeAdapter
from telemetry_session import SessionContextUnavailable


class OpenCodeTelemetryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "synthetic repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        env = patch.dict(os.environ, {
            "HELIXHARNESS_OPENCODE_HOME": str(self.root / "host"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.root / "telemetry"),
            "HELIXHARNESS_SESSION_ID": "ses_synthetic1",
            "HELIXHARNESS_SESSION_CWD": str(self.repo),
            "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        self.adapter = OpenCodeAdapter()
        for name, result in (("ensure_runtime_ready", {}), ("wake_detached", False)):
            stub = patch.object(metrics.telemetry_async, name, return_value=result)
            stub.start()
            self.addCleanup(stub.stop)

    def enable(self):
        telemetry_consent.set_mode(self.repo, "opencode", self.adapter.consent_cache_path(),
                                   "project", "enabled", consent_source="explicit-command", user_confirmed=True)

    def test_registry_and_explicit_session_binding(self):
        self.assertIsInstance(get_host_adapter("opencode"), OpenCodeAdapter)
        self.assertEqual(self.adapter.current_session_context(self.repo),
                         {"session_id": "ses_synthetic1", "session_cwd": str(self.repo)})

    def test_missing_foreign_or_other_host_identity_fails_closed(self):
        for values in ({"HELIXHARNESS_SESSION_ID": ""},
                       {"HELIXHARNESS_SESSION_CWD": str(self.root)},
                       {"HELIXHARNESS_SESSION_ID": "thread_other_host"},
                       {"HELIXHARNESS_SESSION_CWD": "."}):
            with self.subTest(values=values), patch.dict(os.environ, values):
                with self.assertRaises(SessionContextUnavailable):
                    self.adapter.current_session_context(self.repo)

    def test_disabled_does_not_create_ledger(self):
        # Revocation/disabled cleanup must not touch another host's private state.
        other = self.repo / ".helixharness/telemetry-v2/lite-active/demo/propose.json"
        other.parent.mkdir(parents=True)
        original = '{"version":2,"active":{"foreign":{"platform":"cursor","entry_id":"synthetic_foreign"}}}'
        other.write_text(original)
        result = metrics.start(self.repo, "opencode", "demo", "propose", "user_command")
        self.assertEqual(result["reason"], "telemetry_disabled")
        self.assertFalse((self.repo / "docs/eval").exists())
        self.assertEqual(other.read_text(), original)

    def test_enabled_records_and_finishes_without_session_source(self):
        self.enable()
        started = metrics.start(self.repo, "opencode", "demo", "propose", "user_command")
        self.assertEqual(started["status"], "started", started)
        finished = metrics.finish(self.repo, "opencode", "demo", "propose", "completed", None, {})
        self.assertEqual(finished["status"], "recorded", finished)
        for path in self.repo.rglob("*.json"):
            self.assertNotIn('"session_log_path": "', path.read_text())

    def test_invalid_identity_skips_even_after_consent(self):
        self.enable()
        with patch.dict(os.environ, {"HELIXHARNESS_SESSION_ID": ""}):
            result = metrics.start(self.repo, "opencode", "demo", "apply", "user_command")
        self.assertEqual(result["status"], "skipped", result)
        self.assertFalse((self.repo / "docs/eval").exists())

    def test_arbitrary_sources_never_read(self):
        self.assertIsNone(self.adapter.validate_session_source(self.root / "anything.jsonl", self.repo))
        with self.assertRaises(SessionContextUnavailable):
            self.adapter.collect_supplement_window(self.root / "missing", started_at="", ended_at="")

    def test_stage_only_supplement_and_archived_seal_keep_unknown_usage_unavailable(self):
        self.enable()
        started = metrics.start(self.repo, "opencode", "demo", "propose", "user_command")
        metrics.finish(self.repo, "opencode", "demo", "propose", "completed", None, {})
        home = self.root / "telemetry"
        supplement = telemetry_collector.collect_entry(self.repo, started["entry_id"], home=home,
                                                       endpoint="http://127.0.0.1:9/synthetic")
        self.assertEqual(supplement["usage"], {})
        self.assertEqual(supplement["execution"], {})
        self.assertEqual(supplement["evidence"]["status"], "unavailable")
        self.assertEqual(supplement["evidence"]["reason_code"], "host_transcript_not_collected")
        # 平台对每条 supplement 校验 evidence.schema；缺了它会被 invalid_evidence 拒收。
        self.assertEqual(supplement["evidence"]["schema"], "helixharness-evolution-evidence/1.0")
        archived = metrics.start(self.repo, "opencode", "demo", "archive", "user_command")
        metrics.finish(self.repo, "opencode", "demo", "archive", "completed", None, {})
        final = telemetry_collector.collect_entry(self.repo, archived["entry_id"], home=home,
                                                  endpoint="http://127.0.0.1:9/synthetic")
        self.assertEqual(final["usage"], {})
        self.assertEqual(final["platform"], "opencode")
        self.assertEqual(final["evidence"], supplement["evidence"])
