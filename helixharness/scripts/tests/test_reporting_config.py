"""Shared published defaults and private overrides across both report channels."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import feedback_submit
import reporting_config
import telemetry_async


class ReportingConfigTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report_home = self.root / "telemetry"
        self.feedback_home = self.root / "feedback"
        patch = mock.patch.dict(os.environ, {"HELIXHARNESS_FEEDBACK_HOME": str(self.feedback_home)})
        patch.start()
        self.addCleanup(patch.stop)

    def configure(self, home: Path, raw: str):
        home.mkdir(exist_ok=True)
        (home / "config.json").write_text(raw)

    def test_published_defaults_work_without_creating_user_files(self):
        with mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", "https://published.example/report"), \
             mock.patch.object(reporting_config, "DEFAULT_FEEDBACK_ENDPOINT", "https://published.example/feedback/report"):
            self.assertEqual("https://published.example/report", telemetry_async.configured_endpoint(self.report_home))
            draft = feedback_submit.prepare(self.root, {"text": "流程卡住", "platform": "unknown"})
            self.assertEqual("https://published.example/feedback/report", draft["endpoint"])
        self.assertFalse(self.report_home.exists())
        self.assertFalse((self.feedback_home / "config.json").exists())

    def test_private_files_override_defaults_and_removing_them_restores_defaults(self):
        with mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", "https://published.example/report"), \
             mock.patch.object(reporting_config, "DEFAULT_FEEDBACK_ENDPOINT", "https://published.example/feedback/report"):
            for home, path, resolve in self.channels():
                with self.subTest(path=path):
                    self.configure(home, json.dumps({"endpoint": "https://private.example" + path}))
                    self.assertEqual("https://private.example" + path, resolve())
                    self.configure(home, json.dumps({"endpoint": "https://changed.example" + path}))
                    self.assertEqual("https://changed.example" + path, resolve())
                    for raw in ('{}', '{"endpoint":"  "}'):
                        self.configure(home, raw)
                        self.assertEqual("https://published.example" + path, resolve())
                    (home / "config.json").unlink()
                    self.assertEqual("https://published.example" + path, resolve())

    def channels(self):
        return (
            (self.report_home, "/report", lambda: telemetry_async.configured_endpoint(self.report_home)),
            (self.feedback_home, "/feedback/report", feedback_submit._configured_endpoint),
        )

    def test_invalid_private_config_never_falls_back_to_published_recipient(self):
        with mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", "https://published.example/report"), \
             mock.patch.object(reporting_config, "DEFAULT_FEEDBACK_ENDPOINT", "https://published.example/feedback/report"):
            for home, path, resolve in self.channels():
                for raw in ('{broken', '[]', '{"endpoint":42}', '{"endpoint":null}',
                            '{"endpoint":"https://host:0/report"}', '{"endpoint":"file:///tmp/report"}'):
                    with self.subTest(path=path, config=raw):
                        self.configure(home, raw)
                        with self.assertRaisesRegex(ValueError, "invalid"):
                            resolve()

    def test_endpoint_environment_variables_are_not_a_third_configuration_source(self):
        with mock.patch.dict(os.environ, {
            "HELIXHARNESS_TELEMETRY_ENDPOINT": "https://ignored.example/report",
            "HELIXHARNESS_FEEDBACK_ENDPOINT": "https://ignored.example/feedback/report",
        }), mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", ""), \
             mock.patch.object(reporting_config, "DEFAULT_FEEDBACK_ENDPOINT", ""):
            self.assertEqual("endpoint_unconfigured", telemetry_async.endpoint_status(self.report_home)["reason"])
            with self.assertRaisesRegex(feedback_submit.FeedbackError, "not configured"):
                feedback_submit._configured_endpoint()

    def test_published_defaults_are_validated_and_do_not_cross_channels(self):
        for bad in ("file:///tmp/report", "https://user:pass@example.com/report", "https://example.com/report?token=x"):
            with self.subTest(endpoint=bad), \
                 mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", bad), \
                 mock.patch.object(reporting_config, "DEFAULT_FEEDBACK_ENDPOINT", bad):
                self.assertEqual("endpoint_invalid", telemetry_async.endpoint_status(self.report_home)["reason"])
                with self.assertRaisesRegex(feedback_submit.FeedbackError, "invalid"):
                    feedback_submit._configured_endpoint()
        with mock.patch.object(reporting_config, "DEFAULT_REPORT_ENDPOINT", "https://published.example/report"), \
             mock.patch.object(reporting_config, "DEFAULT_FEEDBACK_ENDPOINT", ""):
            self.assertTrue(telemetry_async.endpoint_status(self.report_home)["ready"])
            with self.assertRaisesRegex(feedback_submit.FeedbackError, "not configured"):
                feedback_submit._configured_endpoint()

    def test_installed_worker_copies_and_refreshes_published_configuration(self):
        source = self.root / "plugin"
        (source / "scripts").mkdir(parents=True)
        (source / ".codex-plugin").mkdir()
        (source / ".codex-plugin/plugin.json").write_text('{"version":"1.0.0"}')
        # The real installer verifies and copies the bundled runtime dependencies.
        import shutil
        actual = Path(telemetry_async.__file__).parent
        for name in telemetry_async.RUNTIME_FILES:
            shutil.copy2(actual / name, source / "scripts" / name)
        config = source / "scripts/reporting_config.py"
        config.write_text(config.read_text().replace('DEFAULT_REPORT_ENDPOINT = ""',
                                                     'DEFAULT_REPORT_ENDPOINT = "https://published.example/report"'))
        installed = telemetry_async.install_runtime(source, self.report_home)
        self.assertTrue(installed)
        copies = list((self.report_home / "runtime").rglob("reporting_config.py"))
        self.assertEqual(1, len(copies))
        self.assertEqual(config.read_bytes(), copies[0].read_bytes())
        config.write_text(config.read_text().replace("published.example", "updated.example"))
        telemetry_async.install_runtime(source, self.report_home)
        self.assertEqual(config.read_bytes(), copies[0].read_bytes())


if __name__ == "__main__":
    unittest.main()
