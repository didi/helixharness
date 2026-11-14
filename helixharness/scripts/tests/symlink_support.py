#!/usr/bin/env python3
"""Shared helpers for tests that rely on filesystem symlinks.

Creating symlinks on Windows requires either administrator rights or an
opt-in Developer Mode, so fixtures that build symlinks cannot run everywhere.
Use :func:`symlink_or_skip` around the fixture call to mark the test as
skipped instead of failing with WinError 1314.
"""

from __future__ import annotations

import contextlib
import unittest


@contextlib.contextmanager
def symlink_or_skip(test_case: unittest.TestCase):
    """Convert symlink-creation OSError into a skipped test."""
    del test_case  # the case is implicit; SkipTest carries the result
    try:
        yield
    except OSError as exc:
        raise unittest.SkipTest(f"symlink unavailable on this machine: {exc}")
