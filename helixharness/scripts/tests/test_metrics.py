#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

import telemetry_consent


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = PLUGIN_ROOT / "scripts" / "metrics.py"
CONSENT = PLUGIN_ROOT / "scripts" / "telemetry_consent.py"


class LiteMetricsCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Lite Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "lite@example.test"],
            check=True,
        )
        self.env = os.environ.copy()
        self.env["CODEX_HOME"] = str(self.root / "codex-home")
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "claude-home")
        self.env["HELIXHARNESS_TELEMETRY_HOME"] = str(self.root / "telemetry-home")
        self.env["HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER"] = "1"
        self.configure_endpoint("http://127.0.0.1:9/report")

    def configure_endpoint(self, endpoint: str) -> None:
        home = Path(self.env["HELIXHARNESS_TELEMETRY_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps({"endpoint": endpoint}))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_metrics(self, *args: str, platform: str = "codex") -> dict:
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                "--platform",
                platform,
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return json.loads(completed.stdout)

    def run_consent(self, *args: str, platform: str = "codex") -> dict:
        completed = subprocess.run(
            [
                "python3",
                str(CONSENT),
                "--repo-root",
                str(self.repo),
                "--platform",
                platform,
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        return json.loads(completed.stdout)

    def enable(self, platform: str = "codex") -> None:
        subprocess.run(
            [
                "python3",
                str(CONSENT),
                "--repo-root",
                str(self.repo),
                "--platform",
                platform,
                "set",
                "--scope",
                "project",
                "--mode",
                "enabled",
                "--source",
                "explicit-command",
                "--user-confirmed",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def disable(self) -> None:
        subprocess.run(
            [
                "python3",
                str(CONSENT),
                "--repo-root",
                str(self.repo),
                "--platform",
                "codex",
                "set",
                "--scope",
                "project",
                "--mode",
                "disabled",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_missing_or_invalid_endpoint_does_not_suppress_consent_prompt(self) -> None:
        for platform in ("codex", "claude-code"):
            for endpoint, reason in (("", "endpoint_unconfigured"), ("bad-url", "endpoint_invalid"),
                                     ("http://127.0.0.1:9/report", None)):
                with self.subTest(platform=platform, endpoint=endpoint):
                    self.configure_endpoint(endpoint)
                    result = self.run_consent("resolve", platform=platform)
                    self.assertTrue(result["prompt_due"])
                    self.assertEqual("enable", result["prompt_kind"])
                    self.assertEqual(["enable_project", "enable_user", "keep_disabled"],
                                     [action["id"] for action in result["allowed_actions"]])
                    self.assertEqual(reason, result.get("reason"))
                    self.assertEqual("disabled", result["effective_mode"])
                    self.assertIsNone(result["last_prompted_at"])
                    self.assertFalse(Path(result["cache_path"]).exists())

    def test_interactive_consent_without_endpoint_records_completed_local_stages(self) -> None:
        self.configure_endpoint("")
        for platform in ("codex", "claude-code"):
            with self.subTest(platform=platform):
                selected = self.run_consent("choose", "--action", "enable_project",
                                            "--user-confirmed", platform=platform)
                self.assertEqual("enabled", selected["effective_mode"])
                self.assertFalse(selected["prompt_due"])
                feature = f"offline-{platform}"
                for stage in ("propose", "apply", "archive"):
                    started = self.run_metrics("start", "--feature", feature, "--stage", stage,
                                               "--trigger", "user_command", platform=platform)
                    self.assertEqual("started", started["status"])
                    finished = self.run_metrics("finish", "--feature", feature, "--stage", stage,
                                                "--status", "completed", platform=platform)
                    self.assertEqual("recorded", finished["status"])
                self.assertIn("eval-log", finished["notice"])
                self.assertNotIn("异步", finished["notice"])
                self.assertNotIn("地址", finished["notice"])
                ledger = json.loads((self.repo / f"docs/eval/{feature}/eval-log.json").read_text())
                self.assertEqual(["completed"] * 3, [entry["status"] for entry in ledger["entries"]])
        home = Path(self.env["HELIXHARNESS_TELEMETRY_HOME"])
        self.assertFalse((home / "runtime").exists())
        self.assertFalse(list((home / "outbox").glob("*.json")))

    def test_missing_endpoint_keeps_enable_and_renew_reminder_schedule(self) -> None:
        self.configure_endpoint("")
        for platform in ("codex", "claude-code"):
            with self.subTest(platform=platform), mock.patch.dict(os.environ, self.env):
                disabled = self.run_consent("choose", "--action", "keep_disabled", "--user-confirmed",
                                            platform=platform)
                cache = Path(disabled["cache_path"])
                saved = cache.read_bytes()
                next_prompt = datetime.fromisoformat(disabled["next_prompt_after"].replace("Z", "+00:00"))
                last_prompt = datetime.fromisoformat(disabled["last_prompted_at"].replace("Z", "+00:00"))
                self.assertEqual(timedelta(days=30), next_prompt - last_prompt)
                before = telemetry_consent.resolve(self.repo, platform, cache, next_prompt - timedelta(seconds=1))
                self.assertFalse(before["prompt_due"])
                due = telemetry_consent.resolve(self.repo, platform, cache, next_prompt)
                self.assertTrue(due["prompt_due"])
                self.assertEqual(saved, cache.read_bytes())
                enabled = telemetry_consent.apply_interactive_choice(
                    self.repo, platform, cache, "enable_project", True, next_prompt)
                renewal = datetime.fromisoformat(enabled["next_prompt_after"].replace("Z", "+00:00"))
                self.assertEqual(timedelta(days=180), renewal - next_prompt)
                due = telemetry_consent.resolve(self.repo, platform, cache, renewal)
                self.assertTrue(due["prompt_due"])
                self.assertEqual("renew", due["prompt_kind"])
                renewed = telemetry_consent.apply_interactive_choice(
                    self.repo, platform, cache, "keep_enabled", True, renewal)
                self.assertFalse(renewed["prompt_due"])
                self.assertEqual(enabled["consent_epoch"], renewed["consent_epoch"])

    def test_missing_endpoint_preserves_consent_and_local_ledger(self) -> None:
        self.enable()
        before = self.run_consent("resolve")
        cache = Path(before["cache_path"])
        saved = cache.read_bytes()
        self.configure_endpoint("")
        result = self.run_consent("resolve")
        self.assertFalse(result["prompt_due"])
        self.assertEqual("enabled", result["effective_mode"])
        self.assertEqual(before["consent_epoch"], result["consent_epoch"])
        self.assertEqual(saved, cache.read_bytes())
        started = self.run_metrics("start", "--feature", "offline", "--stage", "propose", "--trigger", "user_command")
        self.assertEqual("started", started["status"])
        self.assertTrue((self.repo / "docs/eval/offline/eval-log.json").is_file())
        self.assertFalse((Path(self.env["HELIXHARNESS_TELEMETRY_HOME"]) / "runtime").exists())

    def test_disabled_consent_skips_without_creating_state_or_public_data(self) -> None:
        for platform in ("codex", "claude-code"):
            for explicit in (False, True):
                if explicit:
                    self.run_consent("choose", "--action", "keep_disabled", "--user-confirmed", platform=platform)
                for endpoint in ("", "bad-url", "http://127.0.0.1:9/report"):
                    with self.subTest(platform=platform, explicit=explicit, endpoint=endpoint):
                        self.configure_endpoint(endpoint)
                        result = self.run_metrics("start", "--feature", "demo", "--stage", "propose",
                                                  "--trigger", "user_command", platform=platform)
                        self.assertEqual("skipped", result["status"])
                        self.assertEqual("telemetry_disabled", result["reason"])
        self.assertFalse((self.repo / "docs" / "eval").exists())
        self.assertFalse(list((self.repo / ".helixharness" / "telemetry-v2").rglob("*.json")))
        self.assertFalse(list((Path(self.env["HELIXHARNESS_TELEMETRY_HOME"]) / "outbox").glob("*.json")))

    def test_shared_interactive_consent_enables_lite_recorder(self) -> None:
        unresolved = self.run_consent("resolve")
        self.assertTrue(unresolved["prompt_due"])
        self.assertIn(
            "enable_project",
            [action["id"] for action in unresolved["allowed_actions"]],
        )

        selected = self.run_consent(
            "choose",
            "--action",
            "enable_project",
            "--user-confirmed",
        )
        self.assertEqual("enabled", selected["effective_mode"])

        started = self.run_metrics(
            "start",
            "--feature",
            "interactive-consent-demo",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
        )
        self.assertEqual("started", started["status"])
        self.assertTrue(
            (self.repo / "docs/eval/interactive-consent-demo/eval-log.json").is_file()
        )

    @unittest.skipIf(os.name == "nt", "POSIX read-only sandbox behavior")
    def test_enabled_stage_lifecycle_writes_ledger_with_read_only_host_config(self) -> None:
        self.enable()
        config_dir = Path(self.env["CODEX_HOME"]) / "helixharness"
        cache_path = config_dir / "telemetry-consent-cache.json"
        lock_path = cache_path.with_suffix(f"{cache_path.suffix}.lock")
        cache_path.chmod(0o444)
        lock_path.chmod(0o444)
        config_dir.chmod(0o555)
        try:
            started = self.run_metrics(
                "start",
                "--feature",
                "read-only-host-config",
                "--stage",
                "propose",
                "--trigger",
                "user_command",
            )
            self.assertEqual("started", started["status"])

            traced = self.run_metrics(
                "trace-event",
                "--feature",
                "read-only-host-config",
                "--stage",
                "propose",
                "--event-id",
                "lite_tasks_ready",
                "--kind",
                "gate",
                "--status",
                "passed",
            )
            self.assertEqual("recorded", traced["status"])

            finished = self.run_metrics(
                "finish",
                "--feature",
                "read-only-host-config",
                "--stage",
                "propose",
                "--status",
                "completed",
            )
            self.assertEqual("recorded", finished["status"])
            ledger = json.loads(
                (
                    self.repo
                    / "docs/eval/read-only-host-config/eval-log.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual("completed", ledger["entries"][0]["status"])
        finally:
            config_dir.chmod(0o755)
            cache_path.chmod(0o600)
            lock_path.chmod(0o644)

    def test_lite_loop_has_a_distinct_telemetry_trigger(self) -> None:
        self.enable()

        started = self.run_metrics(
            "start",
            "--feature",
            "loop-demo",
            "--stage",
            "propose",
            "--trigger",
            "loop",
            "--route-reason",
            "loop_orchestrated",
        )

        self.assertEqual("started", started["status"])
        ledger = json.loads(
            (self.repo / "docs/eval/loop-demo/eval-log.json").read_text(encoding="utf-8")
        )
        self.assertEqual("loop", ledger["entries"][0]["trigger"])

    def test_session_end_cleans_up_after_user_disables_from_another_repo(self) -> None:
        self.enable("claude-code")
        started = self.run_metrics(
            "start",
            "--feature",
            "consent-cleanup",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
            platform="claude-code",
        )
        trace_path = (
            self.repo
            / ".helixharness"
            / "telemetry-v2"
            / "traces"
            / f"{started['entry_id']}.json"
        )
        self.assertTrue(trace_path.is_file())
        code = """
import pathlib
from telemetry_async import Outbox
Outbox(r'{home}').enqueue(
    {{'supplement_id': 'supp_test', 'collected_at': '2026-08-13T00:00:00Z'}},
    repo=pathlib.Path(r'{repo}'),
    platform='claude-code',
    consent_epoch='cep_0123456789abcdef01234567',
)
""".format(home=self.root / "telemetry-home", repo=self.repo)
        env = dict(self.env)
        env["PYTHONPATH"] = str(PLUGIN_ROOT / "scripts")
        completed = subprocess.run(["python3", "-c", code], capture_output=True, text=True, env=env)
        self.assertEqual(0, completed.returncode, completed.stderr)
        env.pop("CODEX_THREAD_ID", None)
        env.pop("HELIXHARNESS_PLATFORM", None)
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
        hooks = json.loads((PLUGIN_ROOT / "hosts/claude-code/hooks.json").read_text())
        command = hooks["hooks"]["SessionEnd"][0]["hooks"][0]["command"]

        def end_session() -> subprocess.CompletedProcess:
            return subprocess.run(
                ["sh", "-c", command],
                input=json.dumps({"hook_event_name": "SessionEnd", "cwd": str(self.repo)}),
                capture_output=True, text=True, env=env, timeout=10,
            )

        # Enabled telemetry survives the real configured hook invocation.
        completed = end_session()
        self.assertEqual(0, completed.returncode, completed.stderr)
        outbox = self.root / "telemetry-home" / "outbox"
        self.assertTrue(list(outbox.glob("*.json.gz")))
        self.assertTrue(trace_path.is_file())

        # A user-wide opt-out elsewhere leaves this project's cleanup to SessionEnd.
        other_repo = self.root / "other-repo"
        other_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
        subprocess.run(
            ["python3", str(CONSENT), "--repo-root", str(other_repo),
             "--platform", "claude-code", "set", "--scope", "user", "--mode", "disabled"],
            capture_output=True, text=True, env=env, check=True,
        )
        self.assertTrue(trace_path.is_file())
        self.assertTrue(list(outbox.glob("*.json.gz")))
        completed = end_session()
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], list(outbox.glob("*.json.gz")))
        self.assertFalse(trace_path.exists())
        self.assertTrue((self.repo / "docs/eval/consent-cleanup/eval-log.json").is_file())

    def test_unsupported_host_skips_without_creating_state_or_public_data(self) -> None:
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                "--platform",
                "cursor",
                "start",
                "--feature",
                "demo",
                "--stage",
                "propose",
                "--trigger",
                "user_command",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )

        result = json.loads(completed.stdout)
        self.assertEqual("skipped", result["status"])
        self.assertEqual("unsupported_host", result["reason"])
        self.assertFalse((self.repo / "docs" / "eval").exists())


    def test_retired_address_recorder_is_rejected_without_writing_ledger(self) -> None:
        completed = subprocess.run(
            ["python3", str(SCRIPT), "--repo-root", str(self.repo), "--platform", "codex",
             "set-requirement-url", "--feature", "demo", "--requirement-url", "https://tracker.example/1"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("invalid choice", completed.stderr)
        self.assertFalse((self.repo / "docs/eval").exists())

    def test_invalid_feature_cannot_escape_private_state_root(self) -> None:
        self.enable()
        escaped = self.root / "escaped" / "apply.json"
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                "--platform",
                "codex",
                "finish",
                "--feature",
                str(escaped.parent),
                "--stage",
                "apply",
                "--status",
                "completed",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(2, completed.returncode)
        self.assertFalse(escaped.exists())

    def test_enabled_three_stage_flow_writes_unified_fixed_ledger(self) -> None:
        self.enable()

        for stage in ("propose", "apply", "archive"):
            started = self.run_metrics(
                "start",
                "--feature",
                "demo",
                "--stage",
                stage,
                "--trigger",
                "bug-fix",
            )
            self.assertEqual("started", started["status"])
            self.assertRegex(started["entry_id"], r"^entry-[0-9a-f]{16}$")

            finish_args = [
                "finish",
                "--feature",
                "demo",
                "--stage",
                stage,
                "--status",
                "completed",
            ]
            if stage == "apply":
                finish_args.extend(
                    [
                        "--metrics-json",
                        json.dumps(
                            {
                                "verification": "passed",
                                "standards_verdict": "APPROVE",
                                "spec_verdict": "APPROVE",
                                "must_fix_rounds": 1,
                            }
                        ),
                    ]
                )
            finished = self.run_metrics(*finish_args)
            self.assertEqual("recorded", finished["status"])
            self.assertGreaterEqual(finished["recorder_duration_ms"], 0)

        path = self.repo / "docs" / "eval" / "demo" / "eval-log.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("helixharness-eval-ledger/1.0", data["schema"])
        self.assertEqual("demo", data["feature"])
        self.assertNotIn("url", data["requirement"])
        self.assertEqual("archived", data["status"])
        self.assertEqual(["propose", "apply", "archive"], [item["command"] for item in data["entries"]])
        self.assertTrue(all("workflow_variant" not in item for item in data["entries"]))
        self.assertTrue(all(item["trigger"] == "bug-fix" for item in data["entries"]))
        self.assertTrue(all(item["platform"] == "codex" for item in data["entries"]))
        plugin_version = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )["version"]
        self.assertTrue(
            all(item["helixharness_version"] == plugin_version for item in data["entries"])
        )

        apply_entry = data["entries"][1]
        self.assertEqual("passed", apply_entry["quality"]["verification"])
        self.assertEqual("APPROVE", apply_entry["quality"]["standards_verdict"])
        self.assertEqual("APPROVE", apply_entry["quality"]["spec_verdict"])
        self.assertEqual(1, apply_entry["quality"]["must_fix_rounds"])

        serialized = json.dumps(data, ensure_ascii=False).lower()
        for forbidden in ("token", "tool", "friction", "transcript"):
            self.assertNotIn(forbidden, serialized)
        self.assertFalse((self.repo / "docs" / "eval" / "demo" / "lite-metrics.json").exists())

    def test_private_workflow_trace_records_route_gate_and_terminal_quality(self) -> None:
        self.enable()
        started = self.run_metrics(
            "start",
            "--feature",
            "trace-demo",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
            "--route-reason",
            "variant_resolved",
        )

        recorded = self.run_metrics(
            "trace-event",
            "--feature",
            "trace-demo",
            "--stage",
            "apply",
            "--event-id",
            "archive_preflight",
            "--kind",
            "gate",
            "--status",
            "passed",
        )
        self.assertEqual("recorded", recorded["status"])
        fallback = self.run_metrics(
            "trace-event",
            "--feature",
            "trace-demo",
            "--stage",
            "apply",
            "--event-id",
            "verification_baseline_waiver",
            "--kind",
            "fallback",
            "--taken",
            "true",
            "--reason-code",
            "user_confirmed_preexisting_baseline",
        )
        self.assertEqual("recorded", fallback["status"])

        self.run_metrics(
            "finish",
            "--feature",
            "trace-demo",
            "--stage",
            "apply",
            "--status",
            "completed",
            "--metrics-json",
            json.dumps(
                {
                    "verification": "passed",
                    "standards_verdict": "APPROVE",
                    "spec_verdict": "APPROVE",
                    "must_fix_rounds": 0,
                }
            ),
        )

        trace_path = (
            self.repo
            / ".helixharness"
            / "telemetry-v2"
            / "traces"
            / f"{started['entry_id']}.json"
        )
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual("helixharness-workflow-trace/1.0", trace["schema"])
        self.assertEqual("variant_resolved", trace["route"]["decision_code"])
        self.assertEqual("workflows/single/apply.md", trace["route"]["workflow_path"])
        self.assertEqual(
            {"router", "workflow", "recorder"},
            set(trace["component_versions"]["components"]),
        )
        self.assertTrue(
            all(
                len(component["sha256"]) == 64
                for component in trace["component_versions"]["components"].values()
            )
        )
        self.assertEqual("passed", trace["gates"][0]["status"])
        self.assertEqual(
            ["verification", "standards_review", "spec_review"],
            [gate["check_id"] for gate in trace["gates"][1:]],
        )
        self.assertEqual(
            "verification_baseline_waiver",
            trace["fallbacks"][0]["fallback_id"],
        )
        self.assertTrue(trace["fallbacks"][0]["taken"])
        self.assertEqual("completed", trace["outcome"]["status"])
        self.assertEqual("completed", trace["transitions"][-1]["to"])

        ledger = json.loads(
            (self.repo / "docs/eval/trace-demo/eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("workflow_trace", json.dumps(ledger))

        from telemetry_trace import load_for_supplement
        loaded = load_for_supplement(self.repo, started["entry_id"])
        self.assertNotEqual(loaded["status"], "unavailable")
        self.assertEqual(
            loaded["component_versions"]["components"]["workflow"]["path"],
            "workflows/single/apply.md",
        )
        # Retired workflow layouts are unsupported, never migrated or rewritten.
        trace["route"]["workflow_path"] = "commands/apply.md"
        trace["component_versions"]["components"]["workflow"]["path"] = "commands/apply.md"
        trace_path.write_text(json.dumps(trace), encoding="utf-8")
        frozen = trace_path.read_bytes()
        loaded = load_for_supplement(self.repo, started["entry_id"])
        self.assertEqual(loaded["status"], "unavailable")
        self.assertEqual(trace_path.read_bytes(), frozen)

    def test_workflow_trace_rejects_free_text_identifiers(self) -> None:
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "trace-invalid",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
        )

        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                "--platform",
                "codex",
                "trace-event",
                "--feature",
                "trace-invalid",
                "--stage",
                "propose",
                "--event-id",
                "contains free text",
                "--kind",
                "gate",
                "--status",
                "passed",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn("invalid workflow trace identifier", completed.stderr)

    def test_decision_needed_is_not_a_terminal_stage_status(self) -> None:
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "interactive-propose",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
        )

        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                "--platform",
                "codex",
                "finish",
                "--feature",
                "interactive-propose",
                "--stage",
                "propose",
                "--status",
                "decision_needed",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual(2, completed.returncode)
        ledger = json.loads(
            (self.repo / "docs/eval/interactive-propose/eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, len(ledger["entries"]))
        self.assertEqual("in_progress", ledger["entries"][0]["status"])
        self.assertEqual(1, ledger["entries"][0]["attempt"])

    def test_explicit_stage_reexecution_increments_attempt(self) -> None:
        self.enable()
        first = self.run_metrics(
            "start",
            "--feature",
            "retry-propose",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
        )
        self.run_metrics(
            "finish",
            "--feature",
            "retry-propose",
            "--stage",
            "propose",
            "--status",
            "failed",
            "--failure-code",
            "agent_failed",
        )

        second = self.run_metrics(
            "start",
            "--feature",
            "retry-propose",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
        )

        self.assertNotEqual(first["entry_id"], second["entry_id"])
        ledger = json.loads(
            (self.repo / "docs/eval/retry-propose/eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([1, 2], [entry["attempt"] for entry in ledger["entries"]])

    def test_completed_apply_recovers_same_session_after_premature_failed_finish(self) -> None:
        self.enable("codex")
        self.env["HELIXHARNESS_SESSION_ID"] = "continued-apply-session"
        failed_entry = self.run_metrics(
            "start",
            "--feature",
            "continued-apply",
            "--stage",
            "apply",
            "--trigger",
            "bug-fix",
        )
        self.run_metrics(
            "finish",
            "--feature",
            "continued-apply",
            "--stage",
            "apply",
            "--status",
            "failed",
            "--failure-code",
            "agent_failed",
        )

        completed = self.run_metrics(
            "finish",
            "--feature",
            "continued-apply",
            "--stage",
            "apply",
            "--status",
            "completed",
            "--metrics-json",
            '{"verification":"passed","standards_verdict":"APPROVE","spec_verdict":"APPROVE","must_fix_rounds":0}',
        )

        self.assertEqual("recorded", completed["status"])
        self.assertEqual("successor_attempt", completed["recovery"])
        self.assertEqual(failed_entry["entry_id"], completed["previous_entry_id"])
        self.assertNotEqual(failed_entry["entry_id"], completed["entry_id"])
        ledger = json.loads(
            (self.repo / "docs/eval/continued-apply/eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual([1, 2], [entry["attempt"] for entry in ledger["entries"]])
        self.assertEqual(
            ["failed", "completed"],
            [entry["status"] for entry in ledger["entries"]],
        )
        self.assertEqual(
            ledger["entries"][0]["ended_at"],
            ledger["entries"][1]["started_at"],
        )
        self.assertEqual("bug-fix", ledger["entries"][1]["trigger"])
        self.assertEqual(
            {
                "verification": "passed",
                "standards_verdict": "APPROVE",
                "spec_verdict": "APPROVE",
                "must_fix_rounds": 0,
            },
            ledger["entries"][1]["quality"],
        )

    def test_repeated_completed_apply_finish_is_idempotent(self) -> None:
        self.enable("codex")
        self.env["HELIXHARNESS_SESSION_ID"] = "completed-apply-session"
        started = self.run_metrics(
            "start",
            "--feature",
            "completed-apply",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )
        self.run_metrics(
            "finish",
            "--feature",
            "completed-apply",
            "--stage",
            "apply",
            "--status",
            "completed",
        )

        repeated = self.run_metrics(
            "finish",
            "--feature",
            "completed-apply",
            "--stage",
            "apply",
            "--status",
            "completed",
        )

        self.assertEqual("recorded", repeated["status"])
        self.assertEqual("idempotent_finish", repeated["recovery"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(started["entry_id"], repeated["entry_id"])
        ledger = json.loads(
            (self.repo / "docs/eval/completed-apply/eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, len(ledger["entries"]))
        self.assertEqual("completed", ledger["entries"][0]["status"])

    def test_completed_apply_never_recovers_failed_entry_from_another_session(self) -> None:
        self.enable("codex")
        self.env["HELIXHARNESS_SESSION_ID"] = "failed-apply-session"
        failed = self.run_metrics(
            "start",
            "--feature",
            "cross-session-recovery",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )
        self.run_metrics(
            "finish",
            "--feature",
            "cross-session-recovery",
            "--stage",
            "apply",
            "--status",
            "failed",
            "--failure-code",
            "agent_failed",
        )
        self.env["HELIXHARNESS_SESSION_ID"] = "different-session"

        completed = self.run_metrics(
            "finish",
            "--feature",
            "cross-session-recovery",
            "--stage",
            "apply",
            "--status",
            "completed",
        )

        self.assertEqual("skipped", completed["status"])
        self.assertEqual("stage_state_missing", completed["reason"])
        ledger = json.loads(
            (self.repo / "docs/eval/cross-session-recovery/eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, len(ledger["entries"]))
        self.assertEqual(failed["entry_id"], ledger["entries"][0]["entry_id"])
        self.assertEqual("failed", ledger["entries"][0]["status"])

    def test_completed_apply_never_recovers_failed_entry_from_an_old_consent_epoch(self) -> None:
        self.enable("codex")
        self.env["HELIXHARNESS_SESSION_ID"] = "consent-epoch-session"
        self.run_metrics(
            "start",
            "--feature",
            "consent-epoch-recovery",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )
        self.run_metrics(
            "finish",
            "--feature",
            "consent-epoch-recovery",
            "--stage",
            "apply",
            "--status",
            "failed",
            "--failure-code",
            "agent_failed",
        )
        self.disable()
        self.enable("codex")

        completed = self.run_metrics(
            "finish",
            "--feature",
            "consent-epoch-recovery",
            "--stage",
            "apply",
            "--status",
            "completed",
        )

        self.assertEqual("skipped", completed["status"])
        self.assertEqual("stage_state_missing", completed["reason"])
        ledger = json.loads(
            (self.repo / "docs/eval/consent-epoch-recovery/eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, len(ledger["entries"]))
        self.assertEqual("failed", ledger["entries"][0]["status"])

    def test_archive_finish_records_confirmed_implementation_commit(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "master"], check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "switch", "-qc", "feature/demo"], check=True)
        (self.repo / "README.md").write_text("implemented\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "implementation"], check=True)
        implementation_commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "demo-commit",
            "--stage",
            "archive",
            "--trigger",
            "user_command",
        )
        (self.repo / "archive-metadata.txt").write_text("not committed yet\n", encoding="utf-8")

        finished = self.run_metrics(
            "finish",
            "--feature",
            "demo-commit",
            "--stage",
            "archive",
            "--status",
            "completed",
            "--implementation-commit",
            implementation_commit,
            "--review-fixed-point",
            implementation_commit,
            "--review-head",
            implementation_commit,
            "--review-scope-sha256",
            "a" * 64,
            "--archive-scope-sha256",
            "b" * 64,
            "--reviewed-path",
            "README.md",
        )

        entry = json.loads(Path(finished["path"]).read_text(encoding="utf-8"))["entries"][0]
        self.assertEqual(implementation_commit, entry["checkpoint"]["implementation_commit_id"])
        self.assertEqual("commit", entry["checkpoint"]["code_resolution"])
        self.assertTrue(entry["checkpoint"]["working_tree_dirty"])
        self.assertEqual(implementation_commit, entry["checkpoint"]["review_fixed_point"])
        self.assertEqual(implementation_commit, entry["checkpoint"]["review_head"])
        self.assertEqual("a" * 64, entry["checkpoint"]["review_scope_sha256"])
        self.assertEqual("b" * 64, entry["checkpoint"]["archive_scope_sha256"])
        self.assertEqual(["README.md"], entry["checkpoint"]["reviewed_paths"])

    def test_commit_ledger_requires_archive_commit_authorization(self) -> None:
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "ledger-not-authorized",
            "--stage",
            "archive",
            "--trigger",
            "user_command",
        )
        self.run_metrics(
            "finish",
            "--feature",
            "ledger-not-authorized",
            "--stage",
            "archive",
            "--status",
            "completed",
        )

        result = self.run_metrics(
            "commit-ledger",
            "--feature",
            "ledger-not-authorized",
        )

        self.assertEqual("skipped", result["status"])
        self.assertEqual("commit_not_authorized", result["reason"])
        self.assertNotEqual(0, subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        ).returncode)

    def test_commit_ledger_creates_exact_second_commit_and_preserves_other_changes(self) -> None:
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        implementation_commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "ledger-commit",
            "--stage",
            "archive",
            "--trigger",
            "user_command",
        )
        self.run_metrics(
            "finish",
            "--feature",
            "ledger-commit",
            "--stage",
            "archive",
            "--status",
            "completed",
            "--implementation-commit",
            implementation_commit,
        )
        (self.repo / "staged.txt").write_text("keep staged\n", encoding="utf-8")
        (self.repo / "unstaged.txt").write_text("keep unstaged\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "staged.txt"], check=True)

        committed = self.run_metrics(
            "commit-ledger",
            "--feature",
            "ledger-commit",
            "--commit-authorized",
        )

        self.assertEqual("committed", committed["status"])
        self.assertNotIn("notice", committed)
        self.assertFalse(committed["pushed"])
        self.assertRegex(committed["commit_id"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            "chore(eval): record ledger-commit",
            subprocess.run(
                ["git", "-C", str(self.repo), "log", "-1", "--format=%s"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )
        self.assertEqual(
            ["docs/eval/ledger-commit/eval-log.json"],
            subprocess.run(
                ["git", "-C", str(self.repo), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines(),
        )
        self.assertEqual(
            "staged.txt",
            subprocess.run(
                ["git", "-C", str(self.repo), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )
        self.assertEqual("?? unstaged.txt", subprocess.run(
            ["git", "-C", str(self.repo), "status", "--short", "--", "unstaged.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip())

        repeated = self.run_metrics(
            "commit-ledger",
            "--feature",
            "ledger-commit",
            "--commit-authorized",
        )
        self.assertEqual("skipped", repeated["status"])
        self.assertEqual("ledger_already_committed", repeated["reason"])
        self.assertEqual(committed["commit_id"], repeated["commit_id"])
        self.assertNotIn("notice", repeated)

    def test_completed_archive_returns_hard_delivery_notice(self) -> None:
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "delivery-notice",
            "--stage",
            "archive",
            "--trigger",
            "user_command",
        )

        finished = self.run_metrics(
            "finish",
            "--feature",
            "delivery-notice",
            "--stage",
            "archive",
            "--status",
            "completed",
        )

        self.assertIn("NOTICE:lite_telemetry_delivery_required", finished["notice"])
        self.assertNotIn("地址", finished["notice"])
        self.assertIn("eval-log", finished["notice"])
        self.assertIn("push", finished["notice"])

    def test_same_stage_tracks_multiple_sessions_without_overwriting_finish_target(self) -> None:
        self.enable("codex")
        self.env["HELIXHARNESS_SESSION_ID"] = "session-one"
        first = self.run_metrics(
            "start",
            "--feature",
            "multi-session",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )
        self.env["HELIXHARNESS_SESSION_ID"] = "session-two"
        second = self.run_metrics(
            "start",
            "--feature",
            "multi-session",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )

        self.env["HELIXHARNESS_SESSION_ID"] = "session-one"
        finished_first = self.run_metrics(
            "finish",
            "--feature",
            "multi-session",
            "--stage",
            "apply",
            "--status",
            "completed",
        )
        self.assertEqual(first["entry_id"], finished_first["entry_id"])

        ledger_path = self.repo / "docs/eval/multi-session/eval-log.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(
            ["completed", "in_progress"],
            [item["status"] for item in ledger["entries"]],
        )

        self.env["HELIXHARNESS_SESSION_ID"] = "session-two"
        finished_second = self.run_metrics(
            "finish",
            "--feature",
            "multi-session",
            "--stage",
            "apply",
            "--status",
            "completed",
        )
        self.assertEqual(second["entry_id"], finished_second["entry_id"])
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(
            ["completed", "completed"],
            [item["status"] for item in ledger["entries"]],
        )

    def test_archive_recovery_can_finish_one_prior_session_after_business_move(self) -> None:
        self.enable("codex")
        self.env["HELIXHARNESS_SESSION_ID"] = "archive-before-move"
        started = self.run_metrics(
            "start",
            "--feature",
            "archive-recovery",
            "--stage",
            "archive",
            "--trigger",
            "user_command",
        )
        self.env["HELIXHARNESS_SESSION_ID"] = "archive-after-move"

        finished = self.run_metrics(
            "finish",
            "--feature",
            "archive-recovery",
            "--stage",
            "archive",
            "--status",
            "completed",
            "--recover-stage",
        )

        self.assertEqual("recorded", finished["status"])
        self.assertEqual(started["entry_id"], finished["entry_id"])

    def test_late_finish_from_unknown_session_never_finishes_another_session(self) -> None:
        self.enable("codex")
        self.env["HELIXHARNESS_SESSION_ID"] = "current-session"
        current = self.run_metrics(
            "start",
            "--feature",
            "late-finish",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )

        self.env["HELIXHARNESS_SESSION_ID"] = "old-session"
        late = self.run_metrics(
            "finish",
            "--feature",
            "late-finish",
            "--stage",
            "apply",
            "--status",
            "completed",
        )

        self.assertEqual("stage_state_missing", late["reason"])
        ledger = json.loads(
            (self.repo / "docs/eval/late-finish/eval-log.json").read_text(encoding="utf-8")
        )
        self.assertEqual(current["entry_id"], ledger["entries"][0]["entry_id"])
        self.assertEqual("in_progress", ledger["entries"][0]["status"])

    def test_concurrent_session_starts_preserve_both_active_entries(self) -> None:
        self.enable("codex")
        processes = []
        for session_id in ("concurrent-one", "concurrent-two"):
            env = dict(self.env)
            env["HELIXHARNESS_SESSION_ID"] = session_id
            processes.append(
                subprocess.Popen(
                    [
                        "python3",
                        str(SCRIPT),
                        "--repo-root",
                        str(self.repo),
                        "--platform",
                        "codex",
                        "start",
                        "--feature",
                        "concurrent-sessions",
                        "--stage",
                        "apply",
                        "--trigger",
                        "user_command",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            )
        for process in processes:
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, stderr)

        state = json.loads(
            (
                self.repo
                / ".helixharness/telemetry-v2/lite-active/concurrent-sessions/apply.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(2, len(state["active"]))
        ledger = json.loads(
            (self.repo / "docs/eval/concurrent-sessions/eval-log.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, len(ledger["entries"]))

    def test_disabling_one_host_preserves_other_host_active_state(self) -> None:
        self.enable("codex")
        self.enable("claude-code")
        self.run_metrics(
            "start",
            "--feature",
            "host-consent",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
            platform="codex",
        )
        claude = self.run_metrics(
            "start",
            "--feature",
            "host-consent",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
            platform="claude-code",
        )

        self.disable()
        skipped = self.run_metrics(
            "start",
            "--feature",
            "host-consent",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
            platform="codex",
        )
        self.assertEqual("telemetry_disabled", skipped["reason"])
        finished = self.run_metrics(
            "finish",
            "--feature",
            "host-consent",
            "--stage",
            "apply",
            "--status",
            "completed",
            platform="claude-code",
        )
        self.assertEqual(claude["entry_id"], finished["entry_id"])

    def test_same_stage_handoff_preserves_each_host_session(self) -> None:
        self.enable("codex")
        self.enable("claude-code")

        first = self.run_metrics(
            "start",
            "--feature",
            "handoff",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
            platform="codex",
        )
        second = self.run_metrics(
            "start",
            "--feature",
            "handoff",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
            platform="claude-code",
        )

        ledger = json.loads(
            (self.repo / "docs/eval/handoff/eval-log.json").read_text(encoding="utf-8")
        )
        self.assertEqual("in_progress", ledger["entries"][0]["status"])
        self.assertEqual("in_progress", ledger["entries"][1]["status"])
        self.assertNotEqual(first["entry_id"], second["entry_id"])

        codex_finish = self.run_metrics(
            "finish",
            "--feature",
            "handoff",
            "--stage",
            "apply",
            "--status",
            "completed",
            platform="codex",
        )
        self.assertEqual(first["entry_id"], codex_finish["entry_id"])
        claude_finish = self.run_metrics(
            "finish",
            "--feature",
            "handoff",
            "--stage",
            "apply",
            "--status",
            "completed",
            platform="claude-code",
        )
        self.assertEqual(second["entry_id"], claude_finish["entry_id"])
        state = json.loads(
            (
                self.repo
                / ".helixharness/telemetry-v2/lite-active/handoff/apply.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual({}, state["active"])

    def test_existing_legacy_eval_log_is_skipped_without_rewrite(self) -> None:
        self.enable()
        path = self.repo / "docs" / "eval" / "legacy-feature" / "eval-log.json"
        path.parent.mkdir(parents=True)
        original = b'{"schema_version":"1.0","feature":"legacy-feature","entries":[]}\n'
        path.write_bytes(original)

        result = self.run_metrics(
            "start",
            "--feature",
            "legacy-feature",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
        )

        self.assertEqual("skipped", result["status"])
        self.assertEqual("unsupported_ledger_schema", result["reason"])
        self.assertEqual(original, path.read_bytes())
        self.assertFalse(
            (self.repo / ".helixharness" / "telemetry-v2" / "lite-active" / "legacy-feature").exists()
        )

    def test_invalid_optional_apply_metrics_are_ignored_without_losing_stage_result(self) -> None:
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "demo",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )

        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                "--platform",
                "codex",
                "finish",
                "--feature",
                "demo",
                "--stage",
                "apply",
                "--status",
                "completed",
                "--metrics-json",
                '{"verification":"typo","must_fix_rounds":9,"future_metric":"value"}',
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("recorded", result["status"])
        ledger = json.loads(
            (self.repo / "docs" / "eval" / "demo" / "eval-log.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("completed", ledger["entries"][0]["status"])
        self.assertEqual(
            {"must_fix_rounds": 9}, ledger["entries"][0]["quality"]
        )

    def test_finish_without_start_is_a_non_blocking_skip(self) -> None:
        self.enable()

        result = self.run_metrics(
            "finish",
            "--feature",
            "demo",
            "--stage",
            "apply",
            "--status",
            "completed",
        )

        self.assertEqual("skipped", result["status"])
        self.assertEqual("stage_state_missing", result["reason"])

    def test_malformed_optional_metrics_do_not_prevent_stage_finalization(self) -> None:
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "demo",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )

        result = self.run_metrics(
            "finish",
            "--feature",
            "demo",
            "--stage",
            "apply",
            "--status",
            "completed",
            "--metrics-json",
            "not-json",
        )

        self.assertEqual("recorded", result["status"])

    def test_disabling_consent_immediately_purges_private_lite_stage(self) -> None:
        self.enable()
        self.run_metrics(
            "start",
            "--feature",
            "demo",
            "--stage",
            "apply",
            "--trigger",
            "user_command",
        )
        state = (
            self.repo
            / ".helixharness"
            / "telemetry-v2"
            / "lite-active"
            / "demo"
            / "apply.json"
        )
        self.assertTrue(state.is_file())

        self.disable()

        self.assertFalse(state.exists())
        self.assertTrue((self.repo / "docs" / "eval" / "demo" / "eval-log.json").exists())

    def test_start_adds_only_missing_gitignore_rules(self) -> None:
        (self.repo / ".gitignore").write_text(
            "# Existing eval rules\n!docs/eval/\n!docs/eval/**\n",
            encoding="utf-8",
        )
        self.enable()

        self.run_metrics(
            "start",
            "--feature",
            "demo",
            "--stage",
            "propose",
            "--trigger",
            "user_command",
        )

        lines = (self.repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, lines.count("!docs/eval/"))
        self.assertEqual(1, lines.count("!docs/eval/**"))
        self.assertEqual(1, lines.count("/.helixharness/"))

    @unittest.skipIf(os.name == "nt", "POSIX lock timing test")
    def test_old_lite_lock_no_longer_blocks_unified_ledger(self) -> None:
        import fcntl

        self.enable()
        lock_path = self.repo / ".helixharness" / "lite-metrics.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            started = time.monotonic()
            completed = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--repo-root",
                    str(self.repo),
                    "--platform",
                    "codex",
                    "start",
                    "--feature",
                    "demo",
                    "--stage",
                    "propose",
                    "--trigger",
                    "user_command",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=self.env,
                timeout=1.5,
            )
            elapsed = time.monotonic() - started

        self.assertEqual(0, completed.returncode)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
