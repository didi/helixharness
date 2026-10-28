from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from web_ui.main import create_app


def _item():
    return {"schema": "helixharness-feedback/1.0", "feedback_id": "fbk-0123456789abcdef",
            "document": {"schema_version": "1.0", "feedback_id": "fbk-0123456789abcdef",
                         "created_at": "2026-09-11T00:00:00Z", "text": "流程卡住 <script>alert(1)</script>",
                         "category": "process_stuck", "plugin_version": "1.0.0", "host": "codex",
                         "os": "test", "python_version": "3.10.0", "user_name": None,
                         "project": "prj_0123456789abcdef", "session": "unknown", "feature": None,
                         "current_command": "feedback", "git_head": None, "related_entry_ids": []}}


def test_report_replay_conflict_invalid_sibling_and_escaped_pages():
    with TestClient(create_app()) as client:
        item = _item()
        response = client.post("/feedback/report", content=gzip.compress(json.dumps({"items": [item, {}]}).encode()),
                               headers={"Content-Encoding": "gzip"})
        assert [row["status"] for row in response.json()["items"]] == ["accepted", "rejected"]
        replay = client.post("/feedback/report", json={"items": [item]})
        assert replay.json()["items"][0]["status"] == "duplicate"
        item["document"]["text"] = "changed"
        assert client.post("/feedback/report", json={"items": [item]}).json()["items"][0]["error"]["code"] == "feedback_id_conflict"
        overview = client.get("/feedback")
        assert overview.status_code == 200
        assert "发布方统一提供" in overview.text and "~/.helixharness/feedback/config.json" in overview.text
        assert "流程卡住" in overview.text and "<script>alert(1)</script>" not in overview.text
        detail = client.get("/feedback/detail/fbk-0123456789abcdef")
        assert detail.status_code == 200 and "&lt;script&gt;" in detail.text
        assert "暂无反馈" in client.get("/feedback?category=bug").text
        assert client.get("/feedback/detail/missing").status_code == 404
        assert client.post("/feedback/report", content=b"invalid").status_code == 400


def test_plugin_draft_to_platform_receipt(tmp_path, monkeypatch):
    """Exercise the real plugin serializer and platform receipt without external HTTP."""
    scripts = Path(__file__).resolve().parents[3] / "helixharness/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("acceptance_feedback_submit", scripts / "feedback_submit.py")
    reporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reporter)
    monkeypatch.setenv("HELIXHARNESS_FEEDBACK_HOME", str(tmp_path / "feedback"))
    (tmp_path / "feedback").mkdir()
    (tmp_path / "feedback/config.json").write_text('{"endpoint":"http://testserver/feedback/report"}')
    repo = tmp_path / "repo"
    repo.mkdir()
    with TestClient(create_app()) as client:
        class Reply:
            status = 200

            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return self.response.content

        def transport(request, timeout):
            assert request.full_url == "http://testserver/feedback/report"
            return Reply(client.post("/feedback/report", content=request.data, headers=dict(request.header_items())))

        draft = reporter.prepare(repo, {"text": "Synthetic feedback from plugin", "platform": "unknown"})
        with mock.patch.object(reporter.urllib.request, "urlopen", side_effect=transport):
            result = reporter.send(Path(draft["draft_path"]), draft["digest"], confirmed=True)
        assert result["status"] == "delivered"
        assert "Synthetic feedback from plugin" in client.get("/feedback").text
        assert list(repo.iterdir()) == []
    client_schema = scripts.parent / "reference/feedback-schema-v1.json"
    platform_schema = Path(__file__).resolve().parents[2] / "eval/schemas/feedback-schema-v1.json"
    assert json.loads(client_schema.read_text()) == json.loads(platform_schema.read_text())


def test_report_accepts_future_host_identity_and_rejects_invalid_ids():
    with TestClient(create_app()) as client:
        item = _item()
        item["document"]["host"] = "future-host"
        receipt = client.post("/feedback/report", json={"items": [item]})
        assert receipt.json()["items"][0]["status"] == "accepted"
        stored = client.app.state.feedback_store.get(item["feedback_id"])
        assert stored["document"]["host"] == "future-host"
        for invalid in ("../future-host", "Future Host", "", "a" * 65, "future-host\n"):
            item["document"]["host"] = invalid
            receipt = client.post("/feedback/report", json={"items": [item]})
            assert receipt.json()["items"][0]["error"]["code"] == "schema_violation"
