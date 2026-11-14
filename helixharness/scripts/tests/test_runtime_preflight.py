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

    def check(self, version: str) -> dict:
        environment = os.environ.copy()
        environment["PATH"] = str(self.bin_dir)
        environment["FAKE_PYTHON_VERSION"] = version
        completed = subprocess.run(
            ["/bin/sh", str(SCRIPT), "check"],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_python_39_is_rejected(self) -> None:
        payload = self.check("3.9.6")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["python"]["status"], "unsupported_version")
        self.assertEqual(payload["python"]["minimum_version"], "3.10")
        self.assertEqual(payload["next_action"], "install_python_or_disable_telemetry")

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
