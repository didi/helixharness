#!/usr/bin/env python3
"""Runtime preflight version-contract regressions."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/runtime_preflight.sh"

FAKE_INTERPRETER = (
    "#!/bin/sh\n"
    "for arg in \"$@\"; do\n"
    "  if [ \"$arg\" = \"-c\" ]; then printf '%s\\n' \"$FAKE_PYTHON_VERSION\"; exit 0; fi\n"
    "done\n"
    "exit 0\n"
)

BROKEN_STUB = (
    "#!/bin/sh\n"
    "# Mimics a Windows Store app-execution alias: present on PATH but unable\n"
    "# to run Python; the probe must skip it.\n"
    "exit 1\n"
)


class RuntimePreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.bin_dir = Path(self.temp_dir.name)
        # Shadow every candidate name so the host machine's real interpreters
        # cannot leak into a test.
        for name in ("python3", "python", "py"):
            self.write_fake(name, FAKE_INTERPRETER)

    def write_fake(self, name: str, body: str) -> None:
        fake = self.bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)

    def check(self, version: str) -> dict:
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            [str(self.bin_dir), environment.get("PATH", "")]
        )
        environment["FAKE_PYTHON_VERSION"] = version
        completed = subprocess.run(
            ["sh", str(SCRIPT), "check"],
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

    def test_ready_payload_reports_selected_command(self) -> None:
        payload = self.check("3.12.13")
        self.assertEqual(payload["python"]["command"], "python3")

    def test_broken_python3_alias_falls_back_to_python(self) -> None:
        self.write_fake("python3", BROKEN_STUB)
        payload = self.check("3.12.16")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["python"]["status"], "ready")
        self.assertEqual(payload["python"]["command"], "python")
        self.assertEqual(payload["python"]["version"], "3.12.16")

    def test_broken_python3_alias_falls_back_to_py_launcher(self) -> None:
        self.write_fake("python3", BROKEN_STUB)
        self.write_fake("python", BROKEN_STUB)
        payload = self.check("3.11.9")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["python"]["status"], "ready")
        self.assertEqual(payload["python"]["command"], "py -3")

    def test_all_candidates_broken_reports_unusable(self) -> None:
        self.write_fake("python3", BROKEN_STUB)
        self.write_fake("python", BROKEN_STUB)
        self.write_fake("py", BROKEN_STUB)
        payload = self.check("3.12.16")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["python"]["status"], "unusable")
        self.assertIsNone(payload["python"]["command"])
        self.assertEqual(payload["next_action"], "install_python_or_disable_telemetry")

    def test_docs_and_hook_share_the_same_minimum(self) -> None:
        protocol = (ROOT / "reference/runtime-preflight.md").read_text(encoding="utf-8")
        hook = (ROOT / "scripts/python_hook_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("最低 Python 版本：3.10", protocol)
        self.assertIn("Python 3.10+", hook)
        self.assertNotIn("3.9+", protocol)
        self.assertNotIn("3.9+", hook)


if __name__ == "__main__":
    unittest.main()
