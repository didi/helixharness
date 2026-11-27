#!/usr/bin/env python3
"""Runtime preflight version-contract regressions."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/runtime_preflight.sh"
HOOK = ROOT / "scripts/python_hook_bootstrap.sh"


@unittest.skipIf(os.name == "nt", "POSIX launcher fixtures; native coverage in test_windows_runtime")
class RuntimePreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.bin_dir = Path(self.temp_dir.name)
        fake_python = self.bin_dir / "python3"
        fake_python.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-c\" ]; then printf '%s\\n' \"$FAKE_PYTHON_VERSION\"; "
            "else exit 0; fi\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        # Keep the negative version test hermetic: no fallback to a system Python.
        for name in ("python", "py", "git"):
            executable = self.bin_dir / name
            executable.write_text("#!/bin/sh\nexit 1\n")
            executable.chmod(0o755)
        (self.bin_dir / "dirname").symlink_to("/usr/bin/dirname")

    def run_launcher(self, launcher: Path, *args: str, version: str, path: str, stdin: str = ""):
        environment = os.environ.copy()
        environment["PATH"] = path
        environment["FAKE_PYTHON_VERSION"] = version
        completed = subprocess.run(
            ["/bin/sh", str(launcher), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def check(self, version: str, path: str | None = None) -> dict:
        return json.loads(self.run_launcher(SCRIPT, "check", version=version, path=path or str(self.bin_dir)))

    def test_python_39_is_rejected(self) -> None:
        payload = self.check("3.9.6")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["python"]["status"], "unsupported_version")
        self.assertEqual(payload["python"]["minimum_version"], "3.10")
        self.assertEqual(payload["python"]["version"], "3.9.6")
        self.assertEqual(payload["python"]["probed_executable"], str(self.bin_dir / "python3"))
        self.assertIsNone(payload["python"]["executable"])
        self.assertEqual(payload["next_action"], "install_python_or_disable_telemetry")

    def test_python_311_later_in_path_is_used_by_check_exec_and_hook(self) -> None:
        later = self.bin_dir / 'Python 安装 "quoted" \\ directory'
        later.mkdir()
        interpreter = later / "python3"
        interpreter.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then printf "%s\\n" 3.11.15; exit 0; fi\n'
            "exec " + shlex.quote(sys.executable) + ' "$@"\n',
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
        script = self.bin_dir / "read_stdin.py"
        script.write_text("import sys\nprint(sys.stdin.read(), end='')\n", encoding="utf-8")
        path = os.pathsep.join(map(str, (self.bin_dir, later)))

        payload = self.check("3.9.6", path)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["python"]["version"], "3.11.15")
        self.assertEqual(payload["python"]["executable"], str(interpreter))
        self.assertEqual(payload["python"]["arguments"], ["-X", "utf8"])
        for launcher, action in ((SCRIPT, "exec"), (HOOK, "SessionStart")):
            with self.subTest(launcher=launcher):
                output = self.run_launcher(
                    launcher, action, str(script), version="3.9.6", path=path, stdin="hook input\n"
                )
                self.assertEqual(output, "hook input\n")

    def test_versioned_python_without_alias_is_discovered(self) -> None:
        versioned = self.bin_dir / "python3.11"
        (self.bin_dir / "python3").rename(versioned)
        self.assertEqual(self.check("3.11.15")["python"]["executable"], str(versioned))

    def test_missing_and_unusable_candidates_are_distinct(self) -> None:
        for name in ("python3", "python", "py"):
            (self.bin_dir / name).unlink()
        self.assertEqual(self.check("unused")["python"]["status"], "missing")
        broken = self.bin_dir / "python3"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        payload = self.check("unused")
        self.assertEqual(payload["python"]["status"], "unusable")
        self.assertEqual(payload["python"]["probed_executable"], str(broken))

    def test_python_310_is_ready(self) -> None:
        payload = self.check("3.10.0")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["python"]["status"], "ready")
        self.assertEqual(payload["next_action"], "continue")

    def test_python_312_is_ready(self) -> None:
        payload = self.check("3.12.13")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["python"]["status"], "ready")
        self.assertEqual(payload["next_action"], "continue")

    def test_explicit_exec_interpreter_preserves_quoted_arguments_stdin_and_exit(self) -> None:
        interpreter = self.bin_dir / 'Preferred Python "引号"'
        interpreter.write_text('#!/bin/sh\nif [ "$1" = -c ]; then echo 3.12.13; exit 0; fi\n'
                               + 'exec ' + shlex.quote(sys.executable) + ' "$@"\n', encoding="utf-8")
        interpreter.chmod(0o755)
        script = self.bin_dir / "echo.py"
        script.write_text("import json,sys; print(json.dumps([sys.argv[1:],sys.stdin.read()])); sys.exit(7)")
        for selector in (str(interpreter), interpreter.name):
            result = subprocess.run(["/bin/sh", str(SCRIPT), "exec", "--python", selector,
                                     str(script), "参数 with spaces"], env={**os.environ, "PATH": str(self.bin_dir)},
                                    input="synthetic stdin", capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), [["参数 with spaces"], "synthetic stdin"])

    def test_explicit_old_python_does_not_fall_back_to_supported_default(self) -> None:
        old = self.bin_dir / "old-python"
        old.write_text('#!/bin/sh\necho 3.9.6\n'); old.chmod(0o755)
        result = subprocess.run(["/bin/sh", str(SCRIPT), "exec", "--python", str(old), "never.py"],
                                env={**os.environ, "PATH": str(self.bin_dir), "FAKE_PYTHON_VERSION": "3.12.13"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["python"]["status"], "unsupported_version")

    def test_explicit_missing_python_returns_runtime_diagnostic(self) -> None:
        result = subprocess.run(["/bin/sh", str(SCRIPT), "exec", "--python", str(self.bin_dir / "missing"), "never.py"],
                                env={**os.environ, "PATH": str(self.bin_dir), "FAKE_PYTHON_VERSION": "3.12.13"},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["python"]["status"], "unusable")

    def test_crlf_version_output_retains_minimum_and_strict_validation(self) -> None:
        for version, status in (("3.12.0\r", "ready"), ("3.9.6\r", "unsupported_version"),
                                ("3.12.0\r\r", "unusable"), ("3.\r12.0", "unusable"),
                                ("3.12.0\t", "unusable"), ("banner\n3.12.0\r", "unusable")):
            with self.subTest(version=version):
                self.assertEqual(status, self.check(version)["python"]["status"])

    def test_crlf_runtime_exec_and_hook_preserve_stdin_and_exit_code(self) -> None:
        interpreter = self.bin_dir / "python3"
        interpreter.write_text(
            '#!/bin/sh\nif [ "$1" = -c ]; then printf "3.12.0\\r\\n"; exit 0; fi\n'
            + "exec " + shlex.quote(sys.executable) + ' "$@"\n', encoding="utf-8",
        )
        script = self.bin_dir / "stdin echo.py"
        script.write_text("import sys; print(sys.stdin.read(), end=''); sys.exit(7)", encoding="utf-8")
        for launcher, action in ((SCRIPT, "exec"), (HOOK, "SessionStart")):
            with self.subTest(action=action):
                result = subprocess.run(
                    ["/bin/sh", str(launcher), action, str(script)],
                    input="合成 Hook 输入\n", env=dict(os.environ, PATH=str(self.bin_dir)),
                    text=True, capture_output=True, timeout=10,
                )
                self.assertEqual(7, result.returncode, result.stderr)
                self.assertEqual("合成 Hook 输入\n", result.stdout)

    def test_unusable_python3_falls_back_and_exec_uses_the_selected_runtime(self) -> None:
        (self.bin_dir / "python3").write_text("#!/bin/sh\nexit 1\n")
        fallback = self.bin_dir / "python"
        fallback.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        fallback.chmod(0o755)
        self.assertTrue(self.check("unused")["ok"])
        result = subprocess.run(["/bin/sh", str(SCRIPT), "exec", "-c", "print(42)"],
                                env=dict(os.environ, PATH=str(self.bin_dir)), capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("42", result.stdout.strip())

    def test_docs_and_hook_share_the_same_minimum(self) -> None:
        protocol = (ROOT / "reference/runtime-preflight.md").read_text(encoding="utf-8")
        hook = (ROOT / "scripts/python_hook_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("最低 Python 版本：3.10", protocol)
        self.assertIn("Python 3.10+", hook)
        self.assertNotIn("3.9+", protocol)
        self.assertNotIn("3.9+", hook)


if __name__ == "__main__":
    unittest.main()
