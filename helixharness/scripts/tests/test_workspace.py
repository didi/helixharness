from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import workflow
import workspace
import workflow_router


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class LiteWorkspaceOverviewContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        for repo in ("api", "web"):
            (self.workspace / repo).mkdir()
        (self.workspace / "helixharness.workspace.yaml").write_text(
            """version: 1
repos:
  api:
    path: ./api
  web:
    path: ./web
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_overview(self, *, status: str = "ready") -> None:
        directory = self.workspace / "docs/specs/active/cross-repo"
        directory.mkdir(parents=True)
        (self.workspace / "docs/specs/completed").mkdir(parents=True, exist_ok=True)
        (directory / "spec.md").write_text(
            """---
feature: cross-repo
execution_scope: workspace
status: {status}
testing_seam_confirmed: true
---
# Cross-repo — Workspace 跨仓概要设计

## 背景、目标与范围

协调 API 与 Web 完成一次兼容变更。

## 总体架构

```mermaid
flowchart LR
    WEB[web] -->|HTTP| API[api]
```

## 仓库职责与依赖

| Repo | Mode | Responsibility | Depends On | Contract Refs |
|---|---|---|---|---|
| api | write | 发布兼容接口 | — | C-01 |
| web | write | 接入兼容接口 | api | C-01 |

## 跨仓接口与数据契约

### C-01：HTTP Contract

- Owner：api
- Consumer：web
- 成功与错误语义：成功返回 200，非法输入返回 400。
- 兼容策略：新增可选字段。

## 端到端业务流程

```mermaid
sequenceDiagram
    participant W as web
    participant A as api
    W->>A: 调用兼容接口
    A-->>W: 返回结果
```

## 状态、数据归属与一致性

API 是数据事实 Owner。

## 兼容、发布与回滚

先发布 API，再发布 Web；可通过开关回滚。

## 稳定性、安全与可观测性

两端记录同一 trace_id。

## 跨仓验收标准

| AC | Scenario | Involved Repos | Expected Evidence |
|---|---|---|---|
| AC-01 | Web 调用新接口 | api, web | 两仓测试与 Review 通过 |

## Repo Handoff

### api

必须实现 C-01 的 Owner；内部实现由本仓决定。

### web

必须消费 C-01；内部实现由本仓决定。

## 已确认决策与非目标

阻塞决策已全部关闭。
""".format(status=status),
            encoding="utf-8",
        )
    def _write_repo_feature(self, repo: str) -> None:
        directory = self.workspace / repo / "docs/specs/active/cross-repo"
        directory.mkdir(parents=True)
        workspace_spec = (
            self.workspace / "docs/specs/active/cross-repo/spec.md"
        ).read_text(encoding="utf-8")
        workspace_receipt = workflow.spec_receipt(workspace_spec)
        (directory / "spec.md").write_text(
            f"""---
feature: cross-repo
execution_scope: single
workspace_repo: {repo}
workspace_contract_sha256: {workspace_receipt}
status: ready
testing_seam_confirmed: true
---
# {repo} delivery slice
""",
            encoding="utf-8",
        )
        (directory / "tasks.md").write_text(
            """---
feature: cross-repo
status: ready
---
| ID | Task | Status |
|---|---|---|
| T-01 | Implement local delivery slice | pending |
""",
            encoding="utf-8",
        )

    def _make_repo_archive_ready(self, repo: str) -> None:
        repo_root = self.workspace / repo
        subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_root), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--allow-empty",
                "-qm",
                "initial",
            ],
            check=True,
        )
        workspace_spec = (
            self.workspace / "docs/specs/active/cross-repo/spec.md"
        ).read_text(encoding="utf-8")
        workspace_receipt = workflow.spec_receipt(workspace_spec)
        directory = repo_root / "docs/specs/active/cross-repo"
        directory.mkdir(parents=True)
        spec = f"""---
feature: cross-repo
execution_scope: single
workspace_repo: {repo}
workspace_contract_sha256: {workspace_receipt}
status: implemented
testing_seam_confirmed: true
---
# {repo} delivery slice
"""
        tasks = """---
feature: cross-repo
status: completed
---
| ID | Task | Status |
|---|---|---|
| T-01 | Implement local delivery slice | completed |
"""
        (directory / "spec.md").write_text(spec, encoding="utf-8")
        (directory / "tasks.md").write_text(tasks, encoding="utf-8")
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        empty_manifest = json.dumps(
            [], ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        state_path = repo_root / ".helixharness/workflow/cross-repo/apply-state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "apply_baseline_head": head,
                    "apply_baseline_manifest": [],
                    "apply_baseline_sha256": workflow.artifact_receipt(
                        empty_manifest
                    ),
                    "apply_spec_receipt_sha256": workflow.spec_receipt(spec),
                    "feature": "cross-repo",
                    "schema_version": 1,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        scope = workflow.seal_review_scope(repo_root, "cross-repo")
        evidence = f"""---
feature: cross-repo
spec_receipt_sha256: {workflow.spec_receipt(spec)}
tasks_receipt_sha256: {workflow.artifact_receipt(tasks)}
review_fixed_point: {scope['review_head']}
review_head: {scope['review_head']}
review_scope_sha256: {scope['review_scope_sha256']}
review_mode: parallel_subagents
standards_verdict: APPROVE
spec_verdict: APPROVE
must_fix_rounds: 0
stage_trace:
  propose: completed
  apply: completed
  archive: pending
---
# Evidence
"""
        (directory / "verify-report.md").write_text(evidence, encoding="utf-8")

    def _move_repo_to_completed(self, repo: str, *, stale: bool = False) -> None:
        self._make_repo_archive_ready(repo)
        active = self.workspace / repo / "docs/specs/active/cross-repo"
        spec_path = active / "spec.md"
        spec = spec_path.read_text(encoding="utf-8").replace(
            "status: implemented", "status: completed"
        )
        if stale:
            spec = re.sub(
                r"(?m)^workspace_contract_sha256: [0-9a-f]{64}$",
                "workspace_contract_sha256: " + ("0" * 64),
                spec,
            )
        old_receipt = workflow.spec_receipt(
            spec_path.read_text(encoding="utf-8")
        )
        new_receipt = workflow.spec_receipt(spec)
        spec_path.write_text(spec, encoding="utf-8")
        evidence_path = active / "verify-report.md"
        evidence_path.write_text(
            evidence_path.read_text(encoding="utf-8")
            .replace("archive: pending", "archive: completed")
            .replace(old_receipt, new_receipt),
            encoding="utf-8",
        )
        completed = self.workspace / repo / "docs/specs/completed/cross-repo"
        completed.parent.mkdir(parents=True)
        active.rename(completed)

    def _revision_case(self, status="implemented"):
        self._write_overview(status="planning")
        self._make_repo_archive_ready("api")
        repo = self.workspace / "api"
        spec_path = repo / "docs/specs/active/cross-repo/spec.md"
        spec_path.write_text(spec_path.read_text().replace("status: implemented", f"status: {status}"))
        source = self.workspace / "docs/specs/active/cross-repo/spec.md"
        source.write_text(source.read_text() + "\nConfirmed transport limit: 1 MiB.\n")
        return repo, source

    def test_changed_workspace_contract_routes_implemented_child_to_revision(self):
        repo, source = self._revision_case()
        plan = workspace.workspace_execution_plan(self.workspace, "cross-repo", "propose")
        job = next(job for job in plan["jobs"] if job["repo"] == "api")
        code, result = workflow_router.route(repo, command="propose", arguments=job["arguments"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["action"], "load_workflow", result)
        self.assertEqual(result["workflow"], job["expected_workflow"])
        self.assertTrue(result["workspace_revision"]["required"])
        self.assertEqual(job["completion_check"], {"command": "validate-tasks", "stage": "apply"})
        # A normal repeat without the controlled Workspace source remains a recommendation.
        _, ordinary = workflow_router.route(repo, command="propose", arguments="cross-repo other-requirement.md")
        self.assertEqual(ordinary["action"], "recommend_command")

    def test_workspace_revision_preserves_first_apply_and_completed_tasks(self):
        repo, source = self._revision_case(status="planning")
        state_path = repo / ".helixharness/workflow/cross-repo/apply-state.json"
        before = json.loads(state_path.read_text())
        (repo / "business.py").write_text("print('initial feature implementation')\n")
        receipt = workflow.spec_receipt(source.read_text())
        begun = workspace.begin_repo_revision(self.workspace, "cross-repo", "api", receipt)
        resumed = workspace.begin_repo_revision(self.workspace, "cross-repo", "api", receipt)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(begun["repair_revision"], resumed["repair_revision"])
        directory = repo / "docs/specs/active/cross-repo"
        spec = directory.joinpath("spec.md").read_text()
        spec = re.sub(r"workspace_contract_sha256: [0-9a-f]{64}", "workspace_contract_sha256: " + receipt, spec)
        spec = spec.replace("testing_seam_confirmed: true", "testing_seam_confirmed: true\nrepair_revision: 1")
        spec += ("\n## Repair Revisions\n\n| Revision | Repair Key | Kind | Source SHA-256 | Summary |\n"
                 "|---|---|---|---|---|\n"
                 f"| R-01 | {begun['repair_key']} | contract_amendment | {receipt} | Transport limit |\n")
        tasks = ("---\nfeature: cross-repo\nstatus: completed\n---\n"
                 "| ID | Task | Status | Revision |\n|---|---|---|---|\n"
                 "| T-01 | Implement local delivery slice | completed | R-00 |\n"
                 "| T-02 | Validate revised transport limit | pending | R-01 |\n")
        directory.joinpath("spec.md").write_text(spec)
        directory.joinpath("tasks.md").write_text(tasks)
        workflow.validate_tasks(repo, "cross-repo", "propose", repair_key=begun["repair_key"])
        result = workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
                    expected_spec_receipt=workflow.spec_receipt(spec), expected_tasks_receipt=workflow.artifact_receipt(tasks))
        self.assertEqual(result["status"], "applying")
        after = json.loads(state_path.read_text())
        for key in ("apply_baseline_head", "apply_baseline_manifest", "apply_baseline_sha256"):
            self.assertEqual(before[key], after[key], key)
        self.assertFalse(state_path.with_name("review-state.json").exists())
        self.assertTrue(state_path.with_name("review-state.r1.invalidated.json").exists())
        self.assertEqual(workflow.validate_tasks(repo, "cross-repo", "apply")["next_task"], "T-02")
        self.assertIn("business.py", workflow.seal_review_scope(repo, "cross-repo")["changed_paths"])
        plan = workspace.workspace_execution_plan(self.workspace, "cross-repo", "propose")
        self.assertNotIn("api", [job["repo"] for job in plan["jobs"]])
        self.assertIn("api", plan["repo_results"])

    def test_workspace_revision_rejects_drift_and_missing_initial_baseline(self):
        repo, source = self._revision_case(status="planning")
        with self.assertRaisesRegex(workspace.WorkspaceError, "receipt"):
            workspace.begin_repo_revision(self.workspace, "cross-repo", "api", "0" * 64)
        state_path = repo / ".helixharness/workflow/cross-repo/apply-state.json"
        state_path.unlink()
        with self.assertRaisesRegex(workflow.WorkflowError, "baseline|Apply"):
            workspace.begin_repo_revision(self.workspace, "cross-repo", "api", workflow.spec_receipt(source.read_text()))
        self.assertFalse(state_path.with_name("repair-state.json").exists())

    def test_workspace_revision_of_ready_child_does_not_create_apply_baseline(self):
        repo, source = self._revision_case(status="ready")
        directory = repo / "docs/specs/active/cross-repo"
        tasks_path = directory / "tasks.md"
        tasks_path.write_text(tasks_path.read_text().replace("status: completed", "status: ready")
                              .replace("| completed |", "| pending |"))
        state = repo / ".helixharness/workflow/cross-repo"
        (state / "apply-state.json").unlink()
        (state / "review-state.json").unlink()
        receipt = workflow.spec_receipt(source.read_text())
        begun = workspace.begin_repo_revision(self.workspace, "cross-repo", "api", receipt)
        saved = json.loads((state / "repair-state.json").read_text())
        self.assertEqual(saved["target_status"], "ready")
        self.assertIsNone(saved["apply_baseline_head"])
        self.assertFalse((state / "apply-state.json").exists())
        source.write_text(source.read_text().replace("status: planning", "status: ready"))
        resumed = workspace.begin_repo_revision(self.workspace, "cross-repo", "api", receipt)
        self.assertEqual(resumed["repair_key"], begun["repair_key"])
        self.assertTrue(resumed["resumed"])

    def test_workspace_revision_rejects_a_child_identity_from_another_repo(self):
        repo, source = self._revision_case()
        spec = repo / "docs/specs/active/cross-repo/spec.md"
        spec.write_text(spec.read_text().replace("workspace_repo: api", "workspace_repo: web"))
        code, result = workflow_router.route(repo, command="propose",
                                            arguments=f"cross-repo {source}")
        self.assertEqual(code, 2, result)
        self.assertEqual(result["action"], "blocked")
        self.assertIn("identity", result["reason"])
        self.assertFalse((repo / ".helixharness/workflow/cross-repo/repair-state.json").exists())

    def _prepare_stale_revision_binding(self, *, controlled=True, status="ready"):
        repo, source = self._revision_case(status=status)
        directory = repo / "docs/specs/active/cross-repo"
        state = repo / ".helixharness/workflow/cross-repo"
        tasks = directory / "tasks.md"
        if status == "ready":
            for name in ("apply-state.json", "review-state.json"):
                (state / name).unlink()
            tasks.write_text(tasks.read_text().replace("status: completed", "status: ready")
                             .replace("| completed |", "| pending |"))
        receipt = workflow.spec_receipt(source.read_text())
        context = workspace.workspace_revision_context(repo, "cross-repo", str(source))
        if controlled:
            begun = workspace.begin_repo_revision(self.workspace, "cross-repo", "api", receipt)
        else:
            # Reproduce a binding produced without the Workspace identity guard.
            facts = workflow._repair_preflight(repo, "cross-repo")
            workflow.begin_repair(repo, "cross-repo", repair_key=context["repair_key"],
                                  source_sha256=receipt, repair_kind="contract_amendment",
                                  expected_spec_receipt=facts["spec_receipt_sha256"],
                                  expected_tasks_receipt=facts["tasks_receipt_sha256"])
            begun = context
        spec = directory / "spec.md"
        spec.write_text(spec.read_text().replace("testing_seam_confirmed: true",
                       "testing_seam_confirmed: true\nrepair_revision: 1")
                       + "\n## Repair Revisions\n\n"
                       "| Revision | Repair Key | Kind | Source SHA-256 | Summary |\n"
                       "|---|---|---|---|---|\n"
                       f"| R-01 | {begun['repair_key']} | contract_amendment | {receipt} | Limit |\n")
        old_status = "pending" if status == "ready" else "completed"
        tasks_status = "ready" if status == "ready" else "completed"
        tasks.write_text(f"---\nfeature: cross-repo\nstatus: {tasks_status}\n---\n"
                         "| ID | Task | Status | Revision |\n|---|---|---|---|\n"
                         f"| T-01 | Implement local delivery slice | {old_status} | R-00 |\n"
                         "| T-02 | Verify transport limit | pending | R-01 |\n")
        return repo, source, begun

    def test_workspace_binding_guard_rejects_old_parent_receipt_before_rebind(self):
        repo, source, begun = self._prepare_stale_revision_binding()
        directory = repo / "docs/specs/active/cross-repo"
        spec = directory / "spec.md"
        tasks = directory / "tasks.md"
        with self.assertRaisesRegex(workflow.WorkflowError, "Workspace.*binding"):
            workflow.validate_tasks(repo, "cross-repo", "propose", repair_key=begun["repair_key"])
        with self.assertRaisesRegex(workflow.WorkflowError, "Workspace.*binding"):
            workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
                expected_spec_receipt=workflow.spec_receipt(spec.read_text()),
                expected_tasks_receipt=workflow.artifact_receipt(tasks.read_text()))
        state = repo / ".helixharness/workflow/cross-repo/repair-state.json"
        self.assertEqual(json.loads(state.read_text())["status"], "in_progress")

    def test_workspace_binding_guard_can_reopen_unchanged_unexecuted_binding(self):
        repo, source, begun = self._prepare_stale_revision_binding(controlled=False)
        directory = repo / "docs/specs/active/cross-repo"
        spec = directory / "spec.md"
        tasks = directory / "tasks.md"
        workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
            expected_spec_receipt=workflow.spec_receipt(spec.read_text()),
            expected_tasks_receipt=workflow.artifact_receipt(tasks.read_text()))
        before = tasks.read_bytes()
        resumed = workspace.begin_repo_revision(self.workspace, "cross-repo", "api",
                                                 workflow.spec_receipt(source.read_text()))
        self.assertEqual(resumed["status"], "in_progress")
        self.assertTrue(resumed["binding_reopened"])
        self.assertEqual(resumed["repair_revision"], 1)
        self.assertEqual(before, tasks.read_bytes())
        self.assertFalse((repo / ".helixharness/workflow/cross-repo/apply-state.json").exists())
        receipt = workflow.spec_receipt(source.read_text())
        spec.write_text(re.sub(r"workspace_contract_sha256: [0-9a-f]{64}",
                              "workspace_contract_sha256: " + receipt, spec.read_text()))
        workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
            expected_spec_receipt=workflow.spec_receipt(spec.read_text()),
            expected_tasks_receipt=workflow.artifact_receipt(tasks.read_text()))
        self.assertFalse(workspace.workspace_revision_context(repo, "cross-repo", str(source))["required"])

    def test_workspace_binding_guard_reopen_rejects_post_bind_edits(self):
        repo, source, begun = self._prepare_stale_revision_binding(controlled=False)
        directory = repo / "docs/specs/active/cross-repo"
        spec = directory / "spec.md"
        tasks = directory / "tasks.md"
        workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
            expected_spec_receipt=workflow.spec_receipt(spec.read_text()),
            expected_tasks_receipt=workflow.artifact_receipt(tasks.read_text()))
        tasks.write_text(tasks.read_text() + "\nPost-bind edit.\n")
        with self.assertRaisesRegex(workflow.WorkflowError, "binding.*changed|binding.*executed"):
            workspace.begin_repo_revision(self.workspace, "cross-repo", "api",
                                            workflow.spec_receipt(source.read_text()))

    def test_workspace_binding_guard_reopen_preserves_existing_apply_baseline(self):
        repo, source, begun = self._prepare_stale_revision_binding(controlled=False, status="implemented")
        directory = repo / "docs/specs/active/cross-repo"
        spec = directory / "spec.md"
        tasks = directory / "tasks.md"
        state = repo / ".helixharness/workflow/cross-repo/apply-state.json"
        before = json.loads(state.read_text())
        workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
            expected_spec_receipt=workflow.spec_receipt(spec.read_text()),
            expected_tasks_receipt=workflow.artifact_receipt(tasks.read_text()))
        receipt = workflow.spec_receipt(source.read_text())
        resumed = workspace.begin_repo_revision(self.workspace, "cross-repo", "api", receipt)
        self.assertTrue(resumed["binding_reopened"])
        spec.write_text(re.sub(r"workspace_contract_sha256: [0-9a-f]{64}",
                              "workspace_contract_sha256: " + receipt, spec.read_text()))
        workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
            expected_spec_receipt=workflow.spec_receipt(spec.read_text()),
            expected_tasks_receipt=workflow.artifact_receipt(tasks.read_text()))
        after = json.loads(state.read_text())
        for key in ("apply_baseline_head", "apply_baseline_sha256", "apply_baseline_manifest"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(workflow.task_rows(tasks.read_text())[0]["status"], "completed")

    def test_workspace_binding_guard_reopen_rejects_started_apply(self):
        repo, source, begun = self._prepare_stale_revision_binding(controlled=False)
        directory = repo / "docs/specs/active/cross-repo"
        spec = directory / "spec.md"
        tasks = directory / "tasks.md"
        workflow.rebind_repair(repo, "cross-repo", repair_key=begun["repair_key"],
            expected_spec_receipt=workflow.spec_receipt(spec.read_text()),
            expected_tasks_receipt=workflow.artifact_receipt(tasks.read_text()))
        workflow._bind_apply_receipt(repo, "cross-repo")
        with self.assertRaisesRegex(workflow.WorkflowError, "binding.*executed"):
            workspace.begin_repo_revision(self.workspace, "cross-repo", "api",
                                            workflow.spec_receipt(source.read_text()))

    def test_workspace_revision_cannot_reopen_archived_child(self):
        self._write_overview(status="planning")
        self._move_repo_to_completed("api")
        source = self.workspace / "docs/specs/active/cross-repo/spec.md"
        source.write_text(source.read_text() + "\nChanged contract.\n")
        with self.assertRaisesRegex(workspace.WorkspaceError, "archived"):
            workspace.workspace_execution_plan(self.workspace, "cross-repo", "propose")

    def test_registry_rejects_workspace_root_as_a_child_repo(self) -> None:
        (self.workspace / "helixharness.workspace.yaml").write_text(
            """version: 1
repos:
  root:
    path: .
""",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            workspace.WorkspaceError, "cannot be the workspace root"
        ):
            workspace.load_registry(self.workspace)

    def test_registry_rejects_an_intermediate_symlink_component(self) -> None:
        (self.workspace / "repo-links").symlink_to(
            self.workspace, target_is_directory=True
        )
        (self.workspace / "helixharness.workspace.yaml").write_text(
            """version: 1
repos:
  api:
    path: ./repo-links/api
""",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            workspace.WorkspaceError, "traverses a symlink"
        ):
            workspace.load_registry(self.workspace)

    def test_overview_rejects_an_intermediate_docs_symlink(self) -> None:
        self._write_overview()
        (self.workspace / "docs").rename(self.workspace / "real-docs")
        (self.workspace / "docs").symlink_to(
            self.workspace / "real-docs", target_is_directory=True
        )

        with self.assertRaisesRegex(
            workspace.WorkspaceError, "traverses a symlink"
        ):
            workspace.validate_workspace_overview(
                self.workspace, "cross-repo", "propose"
            )

    def test_archive_preflight_rejects_a_symlinked_completed_parent(self) -> None:
        self._write_overview(status="implemented")
        completed = self.workspace / "docs/specs/completed"
        completed.rmdir()
        external = self.workspace / "external-completed"
        external.mkdir()
        completed.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(
            workspace.WorkspaceError, "traverses a symlink"
        ):
            workspace.workspace_archive_preflight(
                self.workspace, "cross-repo"
            )

    def test_overview_is_the_only_workspace_artifact_and_exposes_static_repo_contract(self) -> None:
        self._write_overview()

        result = workspace.validate_workspace_overview(
            self.workspace, "cross-repo", "propose"
        )
        plan = workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "propose"
        )
        propose_arguments = plan["jobs"][0]["command"].removeprefix(
            "/helix:propose "
        )
        route_code, route = workflow_router.route(
            self.workspace / "api",
            command="propose",
            arguments=propose_arguments,
            arguments_mode="auto",
        )

        self.assertEqual(["api", "web"], result["repos"])
        self.assertEqual([], result["repositories"][0]["depends_on"])
        self.assertEqual(["api"], result["repositories"][1]["depends_on"])
        self.assertEqual(["C-01"], result["repositories"][1]["contract_refs"])
        self.assertIn(str(result["spec_receipt_sha256"]), plan["jobs"][0].values())
        self.assertEqual(0, route_code)
        self.assertEqual("single", route["execution_scope"])
        self.assertEqual("workflows/single/propose.md", route["workflow"])
        self.assertEqual(
            ["spec.md"],
            sorted(
                path.name
                for path in (
                    self.workspace / "docs/specs/active/cross-repo"
                ).iterdir()
            ),
        )

    def test_repo_jobs_have_an_executable_cross_platform_dispatch_contract(self) -> None:
        self._write_overview()

        plan = workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "propose"
        )
        job = plan["jobs"][0]
        dispatch = (PLUGIN_ROOT / "reference/workspace-repo-dispatch.md").read_text(
            encoding="utf-8"
        )

        self.assertEqual("propose", job["stage"])
        self.assertEqual("single", job["expected_execution_scope"])
        self.assertEqual("workflows/single/propose.md", job["expected_workflow"])
        self.assertEqual([], job["depends_on"])
        self.assertEqual(str((self.workspace / "api").resolve()), job["ownership_root"])
        self.assertEqual("commands/propose.md", job["prompt_source"])
        self.assertEqual("leaf", job["delegation_policy"])
        self.assertEqual("host_native", job["interaction_mode"])
        self.assertEqual(job["completion_check"], job["validation"])
        self.assertIn(str((self.workspace / "api").resolve()), job["write_scope"])
        self.assertIn(
            str((self.workspace / "docs/specs/active/cross-repo").resolve()),
            job["forbidden_scope"],
        )
        self.assertNotIn("--lite", job["arguments"])
        self.assertEqual("validate-tasks", job["completion_check"]["command"])
        self.assertEqual("propose", job["completion_check"]["stage"])
        for workflow in ("propose", "apply", "archive"):
            body = (PLUGIN_ROOT / f"workflows/workspace/{workflow}.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("reference/workspace-repo-dispatch.md", body)
        for required in (
            "reference/subagent-runtime-protocol.md",
            "resources.subagent_protocol",
            "spawn_job",
            "wait_job",
            'subagent_type="general-purpose"',
            "action=load_workflow",
            "expected_workflow",
            "ownership_root",
            "unavailable_fallback",
            "原生终态",
        ):
            self.assertIn(required, dispatch)
        self.assertNotIn("spawn_agent", dispatch)
        claude_adapter = (PLUGIN_ROOT / "hosts/claude-code/subagent.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('subagent_type="general-purpose"', claude_adapter)
        codex_lite_loop = (
            PLUGIN_ROOT / "hosts/codex/skills/propose/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("--resource subagent_protocol", codex_lite_loop)

    def test_apply_jobs_allow_the_required_independent_reviewers(self) -> None:
        self._write_overview()
        for repo in ("api", "web"):
            self._write_repo_feature(repo)

        plan = workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "apply"
        )

        self.assertEqual(2, len(plan["jobs"]))
        for job in plan["jobs"]:
            self.assertEqual("may_delegate", job["delegation_policy"])
            self.assertEqual("archive-preflight", job["completion_check"]["command"])
            self.assertEqual([job["ownership_root"]], job["write_scope"])

    def test_repo_job_ids_are_codex_safe_and_collision_resistant(self) -> None:
        self._write_overview()
        (self.workspace / "helixharness.workspace.yaml").write_text(
            """version: 1
repos:
  SDK.v2:
    path: ./api
  sdk_v2:
    path: ./web
""",
            encoding="utf-8",
        )
        spec_path = self.workspace / "docs/specs/active/cross-repo/spec.md"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8")
            .replace("| api | write |", "| SDK.v2 | write |")
            .replace("| web | write |", "| sdk_v2 | write |")
            .replace("| api | C-01 |", "| SDK.v2 | C-01 |")
            .replace("| api, web |", "| SDK.v2, sdk_v2 |")
            .replace("### api", "### SDK.v2")
            .replace("### web", "### sdk_v2"),
            encoding="utf-8",
        )

        plan = workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "propose"
        )
        job_ids = [job["job_id"] for job in plan["jobs"]]

        self.assertEqual(2, len(set(job_ids)))
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_]+", item) for item in job_ids))
        self.assertTrue(all(len(item) <= 64 for item in job_ids))

    def test_propose_plan_quotes_a_feature_name_with_spaces_for_the_child_router(self) -> None:
        self._write_overview()
        old = self.workspace / "docs/specs/active/cross-repo"
        new = self.workspace / "docs/specs/active/cross repo"
        old.rename(new)
        spec_path = new / "spec.md"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace(
                "feature: cross-repo", "feature: cross repo", 1
            ),
            encoding="utf-8",
        )

        plan = workspace.workspace_execution_plan(
            self.workspace, "cross repo", "propose"
        )
        arguments = plan["jobs"][0]["command"].removeprefix(
            "/helix:propose "
        )
        code, route = workflow_router.route(
            self.workspace / "api",
            command="propose",
            arguments=arguments,
            arguments_mode="auto",
        )

        self.assertIn("'cross repo'", plan["jobs"][0]["command"])
        self.assertEqual(0, code)
        self.assertEqual("cross repo", route["feature"])
        self.assertEqual("workflows/single/propose.md", route["workflow"])

    def test_apply_read_scope_includes_only_declared_dependencies_without_write_access(self) -> None:
        self._write_overview()
        for repo in ("api", "web"):
            self._write_repo_feature(repo)
        jobs = {job["repo"]: job for job in workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "apply"
        )["jobs"]}
        api, web = str((self.workspace / "api").resolve()), str((self.workspace / "web").resolve())
        self.assertIn(api, jobs["web"]["read_scope"])
        self.assertNotIn(web, jobs["api"]["read_scope"])
        self.assertEqual([web], jobs["web"]["write_scope"])
        self.assertIn(api, jobs["web"]["forbidden_scope"])
        self.assertNotIn(str(self.workspace.resolve()), jobs["web"]["read_scope"])

    def test_propose_read_scope_does_not_grant_dependency_implementation_access(self) -> None:
        self._write_overview(status="planning")
        jobs = {job["repo"]: job for job in workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "propose"
        )["jobs"]}
        self.assertNotIn(str((self.workspace / "api").resolve()), jobs["web"]["read_scope"])
        self.assertNotIn(str((self.workspace / "web").resolve()), jobs["api"]["read_scope"])

    def test_archive_read_scope_preserves_dependency_validation_without_write_access(self) -> None:
        self._write_overview(status="implemented")
        for repo in ("api", "web"):
            self._make_repo_archive_ready(repo)
        jobs = {job["repo"]: job for job in workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "archive"
        )["jobs"]}
        api, web = str((self.workspace / "api").resolve()), str((self.workspace / "web").resolve())
        self.assertIn(api, jobs["web"]["read_scope"])
        self.assertNotIn(web, jobs["api"]["read_scope"])
        self.assertEqual([web], jobs["web"]["write_scope"])
        self.assertIn(api, jobs["web"]["forbidden_scope"])

    def test_apply_plan_dispatches_complete_single_lite_in_each_repo(self) -> None:
        self._write_overview()
        self._write_repo_feature("api")
        self._write_repo_feature("web")

        result = workspace.workspace_execution_plan(
            self.workspace, "cross-repo", "apply"
        )

        self.assertEqual([["api"], ["web"]], result["batches"])
        self.assertEqual(
            [
                (
                    "api",
                    "/helix:apply cross-repo",
                    [],
                    "workflows/single/apply.md",
                    {"command": "archive-preflight"},
                ),
                (
                    "web",
                    "/helix:apply cross-repo",
                    ["api"],
                    "workflows/single/apply.md",
                    {"command": "archive-preflight"},
                ),
            ],
            [
                (
                    job["repo"],
                    job["command"],
                    job["depends_on"],
                    job["expected_workflow"],
                    job["completion_check"],
                )
                for job in result["jobs"]
            ],
        )
        self.assertEqual({"api", "web"}, set(result["repo_results"]))

    def test_apply_plan_rejects_repo_spec_from_an_old_workspace_contract(self) -> None:
        self._write_overview()
        self._write_repo_feature("api")
        self._write_repo_feature("web")
        api_spec = self.workspace / "api/docs/specs/active/cross-repo/spec.md"
        api_spec.write_text(
            api_spec.read_text(encoding="utf-8").replace(
                "workspace_contract_sha256: ",
                "workspace_contract_sha256: " + ("0" * 64) + " # replaced ",
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            workspace.WorkspaceError, "workspace contract receipt"
        ):
            workspace.workspace_execution_plan(
                self.workspace, "cross-repo", "apply"
            )

    def test_archive_preflight_uses_each_repo_single_lite_receipt_without_root_evidence(self) -> None:
        self._write_overview(status="implemented")
        self._make_repo_archive_ready("api")
        self._make_repo_archive_ready("web")

        result = workspace.workspace_archive_preflight(
            self.workspace, "cross-repo"
        )
        code, route = workflow_router.route(
            self.workspace, command="archive", arguments="cross-repo"
        )

        self.assertEqual(["api", "web"], result["repos"])
        self.assertEqual([["web"], ["api"]], result["batches"])
        self.assertEqual({"api", "web"}, set(result["repo_results"]))
        self.assertEqual(0, code)
        self.assertEqual("workspace", route["execution_scope"])
        self.assertEqual("workflows/workspace/archive.md", route["workflow"])

    def test_workspace_dispatches_review_choice_and_preserves_strict_apply_gate(self) -> None:
        self._write_overview(status="implemented")
        self._make_repo_archive_ready("api")
        self._make_repo_archive_ready("web")
        (self.workspace / "api/after-review.txt").write_text("new content\n")
        with self.assertRaisesRegex(workspace.WorkspaceError, "review scope drift"):
            workspace.workspace_archive_preflight(self.workspace, "cross-repo")
        args = workspace.parser().parse_args([
            "--workspace-root", str(self.workspace), "workspace-archive-preflight",
            "--feature", "cross-repo", "--review-choice",
        ])
        result = args.handler(self.workspace, args)
        self.assertFalse(result["ready"])
        self.assertEqual(["api"], result["pending_review_repos"])
        self.assertEqual("review_scope_changed", result["decision_needed"])
        self.assertTrue(result["repo_results"]["web"]["ready"])
        code, routed = workflow_router.route(self.workspace, command="propose", arguments="cross-repo --loop")
        self.assertEqual(0, code, routed)
        self.assertEqual("workflows/workspace/archive.md", routed["workflow"])
        args = workspace.parser().parse_args([
            "--workspace-root", str(self.workspace), "workspace-plan", "--feature", "cross-repo",
            "--command", "archive", "--review-choice", "--no-commit",
        ])
        plan = args.handler(self.workspace, args)
        self.assertEqual([["web"], ["api"]], plan["batches"])
        self.assertTrue(all(job["arguments"] == "cross-repo --no-commit" for job in plan["jobs"]))
        self.assertFalse((self.workspace / "api/.helixharness/workflow/cross-repo/archive-review-acceptance.json").exists())
        choice = result["repo_results"]["api"]
        accept = workflow.parser().parse_args([
            "--repo-root", str(self.workspace / "api"), "accept-review-drift", "--feature", "cross-repo",
            "--expected-scope-sha256", choice["current_scope_sha256"], "--user-confirmed",
        ])
        accept.handler(self.workspace / "api", accept)
        accepted = workspace.workspace_archive_preflight(self.workspace, "cross-repo", allow_review_choice=True)
        self.assertTrue(accepted["ready"])
        self.assertTrue(accepted["repo_results"]["api"]["review_accepted_changes"])
        with self.assertRaisesRegex(workspace.WorkspaceError, "review scope drift"):
            workspace.workspace_archive_preflight(self.workspace, "cross-repo")
        evidence = self.workspace / "web/docs/specs/active/cross-repo/verify-report.md"
        evidence.write_text(evidence.read_text().replace("standards_verdict: APPROVE", "standards_verdict: REJECT"))
        with self.assertRaisesRegex(workspace.WorkspaceError, "invalid standards_verdict"):
            workspace.workspace_archive_preflight(self.workspace, "cross-repo", allow_review_choice=True)

    def test_workspace_review_choice_cannot_relax_finalizing_drift(self) -> None:
        self._write_overview(status="implemented")
        self._make_repo_archive_ready("api")
        self._make_repo_archive_ready("web")
        api = self.workspace / "api"
        args = workflow.parser().parse_args([
            "--repo-root", str(api), "authorize-knowledge", "--feature", "cross-repo", "--paths-json", "[]",
        ])
        args.handler(api, args)
        spec = api / "docs/specs/active/cross-repo/spec.md"
        spec.write_text(spec.read_text().replace("status: implemented", "status: completed"))
        evidence = spec.with_name("verify-report.md")
        evidence.write_text(evidence.read_text().replace("archive: pending", "archive: completed"))
        (api / "late-change.txt").write_text("changed during finalization\n")
        with self.assertRaisesRegex(workspace.WorkspaceError, "review scope drift"):
            workspace.workspace_archive_preflight(self.workspace, "cross-repo", allow_review_choice=True)

    def test_archive_commit_option_survives_plan_and_each_child_router(self) -> None:
        self._write_overview(status="implemented")
        self._make_repo_archive_ready("api")
        self._make_repo_archive_ready("web")
        for flag in ("--no-commit", "--commit"):
            with self.subTest(flag=flag):
                args = workspace.parser().parse_args([
                    "--workspace-root", str(self.workspace), "workspace-plan",
                    "--feature", "cross-repo", "--command", "archive", flag,
                ])
                plan = args.handler(self.workspace, args)
                self.assertEqual([["web"], ["api"]], plan["batches"])
                for job in plan["jobs"]:
                    self.assertEqual(job["command"], f"/helix:archive cross-repo {flag}")
                    code, result = workflow_router.route(
                        Path(job["cwd"]), command="archive", arguments=job["arguments"]
                    )
                    self.assertEqual(code, 0, result)
                    self.assertEqual(result["workflow_arguments"], f"cross-repo {flag}")
                    self.assertEqual(result["workflow"], "workflows/single/archive.md")

    def test_workspace_commit_options_are_archive_only(self) -> None:
        for command in ("propose", "apply"):
            with self.assertRaisesRegex(workspace.WorkspaceError, "commit options require archive"):
                workspace.workspace_execution_plan(
                    self.workspace, "cross-repo", command, commit_option="--no-commit"
                )
        with self.assertRaisesRegex(workspace.WorkspaceError, "commit option is invalid"):
            workspace.workspace_execution_plan(
                self.workspace, "cross-repo", "archive", commit_option="--push"
            )

    def test_archive_recovery_rejects_completed_repo_from_an_old_workspace_contract(self) -> None:
        self._write_overview(status="implemented")
        self._move_repo_to_completed("api")
        self._move_repo_to_completed("web", stale=True)

        with self.assertRaisesRegex(
            workspace.WorkspaceError, "workspace contract receipt is stale"
        ):
            workspace.workspace_archive_preflight(
                self.workspace, "cross-repo"
            )


if __name__ == "__main__":
    unittest.main()
