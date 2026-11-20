"""Cursor hook contracts use synthetic payloads and isolated project settings."""

import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from host_runtime import HostRegistry
from lifecycle_service import handle_event, startup_context, startup_context_text
from project_hooks_cli import project_hooks

ROOT = Path(__file__).resolve().parents[2]
CURSOR = ROOT / "hosts/cursor"


def synthetic_python(directory: Path) -> Path:
    """Exercise successful bootstrap dispatch independently of the test Python."""

    launcher = directory / "python3"
    launcher.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = -c ]; then printf '%s\\n' 3.12.0; exit 0; fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    launcher.chmod(0o755)
    return launcher


def copy_bootstrap(directory: Path) -> Path:
    plugin = directory / "plugin"
    hook_root = plugin / "hosts/cursor"
    hook_root.mkdir(parents=True)
    (plugin / "scripts").mkdir()
    shim = hook_root / "hook_bootstrap.sh"
    shutil.copyfile(CURSOR / "hook_bootstrap.sh", shim)
    shutil.copyfile(ROOT / "scripts/python_runtime.sh", plugin / "scripts/python_runtime.sh")
    (directory / "dirname").symlink_to("/usr/bin/dirname")
    return shim


class CursorHooksTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("synthetic_cursor_hook", CURSOR / "hook_entry.py")
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)

    def test_native_startup_has_cursor_identity_even_with_other_host_environment(self):
        command = json.loads((CURSOR / "hooks.json").read_text())["hooks"]["sessionStart"][0]["command"]
        with tempfile.TemporaryDirectory(prefix="helix cursor space ") as directory:
            alias = Path(directory) / "plugin root"
            alias.symlink_to(ROOT, target_is_directory=True)
            synthetic_python(Path(directory))
            env = {
                "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                "CURSOR_PLUGIN_ROOT": str(alias),
                "CLAUDE_PLUGIN_ROOT": "/synthetic/other-install",
                "PLUGIN_ROOT": "/synthetic/other-install",
                "CODEX_THREAD_ID": "synthetic-other-host",
                "HELIXHARNESS_PLATFORM": "claude-code",
            }
            process = subprocess.run(
                ["sh", "-c", command], env=env,
                input=json.dumps({"hook_event_name": "sessionStart", "platform": "codex"}),
                text=True, capture_output=True, timeout=10,
            )
        self.assertEqual(0, process.returncode, process.stderr)
        result = json.loads(process.stdout)
        self.assertNotIn("hookSpecificOutput", result)
        self.assertEqual(str(ROOT), result["env"]["HELIXHARNESS_BOUND_PLUGIN_ROOT"])
        binding = json.loads(result["additional_context"].split(": ", 1)[1].split("\n", 1)[0])
        self.assertEqual("cursor", binding["canonical_host_id"])
        self.assertEqual(str(CURSOR / "host.json"), binding["host_manifest"])

    def test_native_events_discard_session_content_and_do_not_create_business_finish(self):
        for event, normalized in (("sessionStart", "SessionStart"), ("sessionEnd", "SessionEnd")):
            payload = {
                "hook_event_name": event,
                "session_id": "synthetic-session",
                "transcript_path": "/synthetic/do-not-read.jsonl",
                "workspace_roots": ["/synthetic/repository"],
                "cwd": "/synthetic/repository",
                "prompt": "synthetic content not forwarded",
            }
            output = io.StringIO()
            with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), patch(
                "lifecycle_service.handle_event"
            ) as consume, redirect_stdout(output):
                self.assertEqual(0, self.entry.main())
            # No valid native conversation/workspace binding: do not query
            # authorization, repair sessions, or forward untrusted content.
            consume.assert_not_called()
            self.assertNotIn("synthetic content", output.getvalue())
            if event == "sessionEnd":
                self.assertEqual("", output.getvalue())

    def test_foreign_and_malformed_events_are_ignored(self):
        for payload in ("broken", "[]", '{"hook_event_name":"SessionStart"}', '{"hook_event_name":"stop"}', '{"hook_event_name":[]}'):
            with self.subTest(payload=payload), patch.object(sys, "stdin", io.StringIO(payload)), patch(
                "lifecycle_service.handle_event"
            ) as consume, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(0, self.entry.main())
                consume.assert_not_called()
                self.assertEqual("", output.getvalue())

    def test_unbound_native_events_do_not_touch_telemetry(self):
        with patch("lifecycle_service.subprocess.Popen") as spawn, patch.dict(
            sys.modules, {"metrics": None, "telemetry_consent_state": None}
        ):
            for event in ("sessionStart", "sessionEnd"):
                with patch.object(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": event}))), redirect_stdout(io.StringIO()):
                    self.assertEqual(0, self.entry.main())
            spawn.assert_not_called()

    def test_existing_hosts_keep_their_startup_response_contract(self):
        registry = HostRegistry.discover(ROOT / "hosts")
        for host in ("claude-code", "codex"):
            manifest = registry.require(host)
            expected = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": startup_context_text(manifest)}}
            self.assertEqual(expected, startup_context(manifest))
            with redirect_stdout(io.StringIO()) as output, patch("lifecycle_service.repair_session"):
                handle_event(manifest, {"hook_event_name": "SessionStart"})
            self.assertEqual(expected, json.loads(output.getvalue()))

    def test_mixed_host_init_preserves_existing_configs_and_repeats_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for host in ("claude-code", "codex"):
                project_hooks(repo, host, "merge")
            originals = {
                relative: (repo / relative).read_bytes()
                for relative in (".claude/settings.json", ".codex/hooks.json")
            }
            self.assertTrue(project_hooks(repo, "cursor", "merge")["changed"])
            self.assertFalse(project_hooks(repo, "cursor", "merge")["changed"])
            for host in ("claude-code", "codex", "cursor"):
                self.assertEqual("pass", project_hooks(repo, host, "check")["status"])
            for relative, content in originals.items():
                self.assertEqual(content, (repo / relative).read_bytes())
            native = json.loads((repo / ".cursor/hooks.json").read_text())
            self.assertEqual(1, native["version"])
            self.assertEqual(2, len(native["hooks"]["afterFileEdit"]))


class CursorHookBootstrapTest(unittest.TestCase):
    def test_hook_and_command_preflight_agree_on_python_boundary(self):
        for version, ready in (((3, 8, 0), False), ((3, 9, 6), False), ((3, 10, 0), True), ((3, 12, 0), True)):
            with self.subTest(version=version), tempfile.TemporaryDirectory(prefix="cursor version gate ") as directory:
                root = Path(directory)
                shim = copy_bootstrap(root)
                (shim.parent / "hook_entry.py").write_text('print(\'{"target_ran":true}\')\n')
                launcher = root / "python3"
                # Execute the actual version-check expressions with a selected
                # interpreter version; dispatch the target with the test Python.
                code = f"import sys; sys.version_info={version!r}; exec(sys.argv[1])"
                launcher.write_text("#!/bin/sh\nif [ \"${1:-}\" = -c ]; then\n"
                                    f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)} \"$2\"\nfi\n"
                                    f"exec {shlex.quote(sys.executable)} \"$@\"\n")
                launcher.chmod(0o755)
                git = root / "git"
                git.write_text("#!/bin/sh\nexit 0\n")
                git.chmod(0o755)
                env = {"PATH": str(root)}
                check = subprocess.run(["/bin/sh", str(ROOT / "scripts/runtime_preflight.sh"), "check"],
                                       env=env, text=True, capture_output=True, check=True)
                hook = subprocess.run(["/bin/sh", str(shim), "sessionStart"],
                                      env=env, text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(check.stdout)["python"]["status"] == "ready", ready)
                self.assertEqual(json.loads(hook.stdout).get("target_ran", False), ready)

    def test_hook_uses_later_supported_versioned_runtime_and_preserves_stdin(self):
        with tempfile.TemporaryDirectory(prefix="cursor fallback runtime ") as directory:
            root = Path(directory)
            shim = copy_bootstrap(root)
            old_python = root / "python3"
            old_python.write_text("#!/bin/sh\nif [ \"${1:-}\" = -c ]; then printf '%s\\n' 3.9.6; else exit 91; fi\n")
            old_python.chmod(0o755)
            later = root / "Python new"
            later.mkdir()
            selected = synthetic_python(later)
            selected.rename(later / "python3.12")
            (shim.parent / "hook_entry.py").write_text("import sys, json\nprint(json.dumps({'stdin': sys.stdin.read()}))\n")
            result = subprocess.run(["/bin/sh", str(shim), "sessionStart"],
                                    env={"PATH": os.pathsep.join((str(root), str(later)))},
                                    input="synthetic hook payload\n", capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual({"stdin": "synthetic hook payload\n"}, json.loads(result.stdout))

    def test_missing_python_has_native_response_and_session_end_is_silent(self):
        with tempfile.TemporaryDirectory(prefix="cursor hook space ") as directory:
            root = Path(directory)
            shim = copy_bootstrap(root)
            for event in ("sessionStart", "sessionEnd"):
                process = subprocess.run(["/bin/sh", str(shim), event], env={"PATH": str(root)}, text=True, capture_output=True)
                self.assertEqual(0, process.returncode, process.stderr)
                if event == "sessionStart":
                    response = json.loads(process.stdout)
                    self.assertIn("Python 3.10+", response["additional_context"])
                    self.assertNotIn("systemMessage", response)
                else:
                    self.assertEqual("", process.stdout)

    def test_missing_target_has_native_response_with_quoted_script_path(self):
        with tempfile.TemporaryDirectory(prefix="cursor hook space ") as directory:
            shim = copy_bootstrap(Path(directory))
            synthetic_python(Path(directory))
            process = subprocess.run(["/bin/sh", str(shim), "sessionStart"], env={"PATH": directory}, text=True, capture_output=True)
            self.assertEqual(0, process.returncode, process.stderr)
            self.assertIn("runtime_target_missing", json.loads(process.stdout)["additional_context"])

    def test_unsupported_python_never_runs_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = copy_bootstrap(root)
            launcher = root / "python3"
            launcher.write_text("#!/bin/sh\nexit 1\n")
            launcher.chmod(0o755)
            (shim.parent / "hook_entry.py").write_text("raise RuntimeError('must not execute')\n")
            process = subprocess.run(["/bin/sh", str(shim), "sessionStart"], env={"PATH": directory}, text=True, capture_output=True)
            self.assertEqual(0, process.returncode, process.stderr)
            self.assertIn("Python 3.10+", json.loads(process.stdout)["additional_context"])


if __name__ == "__main__":
    unittest.main()
