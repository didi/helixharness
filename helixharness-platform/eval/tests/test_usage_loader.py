"""Tests for schema 1.0 HelixHarness usage telemetry loading."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eval.stage_supplements import LinkedStageSupplement
from eval.usage_ai import (
    _build_dataset,
    _normalize_ai_answer,
    _prompt,
    _team_payload,
    usage_data_state_hash,
)
from eval.usage_loader import (
    _ai_generated_html,
    _ai_placeholder_html,
    _filter_runtime_features,
    _fmt_duration_hours,
    _fmt_tokens,
    _latest_runtime_records,
    _load_ai_insights,
    _resource_usage,
    _runtime_distribution_html,
    _runtime_overview_html,
    aggregate_all,
    aggregate_feature,
    discover_projects,
    render_feature_timeline_html,
    render_usage_overview_html,
)


def test_cached_ai_using_retired_composite_metrics_is_not_rendered() -> None:
    html = _ai_generated_html(
        "AI 解读",
        "历史缓存",
        {
            "status": "success",
            "scope": "team",
            "answer": "成熟度 82，质量信号 76。",
        },
    )

    assert html == ""

    old_terms_html = _ai_generated_html(
        "AI 解读",
        "历史缓存",
        {
            "status": "success",
            "scope": "feature",
            "answer": "工作流完整，业务归档，统计收口。",
        },
    )
    assert old_terms_html == ""

    removed_detail_html = _ai_generated_html(
        "AI 解读",
        "历史缓存",
        {
            "status": "success",
            "scope": "feature",
            "answer": "流程状态受阻，验收结果失败，调整事件 2 次。",
        },
    )
    assert removed_detail_html == ""

    empty_html = _ai_placeholder_html("AI 解读", "只读取最新事实", "AI 解读尚未按当前事实生成。")
    assert "尚未生成 AI 解读" in empty_html
    assert "立即生成" in empty_html
    assert "口径已更新" not in empty_html
    assert "旧版 AI" not in empty_html


def test_token_formatter_switches_to_yi_at_one_hundred_million() -> None:
    assert _fmt_tokens(90_000_000) == "90.0M"
    assert _fmt_tokens(100_000_000) == "1亿"
    assert _fmt_tokens(511_100_000) == "5.11亿"
    assert _fmt_tokens(1_000_000_000) == "10亿"


def test_average_duration_formatter_always_uses_decimal_hours() -> None:
    assert _fmt_duration_hours(None) == "—"
    assert _fmt_duration_hours(1_800) == "0.50h"
    assert _fmt_duration_hours(5_400) == "1.50h"
    assert _fmt_duration_hours(61_140) == "16.98h"


def test_workflow_v2_dashboard_joins_async_supplement_without_logical_stage_run_id(tmp_path: Path) -> None:
    feature_dir = tmp_path / "docs" / "eval" / "example-feature"
    feature_dir.mkdir(parents=True)
    path = feature_dir / "eval-log.json"
    path.write_text(json.dumps(_workflow_log(), ensure_ascii=False), encoding="utf-8")
    record = LinkedStageSupplement(
        source_path=str(path.resolve()),
        effective_at="2026-08-10T00:00:00Z",
        received_at="2026-08-01T00:00:00Z",
        payload_hash="a" * 64,
        payload=_workflow_supplement(),
    )

    feature = aggregate_all(discover_projects([str(tmp_path)], linked_supplements=[record]))["features"][0]

    assert feature["project_name"] == "demo-service"
    assert feature["pending_entries"] == 0
    assert feature["resource_usage"]["total_tokens"] == 42
    assert feature["resource_usage"]["cache_read_tokens"] == 7
    assert feature["entries"][0]["association_effective_at"] == "2026-08-10T00:00:00Z"
    assert "logical_stage_run_id" not in feature["entries"][0]["workflow"]


def test_workflow_detail_separates_linked_async_facts_from_pending_state(tmp_path: Path) -> None:
    feature_dir = tmp_path / "docs" / "eval" / "example-feature"
    feature_dir.mkdir(parents=True)
    path = feature_dir / "eval-log.json"
    path.write_text(json.dumps(_workflow_log(), ensure_ascii=False), encoding="utf-8")
    record = LinkedStageSupplement(
        source_path=str(path.resolve()),
        effective_at="2026-08-10T00:00:00Z",
        received_at="2026-08-01T00:00:00Z",
        payload_hash="a" * 64,
        payload=_workflow_supplement(),
    )

    linked_log = discover_projects([str(tmp_path)], linked_supplements=[record])[0]["features"][0]
    linked_html = render_feature_timeline_html(linked_log)
    pending_log = discover_projects([str(tmp_path)])[0]["features"][0]
    pending_html = render_feature_timeline_html(pending_log)

    assert "异步事实已关联" in linked_html
    assert "异步数据关联" in linked_html
    assert "report 到达" in linked_html
    assert "统计生效" in linked_html
    assert "2026-08-10 08:00:00" in linked_html
    assert "2026-08-01 08:00:00" in linked_html
    assert "2026-08-01 08:00 → 2026-08-01 08:10（北京时间）" in linked_html
    assert "输入 30" in linked_html
    assert "输出 12" in linked_html
    assert "对话记录" in linked_html
    assert "用户交互轮次 <b>1 轮</b>" in linked_html
    assert "AI 回复 <b>1 条</b>" in linked_html
    assert "可查看对话片段" not in linked_html
    assert "省略" not in linked_html
    assert "脱敏" not in linked_html
    assert "<span>用户交互轮次 <b>1 轮</b></span><br><span>AI 回复 <b>1 条</b></span>" in linked_html
    assert "工具调用 <b>2 次</b>" in linked_html
    assert "调用未成功 <b>0 次</b>" in linked_html
    assert "session-0123456789abcdef" not in linked_html
    assert "supp_0123456789abcdef" not in linked_html
    assert "异步事实待关联" in pending_html
    assert "当前不按 0 统计" in pending_html
    assert "工具调用 <b>0 次</b>" not in pending_html
    assert "用户交互轮次 <b>0 轮</b>" not in pending_html


def test_workflow_detail_explains_tool_failures_and_balances_stage_layout(tmp_path: Path) -> None:
    feature_dir = tmp_path / "docs" / "eval" / "example-feature"
    feature_dir.mkdir(parents=True)
    path = feature_dir / "eval-log.json"
    path.write_text(json.dumps(_workflow_log(), ensure_ascii=False), encoding="utf-8")
    payload = _workflow_supplement()
    payload["execution"]["tool_call_count"] = 3
    payload["evidence"]["tools"] = {"succeeded": 2, "failed": 1}
    payload["evidence"]["failed_tools"] = [{"name": "Bash"}]
    record = LinkedStageSupplement(
        source_path=str(path.resolve()),
        effective_at="2026-08-10T00:00:00Z",
        received_at="2026-08-01T00:00:00Z",
        payload_hash="b" * 64,
        payload=payload,
    )

    linked_log = discover_projects([str(tmp_path)], linked_supplements=[record])[0]["features"][0]
    timeline_html = render_feature_timeline_html(linked_log)
    overview_html = render_usage_overview_html(
        [str(tmp_path)],
        "按需求维度",
        linked_supplements=[record],
    )

    assert '<div class="helixharness-stage-summary-grid">' in timeline_html
    assert '<details class="helixharness-stage-block helixharness-stage-exception"><summary>异常详情</summary>' in timeline_html
    assert "工具调用未成功 1 次" in timeline_html
    assert "涉及工具：Bash" in timeline_html
    assert "阶段是否完成以“执行结果”为准" in timeline_html
    assert "已确认的异常路径" not in timeline_html
    assert ".helixharness-stage-summary-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr));" in overview_html
    assert ".helixharness-stage-exception > summary { cursor:pointer;" in overview_html


def test_workflow_bug_fix_count_is_shown_in_existing_workflow(tmp_path: Path) -> None:
    feature_dir = tmp_path / "docs" / "eval" / "workflow-bug-fix"
    feature_dir.mkdir(parents=True)
    log = _workflow_log()
    template = log["entries"][0]
    requirement = {
        **template,
        "entry_id": "entry-requirement-initial",
        "command": "propose",
        "stage_kind": "requirement",
        "attempt": 1,
        "started_at": "2026-08-01T00:00:00Z",
        "ended_at": "2026-08-01T00:05:00Z",
        "artifacts": ["docs/specs/active/example-feature/spec.md"],
        "quality": {},
    }
    coding = {
        **template,
        "entry_id": "entry-coding-initial",
        "started_at": "2026-08-01T00:05:00Z",
        "ended_at": "2026-08-01T00:10:00Z",
    }
    bug_fix = {
        **requirement,
        "entry_id": "entry-requirement-bug-fix",
        "attempt": 2,
        "trigger": "bug-fix",
        "started_at": "2026-08-01T00:20:00Z",
        "ended_at": "2026-08-01T00:20:00Z",
        "status": "in_progress",
        "end_reason": None,
        "artifacts": [],
    }
    log.update(
        {
            "status": "in_progress",
            "last_updated": "2026-08-01T00:20:00Z",
            "entries": [requirement, coding, bug_fix],
        }
    )
    (feature_dir / "eval-log.json").write_text(json.dumps(log, ensure_ascii=False), encoding="utf-8")

    feature = aggregate_all(discover_projects([str(tmp_path)]))["features"][0]
    timeline_html = render_feature_timeline_html(
        {
            "feature": feature["feature"],
            "project": feature["project"],
            "status": feature["status"],
            "entries": feature["entries"],
            "feature_summary": {},
        }
    )
    overview_html = render_usage_overview_html([str(tmp_path)], "按项目与需求维度")

    assert feature["bug_fix_count"] == 1
    assert "HelixHarness · 1/3 步骤 · Bug Fix 1 次" in overview_html
    assert "问题修复场景" not in timeline_html
    assert "helixharness-scenario-pill" not in timeline_html
    assert "/helix:propose" in timeline_html
    assert ">执行中</span>" in timeline_html
    assert "结果：本阶段执行中" in timeline_html
    assert ">in_progress</span>" not in timeline_html


def test_ai_evidence_uses_linked_supplement_and_cache_expires_on_fact_change(tmp_path: Path) -> None:
    root = tmp_path / "demo-service"
    feature_dir = root / "docs" / "eval" / "example-feature"
    feature_dir.mkdir(parents=True)
    path = feature_dir / "eval-log.json"
    path.write_text(json.dumps(_workflow_log(), ensure_ascii=False), encoding="utf-8")
    record = LinkedStageSupplement(
        source_path=str(path.resolve()),
        effective_at="2026-08-10T00:00:00Z",
        received_at="2026-08-01T00:00:00Z",
        payload_hash="a" * 64,
        payload=_workflow_supplement(),
    )

    dataset = _build_dataset([root], linked_supplements=[record])
    compact_feature = dataset["features"][0]
    async_facts = compact_feature["evidence"]["stage_evidence"][0]["async_facts"]
    assert async_facts["linked"] is True
    assert async_facts["effective_at"] == "2026-08-10T00:00:00Z"
    assert async_facts["resource_usage"]["total_tokens"] == 42
    assert async_facts["interaction_summary"]["user_turns"] == 1
    assert async_facts["interactions"][0]["text"] == "修复结算超时"
    for retired_key in ("quality_inputs", "verify_passed", "evidence_complete", "telemetry_finalized"):
        assert retired_key not in compact_feature

    current_hash = usage_data_state_hash(aggregate_all(discover_projects([str(root)], linked_supplements=[record])))
    cache_path = tmp_path / "ai_insights.json"
    cache_path.write_text(
        json.dumps({"status": "success", "data_state_hash": "old", "team": {"status": "success"}}),
        encoding="utf-8",
    )
    cached = _load_ai_insights([str(root)], current_hash)
    assert cached["status"] == "stale"
    assert cached["team"] is None


def test_ai_prompts_use_fact_only_home_and_project_contract() -> None:
    feature_prompt = _prompt("feature", {"async_facts": {"linked": False}}, 800)
    team_prompt = _prompt("team", {"summary": {"feature_total": 3}}, 500)

    assert "关键事实" in feature_prompt
    assert "过程卡点" in feature_prompt
    assert "用户轮次没有完成澄清" in feature_prompt
    assert "不得出现 JSON 字段名" in feature_prompt
    assert "耗时只用 h/m" in feature_prompt
    assert "已归档但阶段记录不完整" in feature_prompt
    assert "不得推断缺失阶段未执行" in feature_prompt
    assert "不要把 JSON 中所有字段" in feature_prompt
    assert "验收结果" not in feature_prompt
    assert "调整事件" not in feature_prompt
    assert "quality_inputs" not in feature_prompt
    assert "不评价个人" in team_prompt
    assert "没有历史基线" in team_prompt


def test_ai_answer_normalization_hides_internal_fields_but_keeps_project_name() -> None:
    answer = (
        "data_archiving_v2 的 observed_stage_count=5、expected_stage_count=6，"
        "review 耗时 7,680 秒；host_authentication_failed，testing status=pending，duration=null，未发现质量风险。"
    )

    normalized = _normalize_ai_answer(answer, {"feature": "data_archiving_v2"})

    assert "data_archiving_v2" in normalized
    assert "已记录阶段数 5" in normalized
    assert "预期阶段数 6" in normalized
    assert "代码审查耗时 2h 8m" in normalized
    assert "主机认证失败" in normalized
    assert "独立验收状态 待处理" in normalized
    assert "耗时 未采集" in normalized
    assert "未发现可确认异常" in normalized
    assert "observed_stage_count" not in normalized
    assert "host_authentication_failed" not in normalized
    assert "质量风险" not in normalized


def test_team_ai_payload_uses_aggregate_facts_without_nested_ai_answers() -> None:
    payload = _team_payload(
        {
            "summary": {"project_total": 30},
            "features": [{"feature": f"feature-{index}"} for index in range(10)],
            "users": [{"key": "u1"}, {"key": "u2"}],
        }
    )

    assert payload["summary"]["project_total"] == 30
    assert "attention_projects" not in payload
    assert "stage_distribution" in payload
    assert "issue_projects" in payload
    assert "feature_insights" not in payload


def _workflow_log() -> dict:
    return {
        "schema": "helixharness-eval-ledger/1.0",
        "schema_version": "1.0",
        "feature": "example-feature",
        "project": {"name": "demo-service"},
        "requirement": {"id": "req_0123456789abcdef01234567", "name": "example-feature", "url": None},
        "created_at": "2026-08-01T00:00:00Z",
        "last_updated": "2026-08-01T00:10:00Z",
        "status": "archived",
        "entries": [
            {
                "entry_id": "entry-0123456789abcdef",
                "helixharness_version": "2.2.0",
                "command": "apply",
                "stage_kind": "coding",
                "attempt": 1,
                "trigger": "user_command",
                "platform": "codex",
                "session_hash": "session-0123456789abcdef0123456789abcdef",
                "started_at": "2026-08-01T00:00:00Z",
                "ended_at": "2026-08-01T00:10:00Z",
                "status": "completed",
                "end_reason": "workflow_completed",
                "actor": {"user_name": "Demo", "user_name_source": "git_config_local_user_name"},
                "checkpoint": {},
                "artifacts": ["docs/specs/active/example-feature/verify-report.md"],
                "quality": {"standards_verdict": "APPROVE", "spec_verdict": "APPROVE"},
            }
        ],
    }


def _workflow_supplement() -> dict:
    return {
        "schema": "helixharness-stage-supplement/1.0",
        "supplement_id": "supp_0123456789abcdef",
        "revision": 1,
        "supersedes": None,
        "entry_id": "entry-0123456789abcdef",
        "requirement_id": "req_0123456789abcdef01234567",
        "requirement_name": "example-feature",
        "feature": "example-feature",
        "session_hash": "session-0123456789abcdef0123456789abcdef",
        "platform": "codex",
        "usage": {
            "input_tokens": 30,
            "output_tokens": 12,
            "cached_input_tokens": 7,
            "model": "gpt-test",
        },
        "execution": {"tool_call_count": 2, "subagent_spawn_count": 0},
        "evidence": {
            "schema": "helixharness-evolution-evidence/1.0",
            "interactions": [
                {"role": "user", "text": "修复结算超时"},
                {"role": "assistant", "text": "已定位重试路径"},
            ],
            "interaction_summary": {"user_turns": 1, "assistant_messages": 1},
            "tools": {"succeeded": 2, "failed": 0},
            "failed_tools": [],
            "coverage": {"in_window": 4, "retained": 2},
        },
        "workflow_trace": {"schema": "helixharness-workflow-trace/1.0", "status": "collected"},
    }


def _repeated_apply_log() -> dict:
    import copy

    log = _workflow_log()
    entries = []
    for index in range(1, 4):
        entry = copy.deepcopy(log["entries"][0])
        entry.update(
            entry_id=f"apply-{index}",
            attempt=index,
            started_at=f"2026-09-0{index}T00:00:00Z",
            ended_at=f"2026-09-0{index}T00:10:00Z",
            status="blocked" if index == 1 else "completed",
            telemetry_status="complete",
        )
        entry["artifacts"] = ["docs/shared.md", f"docs/report-{index}.md"]
        entry["collection"] = {"user_turn_count": 1}
        entry["execution_stats"] = {"tool_call_count": index, "tool_error_count": int(index == 1)}
        entry["resource_usage"] = {
            "collection_status": "collected",
            "attribution": "exact_session_window",
            "total_tokens": index * 1000000,
            "model": "test-model-not-visible",
        }
        entry["async_evidence"] = {"interaction_summary": {"assistant_messages": 2, "user_turns": 1}}
        entries.append(entry)
    log["entries"] = entries
    return log


def test_repeated_stage_missing_facts_are_partial_not_zero() -> None:
    log = _repeated_apply_log()
    log["entries"][1]["telemetry_status"] = "pending"
    log["entries"][1]["started_at"] = None
    log["entries"][1]["ended_at"] = None
    summary = render_feature_timeline_html(log).split('<details class="helixharness-stage-attempts">', 1)[0]
    assert "<h4>Token 已采集小计</h4><div><b>4.0M</b>" in summary
    assert "工具调用 <b>4 次</b>（已记录 2/3 条）" in summary
    assert "累计耗时 <b>20m</b>（已记录 2/3 条）" in summary
    assert "用户交互轮次 <b>2 轮</b>（已记录 2/3 条）" in summary
    for entry in log["entries"]:
        entry["telemetry_status"] = "pending"
    summary = render_feature_timeline_html(log).split('<details class="helixharness-stage-attempts">', 1)[0]
    assert "工具调用 <b>未采集</b>（已记录 0/3 条）" in summary
    assert "用户交互轮次 <b>未采集</b>（已记录 0/3 条）" in summary


def test_apply_implementation_missing_archive_is_distinct_from_zero_and_shown_once_across_profiles() -> None:
    log = _repeated_apply_log()
    for entry in log["entries"]:
        entry["stage_profile"] = entry["entry_id"]
    html = render_feature_timeline_html(log, archive_changes={
        "collection_status": "unavailable", "unavailable_reason": "archive_not_completed",
    })
    assert html.count("<b>实现文件</b>") == 1
    assert 'class="helixharness-stage-card helixharness-stage-rollup"' not in html
    assert "需求尚未完成归档" in html and "截至归档" not in html
    assert "0 个文件" not in html
    empty = render_feature_timeline_html(log, archive_changes={"collection_status": "collected", "file_changes": []})
    assert empty.count('class="helixharness-implementation-files"') == 1
    assert "截至归档 · 0 个文件" in empty
    assert "未发现实现文件变更" in empty
    assert "未采集可解析的归档文件清单" not in empty




def test_current_workflow_uses_three_stage_display_rules(tmp_path: Path) -> None:
    feature_dir = tmp_path / "docs" / "eval" / "example-feature"
    feature_dir.mkdir(parents=True)
    (feature_dir / "eval-log.json").write_text(
        json.dumps(_workflow_log(), ensure_ascii=False),
        encoding="utf-8",
    )

    html = render_usage_overview_html([str(tmp_path)], "按需求维度")

    assert "HelixHarness · 3/3 步骤" in html
    assert "最近阶段：开发与验证" in html
    assert ">数据缺失</span>" in html
    assert "阶段记录 1/3" not in html
    assert "预期 3 个阶段只采集到 1 个阶段的记录" in html
    assert "已归档但记录不完整" not in html


def test_workflow_detail_uses_three_business_stages_for_duration_and_process(tmp_path: Path) -> None:
    feature_dir = tmp_path / "docs" / "eval" / "example-feature"
    feature_dir.mkdir(parents=True)
    (feature_dir / "eval-log.json").write_text(
        json.dumps(_workflow_log(), ensure_ascii=False),
        encoding="utf-8",
    )

    html = render_usage_overview_html([str(tmp_path)], "按需求维度")
    timeline_html = render_feature_timeline_html(_workflow_log())

    assert "开发与验证 10m" in html
    assert "<b>开发与验证</b><small>1 个需求样本</small>" in html
    assert "<b>编码实施</b><small>1 个需求样本</small>" not in html
    assert timeline_html.count('class="helixharness-seg-lbl"') == 3
    assert '<div class="helixharness-bar" style="--helixharness-stage-count:3">' in timeline_html
    assert '<div class="helixharness-seg-lbl">需求与计划</div>' in timeline_html
    assert '<div class="helixharness-seg-lbl">开发与验证</div>' in timeline_html
    assert '<div class="helixharness-seg-lbl">归档</div>' in timeline_html
    assert '<div class="helixharness-seg-lbl">开发计划</div>' not in timeline_html
    assert (
        '<div class="helixharness-stage-card-head"><div><span class="helixharness-stage-index">1</span><b>开发与验证</b>' in timeline_html
    )
    assert "未采集本次代码文件清单" in timeline_html


def test_workflow_detail_displays_helixharness_version_and_execution_platform(tmp_path: Path) -> None:
    feature_dir = tmp_path / "docs" / "eval" / "workflow-runtime"
    feature_dir.mkdir(parents=True)
    (feature_dir / "eval-log.json").write_text(
        json.dumps(_workflow_log(), ensure_ascii=False),
        encoding="utf-8",
    )

    html = render_usage_overview_html([str(tmp_path)], "按需求维度")
    timeline_html = render_feature_timeline_html(_workflow_log())

    assert '<span class="helixharness-runtime-summary">HelixHarness 2.2.0 · 平台 Codex</span>' in html
    assert "<span>HelixHarness 2.2.0 · Codex</span>" in timeline_html


def _runtime_entry(user: str, time: str = "2026-09-08T00:00:00Z", **values) -> dict:
    return {"actor": {"user_name": user}, "ended_at": time, "command": "apply", **values}


def test_runtime_distributions_count_users_and_merge_platform_aliases() -> None:
    entries = [
        _runtime_entry("Alice", helixharness_version="2.2.6", platform="codex"),
        _runtime_entry("Bob", helixharness_version="2.2.6", invocation={"platform": "claude-code"}),
        _runtime_entry("Carol", helixharness_version="2.2.8", platform="claude"),
    ]
    features = [{"entries": [entry]} for entry in entries]
    versions = _runtime_distribution_html(features, "version")
    platforms = _runtime_distribution_html(features, "platform")
    assert 'data-label="2.2.6" data-count="2"' in versions
    assert 'data-label="2.2.8" data-count="1"' in versions
    assert 'data-label="Claude Code" data-count="2"' in platforms
    assert 'data-label="Codex" data-count="1"' in platforms
    for chart in (versions, platforms):
        assert 'data-label="未采集"' not in chart
        assert "66.7%" in chart and "33.3%" in chart
        assert "共 3 人" in chart
    assert 'class="helixharness-version-bars"' in versions
    assert "<svg" not in versions
    assert "width:100.000000%" in versions and "width:50.000000%" in versions
    assert "helixharness-runtime-legend" in platforms
    assert 'class="helixharness-duration-ring helixharness-runtime-ring"' in platforms
    assert "暂无可统计的用户记录" in _runtime_distribution_html([], "version")


def test_runtime_drilldown_shows_each_user_latest_record() -> None:
    features = [
        {
            "feature": "shared-feature",
            "project_name": "repo<example>",
            "entries": [
                _runtime_entry("Alice", "2026-09-01T00:00:00Z", helixharness_version="2.2.6", platform="codex"),
                _runtime_entry("Bob", helixharness_version="2.2.8", platform="claude"),
                _runtime_entry("Alice", helixharness_version="2.2.8", platform="claude-code"),
            ],
        }
    ]
    versions = _runtime_distribution_html(features, "version")
    platforms = _runtime_distribution_html(features, "platform")
    version_detail = re.search(r'<section id="helixharness-runtime-version-0-2-2-8".*?</section>', versions, re.DOTALL)
    assert version_detail is not None
    assert "Alice" in version_detail[0] and "Bob" in version_detail[0]
    assert "2.2.6" not in versions and "Codex" not in platforms
    assert "2 人" in version_detail[0]
    assert "repo&lt;example&gt;" in version_detail[0]
    assert "repo<example>" not in versions
    platform_detail = re.search(r'<section id="helixharness-runtime-platform-0-claude-code".*?</section>', platforms, re.DOTALL)
    assert platform_detail is not None
    assert "Alice" in platform_detail[0] and "Bob" in platform_detail[0]
    assert "2 人" in platform_detail[0]
    assert "最近使用版本</th>" in platform_detail[0]
    assert "最近使用平台</th>" in version_detail[0]
    assert "日志条数</th>" not in versions
    assert versions.count('href="#helixharness-runtime-version-0-2-2-8"') == 1
    assert platforms.count('href="#helixharness-runtime-platform-0-claude-code"') == 2


def test_runtime_time_filter_uses_beijing_dates_and_keeps_all_time_explicit() -> None:
    entries = [
        {"entry_id": "old", "ended_at": "2026-09-01T15:59:59Z"},
        {"entry_id": "first", "ended_at": "2026-09-01T16:00:00Z"},
        {"entry_id": "last", "started_at": "2026-09-08T15:59:59Z"},
        {"entry_id": "future", "ended_at": "2026-09-08T16:00:00Z"},
        {"entry_id": "missing"},
    ]
    features = [{"entries": entries}]
    selected, hint = _filter_runtime_features(features, "7d", now=datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert [e["entry_id"] for e in selected[0]["entries"]] == ["first", "last"]
    assert "2026-09-02 至 2026-09-08" in hint
    assert "1 条无实际时间" in hint
    assert len(features[0]["entries"]) == 5
    assert _filter_runtime_features(features, "all")[0] == features
    custom, _ = _filter_runtime_features(features, "custom", "2026-09-02", "2026-09-02")
    assert [e["entry_id"] for e in custom[0]["entries"]] == ["first"]


def test_runtime_date_filter_does_not_change_project_distributions() -> None:
    features = [
        {
            "feature": "same-feature",
            "project_name": "repo",
            "entries": [
                {
                    "command": "apply",
                    "helixharness_version": "2.2.5",
                    "platform": "claude",
                    "actor": {"user_name": "old-user"},
                    "ended_at": "2026-08-01T00:00:00Z",
                },
                {
                    "command": "apply",
                    "helixharness_version": "2.2.8",
                    "platform": "codex",
                    "actor": {"user_name": "recent-user"},
                    "ended_at": "2026-09-08T00:00:00Z",
                },
            ],
        }
    ]
    features = [
        {**features[0], "feature": str(i), "entries": [entry]} for i, entry in enumerate(features[0]["entries"])
    ]
    html = _runtime_overview_html(features, "custom", "2026-09-08", "2026-09-08")
    assert 'data-label="2.2.8" data-count="1"' in html
    assert 'data-distribution="platform"' in html
    assert "recent-user" in html
    assert "old-user" in html and 'data-label="2.2.5"' in html
    assert "old-user" in _runtime_overview_html(features, "all")
    assert '<details class="helixharness-runtime-disclosure">' in html
    assert '<details class="helixharness-runtime-disclosure" open>' not in html
    assert '<details class="helixharness-runtime-disclosure" open>' in _runtime_overview_html(features, "all", expanded=True)
    assert "开始日期不能晚于结束日期" in _runtime_overview_html(features, "custom", "2026-09-09", "2026-09-08")
    for period, start, end in [("all", "", ""), ("7d", "", ""), ("custom", "2026-09-09", "2026-09-08")]:
        rendered = _runtime_overview_html(features, period, start, end)
        charts = re.findall(
            r'<section class="helixharness-panel helixharness-runtime-distribution".*?</section></section>', rendered, re.DOTALL
        )
        original = re.findall(
            r'<section class="helixharness-panel helixharness-runtime-distribution".*?</section></section>', html, re.DOTALL
        )
        assert charts and charts == original
    assert '阶段耗时占比</div><div class="helixharness-runtime-filter">' in html
    assert "时间筛选仅影响阶段耗时" not in html
    assert "版本与平台使用情况" in html
    assert "持续使用情况" not in html and "helixharness-sustained-usage" not in html
    assert "跨项目按用户去重" not in html
    assert "不代表当前已安装版本" not in html


def test_small_runtime_share_keeps_exact_values_inside_its_own_ring() -> None:
    entries = [_runtime_entry(f"user-{i}", platform="codex" if i < 19 else "claude") for i in range(20)]
    html = _runtime_distribution_html([{"entries": [entry]} for entry in entries], "platform")
    assert html.count('class="helixharness-duration-ring helixharness-runtime-ring"') == 1
    assert 'data-distribution="workflow"' not in html
    assert 'aria-label="Claude Code 1 人，5.0%，查看用户"' in html
    assert 'href="#helixharness-runtime-platform-1-claude-code"' in html
    assert "<small>1 人</small><b>5.0%</b>" in html


def test_latest_runtime_keeps_projects_separate_and_ignores_metadata_time() -> None:
    old = _runtime_entry("Alice", "2026-09-08T15:00:00+08:00", helixharness_version="9.9.9", platform="codex")
    new = _runtime_entry("Alice", "2026-09-08T08:00:00Z", helixharness_version="2.2.8", platform="claude")
    features = [
        {"feature": "old", "project_name": "repo-a", "last_updated": "2099-01-01T00:00:00Z", "entries": [old] * 20},
        {"feature": "new", "project_name": "repo-b", "entries": [new]},
    ]
    assert _latest_runtime_records(features) == [(features[0], old), (features[1], new)]
    assert _latest_runtime_records(list(reversed(features))) == [(features[1], new), (features[0], old)]
    versions = _runtime_distribution_html(features, "version")
    assert 'data-label="2.2.8" data-count="1"' in versions
    assert 'data-label="9.9.9"' not in versions
    assert "repo-b" in versions and "repo-a" not in versions
    assert "2026-09-08" in versions and "16:00:00" in versions


def test_latest_runtime_keeps_missing_values_instead_of_using_an_older_version() -> None:
    old = _runtime_entry("Alice", "2026-09-01T00:00:00Z", helixharness_version="2.2.8", platform="codex")
    new = _runtime_entry("Alice", helixharness_version="unknown", platform="claude")
    features = [{"entries": [new, old]}]
    versions = _runtime_distribution_html(features, "version")
    platforms = _runtime_distribution_html(features, "platform")
    assert 'data-label="未采集" data-count="1"' in versions
    assert "2.2.8" not in versions
    assert 'data-label="Claude Code" data-count="1"' in platforms
    assert "100.0%" in versions


def test_latest_runtime_uses_started_time_but_ignores_admin_commands_but_includes_anonymous_records() -> None:
    valid = _runtime_entry("Alice", "", started_at="2026-09-08T00:00:00Z", status="in_progress")
    features = [
        {
            "entries": [
                valid,
                _runtime_entry("Alice", "2026-09-09T00:00:00Z", command="status", stage_kind="requirement"),
                _runtime_entry("Alice", "2026-09-10T00:00:00Z", command="init", stage_kind="requirement"),
                _runtime_entry("Bob", "", started_at="invalid"),
                _runtime_entry("unknown"),
                _runtime_entry(""),
            ]
        }
    ]
    assert _latest_runtime_records(features) == [(features[0], features[0]["entries"][-1])]
    assert 'data-label="未采集" data-count="1"' in _runtime_distribution_html(features, "platform")


def test_stage_duration_recomputes_samples_and_averages_from_filtered_entries() -> None:
    features = [
        {
            "feature": "example",
            "stage_timeline": [
                {"stage_kind": "requirement", "status": "completed", "duration_sec": 99999},
            ],
            "entries": [
                {
                    "stage_kind": "requirement",
                    "status": "completed",
                    "duration_sec": 1800,
                    "ended_at": "2026-08-01T00:00:00Z",
                },
                {
                    "stage_kind": "coding",
                    "status": "completed",
                    "duration_sec": 600,
                    "ended_at": "2026-09-08T01:00:00Z",
                },
                {
                    "stage_kind": "coding",
                    "status": "completed",
                    "duration_sec": 1200,
                    "ended_at": "2026-09-08T02:00:00Z",
                },
            ],
        }
    ]
    html = _runtime_overview_html(features, "custom", "2026-09-08", "2026-09-08")
    assert "<strong>100.0%</strong><span>30m</span>" in html
    assert "1 个项目样本" in html
    assert "<strong>0.0%</strong><span>—</span>" in html
    all_html = _runtime_overview_html(features, "all")
    assert all_html.count("<strong>50.0%</strong><span>30m</span>") == 2
    assert 'data-label="未采集"' not in html
    assert 'data-distribution="platform"' in html


def test_feature_log_time_falls_back_only_when_event_times_are_unavailable() -> None:
    log = {"feature": "no-events", "entries": [], "last_updated": "2026-09-08T09:00:00"}
    assert aggregate_feature(log)["log_time"] == "2026-09-08T09:00:00+00:00"
    log["last_updated"] = "invalid"
    assert aggregate_feature(log)["log_time"] == ""


def test_entry_resource_requires_schema_status() -> None:
    usage = _resource_usage({"resource_usage": {"total_tokens": 123}})

    assert usage["total_tokens"] is None
    assert usage["collection_status"] == "unavailable"
    assert usage["unavailable_reason"] == "missing_collection_status"


@pytest.mark.parametrize("trigger,expected", [("loop", True), ("user_command", False), ("bug-fix", False)])
def test_current_ledger_trigger_reaches_dashboard_loop_counts(tmp_path: Path, trigger: str, expected: bool) -> None:
    ledger = _workflow_log()
    ledger["entries"][0]["trigger"] = trigger
    root = tmp_path / "docs/eval/example-feature"
    root.mkdir(parents=True)
    (root / "eval-log.json").write_text(json.dumps(ledger), encoding="utf-8")
    summary = aggregate_all(discover_projects([str(tmp_path)]))
    feature = summary["features"][0]
    assert feature["loop_usage"]["enabled"] is expected
    assert feature["loop_count"] == int(expected)
    assert summary["summary"]["loop_count"] == int(expected)
    invocation = feature["entries"][0]["invocation"]
    assert invocation["mode"] == ("loop" if expected else "manual")
    assert invocation["tdd"] is False
    assert invocation["loop_max_turns"] is None


def test_retired_requirement_addresses_do_not_link_or_group_features(tmp_path: Path) -> None:
    source_snapshots = {}
    for number, name in enumerate(("address-free-first", "address-free-second"), 1):
        log = _workflow_log()
        log["feature"] = name
        log["requirement"].update(id=f"req_{number:024x}", name=name, url="https://tracker.example/retired")
        log["requirement_url"] = "https://tracker.example/top-level"
        log["entries"][0]["requirement_url"] = "https://tracker.example/entry"
        path = tmp_path / "docs/eval" / name / "eval-log.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(log), encoding="utf-8")
        source_snapshots[path] = path.read_bytes()

    features = aggregate_all(discover_projects([str(tmp_path)]))["features"]
    assert len(features) == 2
    assert all("requirement_url" not in feature for feature in features)
    assert "requirement_total" not in _build_dataset([str(tmp_path)])["summary"]
    for view in ("按需求维度", "按用户维度"):
        html = render_usage_overview_html([str(tmp_path)], view)
        assert "address-free-first" in html and "address-free-second" in html
        assert "tracker.example" not in html
        assert "需求地址" not in html
        assert 'class="helixharness-requirement-link"' not in html
        assert '<span class="helixharness-requirement-label">无需求</span>' not in html
    for path, original in source_snapshots.items():
        assert path.read_bytes() == original
