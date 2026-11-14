"""Verify documented launch commands over real localhost HTTP, without model calls."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import httpx
import pytest

# All requests in this fixture are to a synthetic localhost server. A user's
# SOCKS/HTTP proxy must neither route them elsewhere nor require extra extras.
urlopen = build_opener(ProxyHandler({})).open


def _wait_for_startup(process, base, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail("Server did not become ready within startup deadline")
        assert process.poll() is None, "Server exited before serving HTTP"
        try:
            with urlopen(base, timeout=min(1.0, remaining)) as response:
                return response.read().decode()
        except (URLError, TimeoutError):
            # A listening socket does not imply the first page is ready yet.
            # Retry response timeouts under the same bounded startup deadline.
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))


@pytest.mark.parametrize("mode", ["auto", "demo", "sync", "full-demo"])
def test_documented_server_modes_start_and_serve_pages(tmp_path, mode):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [sys.executable, "-m", "web_ui.demo" if mode == "full-demo" else "web_ui.main", "--port", str(port)]
    if mode == "full-demo":
        command += ["--data-dir", str(tmp_path / "demo")]
    else:
        command += ["--host", "127.0.0.1", "--usage-data-source", mode]
    env = os.environ.copy()
    env["HELIXHARNESS_PLATFORM_USAGE_AI_ENABLED"] = "false"
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = _wait_for_startup(process, base)
        if mode == "sync":
            assert "暂无统计数据" in page
            assert "Demo project (synthetic)" not in page
        else:
            assert "Demo project (synthetic)" in page
            assert "72.0k" in page
        for path in ("/config", "/feedback", "/framework-evaluation", "/framework-evaluation/cases",
                     "/framework-evaluation/config", "/framework-evaluation/rules", "/framework-evaluation/api/health",
                     "/evolution/summary", "/evolution/hypotheses", "/evolution/experiments", "/evolution/config"):
            with urlopen(base + path, timeout=10) as response:
                assert response.status == 200, path
        if mode == "auto":
            _verify_sync_reporting_and_feedback(base, tmp_path)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _verify_sync_reporting_and_feedback(base, tmp_path):
    """Use a synthetic Git remote and genuine HTTP receipts, without mocks."""
    from eval.demo import _workflow_supplement, seed_demo
    from web_ui.tests.test_feedback_report import _item

    repository = tmp_path / "synthetic-remote"
    source_database = tmp_path / "source-supplements.db"
    seed_demo(repository, source_database)
    (repository / "docs/eval").rename(repository / "telemetry")
    for args in (("init", "-q", "-b", "master"), ("add", "."), ("commit", "-qm", "Synthetic ledger")):
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
        response = client.post("/config/add-repo", data={
            "name": "demo", "url": repository.as_uri(), "branch": "master",
            "eval_dir": "telemetry", "enabled": "true",
        })
        assert response.status_code == 200 and "synthetic-remote" in response.text
        assert "暂无统计数据" in client.get("/").text  # Configured but not synced: no demo fallback.
        assert client.post("/config/sync").status_code == 200
        for _ in range(100):
            state = client.get("/config/task-status/sync").text
            if 'hx-trigger="every 3s"' not in state:
                break
            time.sleep(0.1)
        assert "同步完成" in state
        page = client.get("/").text
        assert "Demo project (synthetic)" in page and "当前看板展示自动生成的示例数据" not in page
        payloads = []
        for index in range(1, 4):
            payload = _workflow_supplement()
            payload.update(supplement_id=f"supp_{index:016x}", entry_id=f"entry-{index:016x}")
            payload["usage"] = {"input_tokens": 10000 * index, "output_tokens": 2000 * index,
                                "cached_input_tokens": 1000 * index, "model": "demo-model"}
            payloads.append(payload)
        receipt = client.post("/report", json={"items": payloads}).json()
        assert len(receipt["items"]) == 3
        assert all(item["status"] == "accepted" for item in receipt["items"]), receipt
        replay = client.post("/report", json={"items": payloads}).json()
        assert all(item["status"] == "duplicate" for item in replay["items"])
        assert "72.0k" in client.get("/").text
        feedback = _item()
        feedback["document"]["text"] = "Synthetic localhost feedback acceptance"
        assert client.post("/feedback/report", json={"items": [feedback]}).json()["items"][0]["status"] == "accepted"
        assert client.post("/feedback/report", json={"items": [feedback]}).json()["items"][0]["status"] == "duplicate"
        assert feedback["document"]["text"] in client.get("/feedback/detail/" + feedback["feedback_id"]).text
        assert client.post("/config/delete-repo", data={"name": "demo"}).status_code == 200
        assert client.post("/config/sync").status_code == 200
        for _ in range(100):
            state = client.get("/config/task-status/sync").text
            if 'hx-trigger="every 3s"' not in state:
                break
            time.sleep(0.1)
        assert "同步完成" in state
        assert "暂无统计数据" in client.get("/").text


def test_startup_wait_retries_connection_and_response_timeouts(monkeypatch):
    from unittest.mock import MagicMock, Mock

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"synthetic ready page"
    opener = Mock(side_effect=[URLError("not listening"), TimeoutError("not ready"), response])
    monkeypatch.setitem(globals(), "urlopen", opener)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert _wait_for_startup(Mock(poll=Mock(return_value=None)), "http://synthetic.invalid") == "synthetic ready page"
    assert opener.call_count == 3


def test_startup_wait_has_a_deadline(monkeypatch):
    from unittest.mock import Mock

    opener = Mock()
    monkeypatch.setitem(globals(), "urlopen", opener)
    with pytest.raises(pytest.fail.Exception, match="deadline"):
        _wait_for_startup(Mock(), "http://synthetic.invalid", timeout=0)
    opener.assert_not_called()


def test_startup_wait_reports_an_exited_server_before_http():
    from unittest.mock import Mock

    with pytest.raises(AssertionError, match="exited"):
        _wait_for_startup(Mock(poll=Mock(return_value=1)), "http://synthetic.invalid")
