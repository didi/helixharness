"""Keep shell fixtures on native Git Bash when Windows also provides WSL."""

from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shell_support import bash_environment, find_bash


class ShellSupportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git = self.root / "Git/cmd/git.exe"
        self.bash = self.root / "Git/bin/bash.exe"
        self.wsl = self.root / "Windows/System32/bash.exe"
        for path in (self.git, self.bash, self.wsl):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def which(self, name):
        return str(self.git if name == "git.exe" else self.wsl)

    def test_git_bash_is_selected_before_the_wsl_launcher(self):
        with patch("shell_support.sys", SimpleNamespace(platform="win32")), \
             patch("shell_support.shutil.which", side_effect=self.which), \
             patch("shell_support.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(str(self.bash), find_bash())
        self.assertEqual(str(self.bash), run.call_args.args[0][0])

    def test_wsl_is_not_probed_when_native_bash_is_missing(self):
        with patch("shell_support.sys", SimpleNamespace(platform="win32")), \
             patch("shell_support.os.environ", {}), \
             patch("shell_support.shutil.which", side_effect=lambda name: str(self.wsl) if name == "bash" else None), \
             patch("shell_support.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "WSL is not supported"):
                find_bash()
        run.assert_not_called()

    def test_git_utilities_precede_the_inherited_path(self):
        with patch("shell_support.sys", SimpleNamespace(platform="win32")), \
             patch("shell_support.os.environ", {"PATH": "inherited", "SYNTHETIC": "preserved"}):
            environment = bash_environment(str(self.bash))
        self.assertTrue(environment["PATH"].startswith(str(self.root / "Git/usr/bin")))
        self.assertTrue(environment["PATH"].endswith("inherited"))
        self.assertEqual("preserved", environment["SYNTHETIC"])

    def test_unusable_git_bash_falls_back_to_another_native_candidate(self):
        alternate = self.root / "Git/usr/bin/bash.exe"
        alternate.parent.mkdir(parents=True)
        alternate.touch()
        with patch("shell_support.sys", SimpleNamespace(platform="win32")), \
             patch("shell_support.shutil.which", side_effect=self.which), \
             patch("shell_support.subprocess.run", side_effect=[
                 subprocess.TimeoutExpired(str(self.bash), 5),
                 subprocess.CompletedProcess([], 0),
             ]):
            self.assertEqual(str(alternate), find_bash())


if __name__ == "__main__":
    unittest.main()
