"""Server-owned presentation model for the current evaluation rulebook."""

from __future__ import annotations

from dataclasses import dataclass

from bench.evaluation_contracts import (
    UNIFIED_SCORING_PROFILE,
    ScoringProfile,
    validate_scoring_profile,
)
from bench.semantic_judging import trusted_score_contract


@dataclass(frozen=True)
class ScoreComponentRule:
    """One visible component of the unified score."""

    key: str
    label: str
    weight: int
    description: str


@dataclass(frozen=True)
class AxisRule:
    """One fixed semantic scoring axis."""

    key: str
    label: str
    weight: int
    description: str


@dataclass(frozen=True)
class EfficiencyMetricRule:
    """One attributable resource observation used by Efficiency."""

    key: str
    label: str
    weight: int
    description: str


@dataclass(frozen=True)
class FormulaRule:
    """One replayable calculation step shown in execution order."""

    label: str
    formula: str
    explanation: str


@dataclass(frozen=True)
class HardGateRule:
    """Readable explanation of one machine-enforced hard gate."""

    gate_id: str
    label: str
    scope: str
    description: str


@dataclass(frozen=True)
class DecisionOutcomeRule:
    """One ordered result or recommendation rule."""

    code: str
    label: str
    condition: str
    effect: str
    tone: str


@dataclass(frozen=True)
class DecisionPolicyRule:
    """Current coverage and promotion policy frozen with the Run."""

    version: str
    comparison_formula: str
    outcomes: tuple[DecisionOutcomeRule, ...]


@dataclass(frozen=True)
class ScoreAnchorRule:
    """One allowed semantic Judge score and its frozen meaning."""

    score: int
    meaning: str


@dataclass(frozen=True)
class EvaluationRulebook:
    """Complete display projection of the executable v2 scoring contract."""

    profile: ScoringProfile
    components: tuple[ScoreComponentRule, ...]
    outcome_axes: tuple[AxisRule, ...]
    workflow_axes: tuple[AxisRule, ...]
    efficiency_metrics: tuple[EfficiencyMetricRule, ...]
    formulas: tuple[FormulaRule, ...]
    global_hard_gates: tuple[HardGateRule, ...]
    case_hard_gates: tuple[HardGateRule, ...]
    decision_policy: DecisionPolicyRule


_COMPONENT_COPY = {
    "outcome_quality": (
        "Outcome Quality",
        "结果质量",
        "F2P/P2P 全部通过后，结合冻结 Requirement、阶段产物和最终代码评价需求满足度与交付实现质量。",
    ),
    "framework_workflow_quality": (
        "Framework Workflow Quality",
        "框架工作流质量",
        "按实际形成的 Markdown 证据使用统一四轴评分；不要求两侧拥有相同命令、阶段、文件名或文件数量。",
    ),
    "efficiency": (
        "Efficiency",
        "效率",
        "只比较可归责 HelixHarness 的 Agent Token、有效执行时间和工作流返工；不计排队、Judge 与基础设施恢复。",
    ),
}

_AXIS_COPY = {
    "requirement_fulfillment": (
        "Requirement 满足度",
        "业务门禁通过后，评价最终代码是否完整实现 Case 中现有的每条 Requirement。",
    ),
    "implementation_quality": (
        "本次交付实现质量",
        "默认评价本次生产代码；仅当 Requirement 明确要求测试交付物时评价对应测试代码。",
    ),
    "goal_requirement_decomposition": (
        "目标理解与需求拆解",
        "评价目标、范围、约束、边界、异常和失败场景是否被正确理解并充分拆解。",
    ),
    "decision_planning_quality": (
        "决策与计划质量",
        "评价真实取舍与实施计划是否合理、可执行并覆盖关键约束。",
    ),
    "implementation_traceability": (
        "实现说明追溯",
        "只评价阶段产物中的 Requirement、决策、任务、实现说明、验证说明和证据能否一致追溯。",
    ),
    "verification_completion_quality": (
        "验证与完成说明质量",
        "评价阶段产物中的验证范围、方法、预期结果、完成项、遗留风险和后续事项是否完整且一致。",
    ),
}

_GATE_DESCRIPTIONS = {
    "UNACCEPTABLE_SECURITY_REGRESSION": "出现不可接受的安全回归。",
    "EVIDENCE_CONTAMINATION": "执行侧接触或污染了受控评测证据。",
    "CHEATING": "发现规避正常实现或操纵评测结果的作弊行为。",
    "FALSE_COMPLETION": "完成声明与实际执行终态或证据不一致。",
    "CORE_BUSINESS_BEHAVIOR_FAILURE": "当前 Case 要求新增或修复的业务行为未达成。",
    "PROTECTED_REGRESSION_FAILURE": "当前 Case 要求保护的既有行为发生回归。",
    "INIT_OUTPUT_INVALID": "HelixHarness 初始化阶段不满足平台初始化产物合同。",
    "CRITICAL_EVIDENCE_MISSING": "历史记录中的关键证据缺失，统一归入工作流未完成。",
    "WORKFLOW_TERMINAL_FAILURE": (
        "执行侧未达到冻结 Execution Strategy 的合法终态，或归档所需最终证据没有形成；"
        "已有产物仍按统一四轴评分。"
    ),
    # Readable labels for the separate trusted-report subsystem. These IDs are
    # not accepted by the unified Scenario v2 Scoring Profile.
    "HIDDEN_CORE_BEHAVIOR_FAILURE": "隐藏核心行为检查失败。",
    "LOCAL_REQUIRED_STAGE_ARTIFACT_FAILURE": "局部修改缺少必需阶段产物，或产物未通过确定性检查。",
    "CHAIN_REQUIRED_MILESTONE_FAILURE": "完整链路的必需里程碑或门禁未通过。",
    "CHAIN_REQUIRED_STAGE_ARTIFACT_FAILURE": "完整链路缺少必需阶段产物，或产物未通过确定性检查。",
    "CHAIN_COMPLETION_TRUTH_FAILURE": "完整链路的完成声明与实际结果不一致。",
    "FROZEN_WORKFLOW_COMPLETION_FAILURE": (
        "执行侧未完成任务创建时冻结的全部必需工作流步骤，不能由质量、Token 或耗时得分抵消。"
    ),
}

_GATE_LABELS = {
    "UNACCEPTABLE_SECURITY_REGRESSION": "不可接受的安全回归",
    "EVIDENCE_CONTAMINATION": "受控评测证据被污染",
    "CHEATING": "作弊行为",
    "FALSE_COMPLETION": "虚假完成",
    "CORE_BUSINESS_BEHAVIOR_FAILURE": "核心业务行为失败",
    "PROTECTED_REGRESSION_FAILURE": "受保护行为回归",
    "INIT_OUTPUT_INVALID": "初始化输出无效",
    "CRITICAL_EVIDENCE_MISSING": "工作流未完成",
    "WORKFLOW_TERMINAL_FAILURE": "工作流未完成",
    "HIDDEN_CORE_BEHAVIOR_FAILURE": "隐藏核心行为检查失败",
    "LOCAL_REQUIRED_STAGE_ARTIFACT_FAILURE": "局部修改阶段产物未通过",
    "CHAIN_REQUIRED_MILESTONE_FAILURE": "完整链路必需里程碑未通过",
    "CHAIN_REQUIRED_STAGE_ARTIFACT_FAILURE": "完整链路阶段产物未通过",
    "CHAIN_COMPLETION_TRUTH_FAILURE": "完整链路完成声明不一致",
    "FROZEN_WORKFLOW_COMPLETION_FAILURE": "冻结工作流未完成",
}


def current_evaluation_rulebook() -> EvaluationRulebook:
    """Project the executable unified v2 profile into the rules page."""

    profile = UNIFIED_SCORING_PROFILE
    validate_scoring_profile(profile)
    components = (
        _component_rule("outcome_quality", profile.outcome_weight),
        _component_rule("framework_workflow_quality", profile.workflow_weight),
        _component_rule("efficiency", profile.efficiency_weight),
    )
    outcome_axes = (
        AxisRule(
            "requirement_fulfillment",
            _AXIS_COPY["requirement_fulfillment"][0],
            profile.requirement_fulfillment_weight,
            _AXIS_COPY["requirement_fulfillment"][1],
        ),
        AxisRule(
            "implementation_quality",
            _AXIS_COPY["implementation_quality"][0],
            profile.implementation_quality_weight,
            _AXIS_COPY["implementation_quality"][1],
        ),
    )
    workflow_axes = tuple(
        AxisRule(key=key, label=_AXIS_COPY[key][0], weight=weight, description=_AXIS_COPY[key][1])
        for key, weight in profile.workflow_axes
    )
    metrics = (
        EfficiencyMetricRule(
            "agent_tokens",
            "Agent Token",
            profile.token_efficiency_weight,
            "评分只统计可归责业务轨迹的 Token；实际 Attempt 消耗仅用于成本和稳定性观察。",
        ),
        EfficiencyMetricRule(
            "active_execution_seconds",
            "有效执行时间",
            profile.active_time_efficiency_weight,
            "按纳入业务轨迹的 Turn 时长求和，排除排队、Judge、暂停空闲和基础设施失败尾段。",
        ),
        EfficiencyMetricRule(
            "workflow_rework_actions",
            "工作流返工",
            profile.rework_efficiency_weight,
            "只统计工作流自身导致、且由冻结策略可归责的返工动作。",
        ),
    )
    return EvaluationRulebook(
        profile=profile,
        components=components,
        outcome_axes=outcome_axes,
        workflow_axes=workflow_axes,
        efficiency_metrics=metrics,
        formulas=_formula_rules(),
        global_hard_gates=_gate_rules(profile.global_hard_gates, scope="GLOBAL / 整个任务"),
        case_hard_gates=_gate_rules(profile.case_hard_gates, scope="CASE / 单个 Case"),
        decision_policy=_decision_policy(profile),
    )


def score_anchor_rules() -> tuple[ScoreAnchorRule, ...]:
    """Project the trusted Judge score contract into display order."""

    contract = trusted_score_contract()
    meanings = contract["anchor_meanings"]
    assert isinstance(meanings, dict)
    return tuple(
        ScoreAnchorRule(int(score), str(meanings[str(score)]))
        for score in contract["anchors"]
    )


def hard_gate_display(gate_id: str) -> dict[str, str]:
    """Return stable user-facing copy for current gate IDs."""

    return {
        "gate_id": gate_id,
        "title": _GATE_LABELS.get(gate_id, "未配置名称的硬门禁"),
        "description": _GATE_DESCRIPTIONS.get(
            gate_id,
            "该硬门禁尚未配置中文说明，请结合保留的原始 ID 定位。",
        ),
    }


def hard_gate_summary(gate_ids: tuple[str, ...] | list[str]) -> str:
    """Summarize persisted hard gates without exposing raw IDs as primary copy."""

    labels = [hard_gate_display(gate_id)["title"] for gate_id in dict.fromkeys(gate_ids)]
    return "、".join(labels)


def _component_rule(key: str, weight: int) -> ScoreComponentRule:
    english_label, chinese_label, description = _COMPONENT_COPY[key]
    return ScoreComponentRule(
        key=key,
        label=f"{english_label} / {chinese_label}",
        weight=weight,
        description=description,
    )


def _formula_rules() -> tuple[FormulaRule, ...]:
    return (
        FormulaRule(
            "单个 Case 的单侧综合分",
            "Unified = Outcome × 60% + Workflow × 20% + Efficiency × 20%",
            "三个分项独立展示。双方 Workflow 都可评分但 Efficiency 为 N/A 时，按 Outcome 75% + Workflow 25% 归一化；任一侧完全没有 Workflow 分时，该 Case 双方都只按 Outcome 计综合分。",
        ),
        FormulaRule(
            "Outcome 内部评分",
            "Outcome = RequirementFulfillment × 90% + ImplementationQuality × 10%",
            "只有全部 F2P/P2P 通过才调用 Outcome Judge；阶段产物只辅助理解，不以文档质量直接加分。",
        ),
        FormulaRule(
            "每项资源相对收益",
            "Advantage = (Base - Candidate) / max(Base, Candidate)",
            "Token、有效执行时间、工作流返工分别计算；两侧都为 0 时该项收益为 0，正数表示 Candidate 更省。",
        ),
        FormulaRule(
            "效率综合收益",
            "A = TokenAdvantage × 50% + TimeAdvantage × 40% + ReworkAdvantage × 10%",
            "只有双方业务验证都通过、工作流都达到可信终态，且资源证据完整可比时才计算 Efficiency；任一条件不满足则双方 Efficiency 均为 N/A。",
        ),
        FormulaRule(
            "两侧效率分",
            "BaseEfficiency = 50 - A × 50；CandidateEfficiency = 50 + A × 50",
            "同一相对收益对称映射到两侧 0–100 分，避免使用不同评分尺度。",
        ),
        FormulaRule(
            "多 Case 业务矩阵与汇总",
            "SideScore = Σ 纳入 CaseScore / 纳入 Case 数；Coverage = 纳入 Case 数 / 已选 Case 数",
            "Base、Candidate 都业务失败的 Case 作为共同失败忽略；只有 Base 通过而 Candidate 失败时 Candidate Outcome 记 0，反向则 Base Outcome 记 0；双方业务通过时都按实际分数纳入。业务结论不确定的 Case 不归责且不生成正式推荐。",
        ),
    )


def _gate_rules(gate_ids: tuple[str, ...], *, scope: str) -> tuple[HardGateRule, ...]:
    return tuple(
        HardGateRule(
            gate_id=gate_id,
            label=_GATE_LABELS[gate_id],
            scope=scope,
            description=_GATE_DESCRIPTIONS[gate_id],
        )
        for gate_id in gate_ids
    )


def _decision_policy(profile: ScoringProfile) -> DecisionPolicyRule:
    recommend = _display_threshold(profile.recommend_unified_delta)
    minimum_outcome = _display_threshold(profile.recommend_minimum_outcome_delta)
    reject_unified = _display_threshold(profile.do_not_recommend_unified_delta)
    reject_outcome = _display_threshold(profile.do_not_recommend_outcome_delta)
    return DecisionPolicyRule(
        version=f"{profile.version}/promotion-policy",
        comparison_formula="Δ = Candidate - Base",
        outcomes=(
            DecisionOutcomeRule(
                "BLOCKED",
                "任务阻断",
                "任一执行侧触发 GLOBAL 硬门禁",
                "阻断整个任务，不计算正式晋级建议。",
                "rose",
            ),
            DecisionOutcomeRule(
                "NOT_COMPARABLE",
                "不可比较",
                "纳入评分 Case 数 = 0，或存在业务验证不确定",
                "保留可用 Outcome、Workflow 与执行证据，不伪造业务结论，也不归责基础设施或 Candidate 质量。",
                "slate",
            ),
            DecisionOutcomeRule(
                "PARTIALLY_COMPARABLE",
                "部分可比较",
                "0 < 纳入评分 Case 数 < 已选 Case 数",
                "共同业务失败 Case 不进入聚合；基于其余 Case 计算分差，并明确展示覆盖率与忽略原因。",
                "blue",
            ),
            DecisionOutcomeRule(
                "DO_NOT_RECOMMEND",
                "不建议采纳 Candidate",
                f"Candidate 业务回归 Case 数 > {profile.candidate_business_regression_tolerance}，或 Unified Δ ≤ {reject_unified}，或 Outcome Δ ≤ {reject_outcome}",
                "允许最多 1 个 Base 通过而 Candidate 失败的 Case 继续进入正常算分；超过容错数量时直接不建议。",
                "amber",
            ),
            DecisionOutcomeRule(
                "RECOMMEND",
                "建议采纳 Candidate",
                f"业务回归 Case 数 ≤ {profile.candidate_business_regression_tolerance}，Outcome Δ ≥ {minimum_outcome}，且 Unified Δ ≥ +{recommend}",
                "结论代表纳入评分的 Case；必须同时展示共同失败、工作流未完成和 Candidate 风险，不等于自动合并、发布或升级。",
                "emerald",
            ),
            DecisionOutcomeRule(
                "NO_CLEAR_GAIN",
                "没有明确收益",
                "可比较 Case 数 > 0，且未命中不建议或建议条件",
                "保留完整分项和差值，不把小幅变化解释为明确改进。",
                "blue",
            ),
        ),
    )


def _display_threshold(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
