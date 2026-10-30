from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import feedback_submit as feedback


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "feedback"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"endpoint": "http://127.0.0.1:7860/feedback/report"}))
        patch = mock.patch.dict(os.environ, {"HELIXHARNESS_FEEDBACK_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def prepare(self, **kwargs):
        return feedback.prepare(self.repo, {"text": "流程卡住", "platform": "unknown", **kwargs})

    def test_missing_endpoint_does_not_capture_write_or_send(self):
        (self.home / "config.json").unlink()
        self.home.rmdir()
        with (mock.patch.object(feedback, "_build_document") as build,
              self.assertRaisesRegex(feedback.FeedbackError, "published default")):
            self.prepare()
        build.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_prepare_is_redacted_private_and_offline(self):
        with mock.patch.object(feedback, "_post_item") as post, mock.patch.object(feedback, "_collect_transcript") as capture:
            draft = self.prepare(text="请检查 token: abcdefghijklmnopqrstuvwxyz")
        post.assert_not_called()
        capture.assert_not_called()
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(draft))
        self.assertEqual("1.0.0", draft["item"]["document"]["plugin_version"])
        self.assertEqual([], list(self.repo.iterdir()))
        self.assertEqual(0o600, Path(draft["draft_path"]).stat().st_mode & 0o777)

    def test_prepare_session_hash_matches_ledger_format(self):
        """反馈的 session 必须与账本同一哈希实现，平台按精确等值关联工作流。"""
        from telemetry_ledger import _session_hash

        repo = self.repo

        class Adapter:
            def current_session_context(self, repo):
                return {"session_id": "raw-session-abc", "session_cwd": str(repo)}

        with mock.patch.object(feedback, "get_host_adapter", return_value=Adapter()):
            draft = self.prepare(platform="claude-code")
        document = draft["item"]["document"]
        self.assertEqual(_session_hash("raw-session-abc"), document["session"])
        # 旧 ses_<24hex> 格式已从 schema 移除，必须被 prepare 校验拒绝，防止格式回流
        document["session"] = "ses_" + "0123456789abcdef01234567"
        with self.assertRaisesRegex(feedback.FeedbackError, "violates schema"):
            feedback._validate_feedback_document(document)

    def test_send_requires_confirmation_and_unchanged_payload(self):
        draft = self.prepare()
        path = Path(draft["draft_path"])
        with mock.patch.object(feedback, "_post_item") as post:
            with self.assertRaisesRegex(feedback.FeedbackError, "confirmation"):
                feedback.send(path, draft["digest"])
            value = json.loads(path.read_text())
            value["endpoint"] = "https://other.example/feedback/report"
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(feedback.FeedbackError, "draft changed"):
                feedback.send(path, draft["digest"], confirmed=True)
            post.assert_not_called()

    def test_registered_future_host_can_prepare_feedback(self):
        adapter = mock.Mock()
        adapter.current_session_context.return_value = None
        with mock.patch.object(feedback, "get_host_adapter", return_value=adapter):
            draft = self.prepare(platform="future-host")
        self.assertEqual("future-host", draft["item"]["document"]["host"])

    def test_registered_host_without_telemetry_adapter_keeps_its_identity(self):
        draft = self.prepare(platform="opencode")
        self.assertEqual("opencode", draft["item"]["document"]["host"])
        self.assertEqual("unknown", draft["item"]["document"]["session"])

    def test_offline_queue_is_bound_to_original_endpoint(self):
        draft = self.prepare()
        with mock.patch.object(feedback, "_post_item", return_value="unreachable"):
            result = feedback.send(Path(draft["draft_path"]), draft["digest"], confirmed=True)
        self.assertEqual("queued", result["status"])
        self.assertEqual(0o600, Path(result["queue_path"]).stat().st_mode & 0o777)
        with mock.patch.object(feedback, "_post_item", return_value="accepted") as post:
            self.assertEqual(0, feedback._flush_outbox("https://other.example/feedback/report")["flushed"])
            post.assert_not_called()
            self.assertEqual(1, feedback._flush_outbox(draft["endpoint"])["flushed"])
        self.assertFalse(Path(result["queue_path"]).exists())
        self.assertEqual([], list(self.repo.iterdir()))

    def test_permanent_rejection_preserves_reviewed_draft(self):
        draft = self.prepare()
        with mock.patch.object(feedback, "_post_item", return_value="rejected"):
            result = feedback.send(Path(draft["draft_path"]), draft["digest"], confirmed=True)
        self.assertEqual("rejected", result["status"])
        self.assertTrue(Path(draft["draft_path"]).exists())
        self.assertFalse((self.home / "outbox").exists())

    def test_endpoint_and_feature_validation(self):
        for endpoint in ("https://user:pass@example.com/feedback/report", "file:///tmp/report", "https://example.com/report?token=abc"):
            with self.subTest(endpoint=endpoint), self.assertRaises(feedback.FeedbackError):
                feedback._validate_endpoint(endpoint)
        with self.assertRaisesRegex(feedback.FeedbackError, "feature must exist"):
            self.prepare(feature="missing")

    def test_sender_requires_matching_acknowledgement(self):
        item = self.prepare()["item"]
        for acknowledgement, expected in (
            (None, "unreachable"),
            ({"feedback_id": "wrong", "status": "accepted"}, "unreachable"),
            ({"feedback_id": item["feedback_id"], "status": "duplicate"}, "accepted"),
        ):
            response = mock.MagicMock()
            response.status = 200
            response.__enter__.return_value = response
            response.read.return_value = json.dumps({"items": [acknowledgement]}).encode()
            with mock.patch.object(feedback.urllib.request, "urlopen", return_value=response):
                self.assertEqual(expected, feedback._post_item("http://localhost/feedback/report", item))

    def test_current_transcript_excludes_thinking_and_tool_outputs(self):
        path = self.root / "current.jsonl"
        records = [
            {"type": "user", "message": {"content": "流程为什么卡住"}},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "PRIVATE_THINKING"},
                {"type": "text", "text": "token: SECRET_VALUE"},
                {"type": "text", "text": "-----BEGIN PRIVATE KEY-----\nPRIVATE_KEY_MATERIAL\n-----END PRIVATE KEY-----"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/home/demo/file"}},
            ]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "RAW_FILE_CONTENT"}]}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in records))
        capture = feedback._collect_transcript("claude-code", {"session_id": "current", "session_log_path": str(path)})
        self.assertTrue(capture["supported"])
        for raw in ("PRIVATE_THINKING", "RAW_FILE_CONTENT", "SECRET_VALUE", "PRIVATE_KEY_MATERIAL", "/home/demo/file"):
            self.assertNotIn(raw, capture["transcript_md"])
        self.assertEqual(1, capture["tool_calls"])
