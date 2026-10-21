#!/usr/bin/env python3

import json
import re
import unittest
from pathlib import Path

from version_update import UpdateError, _normalized_release, _semver, _version_text


PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent


class VersionUpdateSemverTest(unittest.TestCase):
    def test_prerelease_remains_rejected(self):
        with self.assertRaises(UpdateError):
            _version_text("2.0.13-rc.1")

    def test_internal_snapshot_is_not_a_supported_release(self):
        with self.assertRaises(UpdateError):
            _version_text("2.0.15-v1-SNAPSHOT")

    def test_stable_releases_use_numeric_ordering(self):
        self.assertEqual("1.0.0", _version_text("v1.0.0"))
        self.assertGreater(_semver("1.10.0"), _semver("1.9.9"))
        self.assertGreater(_semver("2.0.0"), _semver("1.99.99"))


    def test_retired_release_field_is_rejected(self):
        release = {
            "version": "1.1.0",
            "date": "2026-08-17",
            "update_type": "recommended",
            "title": "test",
            "changes": ["test"],
            "restart_required": True,
            "bundle_digest": {"legacy": True},
        }

        with self.assertRaises(ValueError):
            _normalized_release(release)


if __name__ == "__main__":
    unittest.main()
