"""Claude native hooks and stage statistics through isolated real subprocesses.

Only synthetic transcripts are read. No native model, HTTP sender, real user
configuration or scheduler is used; collection stops at the local outbox.
"""

from __future__ import annotations

import gzip
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]


class ClaudeLifecycleScenariosTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="helix-claude-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "synthetic project 中文 'quoted'"
        self.repo.mkdir()
        self.telemetry = self.root / "telemetry"
        self.telemetry.mkdir()
        (self.telemetry / "config.json").write_text('{"endpoint":""}')
        self.claude = self.root / "claude"
        self.env = {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "CODEX_HOME": str(self.root / "codex"), "CLAUDE_CONFIG_DIR": str(self.claude),
            "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.telemetry),
            "HELIXHARNESS_FEEDBACK_HOME": str(self.root / "feedback"),
            "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
        }
        self.process(["git", "init", "-q", self.repo])

    def process(self, args, *, env=None, payload=None, code=0):
        result = subprocess.run(list(map(str, args)), cwd=self.repo,
                                env=self.env if env is None else env, input=payload,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def cli(self, script, *args, env=None, code=0):
        return json.loads(self.process([sys.executable, "-B", PLUGIN / "scripts" / script, *args],
                                       env=env, code=code).stdout)

    def config(self, *args, code=0):
        base = ("user_config.py", "--repo-root", self.repo, "--platform", "claude-code")
        result = self.cli(*base, *args, code=code)
        if result.get("notice_required"):
            self.assertTrue(result["read_only"])
            self.assertFalse(result["change"]["updated"])
            result = self.cli(*base, *args, "--telemetry-notice-shown", result["notice"]["sha256"], code=code)
        return result

    def consent(self, mode="enabled"):
        return self.config("--telemetry", mode, "--scope", "project", "--user-confirmed")

    def metrics(self, action, *, env=None, feature="demo", stage="apply"):
        tail = ("--trigger", "user_command") if action == "start" else ("--status", "completed")
        return self.cli("metrics.py", "--repo-root", self.repo, "--platform", "claude-code", action,
                        "--feature", feature, "--stage", stage, *tail, env=env)

    def hook(self, event, *, env=None, **payload):
        config = json.loads((PLUGIN / "hosts/claude-code/hooks.json").read_text())
        command = config["hooks"][event][0]["hooks"][0]["command"]
        return self.process(["sh", "-c", command], env=env,
                            payload=json.dumps({"hook_event_name": event, **payload}))

    def source(self, session="synthetic-claude-one"):
        path = self.claude / "projects" / "synthetic-project" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        return path

    def bind(self, source, session="synthetic-claude-one"):
        env_file = self.root / f"{session}.env.sh"
        # This is a new synthetic hook output file, never a user's .env file.
        selected = {**self.env, "CLAUDE_ENV_FILE": str(env_file), "CODEX_THREAD_ID": "foreign-codex",
                    "HELIXHARNESS_CURSOR_CONVERSATION_ID": "foreign-cursor", "HELIXHARNESS_PLATFORM": "cursor"}
        output = self.hook("SessionStart", env=selected, session_id=session,
                           cwd=str(self.repo), transcript_path=str(source))
        specific = json.loads(output.stdout)["hookSpecificOutput"]
        binding = json.loads(specific["additionalContext"].split(": ", 1)[1].split("\n", 1)[0])
        self.assertEqual(binding["canonical_host_id"], "claude-code")
        exports = {}
        for line in env_file.read_text().splitlines():
            statement = shlex.split(line)
            self.assertEqual(statement[0], "export")
            key, value = statement[1].split("=", 1)
            exports[key] = value
        self.assertEqual(exports["HELIXHARNESS_LITE_SESSION_ID"], session)
        self.assertEqual(exports["HELIXHARNESS_LITE_SESSION_CWD"], str(self.repo))
        self.assertEqual(exports["HELIXHARNESS_BOUND_PLUGIN_ROOT"], str(PLUGIN))
        return {**selected, **exports}

    @staticmethod
    def timestamp(offset=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat(timespec="milliseconds")

    def append_usage(self, source, usage, *, offset=0):
        message = {"type": "assistant", "timestamp": self.timestamp(offset),
                   "message": {"content": [{"type": "text", "text": "Synthetic answer"}],
                               "model": "synthetic-claude-model", "usage": usage}}
        with source.open("a") as stream:
            stream.write(json.dumps(message) + "\n")

    def ledger(self, feature="demo"):
        return json.loads((self.repo / f"docs/eval/{feature}/eval-log.json").read_text())

    def locator(self, entry):
        return self.repo / ".helixharness/telemetry-v2/locators" / f"{entry['entry_id']}.json"

    def collect(self):
        self.config("--report-endpoint", "https://telemetry.example.test/report", "--user-confirmed")
        return self.cli("telemetry_collector.py", "--repo-root", self.repo)

    def outbox(self):
        return [json.loads(gzip.decompress(path.read_bytes())) for path in self.telemetry.glob("outbox/*.json.gz")]

    def files(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def test_read_only_commands_disabled_statistics_and_explicit_consent(self):
        before = self.files()
        config = self.config()
        self.assertTrue(config["read_only"])
        self.assertEqual(config["telemetry"]["effective_mode"], "disabled")
        self.assertTrue(self.cli("project_status.py", "--repo-root", self.repo)["empty"])
        refused = self.config("--telemetry", "enabled", "--scope", "project", code=2)
        self.assertIn("explicit user", refused["error"])
        self.assertEqual(self.files(), before)
        self.assertEqual(self.metrics("start")["reason"], "telemetry_disabled")
        self.assertFalse((self.repo / "docs/eval").exists())
        self.assertEqual(self.consent()["telemetry"]["effective_mode"], "enabled")

    def test_native_start_binds_three_stages_and_collects_exact_usage_once(self):
        self.consent()
        source = self.source()
        env = self.bind(source)
        expected = {}
        for index, stage in enumerate(("propose", "apply", "archive"), 1):
            started = self.metrics("start", env=env, stage=stage)
            # Exercise the existing normalized source contract here. The
            # native cache-field compatibility probe below is kept separate.
            usage = {"input_tokens": index * 10, "output_tokens": index,
                     "cache_read_tokens": 0, "cache_write_tokens": 0}
            self.append_usage(source, usage)
            finished = self.metrics("finish", env=env, stage=stage)
            self.assertEqual(started["entry_id"], finished["entry_id"])
            repeated = self.metrics("finish", env=env, stage=stage)
            if stage == "apply":
                self.assertTrue(repeated["idempotent"])
            else:
                self.assertEqual(repeated["reason"], "stage_state_missing")
            expected[started["entry_id"]] = (index * 10, index)
        self.append_usage(source, {"input_tokens": 99999, "output_tokens": 99999}, offset=60)
        self.assertEqual(self.collect()["collected"], 3)
        packages = self.outbox()
        self.assertEqual(len(packages), 3)
        for package in packages:
            supplement = package["supplement"]
            self.assertEqual(supplement["platform"], "claude-code")
            self.assertEqual((supplement["usage"]["input_tokens"], supplement["usage"]["output_tokens"]),
                             expected[supplement["entry_id"]])
            self.assertEqual(supplement["usage"]["cache_read_tokens"], 0)
            self.assertEqual(supplement["usage"]["cache_write_tokens"], 0)
        self.assertEqual(self.collect()["collected"], 0)
        self.assertEqual(len(self.outbox()), 3)
        public = json.dumps(self.ledger())
        self.assertNotIn(str(source), public)
        self.assertNotIn("synthetic-claude-one", public)
        self.assertFalse((self.root / "codex").exists())
        self.assertFalse((self.root / "cursor").exists())

    def test_zero_missing_and_partial_fields_remain_distinct_through_collection(self):
        self.consent()
        for feature, values in (("zero", {"input_tokens": 0, "output_tokens": 0}),
                                ("missing", {}), ("partial", {"input_tokens": 8})):
            source = self.source(feature)
            env = self.bind(source, feature)
            self.metrics("start", env=env, feature=feature)
            self.append_usage(source, values)
            self.metrics("finish", env=env, feature=feature)
        self.assertEqual(self.collect()["collected"], 3)
        by_feature = {item["supplement"]["feature"]: item["supplement"]["usage"] for item in self.outbox()}
        self.assertEqual(by_feature["zero"]["input_tokens"], 0)
        self.assertEqual(by_feature["zero"]["output_tokens"], 0)
        self.assertNotIn("input_tokens", by_feature["missing"])
        self.assertNotIn("output_tokens", by_feature["missing"])
        self.assertEqual(by_feature["partial"]["input_tokens"], 8)
        self.assertNotIn("output_tokens", by_feature["partial"])

    def test_missing_or_foreign_repository_identity_does_not_borrow_other_hosts(self):
        self.consent()
        source = self.source()
        bound = self.bind(source)
        foreign = {**bound, "HELIXHARNESS_LITE_SESSION_CWD": str(self.root / "other project")}
        for feature, env in (("missing", self.env), ("foreign", foreign)):
            started = self.metrics("start", env=env, feature=feature)
            self.metrics("finish", env=env, feature=feature)
            locator = json.loads(self.locator(started).read_text())
            self.assertEqual(locator["raw_session_id"], "unknown")
            self.assertIsNone(locator["session_log_path"])
            self.assertEqual(self.ledger(feature)["entries"][0]["session_hash"], "session-unavailable")
        result = self.collect()
        self.assertEqual((result["collected"], result["skipped"], result["failed"]), (0, 2, 0))
        self.assertEqual(self.outbox(), [])

    def test_parallel_sessions_finish_only_their_own_attempt(self):
        self.consent()
        one = self.bind(self.source("one"), "one")
        two = self.bind(self.source("two"), "two")
        first = self.metrics("start", env=one)
        second = self.metrics("start", env=two)
        self.assertNotEqual(first["entry_id"], second["entry_id"])
        self.assertEqual(self.metrics("finish")["reason"], "stage_state_ambiguous")
        self.assertEqual(self.metrics("finish", env=two)["entry_id"], second["entry_id"])
        entries = {entry["entry_id"]: entry for entry in self.ledger()["entries"]}
        self.assertEqual(entries[first["entry_id"]]["status"], "in_progress")
        self.assertEqual(entries[second["entry_id"]]["status"], "completed")
        self.assertTrue(self.metrics("finish", env=two)["idempotent"])
        self.assertEqual(self.metrics("finish", env=one)["entry_id"], first["entry_id"])
        self.assertEqual(len(self.ledger()["entries"]), 2)

    def test_session_end_does_not_complete_business_and_revocation_preserves_public_ledger(self):
        self.consent()
        source = self.source()
        env = self.bind(source)
        started = self.metrics("start", env=env)
        ended = self.hook("SessionEnd", env=env, cwd=str(self.repo), session_id="synthetic-claude-one")
        self.assertEqual(ended.stdout, "")
        self.assertEqual(self.ledger()["entries"][0]["status"], "in_progress")
        public = (self.repo / "docs/eval/demo/eval-log.json").read_bytes()
        self.assertEqual(self.consent("disabled")["telemetry"]["effective_mode"], "disabled")
        self.assertFalse(self.locator(started).exists())
        self.assertEqual(self.metrics("finish", env=env)["reason"], "telemetry_disabled")
        self.assertEqual((self.repo / "docs/eval/demo/eval-log.json").read_bytes(), public)
        self.assertEqual(source.read_bytes(), b"")

    def test_native_command_hooks_bind_correct_resources_and_ignore_other_commands(self):
        for event, fields in (("UserPromptExpansion", {"command_name": "helix:status"}),
                              ("PreToolUse", {"tool_name": "Skill", "tool_input": {"skill": "helix:config"}})):
            with self.subTest(event=event):
                output = self.hook(event, **fields)
                specific = json.loads(output.stdout)["hookSpecificOutput"]
                self.assertEqual(specific["hookEventName"], event)
                context = specific["additionalContext"]
                self.assertIn('"host_id": "claude-code"', context)
                self.assertIn(str(PLUGIN / "reference/runtime-preflight.md"), context)
        self.assertEqual(self.hook("UserPromptExpansion", command_name="other:status").stdout, "")
        self.assertEqual(self.hook("PreToolUse", tool_name="Read", tool_input={}).stdout, "")
        self.assertFalse((self.repo / "docs").exists())


def probe_native_cache_fields():
    """Expose unsupported native usage aliases without hiding a failed probe.

    This only reads a synthetic assistant.message.usage record and public
    parser source from HEAD. It does not inspect an installed Claude session.
    """
    from telemetry_hosts.supplement_parsers import collect_claude_window

    fixture = ClaudeLifecycleScenariosTest(methodName="runTest")
    try:
        fixture.setUp()
        fixture.consent()
        source = fixture.source()
        env = fixture.bind(source)
        fixture.metrics("start", env=env)
        fixture.append_usage(source, {"input_tokens": 10, "output_tokens": 5,
                                      "cache_read_input_tokens": 4, "cache_creation_input_tokens": 2})
        fixture.metrics("finish", env=env)
        fixture.collect()
        reported = fixture.outbox()[0]["supplement"]["usage"]
        baseline_text = subprocess.check_output(
            ["git", "-C", str(PLUGIN), "show", "HEAD:helixharness/scripts/telemetry_hosts/supplement_parsers.py"],
            env=fixture.env, timeout=10, text=True,
        )
        baseline = {"__name__": "telemetry_hosts.baseline_probe", "__package__": "telemetry_hosts"}
        exec(compile(baseline_text, "<public HEAD parser>", "exec"), baseline)
        window = {"started_at": "2000-01-01T00:00:00Z", "ended_at": "2100-01-01T00:00:00Z"}
        results = []
        for revision, usage in (("current_parser", collect_claude_window(source, **window).usage),
                                ("HEAD_parser", baseline["collect_claude_window"](source, **window).usage),
                                ("current_hook_to_outbox", reported)):
            results.append({"revision": revision, "expected_cache_read_tokens": 4,
                            "expected_cache_write_tokens": 2, "actual_usage": usage,
                            "status": "pass" if usage.get("cache_read_tokens") == 4
                            and usage.get("cache_write_tokens") == 2 else "fail"})
        return {"simulation": True, "real_claude_executed": False,
                "input_location": "assistant.message.usage", "results": results}
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe-native-cache"]:
        findings = probe_native_cache_fields()
        print(json.dumps(findings, ensure_ascii=False, indent=2))
        raise SystemExit(any(item["status"] != "pass" for item in findings["results"]
                             if item["revision"].startswith("current")))
    unittest.main()
