import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host_runtime.project_hooks import ProjectHooks
from project_hooks_cli import project_hooks

ROOT = Path(__file__).resolve().parents[2]


class ProjectHooksTest(unittest.TestCase):
    def test_fresh_merge_repeat_and_user_settings_preservation(self):
        for host, relative in (
            ("codex", ".codex/hooks.json"),
            ("claude-code", ".claude/settings.json"),
        ):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                target = repo / relative
                target.parent.mkdir()
                user_hook = {
                    "type": "command",
                    "command": "echo user-check",
                    "timeout": 17,
                }
                user = {
                    "custom": {"keep": True},
                    "hooks": {
                        "PostToolUse": [
                            {"matcher": "Write|Edit|MultiEdit", "hooks": [user_hook]}
                        ],
                        "Stop": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "echo keep-stop"}
                                ]
                            }
                        ],
                    },
                }
                target.write_text(json.dumps(user))
                base = [
                    sys.executable,
                    str(ROOT / "scripts/project_hooks_cli.py"),
                    "--repo-root",
                    str(repo),
                    "--platform",
                    host,
                ]
                self.assertNotEqual(
                    0, subprocess.run([*base, "check"], capture_output=True).returncode
                )
                for _ in range(2):
                    result = subprocess.run(
                        [*base, "merge"], capture_output=True, text=True
                    )
                    self.assertEqual(
                        0, result.returncode, result.stderr + result.stdout
                    )
                merged = json.loads(target.read_text())
                self.assertEqual(user["custom"], merged["custom"])
                self.assertEqual(user["hooks"]["Stop"], merged["hooks"]["Stop"])
                handlers = merged["hooks"]["PostToolUse"][0]["hooks"]
                self.assertEqual(user_hook, handlers[0])
                self.assertEqual(3, len(handlers))
                handlers[1]["timeout"] = 23
                target.write_text(json.dumps(merged))
                self.assertEqual(
                    0, subprocess.run([*base, "merge"], capture_output=True).returncode
                )
                self.assertEqual(
                    merged,
                    json.loads(target.read_text()),
                    "retain user handler options without a second command",
                )
                self.assertEqual(
                    0, subprocess.run([*base, "check"], capture_output=True).returncode
                )
                target.unlink()
                self.assertNotEqual(
                    0, subprocess.run([*base, "check"], capture_output=True).returncode
                )
                self.assertEqual(
                    0, subprocess.run([*base, "merge"], capture_output=True).returncode
                )
                self.assertEqual(
                    0, subprocess.run([*base, "check"], capture_output=True).returncode
                )

    def test_invalid_user_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / ".codex/hooks.json"
            target.parent.mkdir()
            for raw in (
                "broken",
                '{"hooks": []}',
                '{"hooks":{"PostToolUse":"broken"}}',
            ):
                target.write_text(raw)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/project_hooks_cli.py"),
                        "--repo-root",
                        str(repo),
                        "--platform",
                        "codex",
                        "merge",
                    ],
                    capture_output=True,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(raw, target.read_text())

    def test_complete_required_hooks_do_not_hide_invalid_user_events(self):
        from project_hooks_cli import project_hooks

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            project_hooks(repo, "codex", "merge")
            target = repo / ".codex/hooks.json"
            good = target.read_text()
            for invalid in ("broken", [{"hooks": [{"type": "command"}]}]):
                document = json.loads(good)
                document["hooks"]["Stop"] = invalid
                raw = json.dumps(document)
                target.write_text(raw)
                for action in ("check", "merge"):
                    with self.subTest(invalid=invalid, action=action):
                        with self.assertRaises(ValueError):
                            project_hooks(repo, "codex", action)
                        self.assertEqual(raw, target.read_text())


class FlatProjectHooksTest(unittest.TestCase):
    def setUp(self):
        self.required = {
            "version": 1,
            "hooks": {"afterFileEdit": [{"command": "bash docs/harness/linters/check.sh"}]},
        }
        self.contract = ProjectHooks(
            owner="parent",
            settings_file=".cursor/hooks.json",
            merge_strategy="json_flat_append_dedup",
            dedupe_key="hooks.*[].command+matcher",
            payload=self.required,
        )

    def invoke(self, repo, action):
        with patch("project_hooks_cli.HostRegistry") as registry, patch(
            "project_hooks_cli.load_project_hooks", return_value=self.contract
        ):
            registry.discover.return_value.get.return_value = object()
            return project_hooks(repo, "synthetic-host", action)

    def test_flat_merge_is_idempotent_and_preserves_user_configuration(self):
        with tempfile.TemporaryDirectory(prefix="helix hooks space ") as directory:
            repo = Path(directory)
            target = repo / self.contract.settings_file
            target.parent.mkdir()
            user_hook = {"command": "echo user", "timeout": 17, "custom": True}
            existing_required = {**self.required["hooks"]["afterFileEdit"][0], "timeout": 23}
            user = {
                "version": 1,
                "custom": {"keep": True},
                "hooks": {
                    "afterFileEdit": [user_hook, existing_required, dict(existing_required)],
                    "stop": [{"command": "echo keep-stop", "loop_limit": 2}],
                    "preToolUse": [{"type": "prompt", "prompt": "Check synthetic action", "matcher": "Shell"}],
                },
            }
            target.write_text(json.dumps(user))
            with self.assertRaises(ValueError):
                self.invoke(repo, "check")
            self.assertTrue(self.invoke(repo, "merge")["changed"])
            merged = json.loads(target.read_text())
            self.assertEqual([user_hook, existing_required], merged["hooks"]["afterFileEdit"])
            self.assertEqual(user["custom"], merged["custom"])
            self.assertEqual(user["hooks"]["stop"], merged["hooks"]["stop"])
            self.assertEqual(user["hooks"]["preToolUse"], merged["hooks"]["preToolUse"])
            self.assertFalse(self.invoke(repo, "merge")["changed"])
            self.assertEqual("pass", self.invoke(repo, "check")["status"])

    def test_flat_fresh_project_does_not_touch_other_host_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            unrelated = repo / ".claude/settings.json"
            unrelated.parent.mkdir()
            unrelated.write_text('{"synthetic_user_setting": true}')
            before = unrelated.read_bytes()
            self.invoke(repo, "merge")
            self.assertEqual(self.required, json.loads((repo / self.contract.settings_file).read_text()))
            self.assertEqual(before, unrelated.read_bytes())

    def test_flat_same_command_with_another_matcher_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / self.contract.settings_file
            target.parent.mkdir()
            user_hook = {**self.required["hooks"]["afterFileEdit"][0], "matcher": "synthetic-path"}
            target.write_text(json.dumps({"version": 1, "hooks": {"afterFileEdit": [user_hook]}}))
            self.invoke(repo, "merge")
            handlers = json.loads(target.read_text())["hooks"]["afterFileEdit"]
            self.assertEqual([user_hook, self.required["hooks"]["afterFileEdit"][0]], handlers)

    def test_flat_invalid_user_settings_fail_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / self.contract.settings_file
            target.parent.mkdir()
            for document in (
                {"version": 2, "hooks": {}},
                {"version": True, "hooks": {}},
                {"version": 1, "hooks": []},
                {"version": 1, "hooks": {"stop": "broken"}},
                {"version": 1, "hooks": {"stop": [{"command": ""}]}},
                {"version": 1, "hooks": {"stop": [{"hooks": [{"type": "command", "command": "echo nested"}]}]}},
                {"version": 1, "hooks": {"stop": [{"command": "echo value", "matcher": []}]}},
                {"version": 1, "hooks": {"stop": [{"type": {}, "command": "echo value"}]}},
            ):
                raw = json.dumps(document)
                target.write_text(raw)
                for action in ("merge", "check"):
                    with self.subTest(document=document, action=action):
                        with self.assertRaises(ValueError):
                            self.invoke(repo, action)
                        self.assertEqual(raw, target.read_text())

    def test_flat_target_symlink_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "project"
            repo.mkdir()
            outside = base / "outside.json"
            outside.write_text('{"version":1,"hooks":{}}')
            target = repo / self.contract.settings_file
            target.parent.mkdir()
            target.symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "escapes"):
                self.invoke(repo, "merge")
            self.assertEqual('{"version":1,"hooks":{}}', outside.read_text())


if __name__ == "__main__":
    unittest.main()
