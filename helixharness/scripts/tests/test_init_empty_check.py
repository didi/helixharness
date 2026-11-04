#!/usr/bin/env python3
"""Greenfield init detection and contract regressions."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import init_empty_check

ROOT = Path(__file__).resolve().parents[2]


class InitEmptyCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_empty_directory_is_greenfield(self) -> None:
        result = init_empty_check.inspect_repository(self.root)
        self.assertTrue(result["empty"])
        self.assertEqual(result["reason"], "no_build_or_source_files")
        self.assertEqual(result["signals"], [])

    def test_documentation_only_is_greenfield(self) -> None:
        (self.root / "README.md").write_text("# New project\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs/architecture.md").write_text("TODO\n", encoding="utf-8")
        (self.root / "requirements").mkdir()
        (self.root / "requirements/initial.md").write_text(
            "Build a task app.\n", encoding="utf-8"
        )
        result = init_empty_check.inspect_repository(self.root)
        self.assertTrue(result["empty"])

    def test_build_metadata_is_not_greenfield(self) -> None:
        (self.root / "package.json").write_text("{}\n", encoding="utf-8")
        result = init_empty_check.inspect_repository(self.root)
        self.assertFalse(result["empty"])
        self.assertEqual(result["signals"], ["package.json"])

    def test_source_file_is_not_greenfield(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("print('hi')\n", encoding="utf-8")
        result = init_empty_check.inspect_repository(self.root)
        self.assertFalse(result["empty"])
        self.assertEqual(result["signals"], ["src/app.py"])

    def test_ignored_dependency_directories_do_not_count(self) -> None:
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/left-pad.js").write_text(
            "module.exports = 1\n", encoding="utf-8"
        )
        (self.root / ".venv").mkdir()
        (self.root / ".venv/tool.py").write_text("print(1)\n", encoding="utf-8")
        result = init_empty_check.inspect_repository(self.root)
        self.assertTrue(result["empty"])

    def test_missing_directory_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not exist"):
            init_empty_check.inspect_repository(self.root / "missing")

    def test_cli_emits_json_and_exit_status(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/init_empty_check.py"),
                "--repo-root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["empty"])

        missing = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/init_empty_check.py"),
                "--repo-root",
                str(self.root / "missing"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("does not exist", json.loads(missing.stdout)["error"])


class GreenfieldInitContractTest(unittest.TestCase):
    def test_init_command_has_a_scanless_greenfield_branch(self) -> None:
        command = (ROOT / "commands/init.md").read_text(encoding="utf-8")
        self.assertIn("init_empty_check.py", command)
        self.assertIn("projectType=empty", command)
        self.assertIn("bootstrap_mode=greenfield", command)
        self.assertIn("不得执行 Step 1 的任何扫描 agent", command)

    def test_detection_reference_distinguishes_empty_from_unknown(self) -> None:
        protocol = (ROOT / "reference/project-type-detection.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("在完整类型检测前", protocol)
        self.assertIn("`empty`", protocol)
        self.assertIn("空项目不是 `unknown`", protocol)
        self.assertIn("init_empty_check.py", protocol)

    def test_bootstrap_accepts_greenfield_without_scan_artifacts(self) -> None:
        bootstrap = (ROOT / "agents/init-service-bootstrap.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("service|fullstack-base|greenfield", bootstrap)
        self.assertIn("bootstrap_mode=greenfield", bootstrap)
        self.assertIn("没有扫描产物", bootstrap)


if __name__ == "__main__":
    unittest.main()
