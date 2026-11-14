"""Native PowerShell integration; run by the Windows CI lane, no WSL required."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "nt", "requires native Windows PowerShell")
class WindowsRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(self.powershell)
        self.base = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(ROOT / "scripts/runtime_preflight.ps1")]

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


if __name__ == "__main__":
    unittest.main()
