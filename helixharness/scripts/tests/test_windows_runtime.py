"""Native PowerShell integration; run by the Windows CI lane, no WSL required."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from host_runtime import HostRegistry
from lifecycle_service import startup_context_text

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "requires native Windows PowerShell")
class WindowsRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(self.powershell)
        self.base = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(ROOT / "scripts/runtime_preflight.ps1")]

    def test_generated_codex_binding_runs_in_native_powershell(self):
        manifest = HostRegistry.discover(ROOT / "hosts").require("codex")
        context = startup_context_text(manifest)
        command = context.split("(the preflight must run first):\n", 1)[1].split("\nThe first JSON", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "binding.ps1"
            # Windows PowerShell 5.1 needs a BOM to decode non-ASCII script paths.
            script.write_text(command, encoding="utf-8-sig")
            result = subprocess.run(
                [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        decoder = json.JSONDecoder()
        check, offset = decoder.raw_decode(result.stdout.lstrip())
        resolved = json.loads(result.stdout.lstrip()[offset:].lstrip())
        self.assertTrue(check["ok"])
        self.assertEqual("resolved", resolved["status"])
        self.assertEqual("codex", resolved["host_id"])

    def test_preflight_and_exec_preserve_unicode_spaces_and_exit_code(self):
        result = subprocess.run([*self.base, "check"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(Path(payload["python"]["executable"]).is_file())
        self.assertEqual(payload["python"]["arguments"][-2:], ["-X", "utf8"])
        with tempfile.TemporaryDirectory(prefix="helix space 中文 ") as temporary:
            script = Path(temporary) / "参数 echo.py"
            script.write_text("import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False)); sys.exit(7)", encoding="utf-8")
            result = subprocess.run([*self.base, "exec", str(script), "space value", "中文", "--flag", '{"key":"value"}', "C:\\trailing\\"],
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(json.loads(result.stdout), ["space value", "中文", "--flag", '{"key":"value"}', "C:\\trailing\\"])

    def test_lifecycle_forwards_stdin_as_utf8(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "hook.py"
            script.write_text("import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False))", encoding="utf-8")
            result = subprocess.run([*self.base, "hook", "SessionStart", str(script)],
                                    input='{"message":"中文 🌍"}', capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"message": "中文 🌍"})

    def test_lifecycle_strips_utf8_bom_from_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "hook.py"
            script.write_text("import json,sys; print(json.dumps(json.load(sys.stdin)))", encoding="utf-8")
            result = subprocess.run(
                [*self.base, "hook", "SessionStart", str(script)],
                input=b'\xef\xbb\xbf{"message":"ok"}',
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(json.loads(result.stdout), {"message": "ok"})

    def test_install_requires_explicit_confirmation(self):
        result = subprocess.run([*self.base, "install-python"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error_code"], "python_install_confirmation_required")

    def test_native_preflight_needs_no_sh_on_path(self):
        environment = dict(os.environ, PATH=str(Path(sys.executable).parent))
        self.assertIsNone(shutil.which("sh", path=environment["PATH"]))
        result = subprocess.run([*self.base, "check"], env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["python"]["status"], "ready")

    def test_preflight_discovers_uv_managed_python_when_python_is_not_on_path(self):
        uv = shutil.which("uv.exe")
        if uv is None:
            self.skipTest("uv is not installed")
        environment = dict(os.environ, PATH=str(Path(uv).parent))
        self.assertIsNone(shutil.which("python.exe", path=environment["PATH"]))
        available = subprocess.run(
            [uv, "--no-python-downloads", "python", "find", ">=3.10"],
            env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        if available.returncode != 0:
            self.skipTest("uv has no existing Python 3.10+ installation")
        result = subprocess.run([*self.base, "check"], env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["python"]["status"], "ready", payload)
        self.assertTrue(Path(payload["python"]["executable"]).is_file())

    def test_preflight_discovers_managed_python_without_uv_on_path(self):
        managed_root = Path(sys.executable).parent.parent
        environment = dict(
            os.environ,
            PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
            UV_PYTHON_INSTALL_DIR=str(managed_root),
        )
        self.assertIsNone(shutil.which("python.exe", path=environment["PATH"]))
        self.assertIsNone(shutil.which("uv.exe", path=environment["PATH"]))
        result = subprocess.run([*self.base, "check"], env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["python"]["status"], "ready", payload)
        self.assertTrue(Path(payload["python"]["executable"]).is_file())


@unittest.skipUnless(os.name == "nt", "requires native Windows Git Bash")
class WindowsGitBashRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.shell = shutil.which("sh.exe")
        self.assertIsNotNone(self.shell, "Windows CI must expose Git Bash")
        temporary = tempfile.TemporaryDirectory(prefix="helix-bash-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.plugin = self.base / "plugin 中文 'quoted' $literal"
        shutil.copytree(ROOT, self.plugin, ignore=shutil.ignore_patterns("__pycache__"))
        self.environment = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        self.environment.pop("CLAUDE_ENV_FILE", None)
        self.environment.pop("HELIXHARNESS_BOUND_PLUGIN_ROOT", None)

    def run_bash(self, command, *, payload="", code=0):
        result = subprocess.run(
            [self.shell, "-c", command], env=self.environment, input=payload,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(code, result.returncode, result.stderr + result.stdout)
        return result.stdout

    def test_claude_startup_then_generated_binding_from_quoted_installation(self):
        self.environment["CLAUDE_PLUGIN_ROOT"] = self.plugin.as_posix()
        hooks = json.loads((self.plugin / "hosts/claude-code/hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        output = self.run_bash(command, payload=json.dumps({"hook_event_name": "SessionStart"}))
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        command = next(line for line in context.splitlines() if line.startswith("sh "))
        output = self.run_bash(command)
        decoder = json.JSONDecoder()
        check, offset = decoder.raw_decode(output.lstrip())
        resolved = json.loads(output.lstrip()[offset:].lstrip())
        self.assertTrue(check["ok"])
        self.assertEqual("resolved", resolved["status"])
        self.assertEqual("claude-code", resolved["host_id"])
        self.assertEqual(self.plugin.resolve(), Path(resolved["plugin_root"]).resolve())

    def test_native_python_crlf_probe_exec_and_hook_keep_stdin_and_exit_status(self):
        script = self.base / "echo 中文 'quoted'.py"
        script.write_text("import sys; print(sys.stdin.read(), end=''); sys.exit(7)", encoding="utf-8")
        for launcher, action in (("runtime_preflight.sh", "exec"), ("python_hook_bootstrap.sh", "SessionStart")):
            with self.subTest(action=action):
                command = shlex.join(["sh", (self.plugin / "scripts" / launcher).as_posix(), action, script.as_posix()])
                output = self.run_bash(command, payload="合成输入\n", code=7)
                self.assertEqual("合成输入\n", output)


if __name__ == "__main__":
    unittest.main()
