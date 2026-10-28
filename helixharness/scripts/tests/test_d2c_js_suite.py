"""Run the d-d2c script regression suite from the unified test entry point.

`scripts/run_tests.py` only discovers `test_*.py`, so this wrapper keeps the
JavaScript suite reachable from the same command. It is skipped when Node is not
available instead of failing, matching how the skill treats optional runtime
dependencies.
"""

from pathlib import Path
import shutil
import subprocess
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
RUNNER = PLUGIN_ROOT / "skills" / "d-d2c" / "scripts" / "tests" / "run-tests.cjs"


class D2cScriptSuiteTest(unittest.TestCase):
    def test_d2c_script_suite_passes(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")
        if not RUNNER.exists():
            self.fail(f"d-d2c test runner is missing: {RUNNER}")

        result = subprocess.run(
            [node, str(RUNNER)],
            cwd=str(RUNNER.parent),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(
                "d-d2c script suite failed\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )


if __name__ == "__main__":
    unittest.main()
