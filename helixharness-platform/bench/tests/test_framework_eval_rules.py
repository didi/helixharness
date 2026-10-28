"""Public contract tests for the framework-evaluation rulebook."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_ui import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(
        "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH",
        str(tmp_path / "evaluation.db"),
    )
    with TestClient(create_app()) as test_client:
        yield test_client


def test_rulebook_exposes_current_unified_scoring_contract(client: TestClient) -> None:
    response = client.get("/framework-evaluation/rules")

    assert response.status_code == 200
    assert 'data-scoring-rules="true"' in response.text
    assert 'data-rulebook-palette="light"' in response.text
    assert "bg-slate-950" not in response.text
    assert "bg-black" not in response.text
    assert 'data-scoring-profile="UNIFIED_EVALUATION"' in response.text
    assert "unified-evaluation.v7" in response.text
    assert 'data-profile="CHAIN_BEHAVIOR"' not in response.text
    assert 'data-profile="WORKFLOW_REDESIGN"' not in response.text
    assert "Outcome Quality" in response.text
    assert "Framework Workflow Quality" in response.text
    assert "Efficiency" in response.text
    assert "可归责业务轨迹" in response.text
    assert "实际 Attempt 消耗仅用于成本和稳定性观察" in response.text
    assert 'data-comparison-regime=' not in response.text
    assert 'data-unified-workflow-rubric="true"' in response.text
    assert "Evidence Incompatibility" in response.text
    assert "部分可比较" in response.text


def test_rulebook_explains_weights_and_evidence_authority(client: TestClient) -> None:
    response = client.get("/framework-evaluation/rules")

    assert response.status_code == 200
    assert 'data-group-weight="60"' in response.text
    assert 'data-group-weight="20"' in response.text
    assert 'data-group-weight="70"' not in response.text
    assert 'data-efficiency-weight="50"' in response.text
    assert 'data-efficiency-weight="40"' in response.text
    assert 'data-efficiency-weight="10"' in response.text
    assert "Agent Token" in response.text
    assert "有效执行时间" in response.text
    assert "工作流返工" in response.text
    assert "(Base - Candidate) / max(Base, Candidate)" in response.text
    assert "按纳入业务轨迹的 Turn 时长求和" in response.text
    assert "排除排队、Judge、暂停空闲和基础设施失败尾段" in response.text
    assert "任务结论如何生成" in response.text
    assert "所有工作流统一评分" in response.text
    assert "所有工作流使用同一套语义评分轴" in response.text
    assert "评测场景提供稳定的业务事实和验收要求" in response.text


def test_overview_and_case_library_link_to_rulebook(client: TestClient) -> None:
    overview = client.get("/framework-evaluation")
    cases = client.get("/framework-evaluation/cases")

    assert overview.status_code == 200
    assert cases.status_code == 200
    expected_link = 'href="/framework-evaluation/rules"'
    assert expected_link in overview.text
    assert expected_link in cases.text
    expected_sidebar_item = 'data-sidebar-path="/framework-evaluation/rules"'
    assert expected_sidebar_item in overview.text
    assert expected_sidebar_item in cases.text


def test_rulebook_is_active_in_the_framework_sidebar(client: TestClient) -> None:
    response = client.get("/framework-evaluation/rules")

    assert response.status_code == 200
    assert 'data-sidebar-path="/framework-evaluation/rules"' in response.text
    assert 'data-active-sidebar-path="/framework-evaluation/rules"' in response.text
    assert 'data-top-nav="framework-evaluation-rules"' not in response.text


def test_framework_sidebar_links_to_task_creation_and_progress(client: TestClient) -> None:
    for path in (
        "/framework-evaluation",
        "/framework-evaluation/cases",
        "/framework-evaluation/rules",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert 'data-sidebar-path="/framework-evaluation/runs/new"' in response.text
        assert 'data-sidebar-path="/framework-evaluation/runs"' in response.text
        assert 'data-sidebar-path="/framework-evaluation/reports"' not in response.text

    creation = client.get("/framework-evaluation/runs/new")
    progress = client.get("/framework-evaluation/runs")
    assert "发起评测" in creation.text
    assert "评测任务" in progress.text
    reports = client.get("/framework-evaluation/reports", follow_redirects=False)
    assert reports.status_code == 404


def test_rulebook_lists_four_equal_workflow_axes_and_outcome_axes(client: TestClient) -> None:
    response = client.get("/framework-evaluation/rules")

    assert response.status_code == 200
    for axis in (
        "goal_requirement_decomposition",
        "decision_planning_quality",
        "implementation_traceability",
        "verification_completion_quality",
    ):
        assert f'data-workflow-axis="{axis}"' in response.text
    assert response.text.count('class="px-3 py-3 text-right font-mono text-lg font-semibold text-accent">25') == 4
    assert 'data-outcome-axis="requirement_fulfillment"' in response.text
    assert 'data-outcome-axis="implementation_quality"' in response.text
    assert "Requirement 满足度 90%，本次交付实现质量 10%" in response.text
    assert "业务结果记 0 分，但工作流质量继续独立评分" in response.text
    assert "PASS 后调用 Outcome Judge" in response.text
    assert "需求满足轴必须覆盖全部冻结 Requirement" in response.text
    assert 'data-workflow-terminal-outcome-policy="independent"' in response.text
    assert "双方 Efficiency 均为 N/A" in response.text
    assert "已有产物仍按四轴评分" in response.text
    assert "不把某个维度或 Workflow 总分强制记 0" in response.text
    assert "docs/specs/completed/{feature}/**/*.md" in response.text


def test_rulebook_lists_replayable_scoring_formulas(client: TestClient) -> None:
    response = client.get("/framework-evaluation/rules")

    assert response.status_code == 200
    assert "Unified = Outcome × 60% + Workflow × 20% + Efficiency × 20%" in response.text
    assert "Outcome = RequirementFulfillment × 90% + ImplementationQuality × 10%" in response.text
    assert "Advantage = (Base - Candidate) / max(Base, Candidate)" in response.text
    assert "A = TokenAdvantage × 50% + TimeAdvantage × 40% + ReworkAdvantage × 10%" in response.text
    assert "BaseEfficiency = 50 - A × 50；CandidateEfficiency = 50 + A × 50" in response.text
    assert "Coverage = 纳入 Case 数 / 已选 Case 数" in response.text
    assert "Outcome 75% + Workflow 25%" in response.text
    assert "双方都只按 Outcome 计综合分" in response.text
    assert "Base、Candidate 都业务失败的 Case 作为共同失败忽略" in response.text


def test_rulebook_lists_trusted_judge_score_anchors(client: TestClient) -> None:
    response = client.get("/framework-evaluation/rules")

    assert response.status_code == 200
    assert 'data-score-contract="anchored-v1"' in response.text
    assert "AI 语义轴只允许五档" in response.text
    expected = {
        0: "未实现或与要求冲突",
        25: "仅满足少量要求，主体目标未达成",
        50: "部分满足，存在明显业务或质量缺口",
        75: "核心要求满足，但存在实质性非阻断缺口",
        100: "完整满足，没有实质性缺口",
    }
    for score, meaning in expected.items():
        assert f'data-score-anchor="{score}"' in response.text
        assert meaning in response.text
    assert "低于 100 分必须列出明确扣分项" in response.text


def test_rulebook_lists_ordered_adoption_and_reference_rules(client: TestClient) -> None:
    response = client.get("/framework-evaluation/rules")

    assert response.status_code == 200
    assert 'data-decision-policy="unified-evaluation.v7/promotion-policy"' in response.text
    assert "Δ = Candidate - Base" in response.text
    for outcome in (
        "BLOCKED",
        "NOT_COMPARABLE",
        "PARTIALLY_COMPARABLE",
        "RECOMMEND",
        "DO_NOT_RECOMMEND",
        "NO_CLEAR_GAIN",
    ):
        assert f'data-decision-outcome="{outcome}"' in response.text
    assert "Outcome Δ ≥ 0，且 Unified Δ ≥ +5" in response.text
    assert "Unified Δ ≤ -5，或 Outcome Δ ≤ -5" in response.text
    assert "Candidate 业务回归 Case 数 &gt; 1" in response.text
    assert "业务回归 Case 数 ≤ 1" in response.text
    assert "先按业务结果决定是否纳入" in response.text
    assert "双方都失败则忽略" in response.text
    assert "不等于自动合并、发布或升级" in response.text
    assert "Reference 是可选参考，不是正式资格硬门禁" not in response.text
    assert 'data-hard-gates-summary="true"' in response.text
    assert 'data-hard-gate="UNACCEPTABLE_SECURITY_REGRESSION"' in response.text
    assert 'data-hard-gate="CORE_BUSINESS_BEHAVIOR_FAILURE"' in response.text
    assert 'data-hard-gate="CRITICAL_EVIDENCE_MISSING"' not in response.text
    assert 'data-hard-gate="WORKFLOW_TERMINAL_FAILURE"' in response.text
    assert 'data-gate-scope="GLOBAL"' in response.text
    assert 'data-gate-scope="CASE"' in response.text
