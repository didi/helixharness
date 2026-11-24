"""Synthetic Cursor distribution, projection, and explicit D2C gate checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]
PLUGIN = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import cursor_skill_projection as projection  # noqa: E402
import env_sandbox  # noqa: E402


class CursorProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "plugin with spaces").resolve()
        (self.root / "commands").mkdir(parents=True)
        (self.root / "hosts/cursor").mkdir(parents=True)
        self.projection_path = self.root / "hosts/cursor/command-projection.json"
        self.projection_path.write_text(
            (PLUGIN / "hosts/cursor/command-projection.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        for name in projection.COMMANDS:
            (self.root / f"commands/{name}.md").write_text(
                f'---\ndescription: "Synthetic {name}"\n---\n\nHelixHarness 公共命令唯一契约\n',
                encoding="utf-8",
            )
        self.method = self.root / "skills/example/SKILL.md"
        self.method.parent.mkdir(parents=True)
        self.method.write_text(
            '---\nname: example\ndescription: "Synthetic method"\n'
            'user-invocable: false\nallowed-tools: Read, Bash\n'
            'hooks:\n  Stop:\n    command: forbidden-host-command\n---\n\nBusiness body.\n',
            encoding="utf-8",
        )

    def test_shipped_projection_is_current_and_covers_every_canonical_source(self) -> None:
        projection.check(PLUGIN)
        expected = projection.projected_skills(PLUGIN)
        methods = list((PLUGIN / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(expected), 9 + len(methods))
        for command in projection.COMMANDS:
            text = (PLUGIN / f"hosts/cursor/skills/helix-{command}/SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f"commands/{command}.md", text)
            self.assertIn("--platform cursor", text)
            self.assertIn("disable-model-invocation: true", text)
            self.assertNotIn("codex_entry_load", text)
            self.assertNotIn("spawn_agent", text)

    def test_roundtrip_is_deterministic_and_excludes_foreign_frontmatter(self) -> None:
        projection.write(self.root)
        projection.check(self.root)
        first = projection.projected_skills(self.root)
        projection.write(self.root)
        self.assertEqual(first, projection.projected_skills(self.root))
        text = (self.root / "hosts/cursor/skills/helix-example/SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        self.assertNotIn("hooks", frontmatter)
        self.assertNotIn("allowed-tools", frontmatter)
        self.assertNotIn("user-invocable", frontmatter)
        self.assertNotIn("forbidden-host-command", text)
        self.assertNotIn("Business body.", text)
        self.assertIn("skills/example/SKILL.md", text)
        self.assertIn("正文相对资源从 `skills/example/` 解析", text)

    def test_explicit_invocation_intent_is_preserved_for_method(self) -> None:
        self.method.write_text(
            "---\nname: example\ndescription: >-\n  Synthetic multiline\n  description.\n"
            "disable-model-invocation: true\n---\nBody\n", encoding="utf-8",
        )
        projected = projection.projected_skills(self.root)
        text = projected[self.root / "hosts/cursor/skills/helix-example/SKILL.md"]
        self.assertIn('description: "Synthetic multiline description."', text)
        self.assertIn("disable-model-invocation: true", text)

    def test_missing_or_duplicate_metadata_is_rejected(self) -> None:
        for metadata in (
            "name: example",
            "name: example\ndescription: one\ndescription: two",
            "name: other\ndescription: one",
            "name: example\ndescription: one\ndisable-model-invocation: sometimes",
        ):
            with self.subTest(metadata=metadata):
                self.method.write_text(f"---\n{metadata}\n---\n", encoding="utf-8")
                with self.assertRaises(projection.ProjectionError):
                    projection.projected_skills(self.root)

    def test_projection_rejects_wrong_command_sets_and_redirected_contract(self) -> None:
        original = json.loads(self.projection_path.read_text(encoding="utf-8"))
        for mutation in ("missing", "extra", "redirect", "surface"):
            payload = json.loads(json.dumps(original))
            if mutation == "missing":
                payload["entrypoints"].pop("propose")
            elif mutation == "extra":
                payload["entrypoints"]["publish"] = ["commands/publish.md"] * 2
            elif mutation == "redirect":
                payload["entrypoints"]["propose"][1] = "../outside.md"
            else:
                payload["surface"] = "native-command"
            self.projection_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(mutation=mutation), self.assertRaises(projection.ProjectionError):
                projection.projected_skills(self.root)

    def test_check_detects_source_drift_entry_drift_and_unexpected_skill(self) -> None:
        projection.write(self.root)
        entry = self.root / "hosts/cursor/skills/helix-propose/SKILL.md"
        entry.write_text(entry.read_text(encoding="utf-8") + "unexpected\n", encoding="utf-8")
        with self.assertRaises(projection.ProjectionError):
            projection.check(self.root)
        projection.write(self.root)
        canonical = self.root / "commands/propose.md"
        canonical.write_text(canonical.read_text(encoding="utf-8").replace("Synthetic propose", "Changed description"), encoding="utf-8")
        with self.assertRaises(projection.ProjectionError):
            projection.check(self.root)
        projection.write(self.root)
        extra = self.root / "hosts/cursor/skills/unexpected/SKILL.md"
        extra.parent.mkdir()
        extra.write_text("Unexpected", encoding="utf-8")
        with self.assertRaises(projection.ProjectionError):
            projection.check(self.root)

    def test_symlink_source_and_output_cannot_escape_plugin(self) -> None:
        outside = Path(self.temp.name) / "outside.md"
        outside.write_text("synthetic", encoding="utf-8")
        self.method.unlink()
        self.method.symlink_to(outside)
        with self.assertRaises(projection.ProjectionError):
            projection.projected_skills(self.root)
        self.method.unlink()
        self.method.write_text("---\nname: example\ndescription: method\n---\n", encoding="utf-8")
        entry = self.root / "hosts/cursor/skills/helix-propose/SKILL.md"
        entry.parent.mkdir(parents=True)
        entry.symlink_to(outside)
        with self.assertRaises(projection.ProjectionError):
            projection.write(self.root)
        self.assertEqual(outside.read_text(encoding="utf-8"), "synthetic")

    def test_method_cannot_reuse_workflow_command_name(self) -> None:
        collision = self.root / "skills/propose/SKILL.md"
        collision.parent.mkdir()
        collision.write_text("---\nname: propose\ndescription: collision\n---\n", encoding="utf-8")
        with self.assertRaises(projection.ProjectionError):
            projection.projected_skills(self.root)

    def test_distribution_binds_cursor_only_and_supports_both_repo_layouts(self) -> None:
        manifest = json.loads((PLUGIN / ".cursor-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["skills"], "./hosts/cursor/skills/")
        self.assertEqual(manifest["hooks"], "./hosts/cursor/hooks.json")
        self.assertEqual(manifest["commands"], [])
        self.assertEqual(manifest["agents"], [])
        for root in (PLUGIN, PLUGIN.parent):
            marketplace = json.loads((root / ".cursor-plugin/marketplace.json").read_text(encoding="utf-8"))
            self.assertEqual((root / marketplace["plugins"][0]["source"]).resolve(), PLUGIN)
            self.assertEqual(marketplace["plugins"][0]["version"], manifest["version"])
        host = json.loads((PLUGIN / "hosts/cursor/host.json").read_text(encoding="utf-8"))
        self.assertEqual(host["host_api_version"], "1.2")
        self.assertEqual(host["support"]["level"], "stable")
        self.assertEqual(host["support"]["display_name"], "Cursor")
        self.assertIsNone(host["support"]["evidence"])
        for capability in ("telemetry", "plugin_update"):
            self.assertEqual(host["capabilities"][capability], "emulated")
        self.assertEqual(host["capabilities"]["stop_hook"], "unsupported")
        self.assertEqual(host["adapters"]["telemetry"], "python:telemetry_hosts.cursor:CursorAdapter")
        self.assertEqual(host["adapters"]["update"], "python:update_hosts.cursor:CursorUpdateAdapter")
        self.assertEqual(host["adapters"]["version"], "python:update_hosts.cursor:plugin_version")
        self.assertNotIn("detector", host["adapters"])

    def test_d2c_projection_requires_canonical_steps_and_executable_gate(self) -> None:
        entry = (PLUGIN / "hosts/cursor/skills/helix-d-d2c/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("skills/d-d2c/SKILL.md", entry)
        self.assertIn("hosts/cursor/d2c_gate.cjs", entry)
        self.assertIn("--action ask", entry)
        self.assertIn("--action stop", entry)
        self.assertIn("action.kind=done", entry)
        self.assertIn("phase 6", entry)
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", entry)

    def test_method_resolution_needs_no_command_file_or_command_projection(self) -> None:
        isolated = (Path(self.temp.name) / "methods-only").resolve()
        for canonical in (PLUGIN / "skills").glob("*/SKILL.md"):
            name = canonical.parent.name
            method = isolated / f"skills/{name}/SKILL.md"
            entry = isolated / f"hosts/cursor/skills/helix-{name}/SKILL.md"
            method.parent.mkdir(parents=True)
            entry.parent.mkdir(parents=True)
            shutil.copyfile(canonical, method)
            shutil.copyfile(PLUGIN / entry.relative_to(isolated), entry)
            with self.subTest(method=name):
                receipt = projection.resolve_entry(isolated, entry)
                self.assertEqual(receipt["entry_kind"], "method")
                self.assertEqual(receipt["canonical"], f"skills/{name}/SKILL.md")
                self.assertFalse((isolated / f"commands/{name}.md").exists())
                text = entry.read_text(encoding="utf-8")
                self.assertIn("入口类型：`method`", text)
                self.assertIn("仅执行“方法入口”分支", text)
        self.assertFalse((isolated / "hosts/cursor/command-projection.json").exists())
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "cursor_skill_projection.py"), "--root", str(isolated),
             "--resolve-entry", "hosts/cursor/skills/helix-d-d2c/SKILL.md"],
            capture_output=True, text=True, check=False, timeout=10,
            env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["canonical"], "skills/d-d2c/SKILL.md")

    def test_method_protocol_binds_canonical_root_for_each_shell_invocation(self) -> None:
        runtime = (PLUGIN / "hosts/cursor/runtime.md").read_text(encoding="utf-8")
        entry_protocol = (PLUGIN / "hosts/cursor/skill-entry.md").read_text(encoding="utf-8")
        for text in (runtime, entry_protocol):
            self.assertIn("HELIXHARNESS_PLUGIN", text)
            self.assertIn("已验证的 `plugin_root`", text)
        self.assertIn("每次 Shell 调用", runtime)
        self.assertIn("不依赖跨调用环境保持", runtime)
        self.assertIn("完整路径安全引用", runtime)
        self.assertIn("不信任继承的同名环境变量", runtime)

    def test_command_resolution_requires_exact_projection_and_canonical_contract(self) -> None:
        projection.write(self.root)
        entry = Path("hosts/cursor/skills/helix-propose/SKILL.md")
        receipt = projection.resolve_entry(self.root, entry)
        self.assertEqual(receipt["entry_kind"], "command")
        self.assertEqual(receipt["canonical"], "commands/propose.md")
        payload = json.loads(self.projection_path.read_text(encoding="utf-8"))
        payload["entrypoints"]["propose"][1] = "skills/example/SKILL.md"
        self.projection_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(projection.ProjectionError):
            projection.resolve_entry(self.root, entry)

    def test_entry_resolution_rejects_wrong_identity_missing_method_and_escape(self) -> None:
        projection.write(self.root)
        entry = self.root / "hosts/cursor/skills/helix-example/SKILL.md"
        entry.write_text("---\nname: helix-another\ndescription: synthetic\n---\n", encoding="utf-8")
        with self.assertRaises(projection.ProjectionError):
            projection.resolve_entry(self.root, entry)
        projection.write(self.root)
        self.method.unlink()
        with self.assertRaises(projection.ProjectionError):
            projection.resolve_entry(self.root, entry)
        with self.assertRaises(projection.ProjectionError):
            projection.resolve_entry(self.root, Path("../outside.md"))


@unittest.skipUnless(shutil.which("node"), "Node.js required for the explicit D2C gate")
class CursorD2CGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "synthetic project").resolve()
        self.page = "synthetic-page"
        self.started = "2026-01-01T00:00:00.000Z"
        self.state_dir = self.root / "d-d2c/temp"
        self.page_dir = self.state_dir / self.page
        self.page_dir.mkdir(parents=True)
        self.state_file = self.page_dir / "workflow-state.json"
        self.marker_file = self.state_dir / ".active-workflow.json"
        self.state = {
            "currentPhase": 2,
            "round": 1,
            "phases": {
                "0": {"status": "completed", "enteredAt": self.started, "completedAt": self.started},
                "1": {"status": "completed", "enteredAt": self.started, "completedAt": self.started},
                "2": {"status": "in_progress", "enteredAt": self.started},
            },
        }
        self.marker = {"pageDir": self.page, "workflowStartedAt": self.started, "stateFile": str(self.state_file)}
        self.save()

    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state), encoding="utf-8")
        self.marker_file.write_text(json.dumps(self.marker), encoding="utf-8")

    def run_gate(self, action: str = "stop", *extra: str) -> tuple[int, dict]:
        # No Git or host agent invocation; only synthetic checkpoint files are read.
        result = subprocess.run(
            [shutil.which("node"), str(PLUGIN / "hosts/cursor/d2c_gate.cjs"),
             "--repo-root", str(self.root), "--page-dir", self.page, "--started-at", self.started,
             "--action", action, *extra],
            cwd=self.root, capture_output=True, text=True, check=False, timeout=10,
            env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(result.stderr, "", f"node stderr: {result.stderr[:400]!r}")
        return result.returncode, json.loads(result.stdout)

    def test_incomplete_workflow_cannot_stop_or_ask(self) -> None:
        for action in ("stop", "ask"):
            with self.subTest(action=action):
                code, result = self.run_gate(action)
                self.assertEqual(code, 2)
                self.assertFalse(result["allow"])

    def test_completion_allows_stop_but_not_spurious_question(self) -> None:
        self.complete_phases()
        self.save()
        code, result = self.run_gate()
        self.assertEqual(code, 0)
        self.assertEqual(result["action"]["kind"], "done")
        self.assertEqual(self.run_gate("ask")[0], 2)

    def test_phase4_interactive_decision_allowed_but_headless_must_continue(self) -> None:
        self.complete_phases((0, 1, 2, 3))
        self.state["currentPhase"] = 4
        self.state["phases"]["4"] = {"status": "in_progress", "enteredAt": self.started}
        self.save()
        code, result = self.run_gate("ask")
        self.assertEqual(code, 0)
        self.assertEqual(result["action"]["kind"], "wait-user-decision")
        self.assertEqual(self.run_gate("ask", "--headless")[0], 2)
        self.assertEqual(self.run_gate("stop", "--headless")[0], 2)

    def test_external_input_and_cancellation_preserve_distinct_outcomes(self) -> None:
        for kind, expected, can_ask in (("external-input", "wait-external-input", True), ("user-cancelled", "aborted", False)):
            self.state["workflowPause"] = {"kind": kind, "reason": "synthetic", "detail": "Synthetic input unavailable", "createdAt": self.started}
            self.save()
            with self.subTest(kind=kind):
                code, result = self.run_gate()
                self.assertEqual(code, 0)
                self.assertEqual(result["action"]["kind"], expected)
                self.assertEqual(self.run_gate("ask")[0] == 0, can_ask)

    def test_browser_handoff_waits_until_browser_is_ready_for_user(self) -> None:
        handoff = self.page_dir / "browser-handoff.json"
        for status, expected in (("requested", 2), ("browser-ready", 2), ("awaiting-user", 0)):
            handoff.write_text(json.dumps({"active": True, "status": status, "updatedAt": self.started, "browserReadyAt": self.started}), encoding="utf-8")
            with self.subTest(status=status):
                self.assertEqual(self.run_gate("ask")[0], expected)

    def test_missing_malformed_and_old_checkpoint_never_pass(self) -> None:
        self.marker_file.unlink()
        self.assertEqual(self.run_gate()[0], 2)
        self.marker_file.write_text("invalid JSON", encoding="utf-8")
        self.assertEqual(self.run_gate()[0], 2)
        self.marker["workflowStartedAt"] = "2025-01-01T00:00:00.000Z"
        self.save()
        self.assertEqual(self.run_gate()[0], 2)
        self.marker["workflowStartedAt"] = self.started
        self.state["phases"]["0"]["enteredAt"] = "2025-01-01T00:00:00.000Z"
        self.save()
        self.assertEqual(self.run_gate()[0], 2)

    def test_checkpoint_owner_cannot_be_bypassed(self) -> None:
        self.marker["ownerSessionId"] = "synthetic-owner"
        self.complete_phases()
        self.save()
        self.assertEqual(self.run_gate()[0], 2)
        self.assertEqual(self.run_gate("stop", "--session-id", "another")[0], 2)
        self.assertEqual(self.run_gate("stop", "--session-id", "synthetic-owner")[0], 0)

    def test_symlink_escape_is_rejected(self) -> None:
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text(json.dumps(self.state), encoding="utf-8")
        self.state_file.unlink()
        self.state_file.symlink_to(outside)
        code, result = self.run_gate()
        self.assertEqual(code, 2)
        self.assertFalse(result["allow"])

    def test_duplicate_unknown_or_invalid_action_is_rejected(self) -> None:
        for action, extra in (("invalid", ()), ("stop", ("--action", "ask")), ("stop", ("--unknown", "value"))):
            with self.subTest(action=action, extra=extra):
                self.assertEqual(self.run_gate(action, *extra)[0], 2)

    def test_gate_does_not_modify_checkpoint(self) -> None:
        before = {path: path.read_bytes() for path in (self.marker_file, self.state_file)}
        self.run_gate()
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def run_checkpoint(self, *arguments: str) -> None:
        result = subprocess.run(
            [shutil.which("node"), str(PLUGIN / "skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs"),
             "--page-dir", self.page, *arguments],
            cwd=self.root, capture_output=True, text=True, check=False, timeout=10,
            env={**env_sandbox.boot_vars(), "PATH": os.environ.get("PATH", "")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.marker = json.loads(self.marker_file.read_text(encoding="utf-8"))

    def complete_phases(self, phases: tuple[int, ...] = (0, 1, 2, 3, 4, 6)) -> None:
        self.state["phases"] = {
            str(phase): {"status": "completed", "enteredAt": self.started, "completedAt": self.started}
            for phase in phases
        }
        self.state["currentPhase"] = phases[-1]
        if 4 in phases:
            self.state["phases"]["4"]["decision"] = "cancel"

    def test_real_checkpoint_restart_cannot_reuse_previous_phase6(self) -> None:
        # Seed synthetic completed prerequisites, then use the real canonical CLI
        # for finalization and same-page restart; no browser/network/Git is involved.
        self.complete_phases((0, 1, 2, 3, 4))
        self.save()
        self.run_checkpoint("--enter", "6")
        self.run_checkpoint("--complete", "6")
        self.assertEqual(self.run_gate()[0], 0)
        previous_completion = self.state["phases"]["6"]["completedAt"]
        self.run_checkpoint("--enter", "0")
        self.started = self.state["phases"]["0"]["enteredAt"]
        self.assertEqual(self.state["phases"]["6"]["completedAt"], previous_completion)
        self.assertEqual(self.state["currentPhase"], 0)
        code, result = self.run_gate()
        self.assertEqual(code, 2)
        self.assertFalse(result["allow"])
        # Legal cancellation and external input after restart remain legal; they
        # are recorded by the real checkpoint command and never reported as done.
        self.run_checkpoint("--pause", "external-input", "--reason", "synthetic-input", "--detail", "Synthetic input required")
        code, result = self.run_gate("ask")
        self.assertEqual(code, 0)
        self.assertEqual(result["action"]["kind"], "wait-external-input")
        self.run_checkpoint("--resume")
        self.run_checkpoint("--pause", "user-cancelled", "--reason", "synthetic-cancelled")
        code, result = self.run_gate()
        self.assertEqual(code, 0)
        self.assertEqual(result["action"]["kind"], "aborted")

    def test_completed_phase6_cannot_hide_stale_prerequisites(self) -> None:
        self.complete_phases()
        self.state["phases"]["2"]["enteredAt"] = "2025-01-01T00:00:00.000Z"
        self.state["phases"]["2"]["completedAt"] = "2025-01-01T00:00:00.000Z"
        self.save()
        self.assertEqual(self.run_gate()[0], 2)

    def test_completion_requires_fresh_ordered_timestamps_for_every_phase(self) -> None:
        for phase in (0, 1, 2, 3, 4, 6):
            for field in ("enteredAt", "completedAt"):
                if phase == 0 and field == "enteredAt":
                    continue  # Covered by invocation identity tests.
                for value in (None, "invalid", "2025-01-01T00:00:00.000Z"):
                    self.complete_phases()
                    self.state["phases"][str(phase)][field] = value
                    self.save()
                    with self.subTest(phase=phase, field=field, value=value):
                        self.assertEqual(self.run_gate()[0], 2)
        self.complete_phases()
        self.state["phases"]["2"]["completedAt"] = "2026-01-01T00:00:01.000Z"
        self.save()
        self.assertEqual(self.run_gate()[0], 2)  # phase 3 predates phase 2 completion

    def test_stale_pause_or_browser_handoff_cannot_authorize_a_new_run(self) -> None:
        self.state["workflowPause"] = {"kind": "user-cancelled", "reason": "old", "createdAt": "2025-01-01T00:00:00.000Z"}
        self.save()
        self.assertEqual(self.run_gate()[0], 2)
        del self.state["workflowPause"]
        self.save()
        handoff = self.page_dir / "browser-handoff.json"
        for updated, ready in (("2025-01-01T00:00:00.000Z", self.started), (self.started, "2025-01-01T00:00:00.000Z")):
            handoff.write_text(json.dumps({"active": True, "status": "awaiting-user", "updatedAt": updated, "browserReadyAt": ready}), encoding="utf-8")
            self.assertEqual(self.run_gate("ask")[0], 2)
        # Discard stale handoff evidence before canonical action selection, so it
        # neither authorizes a stop nor hides a valid current invocation pause.
        self.state["workflowPause"] = {"kind": "external-input", "reason": "fresh-input", "detail": "Synthetic input", "createdAt": self.started}
        self.save()
        code, result = self.run_gate("ask")
        self.assertEqual(code, 0)
        self.assertEqual(result["action"]["kind"], "wait-external-input")

    def test_real_checkpoint_pauses_survive_stale_handoffs_without_allowing_old_evidence(self) -> None:
        self.run_checkpoint("--enter", "0")
        self.started = self.state["phases"]["0"]["enteredAt"]
        handoff = self.page_dir / "browser-handoff.json"
        for status in ("requested", "awaiting-user", "expired", "aborted"):
            handoff.write_text(json.dumps({
                "active": status in ("requested", "awaiting-user"), "status": status,
                "updatedAt": "2025-01-01T00:00:00.000Z", "browserReadyAt": "2025-01-01T00:00:00.000Z",
            }), encoding="utf-8")
            for kind, expected, can_ask in (("external-input", "wait-external-input", True), ("user-cancelled", "aborted", False)):
                with self.subTest(status=status, pause=kind):
                    self.run_checkpoint("--resume")
                    self.assertEqual(self.run_gate()[0], 2)
                    self.assertEqual(self.run_gate("stop", "--headless")[0], 2)
                    arguments = ["--pause", kind, "--reason", "synthetic-current-pause"]
                    if kind == "external-input":
                        arguments.extend(["--detail", "Synthetic input required"])
                    self.run_checkpoint(*arguments)
                    code, result = self.run_gate()
                    self.assertEqual(code, 0)
                    self.assertEqual(result["action"]["kind"], expected)
                    self.assertNotEqual(result["action"]["kind"], "done")
                    self.assertEqual(self.run_gate("ask")[0] == 0, can_ask)


if __name__ == "__main__":
    unittest.main()
