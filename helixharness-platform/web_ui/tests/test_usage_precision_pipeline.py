"""Exercise real report receipts, frozen snapshots, materialization and dashboard HTML."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eval.telemetry_materialization import materialize_eval_log
from eval.tests.test_usage_loader import _workflow_log, _workflow_supplement
from eval.usage_loader import _resource_usage, aggregate_feature, render_feature_timeline_html
from web_ui import create_app


def _cursor_usage(**changes: object) -> dict:
    return {
        "collection_status": "collected", "reason": "native_generation_snapshots",
        "scope": "parent_only", "attribution": "complete_generations_within_stage",
        "input_tokens_include_cache": True, "generations": 1,
        "input_tokens": 150, "output_tokens": 10, "cache_read_tokens": 30, "cache_write_tokens": 20,
        **changes,
    }


@pytest.mark.parametrize("usage,total,status,note", [
    (_cursor_usage(), 160, "collected", "仅父代理，不含子代理用量"),
    (_cursor_usage(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0),
     0, "collected", "仅父代理，不含子代理用量"),
    (_cursor_usage(collection_status="partial", unavailable_reason="some_generations_unattributable"),
     160, "partial", "部分轮次跨越阶段边界"),
    (_cursor_usage(collection_status="partial", unavailable_reason="ambiguous_workspace"),
     160, "partial", "多个仓库同时处于活跃阶段，无法确定用量归属"),
    ({"scope": "parent_only", "collection_status": "unavailable", "unavailable_reason": "ambiguous_workspace"},
     None, "unavailable", "多个仓库同时处于活跃阶段，无法确定用量归属"),
    ({"scope": "parent_only", "attribution": "complete_generations_within_stage", "generations": 0,
      "collection_status": "unavailable", "unavailable_reason": "no_complete_generation_in_stage_window"},
     None, "unavailable", "阶段窗口内没有可完整归属的轮次"),
    ({"scope": "parent_only", "input_tokens": 10}, None, "unavailable", "Token 字段不完整"),
    ({"scope": "parent_only", "cache_read_tokens": 30}, None, "unavailable", "Token 字段不完整"),
])
def test_cursor_report_preserves_usage_precision_through_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage: dict, total: int | None, status: str, note: str,
) -> None:
    from eval import usage_sync

    root = tmp_path / "repo"
    path = root / "docs/eval/example-feature/eval-log.json"
    path.parent.mkdir(parents=True)
    ledger = _workflow_log()
    ledger["entries"][0]["platform"] = "cursor"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", str(root))
    monkeypatch.setattr(usage_sync, "schedule_usage_ai_regeneration", lambda roots: None)
    payload = _workflow_supplement()
    payload.update(platform="cursor", usage=usage, execution={
        "collection_status": "collected", "child_agent_scope": "parent_only",
        "tool_call_count": 0, "tool_error_count": 0, "subagent_spawn_count": 0,
    })
    with TestClient(create_app()) as client:
        # Receipt before Git discovery remains inactive; retry does not create another record.
        first = client.post("/report", json={"items": [payload]})
        assert first.json()["items"][0]["status"] == "accepted"
        assert first.json()["items"][0]["linked"] is False
        assert client.post("/report", json={"items": [payload]}).json()["items"][0]["status"] == "duplicate"
        store = client.app.state.stage_supplement_store
        assert store.refresh_links([root]) == 1
        records = store.active_linked_records()
        assert len(records) == 1
        for key, value in usage.items():
            assert records[0].payload["usage"][key] == value
        assert records[0].payload["execution"]["child_agent_scope"] == "parent_only"
        materialized = materialize_eval_log(ledger, {payload["entry_id"]: records[0]})
        entry = materialized["entries"][0]
        resource = _resource_usage(entry)
        assert resource["collection_status"] == status
        assert resource["total_tokens"] == total
        assert resource["usage_scope"] == "parent_only"
        assert entry["async_evidence"]["resource_usage"]["scope"] == "parent_only"
        summary = aggregate_feature(materialized)["resource_usage"]
        assert summary["exact"] is False
        assert summary["total_tokens"] == total
        assert summary["exact_total_tokens"] is None
        timeline = render_feature_timeline_html(materialized)
        assert note in timeline
        assert "工具调用 <b>0 次</b>" in timeline
        assert "Token 数据完整" not in timeline
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert note in dashboard.text
        assert "Cursor" in dashboard.text


def test_three_host_report_rollup_keeps_legacy_totals_and_cursor_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import usage_sync

    root = tmp_path / "repo"
    path = root / "docs/eval/example-feature/eval-log.json"
    path.parent.mkdir(parents=True)
    ledger = _workflow_log()
    template = ledger["entries"][0]
    ledger["entries"] = []
    payloads = []
    for index, host in enumerate(("claude-code", "codex", "cursor")):
        entry_id = f"entry-{index:016x}"
        ledger["entries"].append({**template, "entry_id": entry_id, "platform": host, "attempt": index + 1})
        payload = _workflow_supplement()
        payload.update(entry_id=entry_id, supplement_id=f"supp_{index:016x}", platform=host)
        payload["usage"] = _cursor_usage() if host == "cursor" else {
            "input_tokens": 20, "output_tokens": 10, "cached_input_tokens": 4,
        }
        payloads.append(payload)
    path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", str(root))
    monkeypatch.setattr(usage_sync, "schedule_usage_ai_regeneration", lambda roots: None)
    with TestClient(create_app()) as client:
        receipt = client.post("/report", json={"items": payloads})
        assert [item["status"] for item in receipt.json()["items"]] == ["accepted"] * 3
        store = client.app.state.stage_supplement_store
        assert store.refresh_links([root]) == 3
        records = {record.payload["entry_id"]: record for record in store.active_linked_records()}
        materialized = materialize_eval_log(ledger, records)
        for entry in materialized["entries"][:2]:
            assert entry["resource_usage"]["attribution"] == "exact_session_window"
            assert entry["resource_usage"]["total_tokens"] == 30
        summary = aggregate_feature(materialized)["resource_usage"]
        assert summary["total_tokens"] == 220
        assert summary["cache_read_tokens"] == 38
        assert summary["collection_status"] == "partial"
        assert summary["collected_entries"] == 3
        assert summary["partial_entries"] == 1
        assert summary["exact_total_tokens"] is None
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "仅父代理，不含子代理用量" in dashboard.text
        for label in ("Claude Code", "Codex", "Cursor"):
            assert label in dashboard.text


@pytest.mark.parametrize("status", ["unavailable", "pending", "partial"])
def test_execution_collection_gap_does_not_render_placeholder_zero_as_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    from eval import usage_sync

    root = tmp_path / "repo"
    path = root / "docs/eval/example-feature/eval-log.json"
    path.parent.mkdir(parents=True)
    ledger = _workflow_log()
    template = ledger["entries"][0]
    ledger["entries"] = []
    payloads = []
    for index in range(2):
        entry_id = f"entry-{index:016x}"
        ledger["entries"].append({**template, "entry_id": entry_id, "platform": "cursor", "attempt": index + 1})
        payload = _workflow_supplement()
        payload.update(entry_id=entry_id, supplement_id=f"supp_{index:016x}", platform="cursor")
        payload["usage"] = {
            "scope": "parent_only", "collection_status": "unavailable", "unavailable_reason": "ambiguous_workspace",
        }
        payload["execution"] = {
            "tool_call_count": 0, "tool_error_count": 0, "subagent_spawn_count": 0,
            "collection_status": status, "unavailable_reason": "ambiguous_workspace",
        }
        payloads.append(payload)
    path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", str(root))
    monkeypatch.setattr(usage_sync, "schedule_usage_ai_regeneration", lambda roots: None)
    with TestClient(create_app()) as client:
        receipt = client.post("/report", json={"items": payloads})
        assert [item["status"] for item in receipt.json()["items"]] == ["accepted"] * 2
        store = client.app.state.stage_supplement_store
        assert store.refresh_links([root]) == 2
        records = {record.payload["entry_id"]: record for record in store.active_linked_records()}
        materialized = materialize_eval_log(ledger, records)
        assert all(entry["execution_stats"]["collection_status"] == status for entry in materialized["entries"])
        timeline = render_feature_timeline_html(materialized)
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        for html in (timeline, dashboard.text):
            for title in ("工具调用", "调用未成功", "子 Agent 创建"):
                if status == "partial":
                    assert html.count(f"{title}（已记录小计） <b>0 次</b>") >= 3
                else:
                    assert html.count(f"{title} <b>未采集</b>") >= 3
                assert f"{title} <b>0 次</b>" not in html
