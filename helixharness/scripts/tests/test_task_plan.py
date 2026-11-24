"""One canonical plan store, optimistic receipts and dependency-safe edits."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from task_plan import inspect_plan, update_task
from workflow import WorkflowError

TEXT = """---
feature: demo
status: applying
---
# Keep this description
| ID | Task | Covers | Blocked By | Status |
|---|---|---|---|---|
| T-01 | First | AC-01 | — | pending |
| T-02 | Second | AC-02 | T-01 | pending |
"""


class TaskPlanTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve()
        self.file = self.repo / "docs/specs/active/demo/tasks.md"
        self.file.parent.mkdir(parents=True)
        self.file.write_text(TEXT)

    def receipt(self):
        return hashlib.sha256(self.file.read_bytes()).hexdigest()

    def test_view_has_no_side_effect_and_returns_next_task(self):
        before = sorted(self.repo.rglob("*"))
        result = inspect_plan(self.repo, "demo")
        self.assertEqual(result["next_task"], "T-01")
        self.assertEqual(result["receipt_sha256"], self.receipt())
        self.assertEqual(sorted(self.repo.rglob("*")), before)

    def test_update_preserves_document_and_mode(self):
        os.chmod(self.file, 0o640)
        update_task(self.repo, "demo", "T-01", "in_progress", self.receipt())
        self.assertEqual(self.file.read_text(), TEXT.replace("— | pending", "— | in_progress"))
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o640)
        update_task(self.repo, "demo", "T-01", "completed", self.receipt())
        self.assertEqual(inspect_plan(self.repo, "demo")["next_task"], "T-02")

    def test_stale_receipt_dependency_and_unknown_task_do_not_write(self):
        for task, state, receipt in (("T-01", "completed", "0" * 64),
                                     ("T-02", "in_progress", self.receipt()),
                                     ("T-02", "completed", self.receipt()),
                                     ("T-99", "completed", self.receipt())):
            with self.subTest(task=task, state=state), self.assertRaises(WorkflowError):
                update_task(self.repo, "demo", task, state, receipt)
            self.assertEqual(self.file.read_text(), TEXT)

    def test_closed_or_mismatched_plan_is_not_mutated(self):
        for text in (TEXT.replace("status: applying", "status: completed"),
                     TEXT.replace("feature: demo", "feature: foreign")):
            self.file.write_text(text)
            with self.assertRaises(WorkflowError):
                update_task(self.repo, "demo", "T-01", "completed", self.receipt())
            self.assertEqual(self.file.read_text(), text)

    def test_parent_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            self.file.unlink()
            self.file.parent.rmdir()
            self.file.parent.symlink_to(other, target_is_directory=True)
            Path(other, "tasks.md").write_text(TEXT)
            with self.assertRaises(WorkflowError):
                inspect_plan(self.repo, "demo")

    def test_escaped_pipes_reordered_columns_and_crlf_preserve_all_but_status(self):
        for text in (
            TEXT.replace("First", r"Preserve A\|B"),
            "---\nfeature: demo\nstatus: applying\n---\n| ID | Task | Status |\n|---|---|---|\n| T-01 | Preserve A\\|B | pending |\n",
            "---\r\nfeature: demo\r\nstatus: applying\r\n---\r\n  | Status | Task | ID |\r\n  |---|---|---|\r\n  | pending | Preserve A\\|B | T-01 |\r\n",
        ):
            with self.subTest(text=text):
                self.file.write_bytes(text.encode())
                result = update_task(self.repo, "demo", "T-01", "in_progress", self.receipt())
                self.assertEqual(self.file.read_bytes(), text.replace("pending", "in_progress", 1).encode())
                self.assertEqual(result["tasks"][0]["status"], "in_progress")
                self.assertIn("A|B", result["tasks"][0]["description"])
