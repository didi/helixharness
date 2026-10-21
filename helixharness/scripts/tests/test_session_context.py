from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "hosts/claude-code/hook_entry.py"


class SessionContextTest(unittest.TestCase):
    def run_hook(self, payload: str, **extra_env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=payload, text=True, capture_output=True, check=True,
            env={"PATH": os.environ.get("PATH", ""), **extra_env},
        )

    def context(self, result: subprocess.CompletedProcess) -> str:
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        return output["additionalContext"]

    def test_startup_binds_installed_root_without_environment_file(self):
        context = self.context(self.run_hook('{"hook_event_name":"SessionStart"}'))
        self.assertIn(json.dumps(str(ROOT)), context)
        self.assertIn(json.dumps(str(ROOT / "hosts/claude-code/host.json")), context)
        self.assertIn('"canonical_host_id": "claude-code"', context)

    def test_startup_does_not_use_an_unrelated_plugin_root_or_expose_secrets(self):
        context = self.context(self.run_hook(
            '{"hook_event_name":"SessionStart"}',
            CLAUDE_PLUGIN_ROOT="/unrelated/source-copy",
            ANTHROPIC_API_KEY="private-test-sentinel",
        ))
        self.assertIn(json.dumps(str(ROOT)), context)
        self.assertNotIn("/unrelated/source-copy", context)
        self.assertNotIn("private-test-sentinel", context)

    def test_unwritable_environment_file_does_not_suppress_startup_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.context(self.run_hook(
                '{"hook_event_name":"SessionStart"}', CLAUDE_ENV_FILE=directory,
            ))
        self.assertIn(json.dumps(str(ROOT)), context)

    def test_cwd_changed_preserves_session_environment_without_startup_output(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "session.env"
            payload = {
                "hook_event_name": "CwdChanged", "cwd": "/project with spaces",
                "session_id": "session-one", "transcript_path": "/logs/session.jsonl",
            }
            result = self.run_hook(json.dumps(payload), CLAUDE_ENV_FILE=str(env_file))
            self.assertEqual(result.stdout, "")
            self.assertIn("export HELIXHARNESS_LITE_SESSION_CWD='/project with spaces'", env_file.read_text())
            self.assertIn("export HELIXHARNESS_LITE_SESSION_ID=session-one", env_file.read_text())
            self.assertIn("export HELIXHARNESS_BOUND_PLUGIN_ROOT=" + str(ROOT), env_file.read_text())

    def test_skill_and_direct_command_refresh_source_next_to_entry(self):
        cases = [
            {"hook_event_name": "PreToolUse", "tool_name": "Skill", "tool_input": {"skill": "helix:init"}},
            {"hook_event_name": "UserPromptExpansion", "command_name": "helix:propose", "expansion_type": "slash_command"},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                output = json.loads(self.run_hook(json.dumps(payload)).stdout)["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], payload["hook_event_name"])
                context = output["additionalContext"]
                self.assertIn(str(ROOT / "scripts/host_runtime_cli.py"), context)
                self.assertIn(str(ROOT / "reference/runtime-preflight.md"), context)
                self.assertIn('"resolved_resources"', context)
                self.assertNotIn("permissionDecision", output)

    def test_entry_binding_ignores_other_skills_and_tools(self):
        cases = [
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo helix:init"}},
            {"hook_event_name": "PreToolUse", "tool_name": "Skill", "tool_input": {"skill": "other:init"}},
            {"hook_event_name": "UserPromptExpansion", "command_name": "other:init"},
        ]
        for payload in cases:
            self.assertEqual(self.run_hook(json.dumps(payload)).stdout, "")

    def test_invalid_input_and_unrelated_events_do_not_emit_context(self):
        for payload in ("invalid", "[]", "{}", '{"hook_event_name":"Stop"}'):
            with self.subTest(payload=payload):
                self.assertEqual(self.run_hook(payload).stdout, "")


if __name__ == "__main__":
    unittest.main()
