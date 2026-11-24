"""Cursor native-hook simulations through real CLI processes and local outbox.

All hook payloads and repositories are synthetic. No Cursor model, private
transcript, HTTP sender, or system scheduler is used.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import env_sandbox

PLUGIN = Path(__file__).resolve().parents[2]


class CursorLifecycleScenariosTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="helix-cursor-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "synthetic project 中文"
        self.repo.mkdir()
        self.telemetry = self.root / "telemetry"
        self.telemetry.mkdir()
        (self.telemetry / "config.json").write_text('{"endpoint":""}')
        self.env = {
            **env_sandbox.boot_vars(),
            "PATH": env_sandbox.search_path(),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "CODEX_HOME": str(self.root / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.telemetry),
            "HELIXHARNESS_FEEDBACK_HOME": str(self.root / "feedback"),
            "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
            "CURSOR_PLUGIN_ROOT": str(PLUGIN),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
        }
        self.init_repo(self.repo)

    def run_process(self, args, *, env=None, payload=None, code=0, cwd=None):
        result = subprocess.run(
            list(map(str, args)), env=self.env if env is None else env,
            cwd=cwd or self.repo, input=payload, capture_output=True, text=True,
            timeout=30, check=False,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def init_repo(self, repo):
        self.run_process(["git", "init", "-q", repo])

    def cli(self, script, *args, env=None, code=0):
        result = self.run_process([sys.executable, PLUGIN / "scripts" / script, *args], env=env, code=code)
        return json.loads(result.stdout)

    def config(self, *args, repo=None, code=0):
        base = ("user_config.py", "--repo-root", repo or self.repo, "--platform", "cursor")
        result = self.cli(*base, *args, code=code)
        if result.get("notice_required"):
            self.assertTrue(result["read_only"])
            self.assertFalse(result["change"]["updated"])
            result = self.cli(*base, *args, "--telemetry-notice-shown", result["notice"]["sha256"], code=code)
        return result

    def consent(self, mode="enabled", *, repo=None):
        return self.config("--telemetry", mode, "--scope", "project", "--user-confirmed", repo=repo)

    def enable_collection(self):
        self.config("--report-endpoint", "https://telemetry.example.test/report", "--user-confirmed")

    def hook(self, event, *, conversation="synthetic-cursor-one", roots=None, repo=None, omit_cwd=False, **fields):
        hooks = json.loads((PLUGIN / "hosts/cursor/hooks.json").read_text())["hooks"]
        command = hooks[event][0]["command"]
        payload = {"hook_event_name": event, "conversation_id": conversation,
                   "workspace_roots": list(map(str, roots or [self.repo])), **fields}
        if not omit_cwd:
            payload["cwd"] = str(repo or self.repo)
        result = self.run_process(["sh", "-c", command], payload=json.dumps(payload))
        if event == "beforeSubmitPrompt":
            response = json.loads(result.stdout)
            context = response.pop("additional_context")
            self.assertIn("HELIXHARNESS_CURSOR_CONVERSATION_ID=" + conversation, context)
            self.assertEqual(response, {"continue": True})
        elif event == "subagentStart":
            self.assertEqual(json.loads(result.stdout), {"permission": "allow"})
        elif event != "sessionStart":
            self.assertEqual(result.stdout, "")
        return result

    def bind(self, conversation="synthetic-cursor-one", *, roots=None):
        started = json.loads(self.hook("sessionStart", conversation=conversation, roots=roots).stdout)
        self.assertEqual(started["env"]["HELIXHARNESS_CURSOR_CONVERSATION_ID"], conversation)
        self.assertEqual(started["env"]["HELIXHARNESS_BOUND_PLUGIN_ROOT"], str(PLUGIN))
        return {**self.env, **started["env"]}

    def metrics(self, action, *, env, stage="apply", feature="demo", repo=None):
        tail = ("--trigger", "user_command") if action == "start" else ("--status", "completed")
        return self.cli("metrics.py", "--repo-root", repo or self.repo, "--platform", "cursor",
                        action, "--feature", feature, "--stage", stage, *tail, env=env)

    def ledger(self, *, repo=None, feature="demo"):
        return json.loads(((repo or self.repo) / f"docs/eval/{feature}/eval-log.json").read_text())

    def source(self, entry, *, repo=None):
        locator = (repo or self.repo) / ".helixharness/telemetry-v2/locators" / (entry["entry_id"] + ".json")
        return Path(json.loads(locator.read_text())["session_log_path"])

    def collect(self, *, repo=None):
        return self.cli("telemetry_collector.py", "--repo-root", repo or self.repo)

    def outbox(self):
        return [json.loads(gzip.decompress(path.read_bytes())) for path in self.telemetry.glob("outbox/*.json.gz")]

    def files(self, root=None):
        root = root or self.root
        return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()}

    def generation(self, generation, inputs, outputs, *, conversation="synthetic-cursor-one", roots=None, repo=None):
        options = {"conversation": conversation, "roots": roots, "repo": repo, "generation_id": generation}
        self.hook("beforeSubmitPrompt", **options, prompt="Synthetic request")
        fields = {"input_tokens": inputs, "output_tokens": outputs, "cache_read_tokens": 0, "cache_write_tokens": 0}
        self.hook("afterAgentResponse", **options, text="Synthetic response", **fields)
        self.hook("stop", **options, **fields)

    def test_read_only_commands_and_unconfirmed_consent_leave_business_and_homes_unchanged(self):
        before = self.files()
        result = self.config()
        self.assertTrue(result["read_only"])
        self.assertEqual(result["telemetry"]["effective_mode"], "disabled")
        self.assertTrue(self.cli("project_status.py", "--repo-root", self.repo)["empty"])
        refused = self.config("--telemetry", "enabled", "--scope", "project", code=2)
        self.assertIn("explicit user", refused["error"])
        self.assertEqual(self.files(), before)
        self.assertEqual(self.metrics("start", env=self.env)["reason"], "telemetry_disabled")
        self.hook("beforeSubmitPrompt", generation_id="disabled", prompt="Synthetic ignored content")
        self.assertFalse((self.repo / "docs/eval").exists())
        self.assertFalse(list(self.repo.rglob("*.jsonl")))

    def test_session_binding_three_stages_and_all_event_types_collect_exact_local_supplements(self):
        env = self.bind()
        env.update(CODEX_THREAD_ID="synthetic-foreign", CLAUDE_SESSION_ID="synthetic-foreign",
                   HELIXHARNESS_PLATFORM="codex")
        self.consent()
        self.enable_collection()
        expected = {}
        for stage, inputs, outputs in (("propose", 0, 2), ("apply", 10, 0), ("archive", 20, 3)):
            started = self.metrics("start", stage=stage, env=env)
            generation = "generation-" + stage
            self.generation(generation, inputs, outputs)
            if stage == "apply":
                for _ in range(2):
                    self.hook("postToolUse", generation_id=generation, tool_name="Read", tool_use_id="read-one",
                              tool_input={"file_path": "/synthetic/docs/knowledge/index.md", "secret": "DO_NOT_RETAIN"},
                              tool_output="DO_NOT_RETAIN")
                self.hook("postToolUseFailure", generation_id=generation, tool_name="Shell", tool_use_id="shell-one",
                          failure_type="timeout", error_message="DO_NOT_RETAIN")
                child = {"generation_id": "synthetic-cursor-one", "subagent_id": "child-one", "subagent_type": "generalPurpose"}
                for _ in range(2):
                    self.hook("subagentStart", **child, task="DO_NOT_RETAIN")
                    self.hook("subagentStop", **child, status="completed", duration_ms=15, summary="DO_NOT_RETAIN")
                self.hook("postToolUse", generation_id=generation, tool_name="Task", tool_use_id="task-one")
            finished = self.metrics("finish", stage=stage, env=env)
            self.assertEqual(finished["entry_id"], started["entry_id"])
            expected[started["entry_id"]] = (stage, inputs, outputs)
        ledger = self.ledger()
        self.assertEqual(ledger["status"], "archived")
        self.assertEqual([entry["command"] for entry in ledger["entries"]], ["propose", "apply", "archive"])
        self.assertTrue(all(entry["platform"] == "cursor" and entry["status"] == "completed" for entry in ledger["entries"]))
        self.assertEqual(self.collect()["collected"], 3)
        records = self.outbox()
        self.assertEqual(len(records), 3)
        for envelope in records:
            supplement = envelope["supplement"]
            stage, inputs, outputs = expected[supplement["entry_id"]]
            usage = supplement["usage"]
            self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (inputs, outputs, inputs + outputs))
            self.assertEqual((usage["collection_status"], usage["generations"], usage["scope"]), ("collected", 1, "parent_only"))
            self.assertTrue(envelope["final_for_entry"])
            self.assertEqual(envelope["attempt_count"], 0)
            self.assertNotIn("DO_NOT_RETAIN", json.dumps(envelope))
            if stage == "apply":
                execution = supplement["execution"]
                self.assertEqual(execution["tool_call_count"], 3)
                self.assertEqual(execution["tool_error_count"], 1)
                self.assertEqual(execution["subagent_spawn_count"], 1)
                self.assertEqual(execution["context_reads"]["knowledge"], 1)
        self.assertEqual(self.collect()["collected"], 0)
        self.assertEqual(len(self.outbox()), 3)
        self.assertNotIn("synthetic-cursor-one", json.dumps(ledger))
        self.assertTrue((self.telemetry / "runtime-manifest.json").exists())
        self.assertFalse((self.telemetry / "scheduler").exists())
        self.assertFalse((self.root / "codex").exists())
        self.assertFalse((self.root / "claude").exists())

    def test_shell_must_receive_explicit_hook_identity_and_cannot_borrow_other_host_context(self):
        bound = self.bind()
        self.consent()
        foreign = {**self.env, "CODEX_THREAD_ID": "synthetic-codex", "CLAUDE_SESSION_ID": "synthetic-claude",
                   "HELIXHARNESS_SESSION_ID": "synthetic-generic"}
        for missing in (foreign, {key: value for key, value in bound.items() if key != "HELIXHARNESS_CURSOR_WORKSPACE_ROOTS"}):
            result = self.metrics("start", env=missing)
            self.assertEqual(result["reason"], "cursor_session_context_unavailable")
        self.assertFalse((self.repo / "docs/eval").exists())
        self.assertEqual(self.metrics("start", env=bound)["status"], "started")
        self.assertEqual(self.metrics("finish", env=bound)["status"], "recorded")

    def test_prompt_before_stage_and_response_after_finish_do_not_fabricate_zero_usage(self):
        env = self.bind()
        self.consent()
        self.enable_collection()
        self.hook("beforeSubmitPrompt", generation_id="outside", prompt="Synthetic before stage")
        started = self.metrics("start", env=env)
        self.hook("postToolUse", generation_id="outside", tool_name="Shell", tool_use_id="during-stage")
        self.metrics("finish", env=env)
        source = self.source(started)
        before = source.read_bytes()
        self.hook("afterAgentResponse", generation_id="outside", input_tokens=100, output_tokens=20, text="Synthetic late response")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.collect()["collected"], 1)
        supplement = self.outbox()[0]["supplement"]
        self.assertEqual(supplement["usage"]["collection_status"], "unavailable")
        self.assertNotIn("total_tokens", supplement["usage"])
        self.assertEqual(supplement["execution"]["tool_call_count"], 1)

    def test_concurrent_conversations_collect_only_their_own_attempts_and_finish_idempotently(self):
        first_env = self.bind("synthetic-first")
        second_env = self.bind("synthetic-second")
        self.consent()
        self.enable_collection()
        first = self.metrics("start", env=first_env)
        second = self.metrics("start", env=second_env)
        self.assertNotEqual(self.source(first), self.source(second))
        self.generation("shared-generation-id", 10, 2, conversation="synthetic-first")
        self.generation("shared-generation-id", 30, 4, conversation="synthetic-second")
        self.assertEqual(self.metrics("finish", env=second_env)["entry_id"], second["entry_id"])
        states = {entry["entry_id"]: entry["status"] for entry in self.ledger()["entries"]}
        self.assertEqual(states[first["entry_id"]], "in_progress")
        self.assertTrue(self.metrics("finish", env=second_env)["idempotent"])
        self.assertEqual(self.metrics("finish", env=first_env)["entry_id"], first["entry_id"])
        self.assertEqual(self.collect()["collected"], 2)
        totals = {record["supplement"]["entry_id"]: record["supplement"]["usage"]["total_tokens"] for record in self.outbox()}
        self.assertEqual(totals, {first["entry_id"]: 12, second["entry_id"]: 34})

    def test_multi_repository_workspace_keeps_ambiguous_tokens_out_of_both_totals(self):
        children = [self.repo / name for name in ("api", "web")]
        for child in children:
            child.mkdir()
            self.init_repo(child)
        env = self.bind(roots=[self.repo])
        for child in children:
            self.consent(repo=child)
        self.enable_collection()
        entries = [self.metrics("start", env=env, repo=child) for child in children]
        self.hook("beforeSubmitPrompt", generation_id="ambiguous", omit_cwd=True, prompt="Synthetic ambiguous request")
        self.hook("stop", generation_id="ambiguous", omit_cwd=True, input_tokens=1000, output_tokens=100)
        for child, count in zip(children, (10, 20)):
            self.generation("bound-" + child.name, count, 2, roots=[self.repo], repo=child)
            self.metrics("finish", env=env, repo=child)
            self.assertEqual(self.collect(repo=child)["collected"], 1)
        records = {record["supplement"]["entry_id"]: record["supplement"] for record in self.outbox()}
        for entry, expected in zip(entries, (12, 22)):
            usage = records[entry["entry_id"]]["usage"]
            self.assertEqual(usage["collection_status"], "partial")
            self.assertEqual(usage["unavailable_reason"], "ambiguous_workspace")
            self.assertEqual(usage["total_tokens"], expected)
        self.assertFalse((self.repo / "docs/eval").exists())

    def test_stop_session_end_and_revocation_do_not_claim_business_completion(self):
        env = self.bind()
        self.consent()
        self.enable_collection()
        started = self.metrics("start", env=env)
        self.generation("in-progress", 10, 1)
        self.hook("sessionEnd")
        self.assertEqual(self.ledger()["entries"][0]["status"], "in_progress")
        self.metrics("finish", env=env)
        self.collect()
        self.assertEqual(len(self.outbox()), 1)
        public_path = self.repo / "docs/eval/demo/eval-log.json"
        public = public_path.read_bytes()
        source = self.source(started)
        self.assertTrue(source.exists())
        self.consent("disabled")
        self.assertFalse(source.exists())
        self.assertEqual(self.outbox(), [])
        self.assertEqual(public_path.read_bytes(), public)
        self.assertEqual(self.metrics("finish", env=env)["reason"], "telemetry_disabled")
        self.hook("postToolUse", generation_id="after-revoke", tool_name="Shell", tool_use_id="not-collected")
        self.assertFalse(source.exists())
        self.consent()
        restarted = self.metrics("start", env=env)
        self.assertNotEqual(self.source(restarted), source)
        self.assertTrue(self.source(restarted).exists())

    def test_feedback_draft_binds_cursor_hash_without_private_transcript_or_repository_writes(self):
        bound = self.bind()
        self.config("--feedback-endpoint", "https://feedback.example.invalid/report", "--user-confirmed")
        before = self.files(self.repo)
        for env, include in ((self.env, False), (bound, False), (bound, True)):
            with self.subTest(bound=env is bound, include_transcript=include):
                extra = ("--include-transcript",) if include else ()
                draft = self.cli("feedback_submit.py", "prepare", "--repo-root", self.repo,
                                 "--platform", "cursor", "--text", "Synthetic Cursor command feedback",
                                 *extra, env=env)
                self.assertEqual(draft["status"], "prepared")
                document = draft["item"]["document"]
                self.assertEqual(document["host"], "cursor")
                if env is bound:
                    self.assertRegex(document["session"], r"^session-[0-9a-f]{32}$")
                else:
                    self.assertEqual(document["session"], "unknown")
                self.assertNotIn("synthetic-cursor-one", json.dumps(document))
                self.assertFalse(draft["item"]["transcript_stats"]["supported"])
                self.assertNotIn("transcript_md", draft["item"])
                if include:
                    self.assertIn("claude-code", draft["item"]["transcript_stats"]["reason"])
                path = Path(draft["draft_path"])
                self.assertEqual(path.parent, self.root / "feedback/drafts")
                if os.name != "nt":
                    # Windows records only the read-only bit, so 0600 is unprovable there.
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, self.files(self.repo))
        self.assertFalse((self.root / "feedback/outbox").exists())

    def test_feedback_send_without_confirmation_preserves_reviewable_draft_and_does_not_queue(self):
        self.config("--feedback-endpoint", "https://feedback.example.invalid/report", "--user-confirmed")
        draft = self.cli("feedback_submit.py", "prepare", "--repo-root", self.repo,
                         "--platform", "cursor", "--text", "Synthetic unsent feedback")
        before = self.files()
        rejected = self.run_process([sys.executable, PLUGIN / "scripts/feedback_submit.py", "send",
                                     "--draft", draft["draft_path"], "--digest", draft["digest"]], code=1)
        self.assertIn("confirmation", json.loads(rejected.stderr)["error"])
        self.assertEqual(before, self.files())
        self.assertFalse((self.root / "feedback/outbox").exists())


if __name__ == "__main__":
    unittest.main()
