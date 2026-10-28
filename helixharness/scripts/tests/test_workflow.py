#!/usr/bin/env python3
"""Public CLI behavior tests for the minimal Lite workflow guards."""

from __future__ import annotations
import os

import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from unittest import mock

import atomic_files
import workflow
import workflow_router


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = PLUGIN_ROOT / "scripts" / "workflow.py"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class LiteWorkflowCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test User"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "--allow-empty", "-qm", "initial"],
            check=True,
        )
        self.feature = "checkout"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_retired_qa_artifacts_are_reviewed_like_other_files(self) -> None:
        for state in ("active", "completed"):
            prefix = f"docs/specs/{state}/{self.feature}/"
            for artifact in ("spec.md", "tasks.md", "verify-report.md"):
                self.assertTrue(workflow._review_scope_excluded(self.feature, prefix + artifact))
            for artifact in ("qa-pipeline-case.yaml", "qa-case.draft.json",
                             "qa-automation.final.json", "qa-case-review.final.md",
                             "qa-case.final.incremental.xmind", "notes.md"):
                self.assertFalse(workflow._review_scope_excluded(self.feature, prefix + artifact))

    @property
    def feature_dir(self) -> Path:
        return self.repo / "docs/specs/active" / self.feature

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo-root",
                str(self.repo),
                *args,
            ],
            check=check,
            capture_output=True,
            text=True,
        )

    def write_spec(
        self,
        status: str = "applying",
        body: str = "# Checkout\n\nDo it.\n",
        *,
        trigger: str | None = None,
        bug_signature: str | None = None,
        testing_seam_confirmed: bool = True,
    ) -> None:
        identity = ""
        if trigger is not None:
            identity += f"trigger: {trigger}\n"
        if bug_signature is not None:
            identity += f"bug_signature: {bug_signature}\n"
        if testing_seam_confirmed:
            identity += "testing_seam_confirmed: true\n"
        write(
            self.feature_dir / "spec.md",
            f"""---
feature: {self.feature}
status: {status}
{identity}---
{body}""",
        )

    def write_tasks(
        self,
        *,
        status: str = "applying",
        rows: str | None = None,
        compact: bool = False,
    ) -> None:
        if compact:
            table = """| status | Task | ID |
|---|---|---|
| completed | First | T-01 |
| pending | Second | T-02 |
"""
        else:
            task_rows = rows or (
                "| T-01 | First slice | AC-01 | — | completed |\n"
                "| T-02 | Second slice | AC-02 | T-01 | pending |"
            )
            table = f"""| ID | Task | Covers | Blocked By | Status |
|---|---|---|---|---|
{task_rows}
"""
        write(
            self.feature_dir / "tasks.md",
            f"""---
feature: {self.feature}
status: {status}
---
# Tasks

{table}""",
        )
        if status in {"applying", "completed"} and (self.feature_dir / "spec.md").is_file():
            self.write_apply_state()

    def write_apply_state(self) -> None:
        spec_text = (self.feature_dir / "spec.md").read_text(encoding="utf-8")
        baseline: list[dict[str, object]] = []
        baseline_json = json.dumps(
            baseline, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        write(
            self.repo
            / ".helixharness/workflow"
            / self.feature
            / "apply-state.json",
            json.dumps(
                {
                    "apply_baseline_head": head,
                    "apply_baseline_manifest": baseline,
                    "apply_baseline_sha256": workflow.artifact_receipt(
                        baseline_json
                    ),
                    "apply_spec_receipt_sha256": workflow.spec_receipt(
                        spec_text
                    ),
                    "feature": self.feature,
                    "schema_version": 1,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    def write_evidence(
        self,
        *,
        standards: str = "APPROVE",
        spec: str = "APPROVE",
        body: str = "Free-form evidence written by the Agent.\n",
        trigger: str | None = None,
        bug_signature: str | None = None,
        must_fix_rounds: int = 0,
    ) -> None:
        receipts = json.loads(self.validate("archive").stdout)
        scope = json.loads(
            self.run_cli("review-scope", "--feature", self.feature).stdout
        )
        identity = ""
        if trigger is not None:
            identity += f"trigger: {trigger}\n"
        if bug_signature is not None:
            identity += f"bug_signature: {bug_signature}\n"
        write(
            self.feature_dir / "verify-report.md",
            f"""---
feature: {self.feature}
spec_receipt_sha256: {receipts['spec_receipt_sha256']}
tasks_receipt_sha256: {receipts['tasks_receipt_sha256']}
review_fixed_point: {scope['review_head']}
review_head: {scope['review_head']}
review_scope_sha256: {scope['review_scope_sha256']}
review_mode: parallel_subagents
standards_verdict: {standards}
spec_verdict: {spec}
must_fix_rounds: {must_fix_rounds}
{identity}stage_trace:
  propose: completed
  apply: completed
  archive: pending
---
{body}""",
        )

    def validate(self, stage: str = "apply", *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "validate-tasks",
            "--feature",
            self.feature,
            "--stage",
            stage,
            check=check,
        )

    def test_archive_git_state_resolves_main_and_detached_head(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "switch", "-qc", "feature/work"], check=True)

        branch_state = json.loads(self.run_cli("archive-git-state").stdout)
        subprocess.run(["git", "-C", str(self.repo), "switch", "--detach", "-q"], check=True)
        detached_state = json.loads(self.run_cli("archive-git-state").stdout)

        self.assertEqual("main", branch_state["target_branch"])
        self.assertEqual("feature/work", branch_state["source_branch"])
        self.assertIsNotNone(branch_state["merge_base_commit"])
        self.assertIsNone(detached_state["source_branch"])

    def test_archive_git_state_prefers_origin_head_and_reports_operations(self) -> None:
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/trunk", head],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/trunk",
            ],
            check=True,
        )
        git_dir = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--git-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = self.repo / git_dir_path
        (git_dir_path / "CHERRY_PICK_HEAD").write_text(head + "\n", encoding="ascii")

        state = json.loads(self.run_cli("archive-git-state").stdout)

        self.assertEqual("trunk", state["target_branch"])
        self.assertIn("cherry_pick", state["operations"])

    def test_archive_git_state_blocks_ambiguous_main_and_master_fallback(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "master"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "branch", "main"], check=True)

        state = json.loads(self.run_cli("archive-git-state").stdout)

        self.assertIsNone(state["target_branch"])
        self.assertIsNone(state["target_commit"])

    def test_archive_git_state_prefers_remote_tracking_fallback_commit(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "main"], check=True)
        remote_commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", remote_commit],
            check=True,
        )
        (self.repo / "local-only.txt").write_text("local\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "local-only.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "local advance"], check=True)

        state = json.loads(self.run_cli("archive-git-state").stdout)

        self.assertEqual("main", state["target_branch"])
        self.assertEqual(remote_commit, state["target_commit"])

    def test_atomic_writer_does_not_require_os_fchmod(self) -> None:
        target = self.repo / "atomic.txt"

        with mock.patch.object(
            atomic_files.os,
            "fchmod",
            create=True,
            side_effect=AttributeError("unavailable on this Python/Windows pair"),
        ):
            atomic_files.atomic_write_text(target, "portable\n")

        self.assertEqual("portable\n", target.read_text(encoding="utf-8"))

    def test_atomic_create_does_not_replace_an_existing_file(self) -> None:
        target = self.repo / "atomic-create.txt"

        self.assertTrue(atomic_files.atomic_create_text(target, "first\n"))
        self.assertFalse(atomic_files.atomic_create_text(target, "second\n"))

        self.assertEqual("first\n", target.read_text(encoding="utf-8"))

    def test_validate_tasks_returns_first_resumable_task(self) -> None:
        self.write_spec()
        self.write_tasks()

        result = json.loads(self.validate().stdout)

        self.assertEqual("T-02", result["next_task"])
        self.assertEqual(2, result["task_count"])
        self.assertNotIn("spec_contract_sha256", result)
        self.assertNotIn("tasks_sha256", result)

    def test_optional_ac_and_dependency_columns_do_not_block_execution(self) -> None:
        self.write_spec(body="# 任意标题\n\n不要求 AC 编号。\n")
        self.write_tasks(compact=True)

        result = json.loads(self.validate().stdout)

        self.assertEqual("T-02", result["next_task"])

    def test_task_description_is_the_only_required_business_text(self) -> None:
        self.write_spec()
        self.write_tasks(
            rows="| T-01 |  | AC-01 | — | pending |",
        )

        result = self.validate(check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("empty task description", result.stderr)

    def test_multiple_in_progress_tasks_are_reported_without_blocking(self) -> None:
        self.write_spec()
        self.write_tasks(
            rows=(
                "| T-01 | First | AC-01 | — | in_progress |\n"
                "| T-02 | Second | AC-02 | — | in_progress |"
            )
        )

        result = json.loads(self.validate().stdout)

        self.assertEqual("T-01", result["next_task"])
        self.assertEqual(["T-01", "T-02"], result["in_progress_tasks"])

    def test_unicode_cross_platform_safe_feature_is_supported(self) -> None:
        self.feature = "结算-优化"
        self.write_spec()
        self.write_tasks(compact=True)

        result = json.loads(self.validate().stdout)

        self.assertEqual(self.feature, result["feature"])

    def test_frontmatter_boundaries_and_routing_fields_remain_hard(self) -> None:
        self.write_spec()
        self.write_tasks(compact=True)
        path = self.feature_dir / "spec.md"

        path.write_text("feature: checkout\nstatus: applying\n", encoding="utf-8")
        missing = self.validate(check=False)
        self.assertEqual(2, missing.returncode)
        self.assertIn("frontmatter is missing", missing.stderr)

        path.write_text(
            "---\nfeature: checkout\nstatus: applying\nstatus: ready\n"
            "---\n# Spec\n",
            encoding="utf-8",
        )
        duplicate = self.validate(check=False)
        self.assertEqual(2, duplicate.returncode)
        self.assertIn("duplicate status", duplicate.stderr)

    def test_selection_receipt_cannot_be_used_as_a_formal_spec(self) -> None:
        self.write_spec(status="ready")
        spec = self.feature_dir / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "testing_seam_confirmed: true\n",
                "selection_state: variant_selected\n"
                f"requirement_sha256: {'a' * 64}\n",
            ),
            encoding="utf-8",
        )
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")

        result = self.validate("propose", check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("selection receipt", result.stderr)

    def test_ready_spec_requires_a_confirmed_testing_seam(self) -> None:
        self.write_spec(status="ready", testing_seam_confirmed=False)
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")

        result = self.validate("propose", check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("testing_seam_confirmed", result.stderr)

    def test_apply_rejects_spec_drift_from_its_bound_receipt(self) -> None:
        self.write_spec(status="applying")
        self.write_tasks(status="applying")
        spec = self.feature_dir / "spec.md"
        spec.write_text(spec.read_text(encoding="utf-8") + "\nChanged contract.\n", encoding="utf-8")

        result = self.validate("apply", check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("apply_spec_receipt_sha256", result.stderr)

    def test_apply_can_recover_completed_tasks_before_valid_evidence_exists(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )

        result = json.loads(self.validate("apply").stdout)

        self.assertIsNone(result["next_task"])
        self.assertEqual("apply", result["stage"])

    def prepare_contract_repair(self, source_status: str = "implemented") -> tuple[Path, Path]:
        self.write_spec(status=source_status)
        self.write_tasks(
            status={"implemented": "completed", "applying": "applying"}.get(source_status, "ready"),
            rows="| T-01 | First | AC-01 | — | "
                 + ("completed" if source_status in {"implemented", "applying"} else "pending")
                 + " |",
        )
        spec_path = self.feature_dir / "spec.md"
        tasks_path = self.feature_dir / "tasks.md"
        self.run_cli(
            "begin-repair", "--feature", self.feature,
            "--repair-key", "a" * 64, "--source-sha256", "b" * 64,
            "--repair-kind", "contract_amendment",
            "--expected-spec-receipt", workflow.spec_receipt(spec_path.read_text()),
            "--expected-tasks-receipt", workflow.artifact_receipt(tasks_path.read_text()),
        )
        spec_path.write_text(
            spec_path.read_text().replace("---\n# Checkout", "repair_revision: 1\n---\n# Amended checkout")
            + "\n## Repair Revisions\n\n"
            + "| Revision | Repair Key | Kind | Source SHA-256 | Summary |\n"
            + "|---|---|---|---|---|\n"
            + f"| R-01 | {'a' * 64} | contract_amendment | {'b' * 64} | Change contract |\n"
        )
        tasks_path.write_text(
            tasks_path.read_text().replace("| ID | Task |", "| ID | Revision | Task |")
            .replace("|---|---|---|---|---|", "|---|---|---|---|---|---|")
            .replace("| T-01 | First |", "| T-01 | R-00 | First |")
            + "| T-02 | R-01 | Amend behavior | AC-01 | T-01 | pending |\n"
        )
        return spec_path, tasks_path

    def validate_repair(self, **kwargs):
        return self.run_cli(
            "validate-tasks", "--feature", self.feature, "--stage", "propose",
            "--repair-key", "a" * 64, **kwargs,
        )

    def test_contract_repair_propose_rebind_apply_preserves_history(self) -> None:
        spec_path, tasks_path = self.prepare_contract_repair()
        state_dir = self.repo / ".helixharness/workflow" / self.feature
        apply_before = json.loads((state_dir / "apply-state.json").read_text())
        before = {p: p.read_bytes() for p in (spec_path, tasks_path, *state_dir.glob("*.json"))}
        result = json.loads(self.validate_repair().stdout)
        self.assertEqual("T-02", result["next_task"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.run_cli(
            "repair-rebind", "--feature", self.feature, "--repair-key", "a" * 64,
            "--expected-spec-receipt", result["spec_receipt_sha256"],
            "--expected-tasks-receipt", result["tasks_receipt_sha256"],
        )
        validated = json.loads(self.run_cli(
            "validate-tasks", "--feature", self.feature, "--stage", "apply"
        ).stdout)
        self.assertEqual("T-02", validated["next_task"])
        self.assertEqual(["completed", "pending"], [r["status"] for r in workflow.task_rows(tasks_path.read_text())])
        apply_after = json.loads((state_dir / "apply-state.json").read_text())
        for key in ("apply_baseline_head", "apply_baseline_manifest", "apply_baseline_sha256"):
            self.assertEqual(apply_before[key], apply_after[key])
        self.assertEqual(validated["spec_receipt_sha256"], apply_after["apply_spec_receipt_sha256"])
        self.assertIn("in-progress repair", self.validate_repair(check=False).stderr)

    def test_contract_repair_resumes_reopened_propose_states(self) -> None:
        spec_path, tasks_path = self.prepare_contract_repair()
        original_spec = spec_path.read_text()
        original_tasks = tasks_path.read_text()
        for spec_status in ("planning", "ready"):
            for tasks_status in ("ready", "applying", "completed"):
                with self.subTest(spec=spec_status, tasks=tasks_status):
                    spec_path.write_text(original_spec.replace("status: implemented", f"status: {spec_status}"))
                    tasks_path.write_text(original_tasks.replace("status: completed", f"status: {tasks_status}"))
                    self.validate_repair()
        result = json.loads(self.validate_repair().stdout)
        rebound = json.loads(self.run_cli(
            "repair-rebind", "--feature", self.feature, "--repair-key", "a" * 64,
            "--expected-spec-receipt", result["spec_receipt_sha256"],
            "--expected-tasks-receipt", result["tasks_receipt_sha256"],
        ).stdout)
        self.assertEqual("applying", rebound["status"])

    def test_contract_repair_validates_pending_planning_history(self) -> None:
        self.prepare_contract_repair("planning")
        self.assertEqual("T-01", json.loads(self.validate_repair().stdout)["next_task"])

    def test_contract_repair_from_applying_keeps_completed_tasks(self) -> None:
        self.prepare_contract_repair("applying")
        self.assertEqual("T-02", json.loads(self.validate_repair().stdout)["next_task"])

    def test_contract_repair_rejects_history_and_new_task_corruption(self) -> None:
        spec_path, tasks_path = self.prepare_contract_repair()
        original = tasks_path.read_text()
        for old, new, error in (
            ("First |", "Rewritten |", "task from before"),
            ("— | completed", "— | pending", "task from before"),
            ("| T-01 | R-00", "| T-03 | R-00", "task from before"),
            ("| T-01 | pending", "| T-01 | completed", "invalid tasks for"),
            ("| T-02 | R-01", "| T-02 | R-02", "invalid tasks for"),
            ("| T-01 | pending", "| T-02 | pending", "itself"),
        ):
            with self.subTest(new=new):
                tasks_path.write_text(original.replace(old, new))
                result = self.validate_repair(check=False)
                self.assertEqual(2, result.returncode)
                self.assertIn(error, result.stderr)
        tasks_path.write_text(original)
        spec_path.write_text(spec_path.read_text().replace("a" * 64, "c" * 64))
        self.assertIn("does not match Repair state", self.validate_repair(check=False).stderr)

    def test_repair_key_cannot_bypass_ordinary_stage_guards(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(status="ready")
        result = self.run_cli("validate-tasks", "--feature", self.feature, "--stage", "propose", check=False)
        self.assertIn("only pending tasks", result.stderr)
        self.assertIn("in-progress repair", self.validate_repair(check=False).stderr)
        self.prepare_contract_repair()
        mismatch = self.run_cli("validate-tasks", "--feature", self.feature, "--stage", "propose", "--repair-key", "c" * 64, check=False)
        self.assertIn("in-progress repair", mismatch.stderr)
        wrong_stage = self.run_cli("validate-tasks", "--feature", self.feature, "--stage", "apply", "--repair-key", "a" * 64, check=False)
        self.assertIn("only valid for propose", wrong_stage.stderr)

    def test_repair_rebind_preserves_apply_baseline_and_appends_a_revision(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        apply_state_path = (
            self.repo / ".helixharness/workflow" / self.feature / "apply-state.json"
        )
        original_state = json.loads(apply_state_path.read_text(encoding="utf-8"))
        spec_text = (self.feature_dir / "spec.md").read_text(encoding="utf-8")
        tasks_text = (self.feature_dir / "tasks.md").read_text(encoding="utf-8")
        repair_key = "a" * 64
        source_sha = "b" * 64

        preflight = json.loads(
            self.run_cli("repair-preflight", "--feature", self.feature).stdout
        )
        begun = json.loads(
            self.run_cli(
                "begin-repair",
                "--feature",
                self.feature,
                "--repair-key",
                repair_key,
                "--source-sha256",
                source_sha,
                "--repair-kind",
                "implementation_regression",
                "--expected-spec-receipt",
                workflow.spec_receipt(spec_text),
                "--expected-tasks-receipt",
                workflow.artifact_receipt(tasks_text),
            ).stdout
        )

        self.assertEqual("implemented", preflight["status"])
        self.assertEqual(1, begun["repair_revision"])

        spec_path = self.feature_dir / "spec.md"
        spec_path.write_text(
            spec_text.replace(
                "status: implemented\n", "status: implemented\nrepair_revision: 1\n"
            )
            + "\n## Repair Revisions\n\n"
            + "| Revision | Repair Key | Kind | Source SHA-256 | Summary |\n"
            + "|---|---|---|---|---|\n"
            + f"| R-01 | {repair_key} | implementation_regression | "
            + f"{source_sha} | Rejects valid cards |\n",
            encoding="utf-8",
        )
        tasks_path = self.feature_dir / "tasks.md"
        tasks_path.write_text(
            tasks_text.replace(
                "| ID | Task | Covers | Blocked By | Status |",
                "| ID | Revision | Task | Covers | Blocked By | Status |",
            )
            .replace("|---|---|---|---|---|", "|---|---|---|---|---|---|")
            .replace(
                "| T-01 | First | AC-01 | — | completed |",
                "| T-01 | R-00 | First | AC-01 | — | completed |\n"
                "| T-02 | R-01 | Repair valid card checkout | AC-01 | T-01 | pending |",
            ),
            encoding="utf-8",
        )

        valid_tasks_text = tasks_path.read_text(encoding="utf-8")
        tasks_path.write_text(
            valid_tasks_text.replace("First | AC-01", "Changed first | AC-01"),
            encoding="utf-8",
        )
        mutated = self.run_cli(
            "repair-rebind",
            "--feature",
            self.feature,
            "--repair-key",
            repair_key,
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            check=False,
        )
        self.assertEqual(2, mutated.returncode)
        self.assertIn("task from before this repair", mutated.stderr)
        tasks_path.write_text(valid_tasks_text, encoding="utf-8")

        tasks_path.write_text(
            valid_tasks_text.replace("| T-01 | R-00 |", "| T-01 | R-99 |"),
            encoding="utf-8",
        )
        revision_drift = self.run_cli(
            "repair-rebind",
            "--feature",
            self.feature,
            "--repair-key",
            repair_key,
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            check=False,
        )
        self.assertEqual(2, revision_drift.returncode)
        self.assertIn("task from before this repair", revision_drift.stderr)
        tasks_path.write_text(valid_tasks_text, encoding="utf-8")

        tasks_path.write_text(
            valid_tasks_text
            + "| T-02 | R-01 | Duplicate repair task | AC-01 | T-01 | pending |\n",
            encoding="utf-8",
        )
        duplicate = self.run_cli(
            "repair-rebind",
            "--feature",
            self.feature,
            "--repair-key",
            repair_key,
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            check=False,
        )
        self.assertEqual(2, duplicate.returncode)
        self.assertIn("duplicate task id", duplicate.stderr)
        tasks_path.write_text(valid_tasks_text, encoding="utf-8")

        valid_spec_text = spec_path.read_text(encoding="utf-8")
        spec_path.write_text(
            valid_spec_text.replace("Do it.", "Do something different."),
            encoding="utf-8",
        )
        contract_drift = self.run_cli(
            "repair-rebind",
            "--feature",
            self.feature,
            "--repair-key",
            repair_key,
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            check=False,
        )
        self.assertEqual(2, contract_drift.returncode)
        self.assertIn("original contract", contract_drift.stderr)
        spec_path.write_text(valid_spec_text, encoding="utf-8")

        rebound = json.loads(
            self.run_cli(
                "repair-rebind",
                "--feature",
                self.feature,
                "--repair-key",
                repair_key,
                "--expected-spec-receipt",
                workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
                "--expected-tasks-receipt",
                workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            ).stdout
        )
        updated_state = json.loads(apply_state_path.read_text(encoding="utf-8"))
        validated = json.loads(self.validate("apply").stdout)

        self.assertEqual("applying", rebound["status"])
        self.assertEqual("T-02", validated["next_task"])
        self.assertEqual(
            original_state["apply_baseline_head"],
            updated_state["apply_baseline_head"],
        )
        self.assertEqual(
            original_state["apply_baseline_sha256"],
            updated_state["apply_baseline_sha256"],
        )
        self.assertIn("status: applying", spec_path.read_text(encoding="utf-8"))
        self.assertIn("status: applying", tasks_path.read_text(encoding="utf-8"))

        competing = self.run_cli(
            "begin-repair",
            "--feature",
            self.feature,
            "--repair-key",
            "e" * 64,
            "--source-sha256",
            "f" * 64,
            "--repair-kind",
            "implementation_regression",
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            check=False,
        )
        self.assertEqual(2, competing.returncode)
        self.assertIn("already in progress", competing.stderr)

    def test_repair_rebind_rejects_markdown_wrapped_sha_with_actionable_error(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        spec_path = self.feature_dir / "spec.md"
        tasks_path = self.feature_dir / "tasks.md"
        spec_text = spec_path.read_text(encoding="utf-8")
        tasks_text = tasks_path.read_text(encoding="utf-8")
        repair_key = "a" * 64
        source_sha = "b" * 64

        self.run_cli(
            "begin-repair",
            "--feature",
            self.feature,
            "--repair-key",
            repair_key,
            "--source-sha256",
            source_sha,
            "--repair-kind",
            "implementation_regression",
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_text),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_text),
        )

        spec_path.write_text(
            spec_text.replace(
                "status: implemented\n", "status: implemented\nrepair_revision: 1\n"
            )
            + "\n## Repair Revisions\n\n"
            + "| Revision | Repair Key | Kind | Source SHA-256 | Summary |\n"
            + "|---|---|---|---|---|\n"
            + f"| R-01 | `{repair_key}` | implementation_regression | "
            + f"`{source_sha}` | Rejects valid cards |\n",
            encoding="utf-8",
        )
        tasks_path.write_text(
            tasks_text.replace(
                "| ID | Task | Covers | Blocked By | Status |",
                "| ID | Revision | Task | Covers | Blocked By | Status |",
            )
            .replace("|---|---|---|---|---|", "|---|---|---|---|---|---|")
            .replace(
                "| T-01 | First | AC-01 | — | completed |",
                "| T-01 | R-00 | First | AC-01 | — | completed |\n"
                "| T-02 | R-01 | Repair valid card checkout | AC-01 | T-01 | pending |",
            ),
            encoding="utf-8",
        )

        wrapped_repair_key = self.run_cli(
            "repair-rebind",
            "--feature",
            self.feature,
            "--repair-key",
            repair_key,
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            check=False,
        )
        self.assertEqual(2, wrapped_repair_key.returncode)
        self.assertIn(
            "spec.md Repair Key must be a bare 64-character lowercase SHA-256 "
            "without Markdown formatting",
            wrapped_repair_key.stderr,
        )

        repair_state = json.loads(
            self.run_cli("repair-preflight", "--feature", self.feature).stdout
        )["repair_state"]
        self.assertEqual("in_progress", repair_state["status"])
        self.assertEqual(repair_key, repair_state["repair_key"])

        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace(
                f"`{repair_key}`", repair_key
            ),
            encoding="utf-8",
        )
        wrapped_source_sha = self.run_cli(
            "repair-rebind",
            "--feature",
            self.feature,
            "--repair-key",
            repair_key,
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            check=False,
        )
        self.assertEqual(2, wrapped_source_sha.returncode)
        self.assertIn(
            "spec.md Source SHA-256 must be a bare 64-character lowercase SHA-256 "
            "without Markdown formatting",
            wrapped_source_sha.stderr,
        )

    def test_planning_repair_rebinds_to_ready_without_an_apply_baseline(self) -> None:
        self.write_spec(status="planning")
        spec_path = self.feature_dir / "spec.md"
        spec_text = spec_path.read_text(encoding="utf-8")
        repair_key = "c" * 64
        source_sha = "d" * 64

        preflight = json.loads(
            self.run_cli("repair-preflight", "--feature", self.feature).stdout
        )
        self.assertIsNone(preflight["repair_state"])
        code, routed = workflow_router.route(self.repo, command="bug-fix", arguments=self.feature)
        self.assertEqual((code, routed["action"]), (0, "repair_feature"))
        begun = json.loads(
            self.run_cli(
                "begin-repair",
                "--feature",
                self.feature,
                "--repair-key",
                repair_key,
                "--source-sha256",
                source_sha,
                "--repair-kind",
                "contract_amendment",
                "--expected-spec-receipt",
                preflight["spec_receipt_sha256"],
                "--expected-tasks-receipt",
                "absent",
            ).stdout
        )
        self.assertEqual(1, begun["repair_revision"])

        spec_path.write_text(
            spec_text.replace(
                "status: planning\n", "status: planning\nrepair_revision: 1\n"
            )
            + "\n## Repair Revisions\n\n"
            + "| Revision | Repair Key | Kind | Source SHA-256 | Summary |\n"
            + "|---|---|---|---|---|\n"
            + f"| R-01 | {repair_key} | contract_amendment | "
            + f"{source_sha} | Correct checkout contract |\n",
            encoding="utf-8",
        )
        self.write_tasks(
            status="planning",
            rows="| T-01 | R-01 | Implement corrected checkout contract | AC-01 | — | pending |",
        )
        tasks_path = self.feature_dir / "tasks.md"
        tasks_path.write_text(
            tasks_path.read_text(encoding="utf-8")
            .replace(
                "| ID | Task | Covers | Blocked By | Status |",
                "| ID | Revision | Task | Covers | Blocked By | Status |",
            )
            .replace("|---|---|---|---|---|", "|---|---|---|---|---|---|"),
            encoding="utf-8",
        )

        rebound = json.loads(
            self.run_cli(
                "repair-rebind",
                "--feature",
                self.feature,
                "--repair-key",
                repair_key,
                "--expected-spec-receipt",
                workflow.spec_receipt(spec_path.read_text(encoding="utf-8")),
                "--expected-tasks-receipt",
                workflow.artifact_receipt(tasks_path.read_text(encoding="utf-8")),
            ).stdout
        )

        self.assertEqual("ready", rebound["status"])
        code, routed = workflow_router.route(self.repo, command="bug-fix", arguments=self.feature)
        self.assertEqual((code, routed["action"]), (0, "repair_feature"))
        resume = json.loads(self.run_cli("repair-preflight", "--feature", self.feature).stdout)
        self.assertEqual(resume["repair_state"]["status"], "bound")
        self.assertEqual(resume["repair_state"]["repair_key"], repair_key)
        self.assertEqual(resume["repair_state"]["repair_revision"], 1)
        self.assertIn("status: ready", spec_path.read_text(encoding="utf-8"))
        self.assertIn("status: ready", tasks_path.read_text(encoding="utf-8"))
        self.assertFalse(
            (
                self.repo
                / ".helixharness/workflow"
                / self.feature
                / "apply-state.json"
            ).exists()
        )

    def test_repair_preflight_blocks_finalizing_feature(self) -> None:
        self.write_spec(status="completed")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )

        result = self.run_cli(
            "repair-preflight", "--feature", self.feature, check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("archive finalizing", result.stderr)

    def test_begin_repair_is_idempotent_for_the_same_repair_key(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        spec_text = (self.feature_dir / "spec.md").read_text(encoding="utf-8")
        tasks_text = (self.feature_dir / "tasks.md").read_text(encoding="utf-8")
        arguments = (
            "begin-repair",
            "--feature",
            self.feature,
            "--repair-key",
            "c" * 64,
            "--source-sha256",
            "d" * 64,
            "--repair-kind",
            "implementation_regression",
            "--expected-spec-receipt",
            workflow.spec_receipt(spec_text),
            "--expected-tasks-receipt",
            workflow.artifact_receipt(tasks_text),
        )

        first = json.loads(self.run_cli(*arguments).stdout)
        code, routed = workflow_router.route(self.repo, command="bug-fix", arguments=self.feature)
        self.assertEqual((code, routed["action"]), (0, "repair_feature"))
        self.assertEqual(routed["workflow_arguments"], self.feature)
        resumable = json.loads(
            self.run_cli("repair-preflight", "--feature", self.feature).stdout
        )
        second = json.loads(self.run_cli(*arguments).stdout)

        self.assertFalse(first["resumed"])
        self.assertEqual("c" * 64, resumable["repair_state"]["repair_key"])
        self.assertEqual(1, resumable["repair_state"]["repair_revision"])
        self.assertEqual("in_progress", resumable["repair_state"]["status"])
        self.assertTrue(second["resumed"])
        self.assertEqual(first["repair_revision"], second["repair_revision"])

    def test_repair_revision_is_a_critical_frontmatter_field(self) -> None:
        self.write_spec(status="applying")
        self.write_tasks(status="applying")
        spec = self.feature_dir / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "status: applying\n",
                "status: applying\nrepair_revision: 1\nrepair_revision: 2\n",
            ),
            encoding="utf-8",
        )
        result = self.validate("apply", check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("duplicate repair_revision", result.stderr)

    def test_bind_apply_persists_and_excludes_unchanged_preexisting_dirt(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")
        existing = self.repo / "user-notes.txt"
        existing.write_text("preexisting\n", encoding="utf-8")

        bound = json.loads(self.run_cli("bind-apply", "--feature", self.feature).stdout)
        unchanged_scope = workflow.review_scope(self.repo, self.feature)

        self.assertRegex(bound["apply_baseline_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("user-notes.txt", unchanged_scope["excluded_preexisting_paths"])
        self.assertNotIn("user-notes.txt", unchanged_scope["changed_paths"])
        tasks_text = (self.feature_dir / "tasks.md").read_text(encoding="utf-8")
        self.assertNotIn("apply_baseline_", tasks_text)
        self.assertNotIn("apply_spec_receipt_sha256", tasks_text)
        ignored = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "check-ignore",
                "-q",
                ".helixharness/workflow/checkout/apply-state.json",
            ],
            check=False,
        )
        self.assertEqual(0, ignored.returncode)

    def test_review_scope_only_contains_apply_changes_when_baseline_is_dirty(self) -> None:
        tracked_unstaged = self.repo / "tracked-unstaged.txt"
        tracked_staged = self.repo / "tracked-staged.txt"
        tracked_unstaged.write_text("committed\n", encoding="utf-8")
        tracked_staged.write_text("committed\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", tracked_unstaged.name, tracked_staged.name],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "tracked fixtures"],
            check=True,
        )
        tracked_unstaged.write_text("preexisting unstaged\n", encoding="utf-8")
        tracked_staged.write_text("preexisting staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", tracked_staged.name],
            check=True,
        )
        preexisting_untracked = self.repo / "preexisting-untracked.txt"
        preexisting_untracked.write_text("preexisting untracked\n", encoding="utf-8")
        self.write_spec(status="ready")
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")

        self.run_cli("bind-apply", "--feature", self.feature)
        task_output = self.repo / "task-output.txt"
        task_output.write_text("created by apply\n", encoding="utf-8")
        scope = workflow.review_scope(self.repo, self.feature)

        self.assertEqual(["task-output.txt"], scope["changed_paths"])
        self.assertEqual(
            [
                "preexisting-untracked.txt",
                "tracked-staged.txt",
                "tracked-unstaged.txt",
            ],
            scope["excluded_preexisting_paths"],
        )

    def test_review_scope_rejects_a_path_changed_both_before_and_during_apply(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")
        overlapping = self.repo / "overlapping.txt"
        overlapping.write_text("preexisting\n", encoding="utf-8")
        self.run_cli("bind-apply", "--feature", self.feature)

        overlapping.write_text("also changed by apply\n", encoding="utf-8")
        result = self.run_cli(
            "review-scope", "--feature", self.feature, check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("DECISION_NEEDED", result.stderr)
        self.assertIn("overlaps the dirty Apply baseline", result.stderr)
        self.assertIn("overlapping.txt", result.stderr)

    def test_apply_rejects_missing_private_baseline_state(self) -> None:
        self.write_spec(status="applying")
        self.write_tasks(status="applying")
        (self.repo / ".helixharness/workflow" / self.feature / "apply-state.json").unlink()

        result = self.validate("apply", check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("Apply state is missing", result.stderr)

    def test_bind_apply_does_not_follow_predictable_temporary_symlink(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")
        external = self.repo / "outside.txt"
        external.write_text("keep\n", encoding="utf-8")
        (self.feature_dir / ".tasks.md.helixharness-tmp").symlink_to(external)

        result = self.run_cli("bind-apply", "--feature", self.feature)

        self.assertEqual(0, result.returncode)
        self.assertEqual("keep\n", external.read_text(encoding="utf-8"))
        self.assertFalse((self.feature_dir / "tasks.md").is_symlink())

    def test_bind_apply_rejects_symlinked_private_state_target(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")
        external = self.repo / "outside-state.json"
        external.write_text('{"keep": true}\n', encoding="utf-8")
        state = self.repo / ".helixharness/workflow" / self.feature / "apply-state.json"
        state.parent.mkdir(parents=True)
        state.symlink_to(external)

        result = self.run_cli("bind-apply", "--feature", self.feature, check=False)

        self.assertEqual(2, result.returncode)
        self.assertIn("private state file is unsafe", result.stderr)
        self.assertEqual('{"keep": true}\n', external.read_text(encoding="utf-8"))

    def test_review_scope_rejects_untracked_nested_git_repository(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")
        self.run_cli("bind-apply", "--feature", self.feature)
        nested = self.repo / "nested-repo"
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        subprocess.run(
            ["git", "-C", str(nested), "config", "user.email", "nested@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(nested), "config", "user.name", "Nested User"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(nested), "commit", "--allow-empty", "-qm", "nested"],
            check=True,
        )

        result = self.run_cli(
            "review-scope", "--feature", self.feature, check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("nested repository or directory", result.stderr)

    def test_duplicate_informational_field_is_tolerated(self) -> None:
        self.write_spec()
        path = self.feature_dir / "spec.md"
        text = path.read_text(encoding="utf-8").replace(
            "status: applying\n",
            "status: applying\nbranch: first\nbranch: second\n",
        )
        path.write_text(text, encoding="utf-8")
        self.write_tasks(compact=True)

        self.assertEqual(0, self.validate().returncode)

    def test_dependency_cycle_and_unknown_blocker_are_rejected(self) -> None:
        self.write_spec()
        self.write_tasks(
            rows=(
                "| T-01 | First | AC-01 | T-02 | pending |\n"
                "| T-02 | Second | AC-02 | T-01 | pending |"
            )
        )
        cycle = self.validate(check=False)
        self.assertEqual(2, cycle.returncode)
        self.assertIn("dependency graph contains a cycle", cycle.stderr)

        self.write_tasks(rows="| T-01 | First | AC-01 | T-99 | pending |")
        unknown = self.validate(check=False)
        self.assertEqual(2, unknown.returncode)
        self.assertIn("unknown blocker T-99", unknown.stderr)

        self.write_tasks(
            rows=(
                "| T-01 | First | AC-01 | — | pending |\n"
                "| T-02 | Second | AC-02 | T-01 | completed |"
            )
        )
        out_of_order = self.validate(check=False)
        self.assertEqual(2, out_of_order.returncode)
        self.assertIn("T-02 has an incomplete blocker", out_of_order.stderr)

    def test_archive_checks_only_flow_completion_and_review_verdicts(self) -> None:
        self.write_spec(status="implemented", body="# Spec\n\nCurrent requirement text.\n")
        self.write_tasks(
            status="completed",
            rows=(
                "| T-01 | First | anything | — | completed |\n"
                "| T-02 | Second | nothing | T-01 | completed |"
            ),
        )
        self.write_evidence(body="# 自由格式证据\n\n验证细节由 Agent 负责。\n")

        ready = json.loads(
            self.run_cli("archive-preflight", "--feature", self.feature).stdout
        )
        self.assertTrue(ready["ready"])
        self.assertEqual("ready", ready["archive_state"])

        path = self.feature_dir / "spec.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\nChanged after review.\n",
            encoding="utf-8",
        )
        drifted = self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        )
        self.assertEqual(2, drifted.returncode)
        self.assertIn("invalid apply_spec_receipt_sha256", drifted.stderr)

        self.write_spec(status="implemented", body="# Spec\n\nCurrent requirement text.\n")

        self.write_evidence(spec="MUST_FIX")
        blocked = self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        )
        self.assertEqual(2, blocked.returncode)
        self.assertIn("invalid spec_verdict", blocked.stderr)

    def test_archive_requires_nonempty_evidence_body(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        self.write_evidence(body="")

        result = self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("verify-report.md body is empty", result.stderr)

    def test_archive_requires_fixed_parallel_review_receipt(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        self.write_evidence()
        evidence = self.feature_dir / "verify-report.md"

        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace(
                "review_mode: parallel_subagents", "review_mode: sequential"
            ),
            encoding="utf-8",
        )
        wrong_mode = self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        )
        self.assertEqual(2, wrong_mode.returncode)
        self.assertIn("invalid review_mode", wrong_mode.stderr)

        self.write_evidence()
        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace(
                "review_scope_sha256: "
                + workflow.review_scope(self.repo, self.feature)["review_scope_sha256"],
                "review_scope_sha256: invalid",
            ),
            encoding="utf-8",
        )
        wrong_scope = self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        )
        self.assertEqual(2, wrong_scope.returncode)
        self.assertIn("invalid review_scope_sha256", wrong_scope.stderr)

    def test_archive_rejects_review_scope_drift(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")
        source = self.repo / "src.txt"
        source.write_text("reviewed\n", encoding="utf-8")
        self.write_evidence()

        source.write_text("changed after review\n", encoding="utf-8")
        changed = self.run_cli("archive-preflight", "--feature", self.feature, check=False)
        source.write_text("reviewed\n", encoding="utf-8")
        (self.repo / "new-after-review.txt").write_text("new\n", encoding="utf-8")
        added = self.run_cli("archive-preflight", "--feature", self.feature, check=False)

        self.assertEqual(2, changed.returncode)
        self.assertIn("review scope drift", changed.stderr)
        self.assertEqual(2, added.returncode)
        self.assertIn("review scope drift", added.stderr)

    def test_archive_blocks_unreviewed_retired_qa_artifact(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")
        (self.repo / "src.txt").write_text("reviewed\n", encoding="utf-8")
        self.write_evidence()
        self.run_cli("archive-preflight", "--feature", self.feature)
        extra = self.feature_dir / "qa-automation.final.json"
        write(extra, '{"cases": []}\n')
        rejected = self.run_cli("archive-preflight", "--feature", self.feature, check=False)
        self.assertEqual(2, rejected.returncode)
        self.assertIn("review scope drift", rejected.stderr)
        extra.unlink()
        self.run_cli("archive-preflight", "--feature", self.feature)

    def test_archive_accepts_exact_reviewed_content_packaged_as_a_commit(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")
        source = self.repo / "src.txt"
        source.write_text("reviewed\n", encoding="utf-8")
        self.write_evidence()
        subprocess.run(["git", "-C", str(self.repo), "add", "src.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "package reviewed code"], check=True)

        packaged = self.run_cli("archive-preflight", "--feature", self.feature)
        packaged_scope = json.loads(
            self.run_cli("archive-code-scope", "--feature", self.feature).stdout
        )
        extra = self.repo / "extra.txt"
        extra.write_text("not reviewed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "extra.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "extra code"], check=True)
        drifted = self.run_cli("archive-preflight", "--feature", self.feature, check=False)

        self.assertEqual(0, packaged.returncode)
        self.assertEqual(["src.txt"], packaged_scope["committed_paths"])
        self.assertEqual([], packaged_scope["working_tree_paths"])
        self.assertEqual(2, drifted.returncode)
        self.assertIn("review scope drift", drifted.stderr)

    def test_review_choice_routes_to_archive_and_explicit_apply_stays_strict(self) -> None:
        self.prepare_archive_change()
        for command, arguments in (("archive", self.feature), ("propose", self.feature + " --loop")):
            code, result = workflow_router.route(self.repo, command=command, arguments=arguments)
            self.assertEqual(0, code, result)
            self.assertEqual("workflows/single/archive.md", result["workflow"])
        code, result = workflow_router.route(self.repo, command="apply", arguments=self.feature)
        self.assertEqual(0, code, result)
        self.assertEqual("workflows/single/apply.md", result["workflow"])
        self.assertEqual(2, self.run_cli("archive-preflight", "--feature", self.feature, check=False).returncode)
        self.accept_archive_change(self.archive_choice())
        code, result = workflow_router.route(self.repo, command="apply", arguments=self.feature)
        self.assertEqual(0, code, result)
        self.assertEqual("workflows/single/apply.md", result["workflow"])

    def test_unchanged_content_survives_rewritten_review_head(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")
        write(self.repo / "src.txt", "reviewed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "src.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "reviewed commit"], check=True)
        self.write_evidence()
        subprocess.run(["git", "-C", str(self.repo), "commit", "--amend", "-qm", "same content new identity"], check=True)
        self.assertTrue(self.archive_choice()["ready"])
        self.run_cli("archive-preflight", "--feature", self.feature)
        self.run_cli("archive-code-scope", "--feature", self.feature)

    def prepare_archive_change(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")
        write(self.repo / "src.txt", "reviewed\n")
        write(self.repo / "docs/knowledge/domain.md", "old knowledge\n")
        self.write_evidence()
        write(self.repo / "src.txt", "fixed after review\n")
        write(self.repo / "new.txt", "new after review\n")

    def archive_choice(self) -> dict:
        return json.loads(self.run_cli(
            "archive-preflight", "--feature", self.feature, "--review-choice"
        ).stdout)

    def accept_archive_change(self, choice: dict) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "accept-review-drift", "--feature", self.feature,
            "--expected-scope-sha256", choice["current_scope_sha256"],
            "--user-confirmed",
        )

    def test_archive_change_choice_acceptance_survives_commit_finalize_and_move(self) -> None:
        self.prepare_archive_change()
        review_state = self.repo / ".helixharness/workflow" / self.feature / "review-state.json"
        original_review = review_state.read_bytes()
        original_evidence = workflow.nested_frontmatter(
            (self.feature_dir / "verify-report.md").read_text(), "verify-report.md"
        )
        choice = self.archive_choice()
        self.assertFalse(choice["ready"])
        self.assertEqual("review_scope_changed", choice["decision_needed"])
        self.assertEqual("review", choice["recommended_action"])
        self.assertEqual(["new.txt", "src.txt"], choice["changed_since_review"])
        self.accept_archive_change(choice)
        self.assertTrue(self.archive_choice()["ready"])
        # Apply still requires a current Review even after an Archive-only choice.
        self.assertEqual(2, self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        ).returncode)
        scope = json.loads(self.run_cli("archive-code-scope", "--feature", self.feature).stdout)
        self.assertTrue(scope["review_accepted_changes"])
        self.assertIn("new.txt", scope["working_tree_paths"])
        self.assertNotIn("new.txt", scope["reviewed_paths"])
        subprocess.run(["git", "-C", str(self.repo), "add", "src.txt", "new.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "package accepted changes"], check=True)
        self.assertTrue(self.archive_choice()["ready"])
        self.run_cli("authorize-knowledge", "--feature", self.feature,
                     "--paths-json", '["docs/knowledge/domain.md"]')
        write(self.repo / "docs/knowledge/domain.md", "updated knowledge\n")
        spec = self.feature_dir / "spec.md"
        write(spec, spec.read_text().replace("status: implemented", "status: completed"))
        evidence = self.feature_dir / "verify-report.md"
        write(evidence, evidence.read_text().replace("archive: pending", "archive: completed"))
        self.run_cli("finalize-preflight", "--feature", self.feature)
        write(self.repo / "src.txt", "changed again\n")
        self.assertEqual(2, self.run_cli("finalize-preflight", "--feature", self.feature, check=False).returncode)
        write(self.repo / "src.txt", "fixed after review\n")
        completed = self.repo / "docs/specs/completed" / self.feature
        completed.parent.mkdir(parents=True)
        self.feature_dir.rename(completed)
        recovered = json.loads(self.run_cli("archive-code-scope", "--feature", self.feature, "--completed").stdout)
        self.assertTrue(recovered["review_accepted_changes"])
        self.assertEqual(original_review, review_state.read_bytes())
        final_evidence = workflow.nested_frontmatter((completed / "verify-report.md").read_text(), "verify-report.md")
        for key in ("review_head", "review_scope_sha256", "standards_verdict", "spec_verdict"):
            self.assertEqual(original_evidence[key], final_evidence[key])
        self.assertEqual("user_accepted_changes", final_evidence["archive_review_decision"])

    def test_archive_acceptance_requires_fresh_explicit_choice_and_keeps_other_gates(self) -> None:
        self.prepare_archive_change()
        choice = self.archive_choice()
        missing_confirmation = self.run_cli(
            "accept-review-drift", "--feature", self.feature,
            "--expected-scope-sha256", choice["current_scope_sha256"], check=False,
        )
        self.assertEqual(2, missing_confirmation.returncode)
        write(self.repo / "src.txt", "changed while user decides\n")
        stale = self.run_cli("accept-review-drift", "--feature", self.feature,
                             "--expected-scope-sha256", choice["current_scope_sha256"],
                             "--user-confirmed", check=False)
        self.assertEqual(2, stale.returncode)
        self.accept_archive_change(self.archive_choice())
        write(self.repo / "another.txt", "changed after acceptance\n")
        self.assertFalse(self.archive_choice()["ready"])
        evidence = self.feature_dir / "verify-report.md"
        write(evidence, evidence.read_text().replace("standards_verdict: APPROVE", "standards_verdict: REJECT"))
        blocked = self.run_cli("archive-preflight", "--feature", self.feature, "--review-choice", check=False)
        self.assertEqual(2, blocked.returncode)
        self.assertIn("invalid standards_verdict", blocked.stderr)

    def test_archive_choice_after_merge_master_and_unchanged_local_push(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")
        write(self.repo / "src.txt", "reviewed\n")
        self.write_evidence()
        initial = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-qb", "feature/test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "src.txt"], check=True)
        self.assertTrue(self.archive_choice()["ready"])
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "reviewed content"], check=True)
        remote = Path(self.tempdir.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", str(remote), "HEAD:refs/heads/feature/test"], check=True)
        self.assertTrue(self.archive_choice()["ready"])
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-qb", "master-update", initial], check=True)
        write(self.repo / "master.txt", "master update\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "master.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "master update"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "feature/test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "merge", "--no-edit", "-q", "master-update"], check=True)
        choice = self.archive_choice()
        self.assertFalse(choice["ready"])
        self.assertEqual(["master.txt"], choice["changed_since_review"])
        self.accept_archive_change(choice)
        self.assertTrue(self.archive_choice()["ready"])
        # A real fresh review restores the ordinary path.
        self.write_evidence()
        self.assertTrue(self.archive_choice()["ready"])
        self.assertFalse(self.archive_choice()["review_accepted_changes"])

    def test_archive_code_scope_returns_the_sealed_apply_review_scope(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(
            status="ready",
            rows="| T-01 | First | AC-01 | — | pending |",
        )
        preexisting = self.repo / "user-notes.txt"
        preexisting.write_text("keep\n", encoding="utf-8")
        self.run_cli("bind-apply", "--feature", self.feature)

        source = self.repo / "src.txt"
        source.write_text("reviewed\n", encoding="utf-8")
        spec_path = self.feature_dir / "spec.md"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace(
                "status: ready", "status: implemented"
            ),
            encoding="utf-8",
        )
        tasks_path = self.feature_dir / "tasks.md"
        tasks_path.write_text(
            tasks_path.read_text(encoding="utf-8")
            .replace("status: ready", "status: completed")
            .replace("| T-01 | First | AC-01 | — | pending |", "| T-01 | First | AC-01 | — | completed |"),
            encoding="utf-8",
        )
        self.write_evidence()
        state_path = (
            self.repo
            / ".helixharness/workflow"
            / self.feature
            / "review-state.json"
        )
        sealed_before = state_path.read_bytes()

        scope = json.loads(
            self.run_cli("archive-code-scope", "--feature", self.feature).stdout
        )

        self.assertEqual(["src.txt"], scope["changed_paths"])
        self.assertIn("user-notes.txt", scope["excluded_preexisting_paths"])
        self.assertRegex(scope["review_scope_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(sealed_before, state_path.read_bytes())

    def test_archive_code_scope_includes_the_reviewed_commit_range(self) -> None:
        source = self.repo / "src.txt"
        source.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "src.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base source"], check=True)
        fixed_point = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.write_spec(status="ready")
        self.write_tasks(
            status="ready",
            rows="| T-01 | First | AC-01 | — | pending |",
        )
        self.run_cli("bind-apply", "--feature", self.feature)
        source.write_text("reviewed commit\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "apply source"], check=True)
        for path, old, new in (
            (self.feature_dir / "spec.md", "status: ready", "status: implemented"),
            (self.feature_dir / "tasks.md", "status: ready", "status: completed"),
            (
                self.feature_dir / "tasks.md",
                "| T-01 | First | AC-01 | — | pending |",
                "| T-01 | First | AC-01 | — | completed |",
            ),
        ):
            path.write_text(
                path.read_text(encoding="utf-8").replace(old, new),
                encoding="utf-8",
            )
        self.write_evidence()
        evidence = self.feature_dir / "verify-report.md"
        current_head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace(
                f"review_fixed_point: {current_head}",
                f"review_fixed_point: {fixed_point}",
            ),
            encoding="utf-8",
        )

        scope = json.loads(
            self.run_cli("archive-code-scope", "--feature", self.feature).stdout
        )

        self.assertEqual(["src.txt"], scope["committed_paths"])
        self.assertEqual([], scope["working_tree_paths"])
        self.assertEqual(["src.txt"], scope["changed_paths"])

    def test_archive_code_scope_is_available_during_telemetry_recovery(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        source = self.repo / "src.txt"
        source.write_text("reviewed\n", encoding="utf-8")
        self.write_evidence()
        spec_path = self.feature_dir / "spec.md"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace(
                "status: implemented", "status: completed"
            ),
            encoding="utf-8",
        )
        evidence_path = self.feature_dir / "verify-report.md"
        evidence_path.write_text(
            evidence_path.read_text(encoding="utf-8").replace(
                "archive: pending", "archive: completed"
            ),
            encoding="utf-8",
        )
        completed = self.repo / "docs/specs/completed" / self.feature
        completed.parent.mkdir(parents=True)
        self.feature_dir.rename(completed)

        scope = json.loads(
            self.run_cli(
                "archive-code-scope",
                "--feature",
                self.feature,
                "--completed",
            ).stdout
        )

        self.assertEqual(["src.txt"], scope["working_tree_paths"])
        self.assertEqual(["src.txt"], scope["changed_paths"])

    def test_archive_accepts_any_non_negative_must_fix_round_count(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")

        for rounds in (0, 1, 3, 4, 12):
            with self.subTest(rounds=rounds):
                self.write_evidence(must_fix_rounds=rounds)
                self.assertEqual(
                    0,
                    self.run_cli("archive-preflight", "--feature", self.feature).returncode,
                )

        self.write_evidence(must_fix_rounds=12)
        evidence = self.feature_dir / "verify-report.md"
        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace(
                "must_fix_rounds: 12\n", "must_fix_rounds: -1\n"
            ),
            encoding="utf-8",
        )
        negative = self.run_cli("archive-preflight", "--feature", self.feature, check=False)
        self.assertEqual(2, negative.returncode)
        self.assertIn("must_fix_rounds", negative.stderr)

        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace("must_fix_rounds: -1\n", ""),
            encoding="utf-8",
        )
        missing = self.run_cli("archive-preflight", "--feature", self.feature, check=False)
        self.assertEqual(2, missing.returncode)
        self.assertIn("must_fix_rounds", missing.stderr)

    def test_archive_rejects_deprecated_bug_fix_identity(self) -> None:
        signature = "a" * 64
        self.write_spec(
            status="implemented", trigger="bug-fix", bug_signature=signature
        )
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        self.write_evidence(trigger="bug-fix", bug_signature=signature)
        rejected = self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        )
        self.assertEqual(2, rejected.returncode)
        self.assertIn("deprecated bug-fix identity", rejected.stderr)

    def test_archive_finalizing_state_is_resumable(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        self.write_evidence()
        spec_path = self.feature_dir / "spec.md"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace(
                "status: implemented", "status: completed"
            ),
            encoding="utf-8",
        )
        evidence_path = self.feature_dir / "verify-report.md"
        evidence_path.write_text(
            evidence_path.read_text(encoding="utf-8").replace(
                "archive: pending", "archive: completed"
            ),
            encoding="utf-8",
        )

        result = json.loads(
            self.run_cli("archive-preflight", "--feature", self.feature).stdout
        )

        self.assertEqual("finalizing", result["archive_state"])

    def test_finalize_preflight_allows_only_confirmed_knowledge_after_scope_seal(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        write(self.repo / "docs/knowledge/domain.md", "# Previous knowledge\n")
        self.write_evidence()
        self.run_cli(
            "authorize-knowledge",
            "--feature",
            self.feature,
            "--paths-json",
            json.dumps(["docs/knowledge/domain.md"]),
        )
        write(self.repo / "docs/knowledge/domain.md", "# Confirmed knowledge\n")

        premature = self.run_cli(
            "finalize-preflight", "--feature", self.feature, check=False
        )
        spec = self.feature_dir / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "status: implemented", "status: completed"
            ),
            encoding="utf-8",
        )
        evidence = self.feature_dir / "verify-report.md"
        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace(
                "archive: pending", "archive: completed"
            ),
            encoding="utf-8",
        )
        code = self.repo / "changed-after-review.py"
        code.write_text("print('not reviewed')\n", encoding="utf-8")
        drifted = self.run_cli(
            "finalize-preflight", "--feature", self.feature, check=False
        )
        code.unlink()
        finalized = json.loads(self.run_cli(
            "finalize-preflight", "--feature", self.feature
        ).stdout)

        self.assertEqual(2, premature.returncode)
        self.assertIn("requires finalizing state", premature.stderr)
        self.assertEqual(2, drifted.returncode)
        self.assertIn("review scope drift", drifted.stderr)
        self.assertEqual("finalizing", finalized["archive_state"])

    def prepare_dirty_knowledge_archive(self) -> None:
        self.write_spec(status="ready")
        self.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |")
        write(self.repo / "docs/knowledge/domain.md", "initial knowledge\n")
        write(self.repo / "docs/knowledge/other.md", "unrelated knowledge\n")
        write(self.repo / "unrelated.py", "# preexisting local work\n")
        self.run_cli("bind-apply", "--feature", self.feature)
        for name in ("spec.md", "tasks.md"):
            path = self.feature_dir / name
            path.write_text(path.read_text().replace("status: ready", "status: implemented" if name == "spec.md" else "status: completed").replace("| pending |", "| completed |"))
        write(self.repo / "implementation.py", "# reviewed implementation\n")
        self.write_evidence()
        self.run_cli("authorize-knowledge", "--feature", self.feature,
                     "--paths-json", '["docs/knowledge/domain.md"]')
        write(self.repo / "docs/knowledge/domain.md", "authorized update\n")
        for name, before, after in (("spec.md", "status: implemented", "status: completed"),
                                    ("verify-report.md", "archive: pending", "archive: completed")):
            path = self.feature_dir / name
            path.write_text(path.read_text().replace(before, after))

    def test_finalize_allows_authorized_knowledge_in_dirty_apply_baseline(self) -> None:
        self.prepare_dirty_knowledge_archive()
        result = json.loads(self.run_cli("finalize-preflight", "--feature", self.feature).stdout)
        self.assertEqual("finalizing", result["archive_state"])
        active_scope = json.loads(self.run_cli("archive-code-scope", "--feature", self.feature).stdout)
        self.assertEqual(["implementation.py"], active_scope["working_tree_paths"])
        completed = self.repo / "docs/specs/completed" / self.feature
        completed.parent.mkdir(parents=True)
        self.feature_dir.rename(completed)
        final_scope = json.loads(self.run_cli("archive-code-scope", "--feature", self.feature, "--completed").stdout)
        self.assertEqual(active_scope["working_tree_paths"], final_scope["working_tree_paths"])

    def test_finalize_still_blocks_other_dirty_baseline_knowledge(self) -> None:
        self.prepare_dirty_knowledge_archive()
        write(self.repo / "docs/knowledge/other.md", "unapproved edit\n")
        result = self.run_cli("finalize-preflight", "--feature", self.feature, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("overlaps the dirty Apply baseline", result.stderr)
        self.assertIn("docs/knowledge/other.md", result.stderr)

    def test_finalize_still_blocks_unrelated_dirty_baseline_code(self) -> None:
        self.prepare_dirty_knowledge_archive()
        write(self.repo / "unrelated.py", "# changed without review\n")
        result = self.run_cli("finalize-preflight", "--feature", self.feature, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("overlaps the dirty Apply baseline", result.stderr)
        self.assertIn("unrelated.py", result.stderr)

    def test_finalize_preflight_rejects_unconfirmed_knowledge_path(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        write(self.repo / "docs/knowledge/confirmed.md", "# Previous knowledge\n")
        self.write_evidence()
        self.run_cli(
            "authorize-knowledge",
            "--feature",
            self.feature,
            "--paths-json",
            json.dumps(["docs/knowledge/confirmed.md"]),
        )
        write(self.repo / "docs/knowledge/unconfirmed.md", "# Concurrent drift\n")
        spec = self.feature_dir / "spec.md"
        spec.write_text(
            spec.read_text(encoding="utf-8").replace(
                "status: implemented", "status: completed"
            ),
            encoding="utf-8",
        )
        evidence = self.feature_dir / "verify-report.md"
        evidence.write_text(
            evidence.read_text(encoding="utf-8").replace(
                "archive: pending", "archive: completed"
            ),
            encoding="utf-8",
        )

        result = self.run_cli(
            "finalize-preflight", "--feature", self.feature, check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("review scope drift", result.stderr)

    def test_authorize_knowledge_requires_existing_regular_safe_file(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        self.write_evidence()
        outside = self.repo / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")

        missing = self.run_cli(
            "authorize-knowledge",
            "--feature",
            self.feature,
            "--paths-json",
            json.dumps(["docs/knowledge/missing.md"]),
            check=False,
        )
        symlink = self.repo / "docs/knowledge/link.md"
        symlink.parent.mkdir(parents=True)
        symlink.symlink_to(outside)
        linked = self.run_cli(
            "authorize-knowledge",
            "--feature",
            self.feature,
            "--paths-json",
            json.dumps(["docs/knowledge/link.md"]),
            check=False,
        )

        self.assertEqual(2, missing.returncode)
        self.assertIn("existing regular file", missing.stderr)
        self.assertEqual(2, linked.returncode)
        self.assertIn("existing regular file", linked.stderr)
        self.assertEqual("outside\n", outside.read_text(encoding="utf-8"))

    def test_authorize_knowledge_rejects_edits_before_authorization_without_cleanup(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(
            status="completed",
            rows="| T-01 | First | AC-01 | — | completed |",
        )
        knowledge = self.repo / "docs/knowledge/domain.md"
        write(knowledge, "# Previous knowledge\n")
        self.write_evidence()
        write(knowledge, "# Edited before authorization\n")

        result = self.run_cli(
            "authorize-knowledge",
            "--feature",
            self.feature,
            "--paths-json",
            json.dumps(["docs/knowledge/domain.md"]),
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn(
            "authorize-knowledge requires an unchanged review scope; "
            "authorize before editing knowledge files",
            result.stderr,
        )
        self.assertEqual("# Edited before authorization\n", knowledge.read_text(encoding="utf-8"))
        self.assertFalse(
            (self.repo / ".helixharness/workflow" / self.feature / "archive-state.json").exists()
        )

    def test_quoted_commented_frontmatter_is_equivalent(self) -> None:
        self.write_spec()
        self.write_tasks(compact=True)
        for name in ("spec.md", "tasks.md"):
            path = self.feature_dir / name
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                f"feature: {self.feature}", f'feature: "{self.feature}" # identity'
            ).replace("status: applying", "status: 'applying' # lifecycle")
            path.write_text(text, encoding="utf-8")

        self.assertEqual(0, self.validate().returncode)

    def test_archive_rejects_completed_name_collision(self) -> None:
        self.write_spec(status="implemented")
        self.write_tasks(status="completed", rows="| T-01 | First | AC-01 | — | completed |")
        self.write_evidence()
        (self.repo / "docs/specs/completed" / self.feature).mkdir(parents=True)

        result = self.run_cli(
            "archive-preflight", "--feature", self.feature, check=False
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("completed feature already exists", result.stderr)


if __name__ == "__main__":
    unittest.main()
