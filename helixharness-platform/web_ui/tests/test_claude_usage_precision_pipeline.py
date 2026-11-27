"""Synthetic Claude source usage survives the real plugin-to-dashboard boundary."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eval.telemetry_materialization import materialize_eval_log
from eval.tests.test_usage_loader import _workflow_log, _workflow_supplement
from eval.usage_loader import _resource_usage, aggregate_feature, render_feature_timeline_html
from web_ui import create_app


def _parse_supplement(source: Path, ledger: dict) -> dict:
    """Use separate interpreters so plugin modules never leak into platform imports."""
    scripts = Path(__file__).resolve().parents[3] / "helixharness/scripts"
    if not scripts.is_dir():
        pytest.skip("cross-package acceptance requires the sibling plugin checkout")
    code = """
import json
import sys
from pathlib import Path
from telemetry_hosts.supplement_parsers import collect_claude_window
from telemetry_protocol import build_evolution_evidence, build_stage_supplement

source, ledger = json.load(sys.stdin)
entry = ledger["entries"][0]
window = collect_claude_window(
    Path(source), started_at=entry["started_at"], ended_at=entry["ended_at"],
    max_bytes=Path(source).stat().st_size,
)
fixed = {**entry, "requirement_id": ledger["requirement"]["id"],
         "requirement_name": ledger["requirement"]["name"], "feature": ledger["feature"]}
payload = build_stage_supplement(
    fixed, session_hash=entry["session_hash"], usage=window.usage, execution={},
    evidence=build_evolution_evidence(
        window.events, started_at=entry["started_at"], ended_at=entry["ended_at"],
    ),
)
print(json.dumps(payload))
"""
    env = {**os.environ, "PYTHONPATH": str(scripts), "PYTHONNOUSERSITE": "1",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
           "CLAUDE_CONFIG_DIR": str(source.parent / "synthetic-claude"),
           "HELIXHARNESS_TELEMETRY_HOME": str(source.parent / "synthetic-telemetry"),
           "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code], input=json.dumps([str(source), ledger]),
        cwd=scripts, env=env, check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout)


def _assert_report_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: dict, payload: dict, total: int | None,
) -> None:
    from eval import usage_sync

    root = tmp_path / "repo"
    path = root / "docs/eval/example-feature/eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", str(root))
    monkeypatch.setattr(usage_sync, "schedule_usage_ai_regeneration", lambda roots: None)
    with TestClient(create_app()) as client:
        receipt = client.post("/report", json={"items": [payload]})
        assert receipt.json()["items"][0]["status"] == "accepted"
        store = client.app.state.stage_supplement_store
        assert store.refresh_links([root]) == 1
        records = store.active_linked_records()
        assert len(records) == 1
        assert records[0].payload["usage"] == payload["usage"]
        materialized = materialize_eval_log(ledger, {payload["entry_id"]: records[0]})
        usage = _resource_usage(materialized["entries"][0])
        assert usage["total_tokens"] == total
        assert usage["collection_status"] == ("collected" if total is not None else "unavailable")
        summary = aggregate_feature(materialized)["resource_usage"]
        assert summary["total_tokens"] == total
        assert summary["exact"] is (total is not None)
        assert summary["exact_total_tokens"] == total
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        for html in (render_feature_timeline_html(materialized), dashboard.text):
            assert "Claude Code" in html
            if total is None:
                assert "Token 数据完整" not in html
                assert "Token 不可用" in html
            else:
                assert "Token 数据完整 1/1 条记录" in html
                assert f"输入 {payload['usage']['input_tokens']}" in html
                assert f"输出 {payload['usage']['output_tokens']}" in html


@pytest.mark.parametrize("raw_usages,expected,total", [
    ([{"input_tokens": 0, "output_tokens": 10}], {"input_tokens": 0, "output_tokens": 10}, 10),
    ([{"input_tokens": 10, "output_tokens": 0}], {"input_tokens": 10, "output_tokens": 0}, 10),
    ([{"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}],
     {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}, 0),
    ([{"input_tokens": 0, "output_tokens": 10}, {"input_tokens": 0, "output_tokens": 5}],
     {"input_tokens": 0, "output_tokens": 15}, 15),
    ([{"input_tokens": 10}], {"input_tokens": 10}, None),
    ([{"output_tokens": 10}], {"output_tokens": 10}, None),
    ([{"input_tokens": 10}, {"output_tokens": 20}], {}, None),
    ([{"input_tokens": 10, "output_tokens": 20}, None], {}, None),
    ([{"input_tokens": True, "output_tokens": 10}], {"output_tokens": 10}, None),
    ([{"input_tokens": -1, "output_tokens": 10}], {"output_tokens": 10}, None),
    ([None], {}, None),
])
def test_claude_source_parser_to_supplement_to_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_usages: list, expected: dict, total: int | None,
) -> None:
    ledger = _workflow_log()
    ledger["entries"][0]["platform"] = "claude-code"
    source = tmp_path / "synthetic-session.jsonl"
    records = [{"type": "assistant", "timestamp": "2026-08-01T00:01:00Z",
                "message": {"content": "Synthetic answer", "usage": usage}} for usage in raw_usages]
    # Missing outside-window records neither supply nor invalidate stage counts.
    records.extend({"type": "assistant", "timestamp": timestamp, "message": {"content": "Outside"}}
                   for timestamp in ("2026-07-31T23:59:59Z", "2026-08-01T00:10:01Z"))
    source.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    payload = _parse_supplement(source, ledger)
    assert payload["usage"] == expected
    _assert_report_usage(tmp_path, monkeypatch, ledger, payload, total)


@pytest.mark.parametrize("legacy_usage", [{"input_tokens": 10}, {"output_tokens": 10}, {}])
def test_legacy_supplement_omitted_zero_cannot_be_distinguished_from_missing_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_usage: dict,
) -> None:
    """An old lossy payload cannot justify imputing an unreported primary count."""
    ledger = _workflow_log()
    ledger["entries"][0]["platform"] = "claude-code"
    payload = _workflow_supplement()
    payload.update(platform="claude-code", usage=legacy_usage)
    _assert_report_usage(tmp_path, monkeypatch, ledger, payload, None)
