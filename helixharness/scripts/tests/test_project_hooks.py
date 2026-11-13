import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ProjectHooksTest(unittest.TestCase):
    def test_legacy_hooks_are_upgraded_without_losing_user_options(self):
        from project_hooks_cli import project_hooks

        with tempfile.TemporaryDirectory(prefix="helix space ") as temporary:
            repo = Path(temporary)
            target = repo / ".codex/hooks.json"
            target.parent.mkdir()
            legacy = {"hooks": {"PostToolUse": [{"matcher": "Write|Edit|MultiEdit", "hooks": [
                {"type": "command", "command": "bash docs/harness/linters/invariant-check.sh 2>&1 | head -50", "timeout": 23},
                {"type": "command", "command": "echo custom"},
            ]}]}}
            target.write_text(json.dumps(legacy))
            with self.assertRaises(ValueError):
                project_hooks(repo, "codex", "check")
            self.assertEqual(legacy, json.loads(target.read_text()))
            project_hooks(repo, "codex", "merge")
            handlers = json.loads(target.read_text())["hooks"]["PostToolUse"][0]["hooks"]
            self.assertEqual(3, len(handlers))
            self.assertEqual(23, handlers[0]["timeout"])
            windows_source = base64.b64decode(handlers[0]["commandWindows"].split()[-1]).decode("utf-16-le")
            self.assertIn("project_linter.py", windows_source if os.name == "nt" else handlers[0]["command"])
            self.assertIn("commandWindows", handlers[0])
            self.assertEqual("echo custom", handlers[1]["command"])
            project_hooks(repo, "codex", "check")

    def test_windows_hook_quotes_paths_without_cmd_expansion(self):
        from project_hooks_cli import windows_command

        command = windows_command(["C:\\Python\\python.exe", "C:\\space & %PATH%\\it's a script.py"])
        self.assertNotIn("%PATH%", command)
        self.assertNotIn("&", command)
        source = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertIn("it''s a script.py", source)
        self.assertIn("%PATH%", source)

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


if __name__ == "__main__":
    unittest.main()
