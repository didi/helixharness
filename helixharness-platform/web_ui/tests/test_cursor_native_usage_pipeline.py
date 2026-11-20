"""Synthetic native Cursor hooks reach the real plugin outbox and platform UI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eval.telemetry_materialization import materialize_eval_log
from eval.usage_loader import _resource_usage, aggregate_feature, render_feature_timeline_html
from web_ui import create_app


def _native_stage(root: Path, native_usage: dict | None) -> tuple[Path, dict, dict]:
    """Keep plugin imports separate and obtain unmodified CLI-produced artifacts."""
    plugin = Path(__file__).resolve().parents[3] / "helixharness"
    if not (plugin / "scripts/metrics.py").is_file():
        pytest.skip("cross-package acceptance requires the sibling plugin checkout")
    root.mkdir()
    code = r'''
import gzip, json, subprocess, sys
from pathlib import Path

plugin, root, native_usage = json.load(sys.stdin)
plugin, root = Path(plugin), Path(root)
repo = root / "synthetic Cursor project"
repo.mkdir()
telemetry = root / "telemetry"
telemetry.mkdir()
(telemetry / "config.json").write_text('{"endpoint":""}')
def run(arguments, *, payload=None, env=None):
    result = subprocess.run(list(map(str, arguments)), cwd=repo, env=env,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None
def cli(script, *args, env=None):
    return run([sys.executable, plugin / "scripts" / script, *args], env=env)
hooks = json.loads((plugin / "hosts/cursor/hooks.json").read_text())["hooks"]
identity = {"conversation_id":"synthetic-native-pipeline", "workspace_roots":[str(repo)], "cwd":str(repo)}
def hook(event, **fields):
    return run(["sh", "-c", hooks[event][0]["command"]],
        payload={**identity, "hook_event_name":event, **fields})
run(["git", "init", "-q", repo])
started = hook("sessionStart")
import os
bound = {**os.environ, **started["env"]}
notice = cli("user_config.py", "--repo-root", repo, "--platform", "cursor",
    "--telemetry", "enabled", "--scope", "project", "--user-confirmed")
assert notice["read_only"] and notice["notice_required"] and not notice["change"]["updated"]
assert notice["telemetry"]["effective_mode"] == "disabled"
# Synthetic protocol acknowledgment; native UI display is a separate acceptance.
configured = cli("user_config.py", "--repo-root", repo, "--platform", "cursor",
    "--telemetry", "enabled", "--scope", "project", "--user-confirmed",
    "--telemetry-notice-shown", notice["notice"]["sha256"])
assert configured["change"]["updated"]
cli("user_config.py", "--repo-root", repo, "--platform", "cursor",
    "--report-endpoint", "https://telemetry.example.test/report", "--user-confirmed")
entry = cli("metrics.py", "--repo-root", repo, "--platform", "cursor", "start",
    "--feature", "native-cursor", "--stage", "apply", "--trigger", "user_command", env=bound)
hook("beforeSubmitPrompt", generation_id="synthetic-generation", prompt="Synthetic request")
fields = {} if native_usage is None else native_usage
hook("afterAgentResponse", generation_id="synthetic-generation", text="Synthetic answer", **fields)
hook("stop", generation_id="synthetic-generation", **fields)
finished = cli("metrics.py", "--repo-root", repo, "--platform", "cursor", "finish",
    "--feature", "native-cursor", "--stage", "apply", "--status", "completed", env=bound)
assert finished["entry_id"] == entry["entry_id"]
collection = cli("telemetry_collector.py", "--repo-root", repo)
assert collection["collected"] == 1, collection
files = list((telemetry / "outbox").glob("*.json.gz"))
assert len(files) == 1
envelope = json.loads(gzip.decompress(files[0].read_bytes()))
assert envelope["attempt_count"] == 0
assert not (telemetry / "scheduler").exists()
assert cli("telemetry_collector.py", "--repo-root", repo)["collected"] == 0
ledger = json.loads((repo / "docs/eval/native-cursor/eval-log.json").read_text())
print(json.dumps({"repo":str(repo), "ledger":ledger, "supplement":envelope["supplement"]}))
'''
    environment = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "TMPDIR": str(root), "CURSOR_PLUGIN_ROOT": str(plugin),
        "CODEX_HOME": str(root / "codex"), "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "HELIXHARNESS_CURSOR_HOME": str(root / "cursor"),
        "HELIXHARNESS_TELEMETRY_HOME": str(root / "telemetry"),
        "HELIXHARNESS_FEEDBACK_HOME": str(root / "feedback"),
        "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=root, env=environment,
        input=json.dumps([str(plugin), str(root), native_usage]),
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    artifacts = json.loads(result.stdout)
    return Path(artifacts["repo"]), artifacts["ledger"], artifacts["supplement"]


@pytest.mark.parametrize("native_usage,total,status", [
    ({"input_tokens": 150, "output_tokens": 10, "cache_read_tokens": 30, "cache_write_tokens": 20}, 160, "collected"),
    ({"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}, 0, "collected"),
    (None, None, "unavailable"),
], ids=["complete-native-tokens", "real-native-zero", "native-usage-absent"])
def test_native_cursor_hook_to_local_outbox_to_platform_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_usage: dict | None, total: int | None, status: str,
) -> None:
    from eval import usage_sync

    repo, ledger, payload = _native_stage(tmp_path / "plugin-cli", native_usage)
    assert payload["platform"] == "cursor"
    assert payload["usage"]["collection_status"] == status
    assert payload["usage"]["scope"] == "parent_only"
    assert payload["usage"].get("total_tokens") == total
    assert "synthetic-native-pipeline" not in json.dumps(ledger)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", str(repo))
    monkeypatch.setattr(usage_sync, "schedule_usage_ai_regeneration", lambda roots: None)
    with TestClient(create_app()) as client:
        receipt = client.post("/report", json={"items": [payload]})
        assert receipt.status_code == 200
        assert receipt.json()["items"][0]["status"] == "accepted"
        assert receipt.json()["items"][0]["linked"] is False
        retry = client.post("/report", json={"items": [payload]})
        assert retry.json()["items"][0]["status"] == "duplicate"
        store = client.app.state.stage_supplement_store
        assert store.refresh_links([repo]) == 1
        records = store.active_linked_records()
        assert len(records) == 1
        assert records[0].payload["usage"] == payload["usage"]
        materialized = materialize_eval_log(ledger, {payload["entry_id"]: records[0]})
        usage = _resource_usage(materialized["entries"][0])
        assert usage["collection_status"] == status
        assert usage["total_tokens"] == total
        assert usage["usage_scope"] == "parent_only"
        summary = aggregate_feature(materialized)["resource_usage"]
        assert summary["total_tokens"] == total
        assert summary["exact"] is False
        assert summary["exact_total_tokens"] is None
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        for html in (render_feature_timeline_html(materialized), dashboard.text):
            assert "Cursor" in html
            assert "仅父代理，不含子代理用量" in html
            assert "Token 数据完整" not in html
            if total is None:
                assert "Token 不可用" in html
                assert "阶段窗口内没有可完整归属的轮次" in html
            else:
                assert f"输入 {native_usage['input_tokens']}" in html
                assert f"输出 {native_usage['output_tokens']}" in html
