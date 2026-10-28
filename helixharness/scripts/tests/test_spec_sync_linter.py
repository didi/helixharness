from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PLUGIN_ROOT / "templates/harness/spec-sync-check.sh.tpl"


class SpecSyncLinterTemplateTest(unittest.TestCase):
    def _check_feature(self, *, status: str, report: bool, workspace: bool = False) -> str:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feature = root / "docs/specs/active/demo"
            feature.mkdir(parents=True)
            (feature / "spec.md").write_text(f"---\nstatus: {status}\n---\n# Spec\n")
            if workspace:
                (root / "helixharness.workspace.yaml").write_text("version: 1\n")
            else:
                (feature / "tasks.md").write_text("# Tasks\n")
            if report:
                (feature / "verify-report.md").write_text("# Verified\n")
            script = root / "docs/harness/linters/spec-sync-check.sh"
            script.parent.mkdir(parents=True)
            script.write_text(TEMPLATE.read_text())
            result = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_completed_three_artifacts_do_not_warn(self) -> None:
        output = self._check_feature(status="implemented", report=True)
        self.assertNotIn("⚠️", output)

    def test_planning_does_not_require_future_evidence(self) -> None:
        output = self._check_feature(status="ready", report=False)
        self.assertNotIn("⚠️", output)

    def test_implemented_without_evidence_warns(self) -> None:
        output = self._check_feature(status="implemented", report=False)
        self.assertIn("verify-report.md 缺失", output)

    def test_workspace_root_requires_only_spec(self) -> None:
        output = self._check_feature(status="implemented", report=False, workspace=True)
        self.assertNotIn("⚠️", output)

    def test_finds_repo_root_from_script_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            script = repo / "docs/harness/linters/spec-sync-check.sh"
            feature = repo / "docs/specs/active/feature_demo"
            script.parent.mkdir(parents=True)
            feature.mkdir(parents=True)
            (feature / "tasks.md").write_text("# tasks\n", encoding="utf-8")
            script.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

            completed = subprocess.run(
                ["bash", str(script)],
                cwd=Path(temporary),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stdout)
            self.assertIn("feature_demo", completed.stdout)
            self.assertIn("tasks.md", completed.stdout)
            self.assertNotIn("当前无活跃 feature", completed.stdout)

    def test_bootstrap_agents_use_the_canonical_template(self) -> None:
        for relative in (
            "agents/init-service-bootstrap.md",
            "agents/init-frontend-bootstrap.md",
        ):
            with self.subTest(relative=relative):
                text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("templates/harness/spec-sync-check.sh.tpl", text)


if __name__ == "__main__":
    unittest.main()
