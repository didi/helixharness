"""Cross-contract Cursor regressions, without native model or Git commit claims.

The real workspace CLI builds two-repository dispatch plans. Host-adapter text
assertions protect the mapping of those plans; they do not execute Task workers,
prove native terminal states, or stand in for the six-loop UT workflow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import test_workspace

PLUGIN = Path(__file__).resolve().parents[2]


class CursorWorkspaceDispatchRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        # These two factories only write synthetic Markdown/YAML. In particular,
        # do not call _make_repo_archive_ready, which creates a Git commit.
        self.fixture = test_workspace.LiteWorkspaceOverviewContractTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.workspace.resolve()
        self.fixture._write_overview()
        self.env = {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
        }

    def cli(self, name: str, *arguments: object) -> dict:
        result = subprocess.run(
            [sys.executable, "-B", str(PLUGIN / "scripts" / name), *map(str, arguments)],
            cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def adapter(self) -> str:
        context = self.cli("host_runtime_cli.py", "resolve", "--platform", "cursor",
                           "--expected-plugin-root", PLUGIN)
        self.assertEqual("cursor", context["host_id"])
        resource = Path(context["resolved_resources"]["subagent_protocol"])
        self.assertEqual(PLUGIN / "hosts/cursor/subagent.md", resource)
        return resource.read_text(encoding="utf-8")

    def plan(self, stage: str) -> dict:
        return self.cli("workspace.py", "--workspace-root", self.repo, "workspace-plan",
                        "--feature", "cross-repo", "--command", stage)

    def test_cursor_workspace_apply_preserves_command_authorized_nested_reviews(self) -> None:
        for repo in ("api", "web"):
            self.fixture._write_repo_feature(repo)
        plan = self.plan("apply")
        self.assertEqual([["api"], ["web"]], plan["batches"])
        self.assertEqual(2, len(plan["jobs"]))
        for job in plan["jobs"]:
            self.assertEqual("may_delegate", job["delegation_policy"])
            self.assertEqual("archive-preflight", job["completion_check"]["command"])
            self.assertEqual([job["ownership_root"]], job["write_scope"])

        adapter = self.adapter()
        # This previously failed: the Cursor adapter unconditionally replaced
        # every command-local policy with leaf, breaking the nested review axes.
        self.assertIn("delegation_policy=may_delegate", adapter)
        self.assertIn("未声明时", adapter)
        self.assertNotIn("每次派发注入 `delegation_policy=leaf`", adapter)
        self.assertIn("两个独立 Review", adapter)
        self.assertIn("BLOCKED: agent_failed", adapter)

    def test_cursor_workspace_propose_retains_leaf_and_disjoint_ownership(self) -> None:
        plan = self.plan("propose")
        jobs = {job["repo"]: job for job in plan["jobs"]}
        self.assertEqual({"api", "web"}, set(jobs))
        for name, job in jobs.items():
            other = "web" if name == "api" else "api"
            self.assertEqual("leaf", job["delegation_policy"])
            self.assertEqual([str(self.repo / name)], job["write_scope"])
            self.assertNotIn(str(self.repo / other), job["read_scope"])
            self.assertIn(str(self.repo / "docs/specs/active/cross-repo"), job["forbidden_scope"])
        adapter = self.adapter()
        self.assertIn("delegation_policy=leaf", adapter)
        self.assertIn("read_scope", adapter)
        self.assertIn("write_scope=none", adapter)
        self.assertIn("子集", adapter)
        self.assertIn("BLOCKED: agent_read_scope_violation", adapter)

    def test_nested_review_capability_failure_does_not_authorize_inline_self_review(self) -> None:
        adapter = self.adapter()
        for required in (
            "BLOCKED: cursor_subagent_unavailable",
            "容量暂满",
            "不触发内联重试",
            "失败或取消",
            "不能把两个独立 Review 轴合并",
        ):
            self.assertIn(required, adapter)
        self.assertRegex(adapter, r"(?:原生|真实)终态")


if __name__ == "__main__":
    unittest.main()
