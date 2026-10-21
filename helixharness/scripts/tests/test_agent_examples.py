"""Execute the frontend scanner's documented build detection against small projects."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class FrontendBuildDetectionTests(unittest.TestCase):
    def test_dev_command_uses_detected_package_manager(self) -> None:
        prompt = (PLUGIN_ROOT / "agents/scan-frontend-meta.md").read_text(encoding="utf-8")
        script = re.search(r"```bash\n(.*?)\n```", prompt, re.DOTALL).group(1)
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
                env = dict(os.environ)
                for name in ("PM", "PM_RUN", "PROJECT_ENTRY_FILE"):
                    env.pop(name, None)
                result = subprocess.run(
                    ["bash", "-c", script], cwd=root, env=env,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)


if __name__ == "__main__":
    unittest.main()
