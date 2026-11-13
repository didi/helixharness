import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from project_linter import run_linter


class ProjectLinterTest(unittest.TestCase):
    def test_native_linter_runs_from_repo_and_preserves_failure_with_bounded_output(self):
        with tempfile.TemporaryDirectory(prefix="helix space 中文 ") as temporary:
            repo = Path(temporary)
            folder = repo / "docs/harness/linters"
            folder.mkdir(parents=True)
            (repo / "marker").write_text("synthetic")
            (folder / "invariant-check.py").write_text(
                "from pathlib import Path\nimport sys\n"
                "assert Path('marker').read_text() == 'synthetic'\n"
                "for i in range(100): print('line', i)\nsys.exit(7)\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(7, run_linter(repo, "invariant-check"))
            self.assertEqual(50, len(output.getvalue().splitlines()))

    def test_missing_linter_does_not_silently_pass(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, run_linter(Path(temporary), "invariant-check"))
