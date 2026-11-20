"""Codex command/lifecycle simulations using isolated CLI subprocesses.

Only synthetic rollouts are created and read. The scheduler is disabled and
collection stops at the local outbox; no model, native client or HTTP is used.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
THREAD_ONE = "0195f651-0000-7000-8000-000000000001"
THREAD_TWO = "0195f651-0000-7000-8000-000000000002"


class CodexLifecycleScenariosTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="helix-codex-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "synthetic application 中文"
        self.repo.mkdir()
        self.codex = self.root / "codex"
        self.telemetry = self.root / "telemetry"
        self.telemetry.mkdir()
        (self.telemetry / "config.json").write_text('{"endpoint": ""}')
        # Allowlist avoids inheriting native session bindings or credentials.
        self.env = {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_HOME": str(self.codex),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.telemetry),
            "HELIXHARNESS_FEEDBACK_HOME": str(self.root / "feedback"),
            "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "PLUGIN_ROOT": str(PLUGIN),
        }
        self.run_process(["git", "init", "-q", str(self.repo)])
        self.run_process(["git", "-C", str(self.repo), "config", "user.name", "Synthetic Tester"])
        self.run_process(["git", "-C", str(self.repo), "config", "user.email", "tester@example.test"])

    def run_process(self, args, *, env=None, code=0, payload=None):
        result = subprocess.run(
            list(map(str, args)), cwd=self.repo, env=self.env if env is None else env,
            input=payload, capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def cli(self, script, *args, env=None, code=0):
        result = self.run_process([sys.executable, PLUGIN / "scripts" / script, *args], env=env, code=code)
        return json.loads(result.stdout)

    def config(self, *args, env=None, code=0):
        base = ("user_config.py", "--repo-root", self.repo, "--platform", "codex")
        result = self.cli(*base, *args, env=env, code=code)
        if result.get("notice_required"):
            self.assertTrue(result["read_only"])
            self.assertFalse(result["change"]["updated"])
            result = self.cli(*base, *args, "--telemetry-notice-shown", result["notice"]["sha256"], env=env, code=code)
        return result

    def consent(self, mode="enabled"):
        return self.config("--telemetry", mode, "--scope", "project", "--user-confirmed")

    def metrics(self, action, *, feature="demo", stage="apply", env=None, status="completed"):
        tail = ("--trigger", "user_command") if action == "start" else ("--status", status)
        return self.cli("metrics.py", "--repo-root", self.repo, "--platform", "codex",
                        action, "--feature", feature, "--stage", stage, *tail, env=env)

    def hook(self, event, *, env=None, **payload):
        hooks = json.loads((PLUGIN / "hosts/codex/hooks.json").read_text())["hooks"]
        command = hooks[event][0]["hooks"][0]["command"]
        return self.run_process(["sh", "-c", command], env=env,
                                payload=json.dumps({"hook_event_name": event, **payload}))

    def files(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def ledger(self, feature="demo"):
        return json.loads((self.repo / f"docs/eval/{feature}/eval-log.json").read_text())

    def private(self, entry_id, kind="locators"):
        return self.repo / ".helixharness/telemetry-v2" / kind / f"{entry_id}.json"

    @staticmethod
    def timestamp(offset=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(timespec="milliseconds")

    def rollout(self, thread=THREAD_ONE, *, cwd=None):
        stamp = time.localtime(int(thread.replace("-", "")[:12], 16) / 1000)
        path = self.codex / "sessions" / time.strftime("%Y/%m/%d", stamp) / (
            f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', stamp)}-{thread}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "timestamp": self.timestamp(-2),
                                    "payload": {"id": thread, "cwd": str(cwd or self.repo)}}) + "\n")
        return path

    def append(self, path, *events):
        with path.open("a") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def token_event(self, inputs, outputs, cached, *, offset=0):
        return {"timestamp": self.timestamp(offset), "type": "event_msg", "payload": {
            "type": "token_count", "model": "synthetic-codex-model", "info": {
                "total_token_usage": {"input_tokens": inputs, "output_tokens": outputs,
                                      "cached_input_tokens": cached}}}}

    def enable_local_collection(self):
        self.config("--report-endpoint", "https://telemetry.example.test/report", "--user-confirmed")

    def test_status_and_config_read_only_with_active_feature_and_disabled_metrics(self):
        feature = self.repo / "docs/specs/active/demo"
        feature.mkdir(parents=True)
        (feature / "spec.md").write_text(
            "---\nfeature: demo\nexecution_scope: single\nstatus: ready\n"
            "testing_seam_confirmed: true\n---\nSynthetic requirement.\n")
        (feature / "tasks.md").write_text(
            "---\nfeature: demo\nstatus: ready\n---\n"
            "| ID | Task | Blocked By | Status |\n| --- | --- | --- | --- |\n"
            "| T-01 | Synthetic change | — | pending |\n")
        before = self.files()
        config = self.config()
        status = self.cli("project_status.py", "--repo-root", self.repo)
        self.assertTrue(config["read_only"])
        self.assertEqual(config["telemetry"]["effective_mode"], "disabled")
        self.assertEqual(status["features"][0]["next_command"], "/helix:apply demo")
        self.assertEqual(before, self.files())
        self.assertEqual(self.metrics("start")["reason"], "telemetry_disabled")
        self.assertFalse((self.repo / "docs/eval").exists())
        self.assertFalse((self.root / "claude").exists())
        self.assertFalse((self.root / "cursor").exists())

    def test_explicit_consent_records_three_stages_without_endpoint_or_session(self):
        before = self.files()
        rejected = self.config("--telemetry", "enabled", "--scope", "project", code=2)
        self.assertIn("explicit user", rejected["error"])
        self.assertEqual(before, self.files())
        self.assertEqual(self.consent()["telemetry"]["effective_mode"], "enabled")
        for stage in ("propose", "apply", "archive"):
            started = self.metrics("start", stage=stage)
            finished = self.metrics("finish", stage=stage)
            self.assertEqual(started["entry_id"], finished["entry_id"])
            self.assertEqual(finished["stage_status"], "completed")
        ledger = self.ledger()
        self.assertEqual([e["command"] for e in ledger["entries"]], ["propose", "apply", "archive"])
        self.assertTrue(all(e["platform"] == "codex" and e["session_hash"] == "session-unavailable"
                            for e in ledger["entries"]))
        self.assertFalse((self.telemetry / "runtime").exists())
        self.assertFalse(list(self.telemetry.glob("outbox/*")))
        for entry in ledger["entries"]:
            self.assertIsNone(json.loads(self.private(entry["entry_id"]).read_text())["session_log_path"])

    def test_bound_rollout_stage_collects_exact_usage_and_queues_locally(self):
        self.consent()
        source = self.rollout()
        self.append(source, self.token_event(100, 20, 40, offset=-1))
        env = {**self.env, "CODEX_THREAD_ID": THREAD_ONE,
               "CLAUDE_SESSION_ID": "synthetic-other-host", "HELIXHARNESS_PLATFORM": "cursor"}
        started = self.metrics("start", env=env)
        locator = json.loads(self.private(started["entry_id"]).read_text())
        self.assertEqual(locator["raw_session_id"], THREAD_ONE)
        self.assertEqual(locator["session_log_path"], str(source))
        self.append(source,
                    {"timestamp": self.timestamp(), "type": "response_item", "payload": {
                        "type": "function_call", "name": "read_file", "call_id": "synthetic-read",
                        "arguments": '{"path":"docs/knowledge/index.md"}'}},
                    {"timestamp": self.timestamp(), "type": "response_item", "payload": {
                        "type": "function_call_output", "call_id": "synthetic-read", "output": "synthetic data"}},
                    self.token_event(130, 27, 50))
        finished = self.metrics("finish", env=env)
        # A later turn must not become part of this completed stage.
        self.append(source, self.token_event(9999, 9999, 9999, offset=1))
        self.enable_local_collection()
        collected = self.cli("telemetry_collector.py", "--repo-root", self.repo)
        self.assertEqual((collected["collected"], collected["failed"]), (1, 0))
        paths = list((self.telemetry / "outbox").glob("*.json.gz"))
        self.assertEqual(len(paths), 1)
        envelope = json.loads(gzip.decompress(paths[0].read_bytes()))
        supplement = envelope["supplement"]
        self.assertEqual(supplement["entry_id"], finished["entry_id"])
        self.assertEqual(supplement["platform"], "codex")
        self.assertEqual(supplement["usage"]["input_tokens"], 30)
        self.assertEqual(supplement["usage"]["output_tokens"], 7)
        self.assertEqual(supplement["usage"]["cached_input_tokens"], 10)
        self.assertEqual(supplement["execution"]["context_reads"]["knowledge"], 1)
        self.assertEqual(envelope["attempt_count"], 0)
        again = self.cli("telemetry_collector.py", "--repo-root", self.repo)
        self.assertEqual(again["collected"], 0)
        self.assertEqual(list((self.telemetry / "outbox").glob("*.json.gz")), paths)
        public = json.dumps(self.ledger())
        self.assertNotIn(THREAD_ONE, public)
        self.assertNotIn(str(source), public)
        self.assertFalse((self.root / "claude").exists())
        self.assertFalse((self.root / "cursor").exists())

    def test_foreign_or_missing_rollout_is_not_guessed_from_other_host(self):
        self.consent()
        other_repo = self.root / "other synthetic repo"
        other_repo.mkdir()
        source = self.rollout(cwd=other_repo)
        original_source = source.read_bytes()
        env = {**self.env, "CODEX_THREAD_ID": THREAD_ONE,
               "CLAUDE_SESSION_ID": "synthetic-other-host", "HELIXHARNESS_CURSOR_CONVERSATION_ID": "other"}
        for feature, session_id in (("wrong-repo", THREAD_ONE), ("no-source", THREAD_TWO)):
            with self.subTest(feature=feature):
                selected = {**env, "CODEX_THREAD_ID": session_id}
                started = self.metrics("start", feature=feature, env=selected)
                self.metrics("finish", feature=feature, env=selected)
                locator = json.loads(self.private(started["entry_id"]).read_text())
                self.assertEqual(locator["raw_session_id"], "unknown")
                self.assertIsNone(locator["session_log_path"])
                self.assertEqual(self.ledger(feature)["entries"][0]["session_hash"], "session-unavailable")
        self.enable_local_collection()
        collected = self.cli("telemetry_collector.py", "--repo-root", self.repo)
        self.assertEqual((collected["collected"], collected["skipped"], collected["failed"]), (0, 2, 0))
        self.assertFalse(list((self.telemetry / "outbox").glob("*.json.gz")))
        self.assertEqual(source.read_bytes(), original_source)

    def test_two_codex_sessions_finish_only_their_own_attempt_and_retry_idempotently(self):
        self.consent()
        first_env = {**self.env, "CODEX_THREAD_ID": THREAD_ONE}
        second_env = {**self.env, "CODEX_THREAD_ID": THREAD_TWO}
        self.rollout(THREAD_ONE)
        self.rollout(THREAD_TWO)
        first = self.metrics("start", env=first_env)
        second = self.metrics("start", env=second_env)
        self.assertNotEqual(first["entry_id"], second["entry_id"])
        missing = self.metrics("finish")
        self.assertEqual(missing["reason"], "stage_state_ambiguous")
        self.assertEqual(self.metrics("finish", env=second_env)["entry_id"], second["entry_id"])
        entries = {entry["entry_id"]: entry for entry in self.ledger()["entries"]}
        self.assertEqual(entries[first["entry_id"]]["status"], "in_progress")
        self.assertEqual(entries[second["entry_id"]]["status"], "completed")
        repeated = self.metrics("finish", env=second_env)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["entry_id"], second["entry_id"])
        self.assertEqual(self.metrics("finish", env=first_env)["entry_id"], first["entry_id"])
        self.assertEqual(len(self.ledger()["entries"]), 2)

    def test_session_end_does_not_complete_stage_and_revocation_preserves_public_ledger(self):
        self.consent()
        source = self.rollout()
        env = {**self.env, "CODEX_THREAD_ID": THREAD_ONE}
        started = self.metrics("start", env=env)
        before = self.files()
        result = self.hook("SessionEnd", env=env, cwd=str(self.repo))
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.ledger()["entries"][0]["status"], "in_progress")
        public_path = self.repo / "docs/eval/demo/eval-log.json"
        public = public_path.read_bytes()
        source_before = source.read_bytes()
        self.assertEqual(self.consent("disabled")["telemetry"]["effective_mode"], "disabled")
        self.assertFalse(self.private(started["entry_id"]).exists())
        self.assertFalse(self.private(started["entry_id"], "stages").exists())
        self.assertEqual(self.metrics("finish", env=env)["reason"], "telemetry_disabled")
        self.hook("SessionEnd", env=env, cwd=str(self.repo))
        self.assertEqual(public_path.read_bytes(), public)
        self.assertEqual(source.read_bytes(), source_before)

    def test_native_start_hook_binds_codex_and_invalid_events_are_quiet(self):
        env = {**self.env, "CODEX_THREAD_ID": THREAD_ONE, "CLAUDE_SESSION_ID": "foreign-synthetic",
               "CLAUDE_PLUGIN_ROOT": str(self.root / "foreign-plugin"), "HELIXHARNESS_PLATFORM": "cursor"}
        result = self.hook("SessionStart", env=env, platform="cursor", session_id="synthetic")
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        binding = json.loads(context.split(": ", 1)[1].split("\n", 1)[0])
        self.assertEqual(binding["canonical_host_id"], "codex")
        self.assertEqual(binding["plugin_root"], str(PLUGIN))
        self.assertNotIn("foreign-plugin", context)
        before = self.files()
        for payload in ("invalid", "[]", '{"hook_event_name":"Stop"}', '{}'):
            result = self.run_process([sys.executable, PLUGIN / "hosts/codex/hook_entry.py"],
                                      env=env, payload=payload)
            self.assertEqual(result.stdout, "")
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
