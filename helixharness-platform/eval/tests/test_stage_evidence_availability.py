"""Delivered stage facts must not masquerade as a collected conversation."""
from __future__ import annotations

import json

import pytest

from eval.stage_supplements import EVIDENCE_SCHEMA, LEDGER_SCHEMA, StageSupplementStore
from eval.telemetry_materialization import materialize_eval_log
from eval.usage_loader import _execution_stats, _resource_usage, _user_interactions, render_feature_timeline_html


def _linked(tmp_path, evidence, *, usage=None, execution=None):
    repo = tmp_path / "repo"
    path = repo / "docs/eval/synthetic/eval-log.json"
    path.parent.mkdir(parents=True)
    ledger = {
        "schema": LEDGER_SCHEMA, "feature": "synthetic", "status": "active",
        "requirement": {"id": "req_synthetic", "name": "synthetic"},
        "entries": [{
            "entry_id": "entry-synthetic", "platform": "opencode", "command": "propose",
            "stage_kind": "requirement", "status": "completed",
            "started_at": "2026-11-26T00:00:00Z", "ended_at": "2026-11-26T00:01:00Z",
        }],
    }
    path.write_text(json.dumps(ledger), encoding="utf-8")
    payload = {
        "schema": "helixharness-stage-supplement/1.0", "supplement_id": "supp_synthetic",
        "revision": 1, "supersedes": None, "entry_id": "entry-synthetic",
        "requirement_id": "req_synthetic", "requirement_name": "synthetic", "feature": "synthetic",
        "session_hash": "session-synthetic", "platform": "opencode",
        "usage": usage or {}, "execution": execution or {}, "evidence": evidence,
    }
    store = StageSupplementStore(tmp_path / "supplements.db")
    assert store.ingest(payload)["status"] == "accepted"
    assert store.refresh_links([repo]) == 1
    record, = store.active_linked_records()
    return record, materialize_eval_log(ledger, {"entry-synthetic": record})


def _unavailable(**extra):
    return {"schema": EVIDENCE_SCHEMA, "status": "unavailable",
            "reason_code": "host_transcript_not_collected", **extra}


def test_final_snapshot_keeps_unavailable_state_without_private_fields(tmp_path):
    record, _ = _linked(tmp_path, _unavailable(private_note="synthetic-private-marker"))
    assert record.payload["evidence"] == _unavailable()
    assert "synthetic-private-marker" not in json.dumps(record.payload)


def test_unrecognized_reason_is_not_copied_into_public_snapshot(tmp_path):
    record, projected = _linked(tmp_path, _unavailable(reason_code="/synthetic/private/location"))
    assert record.payload["evidence"] == {"schema": EVIDENCE_SCHEMA, "status": "unavailable"}
    assert projected["entries"][0]["collection"]["semantic_status"] == "unavailable"
    assert "/synthetic/private/location" not in json.dumps(projected)


@pytest.mark.parametrize("summary", [None, {"user_turns": 12, "assistant_messages": 34}])
def test_unavailable_session_never_claims_exact_or_collected_messages(tmp_path, summary):
    evidence = _unavailable()
    if summary is not None:
        evidence["interaction_summary"] = summary
    _, projected = _linked(tmp_path, evidence)
    entry, = projected["entries"]
    collection = entry["collection"]
    assert entry["status"] == "completed"
    assert entry["telemetry_status"] == "complete"  # Delivery is complete, the transcript is not.
    assert collection["scope"] == "none"
    assert collection["source_log_count"] == 0
    assert collection["user_turn_count"] is None
    assert collection["messages_scanned"] is None
    assert collection["confidence"] == "unknown"
    assert collection["semantic_status"] == "unavailable"
    assert collection["unavailable_reason"] == "host_transcript_not_collected"
    assert entry["async_evidence"]["interaction_summary"] == {}
    assert entry["friction"]["classification_source"] == "unavailable"
    assert _user_interactions([entry])["total"] is None
    assert _resource_usage(entry)["total_tokens"] is None
    assert _execution_stats(entry)["tool_error_count"] is None


@pytest.mark.parametrize("total", [0, 30])
def test_session_unavailability_preserves_independently_observed_usage(tmp_path, total):
    _, projected = _linked(tmp_path, _unavailable(), usage={"input_tokens": total, "output_tokens": 0},
                           execution={"tool_error_count": 0})
    entry, = projected["entries"]
    assert entry["collection"]["semantic_status"] == "unavailable"
    assert _resource_usage(entry)["total_tokens"] == total
    assert _execution_stats(entry)["tool_error_count"] == 0


def test_measured_empty_conversation_remains_collected_zero(tmp_path):
    _, projected = _linked(tmp_path, {
        "schema": EVIDENCE_SCHEMA, "interaction_summary": {"user_turns": 0, "assistant_messages": 0},
        "coverage": {"in_window": 0, "retained": 0},
    })
    entry, = projected["entries"]
    assert entry["collection"]["scope"] == "exact_session_window"
    assert entry["collection"]["source_log_count"] == 1
    assert entry["collection"]["user_turn_count"] == 0
    assert entry["collection"]["confidence"] == "high"
    assert _user_interactions([entry])["total"] == 0
    html = render_feature_timeline_html(projected)
    assert "用户交互轮次 <b>0 轮</b>" in html
    assert "会话未采集" not in html


def test_delivered_stage_renders_explicit_session_unavailability(tmp_path):
    _, projected = _linked(tmp_path, _unavailable())
    html = render_feature_timeline_html(projected)
    assert "会话未采集" in html
    assert "仅记录阶段信息" in html
    assert "用户交互轮次 <b>未采集</b>" in html
    assert "AI 回复 <b>未采集</b>" in html
    assert "用户交互轮次 <b>0 轮</b>" not in html
    assert "异步事实待关联" not in html
