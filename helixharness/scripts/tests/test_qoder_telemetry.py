"""Qoder telemetry adapter: session context, source validation and window parsing."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from telemetry_hosts import get_host_adapter, supported_host_ids  # noqa: E402
from telemetry_hosts.qoder import QoderAdapter  # noqa: E402
from telemetry_hosts.supplement_parsers import collect_qoder_window  # noqa: E402


def _transcript(records: list[dict]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


class QoderTelemetryTests(unittest.TestCase):
    def test_adapter_is_registered(self):
        self.assertIn("qoder", supported_host_ids())
        adapter = get_host_adapter("qoder")
        self.assertIsNotNone(adapter)
        self.assertEqual("qoder", adapter.host_id)
        self.assertTrue(adapter.parser_version.startswith("lite-qoder/"))

    def test_consent_cache_lives_in_the_qoder_config_home(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"QODERCN_CONFIG_DIR": directory}):
                path = QoderAdapter().consent_cache_path()
            expected = (
                Path(directory) / "helixharness" / "telemetry-consent-cache.json"
            ).resolve()
            self.assertEqual(expected, path.resolve())

    def test_session_source_must_be_a_project_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects" / "-demo-repo"
            projects.mkdir(parents=True)
            session = projects / "session-1.jsonl"
            session.write_text('{"cwd": "%s"}\n' % root, encoding="utf-8")
            outside = root / "elsewhere.jsonl"
            outside.write_text("{}\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"QODERCN_CONFIG_DIR": directory}):
                adapter = QoderAdapter()
                self.assertEqual(str(session.resolve()), adapter.validate_session_source(session))
                self.assertIsNone(adapter.validate_session_source(outside))
                self.assertIsNone(adapter.validate_session_source(projects))
                self.assertIsNone(adapter.validate_session_source(""))

    def test_current_session_context_uses_the_hook_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            projects = root / "config" / "projects" / "-repo"
            projects.mkdir(parents=True)
            session = projects / "abc-123.jsonl"
            session.write_text(_transcript([{"type": "user", "cwd": str(repo)}]), encoding="utf-8")
            marker = repo / ".helixharness" / "host-session" / "qoder.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(
                json.dumps(
                    {
                        "session_id": "abc-123",
                        "session_cwd": str(repo),
                        "transcript_path": str(session),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"QODERCN_CONFIG_DIR": str(root / "config")}):
                context = QoderAdapter().current_session_context(repo)
            self.assertIsNotNone(context)
            self.assertEqual("abc-123", context["session_id"])
            self.assertEqual(str(session.resolve()), context["session_log_path"])

    def test_marker_for_another_repo_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            marker = repo / ".helixharness" / "host-session" / "qoder.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(
                json.dumps({"session_id": "x", "session_cwd": str(root)}), encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"QODERCN_CONFIG_DIR": str(root)}, clear=False):
                os.environ.pop("HELIXHARNESS_LITE_SESSION_ID", None)
                self.assertIsNone(QoderAdapter().current_session_context(repo))

    def test_window_parser_ignores_qoder_specific_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                _transcript(
                    [
                        {"type": "workspace-directories", "sessionId": "s", "directories": ["/r"]},
                        {"type": "runtime-config", "sessionId": "s", "model": "auto", "timestamp": 1793611325467},
                        {"type": "active-leaf", "timestamp": "2026-11-03T02:00:00.000Z", "leafUuid": "u"},
                        {
                            "type": "user",
                            "timestamp": "2026-11-03T02:00:01.000Z",
                            "message": {"role": "user", "content": "do the work"},
                        },
                        {
                            "type": "assistant",
                            "timestamp": "2026-11-03T02:00:02.000Z",
                            "message": {
                                "role": "assistant",
                                "model": "auto",
                                "content": [
                                    {"type": "thinking", "thinking": "internal"},
                                    {"type": "text", "text": "running"},
                                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
                                ],
                            },
                        },
                        {
                            "type": "user",
                            "timestamp": "2026-11-03T02:00:03.000Z",
                            "message": {
                                "role": "user",
                                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )
            window = collect_qoder_window(
                path,
                started_at="2026-11-03T00:00:00.000Z",
                ended_at="2026-11-03T03:00:00.000Z",
            )
            kinds = [event["type"] for event in window.events]
            self.assertEqual(["user", "assistant", "tool"], kinds)
            tool = window.events[-1]
            self.assertEqual("Bash", tool["name"])
            self.assertEqual("completed", tool["status"])
            # Qoder records carry no token usage; the adapter must not invent it.
            self.assertEqual({"models": ["auto"], "model": "auto"}, window.usage)


if __name__ == "__main__":
    unittest.main()
