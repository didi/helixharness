"""Execute the frontend scanner's documented build detection against small projects."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shell_support import bash_environment, find_bash

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class FrontendBuildDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = find_bash()

    def test_dev_command_uses_detected_package_manager(self) -> None:
        prompt = (PLUGIN_ROOT / "agents/scan-frontend-meta.md").read_text(encoding="utf-8")
        script = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL).group(1)
        # Test the documented detection with the suite's interpreter, rather
        # than a WindowsApps alias or a different Python on PATH.
        script = script.replace("python3 -c", f"{shlex.quote(Path(sys.executable).as_posix())} -c")
        for lockfile, expected in (
            ("yarn.lock", "dev server 命令: yarn dev"),
            ("pnpm-lock.yaml", "dev server 命令: pnpm dev"),
            ("package-lock.json", "dev server 命令: npm run dev"),
            (None, "dev server 命令: npm run dev"),
        ):
            with self.subTest(lockfile=lockfile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
                if lockfile:
                    (root / lockfile).touch()
                env = bash_environment(self.bash)
                for name in ("PM", "PM_RUN", "PROJECT_ENTRY_FILE"):
                    env.pop(name, None)
                result = subprocess.run(
                    [self.bash, "--noprofile", "--norc", "-c", script], cwd=root, env=env,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)


if __name__ == "__main__":
    unittest.main()
