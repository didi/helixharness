"""Daily observation, bounded context and failure recovery without external calls."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from eval.usage_ai_daily import (
    HISTORY_FILE,
    MAX_INPUT_CHARS,
    bounded_payload,
    daily_records,
    generate_daily_insight,
    global_facts,
)


def feature(index=0, *, tokens=100, status="completed", linked=True):
    return {
        "project_id": "repo", "project_name": "service", "feature": f"feature-{index}",
        "archive_completed": False, "collection_coverage": "observed_from_start",
        "expected_stage_count": 3, "current_stage": "coding",
        "stage_timeline": [{
            "stage_kind": "coding", "entry_count": 1, "attempt_count": 1,
            "continuation_segment_count": 0, "status": status,
        }],
        "entries": [{
            "entry_id": f"entry-{index}", "stage_kind": "coding", "command": "apply",
            "status": status, "duration_sec": 60,
            "started_at": "2026-01-01T00:00:00Z",  # Deliberately old execution.
            "telemetry_status": "complete" if linked else "pending",
            "resource_usage": {"total_tokens": tokens, "collection_status": "collected" if linked else "pending"},
        }],
    }


def dataset(features):
    return {
        "summary": {"project_total": len(features), "resource_usage": {"total_tokens": 12345}},
        "daily_records": daily_records(features),
    }


class Models:
    def __init__(self):
        self.inputs = []
        self.batches = []

    def generate(self, payload):
        self.inputs.append(copy.deepcopy(payload))
        return {"status": "success", "answer": "结论：当前事实。主要卡点：待核查。建议动作：查看阶段明细。"}

    def summarize(self, payload):
        self.batches.append(copy.deepcopy(payload))
        return "本批包含已采集的阶段记录；无额外根因证据。"


def run(tmp_path, data, models, day=10, hour=4):
    return generate_daily_insight(
        data, tmp_path, [tmp_path / "repo"], models.generate, models.summarize,
        now=datetime(2026, 9, day, hour, tzinfo=timezone.utc),
    )


def test_bootstrap_then_daily_updates_include_late_telemetry_and_archive(tmp_path):
    models = Models()
    old = feature(linked=False)
    run(tmp_path, dataset([old]), models)
    assert models.inputs[-1]["daily_window"]["bootstrap"] is True
    assert len(models.inputs[-1]["changes"]) == 2
    updated = feature(tokens=400)
    updated["archive_completed"] = True
    run(tmp_path, dataset([updated, feature(1)]), models, day=11)
    payload = models.inputs[-1]
    assert payload["daily_window"]["bootstrap"] is False
    assert payload["daily_window"]["change_counts"] == {"updated": 2, "new": 2}
    assert payload["daily_window"]["project_count"] == 2
    assert payload["history"]["recent_generated_days"][0]["day"] == "2026-09-10"
    entry = next(r for r in payload["changes"] if r["kind"] == "entry" and r["feature"] == "feature-0")
    assert entry["resource_usage"]["total_tokens"] == 400
    assert entry["change"] == "updated"  # Old execution time never hides late arrivals.
    assert payload["summary"]["resource_usage"]["total_tokens"] == 12345


def test_same_day_repeated_generation_keeps_union_and_latest_values(tmp_path):
    models = Models()
    data = dataset([feature()])
    run(tmp_path, data, models)
    run(tmp_path, data, models)
    assert models.inputs[-1] == models.inputs[-2]
    run(tmp_path, dataset([feature(tokens=500), feature(1)]), models)
    run(tmp_path, dataset([feature(tokens=700), feature(1)]), models)
    payload = models.inputs[-1]
    assert len(payload["changes"]) == 4
    assert payload["history"]["generated_day_count"] == 0
    entry = next(r for r in payload["changes"] if r["kind"] == "entry" and r["feature"] == "feature-0")
    assert entry["resource_usage"]["total_tokens"] == 700
    with sqlite3.connect(tmp_path / HISTORY_FILE) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0] == 1


def test_failed_generation_preserves_checkpoint_and_summary_for_retry(tmp_path):
    models = Models()
    run(tmp_path, dataset([feature()]), models)
    path = tmp_path / HISTORY_FILE
    with sqlite3.connect(path) as connection:
        before = connection.execute("SELECT * FROM checkpoints").fetchall()
    updated = dataset([feature(tokens=500)])

    def fail(_):
        raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        generate_daily_insight(updated, tmp_path, [tmp_path / "repo"], fail, models.summarize,
                               now=datetime(2026, 9, 11, tzinfo=timezone.utc))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM checkpoints").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0] == 1
    run(tmp_path, updated, models, day=11)
    assert models.inputs[-1]["daily_window"]["change_counts"] == {"updated": 1}


def test_shanghai_day_boundary_no_activity_days_and_removed_scope(tmp_path):
    models = Models()
    data = dataset([feature()])
    run(tmp_path, data, models, hour=15)
    run(tmp_path, data, models, hour=16)
    assert models.inputs[-1]["daily_window"]["day"] == "2026-09-11"
    assert models.inputs[-1]["changes"] == []
    run(tmp_path, dataset([]), models, day=15)
    payload = models.inputs[-1]
    assert payload["daily_window"]["observed_since"].startswith("2026-09-11")
    assert {r["change"] for r in payload["changes"]} == {"removed_from_scope"}
    assert payload["history"]["generated_day_count"] == 2  # No fabricated missing days.


def test_recent_history_and_older_fact_rollup_stay_bounded(tmp_path):
    models = Models()
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for day in range(40):
        generate_daily_insight(dataset([feature(status="failed")]), tmp_path, [tmp_path / "repo"],
                               models.generate, models.summarize, now=start + timedelta(days=day))
    history = models.inputs[-1]["history"]
    assert len(history["recent_generated_days"]) == 7
    assert history["older_history"]["generated_days"] == 32
    assert history["older_history"]["days_with_issue"]["recorded_failed"] == 32
    assert history["older_history"]["last_snapshot"]["summary"]["resource_usage"]["total_tokens"] == 12345
    assert len(json.dumps(models.inputs[-1], ensure_ascii=False)) < MAX_INPUT_CHARS


def test_all_increment_records_reach_bounded_model_batches(tmp_path):
    models = Models()
    data = dataset([feature(index) for index in range(200)])
    result = run(tmp_path, data, models)
    assert models.batches
    assert result["daily_context"]["input_chars"] <= MAX_INPUT_CHARS
    original = [r for b in models.batches if b["reduction_round"] == 1 for r in b["records_or_summaries"]]
    assert {r["record_key"] for r in original} == {r["record_key"] for r in data["daily_records"]}
    assert len(original) == 400
    assert all(len(json.dumps(b, ensure_ascii=False, separators=(",", ":"))) < MAX_INPUT_CHARS for b in models.batches)
    assert models.inputs[-1]["daily_window"]["project_count"] == 200


def test_batch_failure_does_not_consume_any_records(tmp_path):
    models = Models()

    def fail(_):
        raise RuntimeError("batch failure")

    data = dataset([feature(index) for index in range(40)])
    with pytest.raises(RuntimeError, match="batch failure"):
        generate_daily_insight(data, tmp_path, [tmp_path / "repo"], models.generate, fail)
    assert models.inputs == []
    run(tmp_path, data, models)
    assert models.inputs[-1]["daily_window"]["bootstrap"] is True


def test_recursive_reduction_and_budget_errors_are_explicit():
    batches = []

    def summarize(payload):
        batches.append(payload)
        return "记录摘要" * 100

    result = bounded_payload({}, [{"detail": "x" * 1000} for _ in range(50)], summarize, 4000)
    assert result["reduction_rounds"] > 1
    assert len(json.dumps(result, ensure_ascii=False)) < 4000
    with pytest.raises(ValueError, match="exceeds batch budget"):
        bounded_payload({}, [{"detail": "x" * 5000}], summarize, 4000)
    with pytest.raises(ValueError, match="empty daily batch"):
        bounded_payload({}, [{"detail": "x" * 1000} for _ in range(10)], lambda _: "", 4000)


def test_global_issues_cover_all_projects_and_collection_is_not_failure():
    items = [feature(index) for index in range(10)]
    items[9] = feature(9, status="failed")
    items[0]["collection_coverage"] = "partial"
    items[1]["stage_timeline"][0]["continuation_segment_count"] = 1
    facts = global_facts(dataset(items))
    assert facts["issue_denominator_projects"] == 10
    assert facts["issue_projects"] == {
        "recorded_failed": 1, "latest_stage_exception": 1,
        "incomplete_collection": 1, "cross_session_continuation": 1,
    }
    assert facts["stage_distribution"]["开发与验证"] == {
        "entry_count": 10, "project_count": 10, "duration_samples": 10, "duration_sec": 600,
    }


def test_allowlist_excludes_raw_content_and_unlinked_evidence():
    item = feature(linked=False)
    entry = item["entries"][0]
    entry["failure_code"] = "echo private-text"
    entry["async_evidence"] = {
        "interactions": [{"text": "private-text"}], "prompt": "private-text",
        "failed_tools": [{"name": "Bash", "arguments": "private-text", "output": "private-text"}],
        "workflow_trace": {"gates": [{"status": "failed", "details": "private-text"}]},
    }
    record = next(r for r in daily_records([item]) if r["kind"] == "entry")
    assert "private-text" not in json.dumps(record)
    assert record["failed_tool_count"] is None
    assert record["resource_usage"]["total_tokens"] is None
    entry["telemetry_status"] = "complete"
    record = next(r for r in daily_records([item]) if r["kind"] == "entry")
    assert record["failed_gate_count"] == 1
    assert record["failed_tool_names"] == ["Bash"]
    assert "private-text" not in json.dumps(record)
    facts = global_facts(dataset([feature(), item]))
    assert facts["evidence_coverage"]["tool_observed_entries"] == 1
    assert facts["evidence_coverage"]["eligible_entries"] == 2


def test_scope_change_starts_a_separate_baseline(tmp_path):
    models = Models()
    data = dataset([feature()])
    run(tmp_path, data, models)
    generate_daily_insight(data, tmp_path, [tmp_path / "another-repo"], models.generate, models.summarize)
    assert models.inputs[-1]["daily_window"]["bootstrap"] is True
    assert models.inputs[-1]["history"]["generated_day_count"] == 0


def test_empty_model_answer_does_not_create_successful_day(tmp_path):
    with pytest.raises(ValueError, match="nonempty"):
        generate_daily_insight(dataset([feature()]), tmp_path, [],
                               lambda _: {"status": "success", "answer": " "}, lambda _: "")
    with sqlite3.connect(tmp_path / HISTORY_FILE) as connection:
        assert connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0


def test_removed_then_reappearing_record_is_not_reported_as_removed(tmp_path):
    models = Models()
    run(tmp_path, dataset([feature()]), models)
    run(tmp_path, dataset([]), models)
    run(tmp_path, dataset([feature()]), models)
    assert {r["change"] for r in models.inputs[-1]["changes"]} == {"reappeared_in_scope"}


def test_missing_duration_is_unknown_with_explicit_sample_count():
    item = feature()
    item["entries"][0].pop("duration_sec")
    facts = global_facts(dataset([item]))
    assert facts["summary"]["total_duration_sec"] is None
    assert facts["summary"]["duration_sample_entries"] == 0
    assert facts["stage_distribution"]["开发与验证"]["duration_sec"] is None


def test_full_and_homepage_generation_share_history_and_accept_new_facts(tmp_path, monkeypatch):
    from eval import usage_ai

    inputs = []
    data = {
        **dataset([feature()]), "features": [{"key": "f0", "feature": "feature-0"}],
        "data_state_hash": "version-1",
    }

    def fake_api(_endpoint, question, *_args, **_kwargs):
        inputs.append(question)
        return {"answer": "结论：当前项目事实。主要卡点：未发现可确认卡点。建议动作：核对记录。"}

    monkeypatch.setattr(usage_ai, "_build_dataset", lambda *args: data)
    monkeypatch.setattr(usage_ai, "_call_api", fake_api)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_ENABLED", "true")
    config = {"ai_analysis": {"enabled": True}}
    initial = usage_ai.generate_usage_ai_insights(config, [tmp_path / "repo"], tmp_path, linked_supplements=[])
    assert initial["status"] == "success"
    assert len(inputs) == 2
    data.update(dataset([feature(tokens=500), feature(1)]))
    data["data_state_hash"] = "version-2"
    refreshed = usage_ai.regenerate_usage_team_ai_insight(config, [tmp_path / "repo"], tmp_path, linked_supplements=[])
    assert len(inputs) == 3
    assert refreshed["team"]["status"] == "success"
    assert refreshed["data_state_hash"] == "version-2"
    assert refreshed["features"]["f0"]["status"] == "stale"
    assert refreshed["features"]["f0"]["answer"] == initial["features"]["f0"]["answer"]
    payload = json.loads(inputs[-1].split("JSON 指标：", 1)[1])
    assert payload["daily_window"]["project_count"] == 2
    assert len(payload["changes"]) == 4
    assert "attention_projects" not in payload
    with sqlite3.connect(tmp_path / HISTORY_FILE) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0] == 1
