"""Execute the POSIX preflight/hook with controlled interpreter versions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from shell_support import bash_environment, find_bash

ROOT = Path(__file__).resolve().parents[2]


class HookPythonVersionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = find_bash()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='helix interpreter ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root/'bin'
        self.bin.mkdir()
        self.target = self.root/'target with spaces.py'
        self.target.write_text('import json,sys\nprint(json.dumps(sys.stdin.read(), ensure_ascii=False))\nsys.exit(7)\n', encoding='utf-8')

    def candidate(self, name, version, broken=False):
        code = ("import os,sys\nargs=sys.argv[1:]\n"
                + ("assert args.pop(0)=='-3'\n" if name == 'py' else '')
                + ('sys.exit(1)\n' if broken else '')
                + f"if args[0]=='-c':\n sys.version_info={version!r}\n exec(args[1])\n"
                + "else:\n os.execv(sys.executable,[sys.executable]+args)\n")
        script = '#!/bin/sh\nexec ' + shlex.quote(Path(sys.executable).as_posix()) + ' -c ' + shlex.quote(code) + ' "$@"\n'
        path = self.bin/name
        path.write_text(script, encoding='utf-8', newline='\n')
        path.chmod(0o755)

    def invoke(self, event='SessionStart', stdin=''):
        env = bash_environment(self.bash)
        env['PATH'] = str(self.bin) + os.pathsep + env['PATH']
        env['PYTHONUTF8'] = '1'
        return subprocess.run([self.bash, str(ROOT/'scripts/python_hook_bootstrap.sh'), event, self.target.as_posix()],
                              input=stdin, capture_output=True, text=True, encoding='utf-8', env=env, timeout=15)

    def test_python39_is_rejected_by_lifecycle_hook_for_every_candidate(self):
        for name in ('python3', 'python', 'py'):
            self.candidate(name, (3, 9, 99))
        result = self.invoke()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('3.10+', json.loads(result.stdout)['systemMessage'])
        result = self.invoke('SessionEnd')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('', result.stdout)

    def test_python310_and_launcher_fallback_preserve_stdin_and_exit_code(self):
        for ready in ('python3', 'python', 'py'):
            with self.subTest(ready=ready):
                for name in ('python3', 'python', 'py'):
                    self.candidate(name, (3, 10, 0), broken=name != ready)
                result = self.invoke(stdin='中文 synthetic input')
                self.assertEqual(7, result.returncode, result.stderr)
                self.assertEqual('中文 synthetic input', json.loads(result.stdout))

    def test_preflight_install_requires_confirmation_without_running_a_manager(self):
        result = subprocess.run([self.bash, str(ROOT/'scripts/runtime_preflight.sh'), 'install-python'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(2, result.returncode)
        self.assertEqual('python_install_confirmation_required', json.loads(result.stdout)['error_code'])


if __name__ == '__main__':
    unittest.main()
