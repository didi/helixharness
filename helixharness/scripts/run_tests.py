#!/usr/bin/env python3
"""Run all bundled tests from scripts/tests without extra dependencies.

The suite assumes UTF-8 content (JSON contracts, Markdown docs and subprocess
output). On locales whose default encoding is not UTF-8 (for example GBK on
Chinese Windows), Python would otherwise decode that content with the legacy
codepage and fail with UnicodeDecodeError. Re-exec once in UTF-8 mode so the
suite behaves identically on every machine; PYTHONUTF8=1 is honoured when
already set.
"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


def _ensure_utf8_mode() -> None:
    if sys.flags.utf8_mode:
        return
    env = dict(os.environ, PYTHONUTF8="1")
    completed = subprocess.run(
        [sys.executable, os.path.abspath(__file__), *sys.argv[1:]], env=env
    )
    raise SystemExit(completed.returncode)


def _sanitize_git_env() -> None:
    """Drop injected GIT_CONFIG_* pairs that break git on Windows.

    Some agent/CI runtimes inject GIT_CONFIG_COUNT/KEY/VALUE entries with an
    empty value (for example to reset credential.helper). Windows cannot pass
    empty environment values to child processes, so git then fails every
    invocation with "missing config value GIT_CONFIG_VALUE_n". The suite only
    builds throwaway repositories, so these injected entries are removed.
    """
    for name in list(os.environ):
        if name == "GIT_CONFIG_COUNT" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            os.environ.pop(name, None)


def main() -> int:
    _ensure_utf8_mode()
    _sanitize_git_env()
    root = Path(__file__).resolve().parent
    # coverage/runpy on Python 3.10 need not add the script directory to
    # sys.path as direct execution does. Tests import the bundled scripts.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    suite = unittest.TestLoader().discover(str(root / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
