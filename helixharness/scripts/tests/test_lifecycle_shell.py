"""Host-selected shell contracts; Windows paths are synthetic on POSIX."""

import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

from host_runtime import HostRegistry
from lifecycle_service import startup_context_text

ROOT = Path(__file__).resolve().parents[2]


class LifecycleShellTest(unittest.TestCase):
    def setUp(self):
        self.registry = HostRegistry.discover(ROOT / "hosts")

    def test_windows_bash_paths_preserve_arguments_and_native_binding(self):
        for root in (PureWindowsPath("C:/插件 'quoted' $cash/install"),
                     PureWindowsPath("//server/share/插件 'quoted'/install")):
            with self.subTest(root=root):
                manifest = replace(self.registry.require("claude-code"), root=root / "hosts/claude-code")
                with patch("lifecycle_service.os", SimpleNamespace(name="nt")):
                    context = startup_context_text(manifest, command_shell="bash")
                binding = json.loads(context.split(": ", 1)[1].split("\n", 1)[0])
                self.assertEqual(str(root), binding["plugin_root"])
                command = next(line for line in context.splitlines() if line.startswith("sh "))
                launcher = (root / "scripts/runtime_preflight.sh").as_posix()
                self.assertEqual(shlex.split(command), [
                    "sh", launcher, "require", "&&", "sh", launcher, "exec",
                    (root / "scripts/host_runtime_cli.py").as_posix(), "resolve",
                    "--platform", "claude-code", "--expected-plugin-root", root.as_posix(),
                ])
                if os.name != "nt":
                    result = subprocess.run(["/bin/bash", "-n", "-c", command], capture_output=True)
                    self.assertEqual(0, result.returncode, result.stderr)

    def test_claude_real_hook_selects_bash_for_start_and_command_events(self):
        spec = importlib.util.spec_from_file_location("synthetic_claude_shell", ROOT / "hosts/claude-code/hook_entry.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        payloads = [
            {"hook_event_name": "SessionStart"},
            {"hook_event_name": "PreToolUse", "tool_name": "Skill", "tool_input": {"skill": "helix:status"}},
            {"hook_event_name": "UserPromptExpansion", "command_name": "helix:status"},
        ]
        for payload in payloads:
            with self.subTest(event=payload["hook_event_name"]):
                output = io.StringIO()
                with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                     patch.object(entry, "persist_environment"), \
                     patch("lifecycle_service.os", SimpleNamespace(name="nt")), \
                     redirect_stdout(output):
                    self.assertEqual(0, entry.main())
                specific = json.loads(output.getvalue())["hookSpecificOutput"]
                self.assertEqual(payload["hook_event_name"], specific["hookEventName"])
                context = specific["additionalContext"]
                self.assertIn(" require && sh ", context)
                self.assertNotIn("$helixPreflight", context)

    def test_default_windows_codex_and_cursor_keep_powershell(self):
        for host in ("codex", "cursor"):
            with self.subTest(host=host), patch("lifecycle_service.os", SimpleNamespace(name="nt")):
                context = startup_context_text(self.registry.require(host))
                self.assertIn("$helixPreflight = & ", context)
                self.assertIn("if (-not ($helixPreflight | ConvertFrom-Json).ok) { exit 2 }", context)
                self.assertNotIn("require &&", context)

    def test_posix_default_and_explicit_bash_are_identical_for_all_hosts(self):
        for host in self.registry.supported_ids():
            with self.subTest(host=host), patch("lifecycle_service.os", SimpleNamespace(name="posix")):
                manifest = self.registry.require(host)
                self.assertEqual(startup_context_text(manifest),
                                 startup_context_text(manifest, command_shell="bash"))

    def test_invalid_shell_is_rejected(self):
        with self.assertRaises(ValueError):
            startup_context_text(self.registry.require("claude-code"), command_shell="cmd")
