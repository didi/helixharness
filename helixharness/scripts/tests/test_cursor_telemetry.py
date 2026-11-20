"""Native Cursor synthetic hooks through consent, recorder, collector and Outbox."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metrics
import telemetry_async
import telemetry_collector
import telemetry_consent
import telemetry_ledger
from telemetry_hosts.cursor import CursorAdapter, _normalized_event, _timestamp, _usage, record_hook
from telemetry_hosts import RUNTIME_SNAPSHOT_NAME, _RuntimeRegistry, build_runtime_snapshot


ROOT = Path(__file__).resolve().parents[2]


class CursorTelemetryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cursor telemetry space ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "synthetic repo"
        self.repo.mkdir()
        self.env = {**os.environ, "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor"),
                    "HELIXHARNESS_TELEMETRY_HOME": str(self.root / "telemetry"),
                    "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
                    "HELIXHARNESS_CURSOR_CONVERSATION_ID": "synthetic-conversation-1",
                    "HELIXHARNESS_CURSOR_WORKSPACE_ROOTS": json.dumps([str(self.repo)]),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull}
        self.env.pop("HELIXHARNESS_BOUND_PLUGIN_ROOT", None)
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        subprocess.run(["git", "init", "-q", str(self.repo)], env=self.env, check=True)
        self.adapter = CursorAdapter()
        self.home = self.root / "telemetry"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"endpoint": "http://127.0.0.1:9/report"}))
        self.payload = {"conversation_id": "synthetic-conversation-1", "generation_id": "generation-1",
                        "workspace_roots": [str(self.repo)], "cwd": str(self.repo), "cursor_version": "3.15.6"}
        self.no_runtime = patch.object(metrics.telemetry_async, "ensure_runtime_ready", return_value={})
        self.no_runtime.start()
        self.addCleanup(self.no_runtime.stop)
        wake = patch.object(metrics.telemetry_async, "wake_detached", return_value=False)
        wake.start()
        self.addCleanup(wake.stop)

    def consent(self, mode="enabled"):
        return telemetry_consent.set_mode(self.repo, "cursor", self.adapter.consent_cache_path(), "project", mode,
                                          consent_source="explicit-command", user_confirmed=True)

    def start(self, *, at="2026-11-18T01:00:00.000Z", feature="demo", stage="propose"):
        with patch.object(telemetry_ledger, "_now", return_value=at):
            result = metrics.start(self.repo, "cursor", feature, stage, "user_command")
        self.assertEqual(result["status"], "started", result)
        return result

    def event(self, event, *, at="2026-11-18T01:00:01.000Z", **fields):
        with patch("telemetry_hosts.cursor._now", return_value=at):
            return record_hook({**self.payload, "hook_event_name": event, **fields})

    def finish(self, *, at="2026-11-18T01:00:10.000Z", feature="demo", stage="propose"):
        with patch.object(telemetry_ledger, "_now", return_value=at):
            result = metrics.finish(self.repo, "cursor", feature, stage, "completed", None, {})
        self.assertEqual(result["status"], "recorded", result)
        return result

    def test_consent_disabled_does_not_capture_or_create_ledger(self):
        self.assertEqual(self.event("beforeSubmitPrompt", prompt="synthetic"), 0)
        result = metrics.start(self.repo, "cursor", "demo", "propose", "user_command")
        self.assertEqual(result["reason"], "telemetry_disabled")
        self.assertFalse((self.repo / "docs/eval").exists())
        self.assertFalse((self.repo / ".helixharness/telemetry-v2/cursor-events").exists())

    def test_missing_or_foreign_identity_never_uses_other_host_or_last_session(self):
        self.consent()
        for change in ({"HELIXHARNESS_CURSOR_CONVERSATION_ID": ""},
                       {"HELIXHARNESS_CURSOR_WORKSPACE_ROOTS": json.dumps([str(self.root / "other")])},
                       {"HELIXHARNESS_CURSOR_CONVERSATION_ID": "../../escape"}):
            with patch.dict(os.environ, {**change, "CODEX_THREAD_ID": "synthetic-codex", "HELIXHARNESS_SESSION_ID": "foreign"}):
                result = metrics.start(self.repo, "cursor", "demo", "propose", "user_command")
            self.assertEqual(result["reason"], "cursor_session_context_unavailable")
        self.assertFalse((self.repo / "docs/eval").exists())

    def test_native_events_dlp_own_source_and_same_pipeline(self):
        self.consent()
        started = self.start()
        self.assertEqual(self.event("beforeSubmitPrompt", prompt="Synthetic question password=synthetic-secret contact user@example.test"), 1)
        self.event("postToolUse", tool_name="Read", tool_use_id="read-1", tool_input={"file_path": "/synthetic/docs/knowledge/index.md", "secret": "NEVER_STORE"}, tool_output="NEVER_STORE", duration=100)
        self.event("postToolUseFailure", tool_name="Shell", tool_use_id="shell-1", failure_type="timeout", error_message="NEVER_STORE", tool_input={"command": "NEVER_STORE"})
        self.event("postToolUse", tool_name="Task", tool_use_id="task-1", tool_output="NEVER_STORE")
        self.event("postToolUse", tool_name="Task", tool_use_id="task-1", tool_output="NEVER_STORE")
        self.event("afterAgentResponse", at="2026-11-18T01:00:03.000Z", text="Synthetic answer", input_tokens=150, output_tokens=10, cache_read_tokens=30, cache_write_tokens=20)
        self.event("stop", at="2026-11-18T01:00:04.000Z", input_tokens=150, output_tokens=10, cache_read_tokens=30, cache_write_tokens=20)
        self.finish()
        locator = telemetry_ledger.load_locator(self.repo, started["entry_id"])
        source = Path(locator["session_log_path"])
        self.assertNotIn("NEVER_STORE", source.read_text())
        self.assertNotIn("synthetic-secret", source.read_text())
        self.assertEqual(self.adapter.validate_session_source(source, self.repo), str(source))
        supplement = telemetry_collector.collect_entry(self.repo, started["entry_id"], home=self.home)
        self.assertEqual(supplement["usage"]["total_tokens"], 160)
        self.assertEqual(supplement["usage"]["collection_status"], "collected")
        self.assertEqual(supplement["usage"]["scope"], "parent_only")
        self.assertEqual(supplement["execution"]["tool_call_count"], 3)
        self.assertEqual(supplement["execution"]["subagent_spawn_count"], 1)
        self.assertEqual(supplement["execution"]["context_reads"]["knowledge"], 1)
        self.assertEqual(supplement["session"]["session_id"], self.payload["conversation_id"])
        self.assertNotIn("synthetic-secret", json.dumps(supplement))
        self.assertNotIn("NEVER_STORE", json.dumps(supplement))
        records = telemetry_async.Outbox(self.home).records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]["platform"], "cursor")
        ledger = json.loads(Path(started["path"]).read_text())
        self.assertNotIn("input_tokens", json.dumps(ledger))
        self.assertNotIn(self.payload["conversation_id"], json.dumps(ledger))

    def test_real_order_first_prompt_before_start_and_last_response_after_finish(self):
        self.consent()
        self.assertEqual(self.event("beforeSubmitPrompt", at="2026-11-18T00:59:59.000Z", prompt="Start workflow"), 0)
        started = self.start()
        self.event("postToolUse", tool_name="Shell", tool_use_id="build-1", duration=10)
        self.finish()
        self.assertEqual(self.event("afterAgentResponse", at="2026-11-18T01:00:11.000Z", text="Completed", input_tokens=50, output_tokens=5), 0)
        result = telemetry_collector.collect_entry(self.repo, started["entry_id"], home=self.home)
        self.assertEqual(result["usage"]["collection_status"], "unavailable")
        self.assertNotIn("input_tokens", result["usage"])
        self.assertEqual(result["execution"]["tool_call_count"], 1)

    def test_native_empty_cwd_counts_tool_once_in_current_stage(self):
        self.consent()
        started = self.start()
        for _ in range(2):
            self.assertEqual(self.event("postToolUse", cwd="", tool_name="Shell", tool_use_id="empty-cwd-tool"), 1)
        self.finish()
        result = telemetry_collector.collect_entry(self.repo, started["entry_id"], home=self.home)
        self.assertEqual(result["execution"]["tool_call_count"], 1)

    def test_empty_cwd_does_not_relax_identity_or_invalid_directory_checks(self):
        self.consent()
        started = self.start()
        for cwd in (None, 0, [], ".", "../other", str(self.root / "other")):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.event("postToolUse", cwd=cwd, tool_name="Shell", tool_use_id="invalid-cwd"), 0)
        self.assertEqual(self.event("postToolUse", cwd="", conversation_id="foreign-session", tool_name="Shell", tool_use_id="foreign"), 0)
        self.finish()
        result = telemetry_collector.collect_entry(self.repo, started["entry_id"], home=self.home)
        self.assertEqual(result["execution"]["tool_call_count"], 0)

    def test_empty_authorized_journal_is_valid_without_tokens(self):
        self.consent()
        started = self.start()
        self.finish()
        result = telemetry_collector.collect_entry(self.repo, started["entry_id"], home=self.home)
        self.assertEqual(result["usage"]["collection_status"], "unavailable")
        self.assertNotIn("total_tokens", result["usage"])

    def test_subagents_use_native_ids_and_do_not_duplicate_task_callbacks(self):
        self.consent()
        started = self.start()
        fields = {"subagent_id": "child-1", "subagent_type": "generalPurpose", "generation_id": self.payload["conversation_id"],
                  "task": "NEVER_STORE", "summary": "NEVER_STORE", "agent_transcript_path": "/synthetic/never-read"}
        for _ in range(2):
            self.assertEqual(self.event("subagentStart", **fields), 1)
        self.event("subagentStop", **fields, status="completed", duration_ms=500, loop_count=0)
        self.event("subagentStop", **fields, status="completed", duration_ms=500, loop_count=0)
        self.event("subagentStop", **fields, status="completed", duration_ms=700, loop_count=1)
        self.assertEqual(self.event("subagentStart", subagent_type="unknown"), 0)
        self.event("postToolUse", tool_name="Task", tool_use_id="task-1")
        self.finish()
        result = telemetry_collector.collect_entry(self.repo, started["entry_id"], home=self.home)
        self.assertEqual(result["execution"]["subagent_spawn_count"], 1)
        self.assertEqual(result["execution"]["tool_call_count"], 1)
        self.assertEqual(result["execution"]["subagents"], [{"name": "generalPurpose", "status": "completed", "duration_ms": 700}])
        self.assertNotIn("NEVER_STORE", json.dumps(result))
        self.assertEqual(result["usage"]["collection_status"], "unavailable")

    def test_archive_seals_source_and_delivery_releases_only_unreferenced_owned_journal(self):
        self.consent()
        first = self.start()
        self.event("postToolUse", tool_name="Shell", tool_use_id="first-tool")
        self.finish()
        final = self.start(at="2026-11-18T01:00:12Z", stage="archive")
        self.finish(at="2026-11-18T01:00:15Z", stage="archive")
        source = Path(telemetry_ledger.load_locator(self.repo, first["entry_id"])["session_log_path"])
        late = _normalized_event({**self.payload, "hook_event_name": "postToolUse", "tool_name": "Shell", "tool_use_id": "late-backdated-tool"}, "2026-11-18T01:00:05Z")
        with source.open("a") as stream:
            stream.write(json.dumps(late) + "\n")
        report = telemetry_collector.collect_finished(self.repo, home=self.home, platform="cursor")
        self.assertEqual(report["collected"], 2, report)
        records = telemetry_async.Outbox(self.home).records()
        self.assertTrue(all(record["final_for_entry"] for _, record in records))
        first_supplement = next(record["supplement"] for _, record in records if record["supplement"]["entry_id"] == first["entry_id"])
        self.assertEqual(first_supplement["execution"]["tool_call_count"], 1)
        self.assertEqual(telemetry_collector.collect_finished(self.repo, home=self.home, platform="cursor")["collected"], 0)
        result = telemetry_async.run_once(self.home,
            sender=lambda endpoint, supplements: [{"supplement_id": item["supplement_id"], "status": "accepted"} for item in supplements],
            health_sender=lambda endpoint, payload: "accepted", batch_pause_seconds=0)
        self.assertEqual(result["accepted"], 2, result)
        self.assertFalse(source.exists())
        self.assertTrue(Path(first["path"]).is_file())
        self.assertTrue(Path(final["path"]).is_file())

    def test_clean_installed_runtime_uses_snapshot_for_all_hosts_without_checkout_imports(self):
        telemetry_async.install_runtime(ROOT, self.home)
        runtime = self.home / "runtime"
        code = (
            "import json,sys;sys.path.insert(0,sys.argv[1]);"
            "from telemetry_hosts import get_host_adapter,supported_host_ids;"
            "print(json.dumps({'hosts':supported_host_ids(),'ids':[get_host_adapter(x).host_id for x in supported_host_ids()],"
            "'unknown':get_host_adapter('unregistered') is None,'source_imported':'host_runtime' in sys.modules}))"
        )
        clean = {**self.env, "PYTHONPATH": ""}
        result = subprocess.run([sys.executable, "-I", "-c", code, str(runtime)], cwd=self.root, env=clean, text=True, capture_output=True, check=True)
        output = json.loads(result.stdout)
        self.assertEqual(set(output["hosts"]), {"claude-code", "codex", "cursor"})
        self.assertEqual(output["hosts"], output["ids"])
        self.assertTrue(output["unknown"])
        self.assertFalse(output["source_imported"])
        # Execute the shipped CLI itself; empty endpoint returns a controlled
        # skip and never attempts network or OS scheduler operations.
        (self.home / "config.json").write_text('{"endpoint":""}')
        cli = subprocess.run([sys.executable, "-I", str(runtime / "telemetryctl.py"), "--home", str(self.home), "run", "--all"], cwd=self.root, env=clean, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(cli.stdout)["status"], "skipped")
        self.assertTrue(telemetry_async._runtime_files_current(ROOT, self.home))
        (runtime / RUNTIME_SNAPSHOT_NAME).unlink()
        self.assertFalse(telemetry_async._runtime_files_current(ROOT, self.home))
        failed = subprocess.run([sys.executable, "-I", "-c", code, str(runtime)], cwd=self.root, env=clean, text=True, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)

    def test_snapshot_rejects_duplicate_identity_or_external_import(self):
        good = build_runtime_snapshot(ROOT)
        for mutation in (lambda payload: payload["hosts"].append(payload["hosts"][0]),
                         lambda payload: payload["hosts"][0].update(adapter="python:os:system"),
                         lambda payload: payload.update(schema="unknown"),
                         lambda payload: payload["hosts"][0].update(aliases=["../cursor"])):
            payload = json.loads(json.dumps(good))
            mutation(payload)
            with self.assertRaises(ValueError):
                _RuntimeRegistry(payload)

    def test_disabled_endpoint_keeps_only_basic_metadata(self):
        self.consent()
        (self.home / "config.json").write_text('{"endpoint":""}')
        started = self.start()
        self.assertEqual(self.event("beforeSubmitPrompt", prompt="Not collected"), 0)
        self.finish()
        source = Path(telemetry_ledger.load_locator(self.repo, started["entry_id"])["session_log_path"])
        self.assertEqual(len(source.read_text().splitlines()), 1)
        self.assertTrue(Path(started["path"]).is_file())

    def test_parallel_conversations_epochs_and_disable_cleanup(self):
        self.consent()
        first = self.start()
        source1 = Path(telemetry_ledger.load_locator(self.repo, first["entry_id"])["session_log_path"])
        with patch.dict(os.environ, {"HELIXHARNESS_CURSOR_CONVERSATION_ID": "synthetic-conversation-2"}):
            second = self.start(at="2026-11-18T01:00:00.001Z")
        source2 = Path(telemetry_ledger.load_locator(self.repo, second["entry_id"])["session_log_path"])
        self.assertNotEqual(source1, source2)
        self.event("beforeSubmitPrompt", prompt="Only conversation one")
        self.assertIn("Only conversation one", source1.read_text())
        self.assertNotIn("Only conversation one", source2.read_text())
        self.consent("disabled")
        self.assertFalse(source1.exists())
        self.assertFalse(source2.exists())
        self.consent()
        third = self.start(at="2026-11-18T01:00:12.000Z")
        source3 = Path(telemetry_ledger.load_locator(self.repo, third["entry_id"])["session_log_path"])
        self.assertNotEqual(source1, source3)

    def test_workspace_without_cwd_visits_only_registered_active_children(self):
        sources = []
        children = []
        for name in ("api", "web"):
            child = self.repo / name
            child.mkdir()
            subprocess.run(["git", "init", "-q", str(child)], env=self.env, check=True)
            telemetry_consent.set_mode(child, "cursor", self.adapter.consent_cache_path(), "project", "enabled",
                                       consent_source="explicit-command", user_confirmed=True)
            started = metrics.start(child, "cursor", "demo", "propose", "user_command")
            self.assertEqual(started["status"], "started")
            sources.append(Path(telemetry_ledger.load_locator(child, started["entry_id"])["session_log_path"]))
            children.append(child)
            telemetry_async.register_repository(child, "cursor", self.home)
        payload = {key: value for key, value in self.payload.items() if key != "cwd"}
        payload.update(hook_event_name="beforeSubmitPrompt", prompt="Synthetic workspace decision")
        self.assertEqual(record_hook(payload), 0)
        self.assertTrue(all("Synthetic workspace decision" not in source.read_text() for source in sources))
        self.assertTrue(all("ambiguous_workspace" in source.read_text() for source in sources))
        self.assertEqual(record_hook({**payload, "cwd": "", "hook_event_name": "postToolUse",
                                      "tool_name": "Shell", "tool_use_id": "ambiguous-tool"}), 0)
        for source in sources:
            rows = [json.loads(line) for line in source.read_text().splitlines()[1:]]
            self.assertEqual(rows[-1]["type"], "collection_gap")
            self.assertFalse(any(row.get("type") == "tool" for row in rows))
        # A native cwd selects one repository even when two stages are active.
        self.assertEqual(record_hook({**payload, "cwd": str(children[0]), "prompt": "Only api"}), 1)
        self.assertIn("Only api", sources[0].read_text())
        self.assertNotIn("Only api", sources[1].read_text())
        # Once the second stage finishes, the remaining active one is unique.
        metrics.finish(children[1], "cursor", "demo", "propose", "completed", None, {})
        self.assertEqual(record_hook({**payload, "prompt": "Single active repository"}), 1)
        self.assertIn("Single active repository", sources[0].read_text())
        self.assertNotIn("Single active repository", sources[1].read_text())
        outside = self.root / "outside-workspace"
        outside.mkdir()
        subprocess.run(["git", "init", "-q", str(outside)], env=self.env, check=True)
        telemetry_consent.set_mode(outside, "cursor", self.adapter.consent_cache_path(), "project", "enabled",
                                   consent_source="explicit-command", user_confirmed=True)
        with patch.dict(os.environ, {"HELIXHARNESS_CURSOR_WORKSPACE_ROOTS": json.dumps([str(outside)])}):
            outside_started = metrics.start(outside, "cursor", "demo", "propose", "user_command")
        outside_source = Path(telemetry_ledger.load_locator(outside, outside_started["entry_id"])["session_log_path"])
        telemetry_async.register_repository(outside, "cursor", self.home)
        before = outside_source.read_bytes()
        self.assertEqual(record_hook({**payload, "prompt": "Bound to original workspace"}), 1)
        self.assertEqual(record_hook({**payload, "cwd": str(outside)}), 0)
        self.assertEqual(before, outside_source.read_bytes())
        payload["conversation_id"] = "different-conversation"
        self.assertEqual(record_hook(payload), 0)

    def test_source_paths_cannot_be_external_or_symlinked(self):
        self.consent()
        self.assertIsNone(self.adapter.validate_session_source(self.root / "external.jsonl", self.repo))
        target = self.repo / ".helixharness"
        target.mkdir(exist_ok=True)
        outside = self.root / "outside"
        outside.mkdir()
        (target / "telemetry-v2").symlink_to(outside)
        with self.assertRaises(ValueError):
            metrics.start(self.repo, "cursor", "demo", "propose", "user_command")
        self.assertEqual(list(outside.iterdir()), [])

    def test_complete_cli_chain_is_authorized_and_does_not_install_scheduler(self):
        # CLI subprocesses use the isolated homes above; empty endpoint prevents
        # any runtime/scheduler operation. Re-enable the synthetic endpoint only
        # for direct collector/Outbox checks, never make a network request.
        (self.home / "config.json").write_text('{"endpoint":""}')
        def cli(name, *args):
            result = subprocess.run([sys.executable, str(ROOT / "scripts" / name), "--repo-root", str(self.repo), "--platform", "cursor", *args], env=self.env, text=True, capture_output=True, check=True)
            return json.loads(result.stdout)
        self.assertEqual(cli("telemetry_consent.py", "resolve")["effective_mode"], "disabled")
        cli("telemetry_consent.py", "set", "--scope", "project", "--mode", "enabled", "--source", "explicit-command", "--user-confirmed")
        started = cli("metrics.py", "start", "--feature", "cli-demo", "--stage", "apply", "--trigger", "user_command")
        self.assertEqual(started["status"], "started")
        finished = cli("metrics.py", "finish", "--feature", "cli-demo", "--stage", "apply", "--status", "completed")
        self.assertEqual(finished["status"], "recorded")
        self.assertFalse((self.home / "runtime-manifest.json").exists())


class CursorUsageTest(unittest.TestCase):
    def window(self, records):
        return _usage(records, _timestamp("2026-11-18T01:00:00Z"), _timestamp("2026-11-18T01:01:00Z"))

    def record(self, event, *, generation="g1", at="2026-11-18T01:00:02Z", **fields):
        return _normalized_event({"hook_event_name": event, "generation_id": generation, **fields}, at)

    def test_duplicate_snapshots_and_distinct_generations(self):
        records = []
        for generation in ("g1", "g2"):
            records.append(self.record("beforeSubmitPrompt", generation=generation))
            records.append(self.record("afterAgentResponse", generation=generation, input_tokens=150, output_tokens=10, cache_read_tokens=30, cache_write_tokens=20))
            records.append(self.record("stop", generation=generation, input_tokens=150, output_tokens=10, cache_read_tokens=30, cache_write_tokens=20))
        result = self.window(records + records)
        self.assertEqual(result["total_tokens"], 320)
        self.assertEqual(result["generations"], 2)

    def test_missing_usage_can_be_completed_by_stop_but_never_zero_filled(self):
        records = [self.record("beforeSubmitPrompt"), self.record("afterAgentResponse", text="Answer")]
        self.assertEqual(self.window(records)["collection_status"], "unavailable")
        records.append(self.record("stop", input_tokens=5, output_tokens=2))
        result = self.window(records)
        self.assertEqual(result["total_tokens"], 7)
        self.assertNotIn("cache_read_tokens", result)

    def test_invalid_partial_and_conflicting_totals_remain_unavailable(self):
        for values in ({"input_tokens": 1}, {"input_tokens": True, "output_tokens": 1},
                       {"input_tokens": -1, "output_tokens": 1}, {"input_tokens": 2**54, "output_tokens": 1}):
            result = self.window([self.record("beforeSubmitPrompt"), self.record("stop", **values)])
            self.assertEqual(result["collection_status"], "unavailable")
            self.assertNotIn("total_tokens", result)
        records = [self.record("beforeSubmitPrompt"), self.record("afterAgentResponse", input_tokens=5, output_tokens=2), self.record("stop", input_tokens=7, output_tokens=2)]
        self.assertEqual(self.window(records)["collection_status"], "unavailable")

    def test_cross_window_and_missing_start_are_not_attributed(self):
        records = [self.record("beforeSubmitPrompt", at="2026-11-18T00:59:59Z"), self.record("stop", input_tokens=10, output_tokens=2)]
        self.assertEqual(self.window(records)["collection_status"], "unavailable")
        self.assertEqual(self.window(records[1:])["collection_status"], "unavailable")

    def test_child_generation_does_not_create_parent_token_partial(self):
        records = [self.record("beforeSubmitPrompt"), self.record("stop", input_tokens=10, output_tokens=2),
                   self.record("subagentStart", generation="parent-conversation", subagent_id="child-1"),
                   self.record("subagentStop", generation="parent-conversation", subagent_id="child-1", status="completed")]
        result = self.window(records)
        self.assertEqual(result["collection_status"], "collected")
        self.assertEqual(result["generations"], 1)
        self.assertEqual(result["total_tokens"], 12)

    def test_contradictory_cache_breakdowns_are_not_silently_selected(self):
        start = self.record("beforeSubmitPrompt")
        for records in (
            [start, self.record("stop", input_tokens=100, output_tokens=10, cache_read_tokens=60, cache_write_tokens=60)],
            [start, self.record("afterAgentResponse", input_tokens=100, output_tokens=10, cache_read_tokens=30),
             self.record("stop", input_tokens=100, output_tokens=10, cache_read_tokens=40)],
        ):
            result = self.window(records)
            self.assertEqual(result["collection_status"], "unavailable")
            self.assertNotIn("total_tokens", result)

    def test_workspace_ambiguity_is_explicit_without_numeric_duplication(self):
        gap = {"type": "collection_gap", "reason": "ambiguous_workspace", "timestamp": "2026-11-18T01:00:02Z"}
        result = self.window([gap])
        self.assertEqual(result["collection_status"], "unavailable")
        self.assertEqual(result["unavailable_reason"], "ambiguous_workspace")
        self.assertNotIn("total_tokens", result)
        records = [self.record("beforeSubmitPrompt"), self.record("stop", input_tokens=10, output_tokens=2), gap]
        self.assertEqual(self.window(records)["collection_status"], "partial")


if __name__ == "__main__":
    unittest.main()
