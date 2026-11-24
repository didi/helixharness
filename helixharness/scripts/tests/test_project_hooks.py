import base64
import json
import os
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

    def test_reinit_replaces_old_python_hooks_and_keeps_custom_hooks(self):
        from project_hooks_cli import project_hooks

        for host in ("codex", "claude-code"):
            with self.subTest(host=host), tempfile.TemporaryDirectory(prefix="helix space ") as directory:
                repo = Path(directory)
                old_python = str(repo / "Python '3.11'" / "python3")
                new_python = str(repo / "Python 3.14" / "python3")
                with patch("project_hooks_cli.sys.executable", old_python):
                    initial = project_hooks(repo, host, "merge")
                    target = repo / initial["settings_file"]
                original = json.loads(target.read_text())
                old_groups = original["hooks"]["PostToolUse"]
                old_handlers = old_groups[0]["hooks"]
                custom = {"type": "command", "command": "echo invariant-check", "timeout": 17}
                old_handlers[0]["timeout"] = 23
                old_handlers.append(custom)
                # Projects that have already hit the bug can have duplicate stale entries.
                old_groups.append({
                    "matcher": old_groups[0]["matcher"],
                    "hooks": [old_handlers[0].copy()],
                })
                untouched = {"hooks": [{"type": "command", "command": "echo custom stop"}]}
                original["hooks"]["Stop"] = [untouched]
                target.write_text(json.dumps(original))

                with patch("project_hooks_cli.sys.executable", new_python):
                    with self.assertRaisesRegex(ValueError, "project_hooks_missing_or_duplicated"):
                        project_hooks(repo, host, "check")
                    self.assertTrue(project_hooks(repo, host, "merge")["changed"])
                    self.assertFalse(project_hooks(repo, host, "merge")["changed"])
                    self.assertEqual("pass", project_hooks(repo, host, "check")["status"])
                    updated = json.loads(target.read_text())["hooks"]
                    target.unlink()
                    project_hooks(repo, host, "merge")
                    expected = json.loads(target.read_text())["hooks"]["PostToolUse"][0]["hooks"]
                current = [handler for group in updated["PostToolUse"] for handler in group["hooks"]]
                expected[0]["timeout"] = 23
                self.assertEqual([*expected, custom], current)
                self.assertEqual([untouched], updated["Stop"])

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

    def test_qoder_collapse_removes_version_dir_duplicates(self):
        """Under linter_flag policy, entries differing only in --repo-root collapse."""
        from project_hooks_cli import project_hooks

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / ".qoder/settings.json"
            target.parent.mkdir()
            # Simulate stale entries from two different plugin versions
            stale = {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write|Edit|NotebookEdit",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"'{sys.executable}' -X utf8 /old/version/1.0.0/scripts/project_linter.py --repo-root /tmp/old --linter invariant-check",
                                },
                                {
                                    "type": "command",
                                    "command": f"'{sys.executable}' -X utf8 /new/version/1.1.0/scripts/project_linter.py --repo-root /tmp/new --linter invariant-check",
                                },
                                {
                                    "type": "command",
                                    "command": f"'{sys.executable}' -X utf8 /old/version/1.0.0/scripts/project_linter.py --repo-root /tmp/old --linter spec-sync-check",
                                },
                                {
                                    "type": "command",
                                    "command": f"'{sys.executable}' -X utf8 /new/version/1.1.0/scripts/project_linter.py --repo-root /tmp/new --linter spec-sync-check",
                                },
                            ],
                        }
                    ]
                }
            }
            target.write_text(json.dumps(stale))
            result = project_hooks(repo, "qoder", "merge")
            self.assertTrue(result["changed"])
            self.assertIn("warnings", result)
            # Should have warnings about removed duplicates
            self.assertGreater(len(result["warnings"]), 0)
            merged = json.loads(target.read_text())
            handlers = merged["hooks"]["PostToolUse"][0]["hooks"]
            # Should collapse to 2 handlers (one per linter)
            self.assertEqual(2, len(handlers))
            # Check that both linters are present
            linter_names = set()
            for handler in handlers:
                command = handler["command"]
                if "--linter invariant-check" in command:
                    linter_names.add("invariant-check")
                elif "--linter spec-sync-check" in command:
                    linter_names.add("spec-sync-check")
            self.assertEqual({"invariant-check", "spec-sync-check"}, linter_names)

    def test_qoder_check_warns_by_default_fails_with_strict(self):
        """Under warn policy, check returns warnings; --strict makes it fail."""
        from project_hooks_cli import project_hooks

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / ".qoder/settings.json"
            target.parent.mkdir()
            # Missing hooks entirely
            result = project_hooks(repo, "qoder", "check", strict=False)
            self.assertEqual("pass", result["status"])
            self.assertIn("warnings", result)
            # With strict, should raise
            with self.assertRaisesRegex(ValueError, "project_hooks_missing_or_duplicated"):
                project_hooks(repo, "qoder", "check", strict=True)

    def test_qoder_does_not_claim_shape_mismatched_commands(self):
        """Commands with project_linter.py but wrong flags are not recognized."""
        from project_hooks_cli import project_hooks

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            target = repo / ".qoder/settings.json"
            target.parent.mkdir()
            user_hook = {
                "type": "command",
                "command": f"'{sys.executable}' /some/path/scripts/project_linter.py --unknown-flag",
                "timeout": 42,
            }
            settings = {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write|Edit|NotebookEdit",
                            "hooks": [user_hook],
                        }
                    ]
                }
            }
            target.write_text(json.dumps(settings))
            project_hooks(repo, "qoder", "merge")
            merged = json.loads(target.read_text())
            handlers = merged["hooks"]["PostToolUse"][0]["hooks"]
            # User hook should be preserved (not recognized as our linter)
            self.assertEqual(3, len(handlers))  # user + 2 linters
            self.assertEqual(42, handlers[0]["timeout"])
            self.assertEqual(user_hook["command"], handlers[0]["command"])


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

    def test_cursor_owned_linters_refresh_runtime_and_preserve_user_hooks(self):
        with tempfile.TemporaryDirectory(prefix="cursor hook migration ") as directory:
            repo = Path(directory)
            target = repo / ".cursor/hooks.json"
            target.parent.mkdir()
            legacy = {"command": "bash docs/harness/linters/invariant-check.sh 2>&1 | head -50", "timeout": 23}
            custom = {"command": "echo custom", "timeout": 17}
            scoped = {**legacy, "matcher": "custom-scope"}
            target.write_text(json.dumps({"version": 1, "hooks": {
                "afterFileEdit": [legacy, dict(legacy), custom, scoped],
                "stop": [{"command": "echo keep-stop"}],
            }}))
            old_python = str(repo / "Python 'old'" / "python3")
            new_python = str(repo / "Python new" / "python3")
            for python in (old_python, new_python):
                with self.subTest(python=python), patch("project_hooks_cli.sys.executable", python):
                    with self.assertRaisesRegex(ValueError, "project_hooks_missing_or_duplicated"):
                        project_hooks(repo, "cursor", "check")
                    self.assertTrue(project_hooks(repo, "cursor", "merge")["changed"])
                    self.assertFalse(project_hooks(repo, "cursor", "merge")["changed"])
                    self.assertEqual("pass", project_hooks(repo, "cursor", "check")["status"])
                    document = json.loads(target.read_text())
                    handlers = document["hooks"]["afterFileEdit"]
                    self.assertEqual(4, len(handlers))
                    self.assertEqual(23, handlers[0]["timeout"])
                    self.assertIn("project_linter.py", handlers[0]["command"])
                    self.assertEqual(custom, handlers[1])
                    self.assertEqual(scoped, handlers[2])
                    self.assertEqual([{"command": "echo keep-stop"}], document["hooks"]["stop"])
            self.assertNotIn("Python 'old'", handlers[0]["command"])

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
