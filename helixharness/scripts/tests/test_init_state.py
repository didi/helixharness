"""Synthetic Init state transitions and parent-owned completion contracts."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_hooks_cli import project_hooks

PLUGIN = Path(__file__).resolve().parents[2]
SPEC = importlib.util.find_spec("init_state")
if SPEC is not None:
    import init_state


class InitStateTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SPEC, "Init needs a deterministic parent-only state writer")
        temporary = tempfile.TemporaryDirectory(prefix="helix-init-state-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve()
        self.state_path = self.repo / "docs/.helixharness-init-state.json"
        self.state_path.parent.mkdir()
        self.initial = {
            "projectType": "backend", "scanPlan": [], "confidence": 0.95,
            "scanStatus": "pending", "bootstrapStatus": "pending",
            "scanTimings": {"scan-tech": 1.2}, "extension": {"keep": [1, 2]},
        }
        self.write_state(self.initial)
        self.files = {
            "knowledge": ["docs/knowledge/index.md", "docs/knowledge/project-context.md"],
            "harness": ["docs/harness/index.md"],
            "hooks": ["docs/hooks/index.md"],
        }
        for relative in (p for paths in self.files.values() for p in paths):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Synthetic Init artifact\n")
        self.gates = {"artifacts": True, "hooks": True, "telemetry": True}

    def write_state(self, value):
        self.state_path.write_text(json.dumps(value))

    def state(self):
        return json.loads(self.state_path.read_text())

    def change(self, action, mode="service", **kwargs):
        return init_state.transition(self.repo, mode, action, **kwargs)

    def scanned(self, mode="service"):
        return self.change("scan-complete", mode, now=100)

    def complete(self, mode="service", **kwargs):
        return self.change("complete", mode, deliverables=self.files,
                           verified=self.gates, now=300, **kwargs)

    def test_scan_checkpoint_preserves_extensions_and_does_not_complete(self):
        self.scanned()
        state = self.state()
        self.assertEqual(state["scanStatus"], "completed")
        self.assertEqual(state["bootstrapStatus"], "pending")
        self.assertNotIn("completedAt", state)
        self.assertEqual(state["scanPlan"], init_state.SCAN_PLANS["service"])
        self.assertEqual(state["extension"], self.initial["extension"])
        self.assertEqual(state["scanTimings"], self.initial["scanTimings"])

    def test_scan_plan_nonempty_is_preserved(self):
        self.initial["scanPlan"] = ["synthetic-prevalidated-scan"]
        self.write_state(self.initial)
        self.scanned()
        self.assertEqual(self.state()["scanPlan"], self.initial["scanPlan"])

    def test_service_and_frontend_complete_after_gates(self):
        for mode, project_type in (("service", "unknown"), ("frontend", "frontend")):
            with self.subTest(mode=mode):
                self.write_state({**self.initial, "projectType": project_type})
                self.scanned(mode)
                self.complete(mode)
                state = self.state()
                self.assertEqual(state["bootstrapStatus"], "completed")
                self.assertEqual(state["completedAt"], 300)
                self.assertEqual(state["extension"], self.initial["extension"])

    def test_each_missing_parent_gate_leaves_state_unchanged(self):
        self.scanned()
        for key in self.gates:
            with self.subTest(gate=key):
                before = self.state_path.read_bytes()
                gates = {**self.gates, key: False}
                with self.assertRaisesRegex(init_state.InitStateError, "init_completion_gates_incomplete"):
                    self.change("complete", deliverables=self.files, verified=gates, now=300)
                self.assertEqual(self.state_path.read_bytes(), before)
                self.assertNotIn("completedAt", self.state())

    def test_complete_requires_scanning_fact(self):
        with self.assertRaisesRegex(init_state.InitStateError, "init_scan_incomplete"):
            self.complete()
        self.assertNotIn("completedAt", self.state())

    def test_missing_corrupt_empty_nonobject_state_is_not_rebuilt(self):
        for raw in (None, b"", b"{bad", b"[]", b"null", b'"text"'):
            with self.subTest(raw=raw):
                if raw is None:
                    self.state_path.unlink(missing_ok=True)
                else:
                    self.state_path.write_bytes(raw)
                with self.assertRaises(init_state.InitStateError):
                    self.scanned()
                if raw is None:
                    self.assertFalse(self.state_path.exists())
                else:
                    self.assertEqual(self.state_path.read_bytes(), raw)

    def test_bad_types_and_mode_mismatch_are_blocked(self):
        for changes in ({"scanPlan": "not-array"}, {"scanPlan": [False]},
                        {"projectType": "frontend"}, {"bootstrapStatus": []}):
            with self.subTest(changes=changes):
                self.write_state({**self.initial, **changes})
                before = self.state_path.read_bytes()
                with self.assertRaises(init_state.InitStateError):
                    self.scanned()
                self.assertEqual(self.state_path.read_bytes(), before)

    def test_deliverables_are_sorted_unique_and_checked_before_write(self):
        self.scanned()
        self.files["knowledge"] = list(reversed(self.files["knowledge"])) * 2
        self.complete()
        for group, paths in self.files.items():
            recorded = self.state()["deliverables"][group]
            self.assertEqual(recorded["files"], sorted(set(paths)))
            self.assertEqual(recorded["count"], len(set(paths)))

    def test_missing_empty_or_escaping_deliverable_does_not_complete(self):
        self.scanned()
        for path in ("docs/knowledge/missing.md", "docs/knowledge/empty.md", "../outside.md", "/tmp/x"):
            with self.subTest(path=path):
                (self.repo / "docs/knowledge/empty.md").write_text("")
                manifest = {**self.files, "knowledge": [path]}
                before = self.state_path.read_bytes()
                with self.assertRaises(init_state.InitStateError):
                    self.change("complete", deliverables=manifest, verified=self.gates)
                self.assertEqual(self.state_path.read_bytes(), before)

    def test_fullstack_preserves_backend_checkpoint_until_final_success(self):
        for project_type in ("fullstack-node", "fullstack-separated", "monorepo"):
            with self.subTest(project_type=project_type):
                self.write_state({**self.initial, "projectType": project_type})
                self.scanned("fullstack")
                self.change("backend-complete", "fullstack", deliverables=self.files,
                            verified={"artifacts": True}, now=200)
                checkpoint = self.state_path.read_bytes()
                self.assertEqual(self.state()["bootstrapStatus"], "backend_completed")
                self.assertNotIn("completedAt", self.state())
                # Frontend cancellation/failure invokes no completion transition.
                self.assertEqual(self.state_path.read_bytes(), checkpoint)
                with self.assertRaises(init_state.InitStateError):
                    self.change("complete", "fullstack", deliverables=self.files,
                                verified={**self.gates, "hooks": False}, now=300)
                self.assertEqual(self.state_path.read_bytes(), checkpoint)
                self.complete("fullstack")
                self.assertEqual(self.state()["backendCompletedAt"], 200)
                self.assertEqual(self.state()["frontendCompletedAt"], 300)
                self.assertEqual(self.state()["completedAt"], 300)

    def test_fullstack_requires_backend_checkpoint(self):
        self.write_state({**self.initial, "projectType": "fullstack-node"})
        self.scanned("fullstack")
        with self.assertRaisesRegex(init_state.InitStateError, "backend_bootstrap_incomplete"):
            self.complete("fullstack")
        self.assertNotIn("completedAt", self.state())

    def test_backend_checkpoint_does_not_require_parent_owned_settings(self):
        self.write_state({**self.initial, "projectType": "fullstack-separated"})
        self.scanned("fullstack")
        self.change("backend-complete", "fullstack", deliverables=self.files,
                    verified={"artifacts": True}, now=200)
        self.assertEqual(self.state()["bootstrapStatus"], "backend_completed")
        self.assertFalse((self.repo / ".cursor").exists())
        self.assertFalse((self.repo / ".codex").exists())

    def test_fullstack_cannot_drop_verified_backend_deliverables(self):
        self.write_state({**self.initial, "projectType": "fullstack-node"})
        self.scanned("fullstack")
        self.change("backend-complete", "fullstack", deliverables=self.files,
                    verified={"artifacts": True}, now=200)
        before = self.state_path.read_bytes()
        incomplete = {**self.files, "knowledge": self.files["knowledge"][:1]}
        with self.assertRaisesRegex(init_state.InitStateError, "backend_deliverables_missing"):
            self.change("complete", "fullstack", deliverables=incomplete, verified=self.gates)
        self.assertEqual(self.state_path.read_bytes(), before)
        frontend = self.repo / "docs/knowledge/frontend.md"
        frontend.write_text("Synthetic frontend artifact\n")
        combined = {**self.files, "knowledge": self.files["knowledge"] + ["docs/knowledge/frontend.md"]}
        self.change("complete", "fullstack", deliverables=combined, verified=self.gates, now=300)
        self.assertEqual(set(self.state()["deliverables"]["knowledge"]["files"]), set(combined["knowledge"]))

    def test_malformed_project_type_and_checkpoint_time_are_blocked(self):
        for state in ({**self.initial, "projectType": []},
                      {**self.initial, "projectType": "fullstack-node", "backendCompletedAt": "yesterday"}):
            with self.subTest(state=state):
                self.write_state(state)
                before = self.state_path.read_bytes()
                with self.assertRaises(init_state.InitStateError):
                    self.scanned("fullstack")
                self.assertEqual(self.state_path.read_bytes(), before)

    def test_detection_reinitializes_a_previously_completed_run(self):
        self.scanned()
        self.complete()
        (self.repo / "pom.xml").write_text("<project/>\n")
        protocol = (PLUGIN / "reference/project-type-detection.md").read_text()
        script = protocol.split("```bash\n", 1)[1].split("```", 1)[0]
        process = subprocess.run(["bash", "-c", script], cwd=self.repo,
                                 text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(self.state()["bootstrapStatus"], "pending")
        self.assertEqual(self.state()["scanStatus"], "pending")
        self.assertNotIn("completedAt", self.state())
        self.assertNotIn("deliverables", self.state())

    def test_checkpoint_is_idempotent_and_preserves_metadata(self):
        self.write_state({**self.initial, "projectType": "fullstack-node",
                          "deliverables": {"extension": {"keep": True},
                                           "knowledge": {"review": "parent"}}})
        self.scanned("fullstack")
        self.change("backend-complete", "fullstack", deliverables=self.files,
                    verified={"artifacts": True}, now=200)
        before = self.state_path.read_bytes()
        result = self.change("backend-complete", "fullstack", deliverables=self.files,
                             verified={"artifacts": True}, now=999)
        self.assertFalse(result["changed"])
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self.state()["deliverables"]["extension"], {"keep": True})
        self.assertEqual(self.state()["deliverables"]["knowledge"]["review"], "parent")

    def test_false_or_missing_gate_is_not_a_truthy_declaration(self):
        self.scanned()
        for value in ("true", 1, None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(init_state.InitStateError):
                    self.change("complete", deliverables=self.files,
                                verified={**self.gates, "hooks": value})
                self.assertNotIn("completedAt", self.state())

    def test_repeated_success_does_not_rewrite_completion_or_mtime(self):
        self.scanned()
        self.complete()
        before, mtime = self.state_path.read_bytes(), self.state_path.stat().st_mtime_ns
        result = self.change("complete", deliverables=self.files, verified=self.gates, now=999)
        self.assertFalse(result["changed"])
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self.state_path.stat().st_mtime_ns, mtime)

    def test_completed_state_cannot_be_mutated_into_another_run(self):
        self.scanned()
        self.complete()
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(init_state.InitStateError, "init_state_already_completed"):
            self.scanned()
        self.assertEqual(self.state_path.read_bytes(), before)
        self.write_state(copy.deepcopy(self.initial))  # Step 0 starts the next run.
        self.scanned()
        with self.assertRaises(init_state.InitStateError):
            self.change("complete", deliverables=self.files, verified={"artifacts": True})
        self.assertNotIn("completedAt", self.state())

    def test_atomic_replace_failure_preserves_checkpoint_and_no_temp_files(self):
        self.scanned()
        before = self.state_path.read_bytes()
        with patch.object(init_state.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(init_state.InitStateError):
                self.complete()
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(list(self.state_path.parent.glob(".helixharness-init-state-*.tmp")), [])

    def test_workspace_is_rejected_without_writes(self):
        before = self.state_path.read_bytes()
        with self.assertRaisesRegex(init_state.InitStateError, "unsupported_init_mode"):
            self.change("complete", "workspace", deliverables=self.files, verified=self.gates)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_symlink_state_is_rejected_without_touching_target(self):
        outside = self.repo / "external-state.json"
        outside.write_bytes(self.state_path.read_bytes())
        self.state_path.unlink()
        self.state_path.symlink_to(outside)
        before = outside.read_bytes()
        with self.assertRaises(init_state.InitStateError):
            self.scanned()
        self.assertEqual(outside.read_bytes(), before)

    def test_cli_requires_gates_and_reports_blocked_json(self):
        self.scanned()
        cmd = [sys.executable, str(PLUGIN / "scripts/init_state.py"),
               "--repo-root", str(self.repo), "--mode", "service", "complete",
               "--deliverables-json", json.dumps(self.files)]
        failed = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(json.loads(failed.stdout)["status"], "blocked")
        self.assertNotIn("completedAt", self.state())
        passed = subprocess.run(cmd + ["--artifacts-verified", "--hooks-verified", "--telemetry-verified"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["bootstrap_status"], "completed")

    def test_three_host_hook_failure_then_merge_and_complete(self):
        # Simulate the parent's real hook gate; proof flags are declarations, not an independent oracle.
        for platform in ("claude-code", "codex", "cursor"):
            with self.subTest(platform=platform):
                self.write_state(self.initial)
                self.scanned()
                with self.assertRaises(ValueError):
                    project_hooks(self.repo, platform, "check")
                self.assertNotIn("completedAt", self.state())
                project_hooks(self.repo, platform, "merge")
                project_hooks(self.repo, platform, "check")
                self.complete()
                self.assertEqual(self.state()["bootstrapStatus"], "completed")

    def test_three_hosts_all_modes_preserve_settings_and_fail_closed_on_broken_hooks(self):
        from host_runtime import HostRegistry
        from host_runtime.project_hooks import load_project_hooks

        for platform in ("claude-code", "codex", "cursor"):
            contract = load_project_hooks(HostRegistry.discover(PLUGIN / "hosts").require(platform))
            target = self.repo / contract.settings_file
            for mode, project_type in (("service", "backend"), ("frontend", "frontend"),
                                       ("fullstack", "fullstack-separated")):
                with self.subTest(platform=platform, mode=mode):
                    self.write_state({**self.initial, "projectType": project_type})
                    self.scanned(mode)
                    if mode == "fullstack":
                        self.change("backend-complete", mode, deliverables=self.files,
                                    verified={"artifacts": True}, now=200)
                    checkpoint = self.state_path.read_bytes()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    for malformed in ("{broken", "[]", '{"hooks": []}'):
                        target.write_text(malformed)
                        with self.assertRaises(ValueError):
                            project_hooks(self.repo, platform, "check")
                        self.assertEqual(self.state_path.read_bytes(), checkpoint)
                    settings = {"userSetting": {"keep": [1, 2]}}
                    target.write_text(json.dumps(settings))
                    project_hooks(self.repo, platform, "merge")
                    project_hooks(self.repo, platform, "check")
                    self.assertEqual(json.loads(target.read_text())["userSetting"], settings["userSetting"])
                    first_merge = target.read_bytes()
                    project_hooks(self.repo, platform, "merge")
                    self.assertEqual(target.read_bytes(), first_merge)
                    self.complete(mode)
                    self.assertEqual(self.state()["bootstrapStatus"], "completed")


class InitParentContractTest(unittest.TestCase):
    def test_bootstrap_prompts_do_not_commit_final_state_or_announce_overall_completion(self):
        for filename in ("init-service-bootstrap.md", "init-frontend-bootstrap.md"):
            with self.subTest(filename=filename):
                text = (PLUGIN / "agents" / filename).read_text()
                self.assertNotIn("`bootstrapStatus=completed`", text)
                self.assertNotIn("## HelixHarness 初始化完成", text)
                self.assertNotIn("## HelixHarness 前端初始化完成", text)
                self.assertIn("父流程", text)
                self.assertIn("不得写入", text)

    def test_parent_finalizes_after_hook_and_telemetry_gates(self):
        text = (PLUGIN / "commands/init.md").read_text()
        final = text.index("#### Step 4：父流程提交最终状态")
        self.assertGreater(final, text.index("BLOCKED: project_hooks_incomplete"))
        self.assertGreater(final, text.index("WARN:usage_telemetry_gitignored"))
        self.assertIn("scripts/init_state.py", text[final:])
        self.assertIn("--artifacts-verified --hooks-verified --telemetry-verified", text[final:])
        self.assertIn("backend-complete", text)
        self.assertIn("不得调用", text[final:])

    def test_detection_starts_new_noncompleted_state(self):
        text = (PLUGIN / "reference/project-type-detection.md").read_text()
        self.assertIn('"bootstrapStatus": "pending"', text)
        self.assertIn('"scanStatus": "pending"', text)
        self.assertNotIn('"completedAt":', text)

    def test_ci_validator_stays_out_of_business_command(self):
        text = (PLUGIN / "commands/init.md").read_text()
        self.assertNotIn("scripts/validate_init_outputs.py", text)

    def test_hook_owners_and_workspace_state_scope_are_preserved(self):
        for host, owner in (("claude-code", "bootstrap"), ("codex", "parent"), ("cursor", "parent")):
            declaration = json.loads((PLUGIN / "hosts" / host / "project-hooks.json").read_text())
            self.assertEqual(declaration["owner"], owner)
        text = (PLUGIN / "commands/init.md").read_text()
        self.assertIn("Workspace 跳过单仓检测和状态提交器", text)
        self.assertIn("--mode workspace check", text)

    def test_external_knowledge_defers_only_the_service_scan_checkpoint(self):
        text = (PLUGIN / "commands/init.md").read_text()
        self.assertIn("仅 service 分支因 `--knowledge` 跳过扫描", text)
        self.assertIn("已经完成扫描的 frontend/fullstack 仍立即提交扫描检查点", text)
        self.assertIn("service 的 `--knowledge` 导入分支", text)
        self.assertIn("仍按已确认计划执行，不因 `--knowledge` 跳过", text)


if __name__ == "__main__":
    unittest.main()
