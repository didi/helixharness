#!/usr/bin/env python3
"""Run all bundled tests from scripts/tests without extra dependencies."""

from pathlib import Path
import unittest


def main() -> int:
    root = Path(__file__).resolve().parent
    suite = unittest.TestLoader().discover(str(root / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
