"""JSON CLI entries must still answer a caller that runs in legacy Windows mode.

`{helix_python}` appends `-X utf8`, but a plugin script also gets invoked by
anything that does not use the launcher. These entries echo installation paths,
so a code page that cannot encode them replaces the JSON with a traceback and the
host loses the result entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import env_sandbox

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CJK_NAME = "应用 空格"


class LegacyCodePageCliTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="helix-cli-stdio-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / CJK_NAME
        self.repo.mkdir()
        self.env = {
            **os.environ,
            **env_sandbox.boot_vars(),
            **env_sandbox.home_vars(self.root / "home"),
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
            "CODEX_HOME": str(self.root / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-home"),
            "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor-home"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.root / "telemetry-home"),
            "HELIXHARNESS_FEEDBACK_HOME": str(self.root / "feedback-home"),
            "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
        }
        self.env.pop("PYTHONIOENCODING", None)

    def require_legacy_stdio(self) -> None:
        probe = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-c",
             "import sys; print(sys.stdout.encoding.lower())"],
            env=self.env, capture_output=True, text=True, timeout=20,
        )
        if "utf" in probe.stdout.strip():
            self.skipTest("the C locale still yields UTF-8 stdio on this platform")

    def run_cli(self, script: str, *arguments: str) -> dict:
        self.require_legacy_stdio()
        completed = subprocess.run(
            [sys.executable, "-X", "utf8=0", str(PLUGIN_ROOT / "scripts" / script),
             "--platform", "codex", "--repo-root", str(self.repo), *arguments],
            env=self.env, cwd=self.repo, capture_output=True, timeout=30,
        )
        self.assertEqual(0, completed.returncode,
                         completed.stdout.decode("utf-8", errors="replace")
                         + completed.stderr.decode("utf-8", errors="replace"))
        # Strict decode: a non-UTF-8 stream here is exactly the reported defect.
        stdout = completed.stdout.decode("utf-8")
        payload = json.loads(stdout)
        self.assertIn(CJK_NAME, stdout)
        return payload

    def test_version_update_answers_a_legacy_caller_with_utf8_json(self) -> None:
        cache = self.root / ("installed plugin " + CJK_NAME) / "cache.json"
        payload = self.run_cli("version_update.py", "--cache-path", str(cache), "resolve")
        self.assertIn(CJK_NAME, payload["cache_path"])

    def test_user_config_answers_a_legacy_caller_with_utf8_json(self) -> None:
        payload = self.run_cli("user_config.py")
        self.assertTrue(payload["read_only"])


if __name__ == "__main__":
    unittest.main()
