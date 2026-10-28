from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class SourceBindingTest(unittest.TestCase):
    def invoke(self, script, *args, bound=None):
        env = os.environ.copy()
        env.pop("HELIXHARNESS_BOUND_PLUGIN_ROOT", None)
        if bound is not None:
            env["HELIXHARNESS_BOUND_PLUGIN_ROOT"] = str(bound)
        return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                              env=env, text=True, capture_output=True)

    def test_context_identifies_root_and_resolves_each_resource(self):
        result = self.invoke("host_runtime_cli.py", "resolve", "--platform", "claude-code")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(Path(data["plugin_root"]), ROOT.resolve())
        self.assertEqual(Path(data["host_manifest"]), ROOT / "hosts/claude-code/host.json")
        for key, relative in data["resources"].items():
            self.assertEqual(Path(data["resolved_resources"][key]), (ROOT / relative).resolve())

    def test_explicit_different_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            result = self.invoke("host_runtime_cli.py", "resolve", "--platform", "claude-code",
                                 "--expected-plugin-root", other)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["reason"], "plugin_source_mismatch")

    def test_mismatching_session_source_stops_entrypoints_before_project_work(self):
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "other-plugin"
            other.mkdir()
            repo = Path(directory) / "project"
            repo.mkdir()
            cases = [
                ("host_runtime_cli.py", "resolve", "--platform", "claude-code"),
                ("workflow_router.py", "--repo-root", str(repo), "route", "--command", "propose", "--arguments", "demo requirement"),
                ("workflow.py", "--repo-root", str(repo), "bind-apply", "--feature", "demo"),
                ("version_update.py", "--platform", "claude-code", "--repo-root", str(repo), "check"),
                ("codex_entry_load.py", "--plugin-root", str(ROOT), "--command", "init"),
            ]
            for script, *args in cases:
                with self.subTest(script=script):
                    result = self.invoke(script, *args, bound=other)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(json.loads(result.stdout)["reason"], "plugin_source_mismatch")
                    self.assertEqual(list(repo.iterdir()), [])

    def test_equivalent_symlink_spelling_of_bound_root_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "plugin-link"
            alias.symlink_to(ROOT, target_is_directory=True)
            result = self.invoke("host_runtime_cli.py", "resolve", "--platform", "claude-code", bound=alias)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_root_cannot_override_another_session_binding(self):
        with tempfile.TemporaryDirectory() as other:
            result = self.invoke("host_runtime_cli.py", "resolve", "--platform", "claude-code",
                                 "--expected-plugin-root", str(ROOT), bound=other)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["reason"], "plugin_source_mismatch")

if __name__ == "__main__":
    unittest.main()
