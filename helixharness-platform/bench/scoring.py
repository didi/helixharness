"""Workflow-neutral Case scoring and equal-weight Run aggregation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, TypeAlias

from bench.evaluation_contracts import (
    UNIFIED_SCORING_PROFILE,
    ScoringProfile,
    validate_scoring_profile,
)

RunComparisonStatus: TypeAlias = Literal[
    "FULLY_COMPARABLE",
    "PARTIALLY_COMPARABLE",
    "NOT_COMPARABLE",
    "BLOCKED",
]
BusinessVerificationStatus: TypeAlias = Literal["PASS", "FAIL", "INCONCLUSIVE"]
PromotionRecommendation: TypeAlias = Literal[
    "RECOMMEND",
    "DO_NOT_RECOMMEND",
    "NO_CLEAR_GAIN",
]


@dataclass(frozen=True)
class ResourceObservation:
    """Only execution-side resources attributable to HelixHarness."""

    agent_tokens: float
    active_execution_seconds: float
    workflow_rework_actions: float


@dataclass(frozen=True)
class SideEvaluation:
    """Independent absolute scores and gates for one execution side."""

    outcome_quality: float | None
    framework_workflow_quality: float | None
    resources: ResourceObservation | None = None
    case_hard_gates: tuple[str, ...] = ()
    global_hard_gates: tuple[str, ...] = ()
    reached_comparable_business_terminal: bool = True
    business_verification_status: BusinessVerificationStatus = "PASS"


@dataclass(frozen=True)
class CaseEvaluation:
    """Base/Candidate evidence calculation for one selected Scenario."""

    case_id: str
    base: SideEvaluation
    candidate: SideEvaluation
    assessment_compatible: bool = True
    incompatibility_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SideCaseScore:
    """One side's visible score components for a comparable Case."""

    outcome_quality: float | None
    framework_workflow_quality: float | None
    efficiency: float | None
    unified_score: float | None
    case_hard_gates: tuple[str, ...]


@dataclass(frozen=True)
class CaseComparisonScore:
    """Comparable score or explicit incompatibility for one selected Case."""

    case_id: str
    base: SideCaseScore
    candidate: SideCaseScore
    incompatibility_reasons: tuple[str, ...] = ()
    candidate_new_hard_gates: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunAggregation:
    """Equal-weight aggregate over only the Cases selected for this Run."""

    status: RunComparisonStatus
    selected_case_count: int
    comparable_case_count: int
    comparable_coverage: float
    shared_business_failure_case_count: int
    candidate_business_regression_case_count: int
    business_inconclusive_case_count: int
    cases: tuple[CaseComparisonScore, ...]
    base_outcome_quality: float | None
    candidate_outcome_quality: float | None
    outcome_delta: float | None
    base_unified_score: float | None
    candidate_unified_score: float | None
    unified_delta: float | None
    base_case_hard_gates: tuple[str, ...]
    candidate_case_hard_gates: tuple[str, ...]
    global_hard_gates: tuple[str, ...]
    recommendation: PromotionRecommendation | None


def aggregate_run(
    cases: tuple[CaseEvaluation, ...],
    *,
    scoring_profile: ScoringProfile = UNIFIED_SCORING_PROFILE,
) -> RunAggregation:
    """Score selected Cases equally and apply the v2 promotion policy."""

    validate_scoring_profile(scoring_profile)
    if not cases:
        raise ValueError("At least one selected Case is required")
    case_ids = [case.case_id for case in cases]
    if any(not case_id for case_id in case_ids) or len(set(case_ids)) != len(case_ids):
        raise ValueError("Selected Case IDs must be non-empty and unique")

    current_policy = (
        scoring_profile.case_aggregation_formula
        == "equal-business-comparable-cases-tolerant-regression.v3"
    )
    scored_cases: list[CaseComparisonScore] = []
    global_gates: set[str] = set()
    base_case_gates: set[str] = set()
    candidate_case_gates: set[str] = set()
    shared_business_failures = 0
    candidate_business_regressions = 0
    business_inconclusive_cases = 0
    for case in cases:
        _validate_side(
            case.base,
            case_id=case.case_id,
            side_name="BASE",
            scoring_profile=scoring_profile,
        )
        _validate_side(
            case.candidate,
            case_id=case.case_id,
            side_name="CANDIDATE",
            scoring_profile=scoring_profile,
        )
        global_gates.update(case.base.global_hard_gates)
        global_gates.update(case.candidate.global_hard_gates)
        base_gates = (
            merge_workflow_completion_gates(case.base.case_hard_gates)
            if current_policy
            else tuple(sorted(set(case.base.case_hard_gates)))
        )
        candidate_gates = (
            merge_workflow_completion_gates(case.candidate.case_hard_gates)
            if current_policy
            else tuple(sorted(set(case.candidate.case_hard_gates)))
        )
        base_case_gates.update(base_gates)
        candidate_case_gates.update(candidate_gates)
        candidate_new_hard_gates = tuple(
            sorted(set(candidate_gates) - set(base_gates))
        )
        if current_policy:
            base_status = case.base.business_verification_status
            candidate_status = case.candidate.business_verification_status
            current_reasons: tuple[str, ...] = ()
            if base_status == "INCONCLUSIVE" or candidate_status == "INCONCLUSIVE":
                current_reasons = tuple(
                    f"{side}: BUSINESS_VERIFICATION_INCONCLUSIVE"
                    for side, status in (("BASE", base_status), ("CANDIDATE", candidate_status))
                    if status == "INCONCLUSIVE"
                )
            elif base_status == "PASS" and case.base.outcome_quality is None:
                current_reasons = ("BASE: BUSINESS_VERIFICATION_INCONCLUSIVE",)
            elif candidate_status == "PASS" and case.candidate.outcome_quality is None:
                current_reasons = ("CANDIDATE: BUSINESS_VERIFICATION_INCONCLUSIVE",)
            if current_reasons:
                business_inconclusive_cases += 1
                scored_cases.append(
                    CaseComparisonScore(
                        case_id=case.case_id,
                        base=_excluded_side_score(case.base, case_hard_gates=base_gates),
                        candidate=_excluded_side_score(case.candidate, case_hard_gates=candidate_gates),
                        incompatibility_reasons=current_reasons,
                        candidate_new_hard_gates=candidate_new_hard_gates,
                    )
                )
                continue
            if base_status == "FAIL" and candidate_status == "FAIL":
                shared_business_failures += 1
                scored_cases.append(
                    CaseComparisonScore(
                        case_id=case.case_id,
                        base=_excluded_side_score(
                            case.base,
                            outcome_quality=0.0,
                            case_hard_gates=base_gates,
                        ),
                        candidate=_excluded_side_score(
                            case.candidate,
                            outcome_quality=0.0,
                            case_hard_gates=candidate_gates,
                        ),
                        incompatibility_reasons=("SHARED_BUSINESS_FAILURE",),
                        candidate_new_hard_gates=candidate_new_hard_gates,
                    )
                )
                continue
            if base_status == "PASS" and candidate_status == "FAIL":
                candidate_business_regressions += 1
            base_outcome = 0.0 if base_status == "FAIL" else _required_number(case.base.outcome_quality)
            candidate_outcome = (
                0.0 if candidate_status == "FAIL" else _required_number(case.candidate.outcome_quality)
            )
            workflow_comparable = (
                case.base.framework_workflow_quality is not None
                and case.candidate.framework_workflow_quality is not None
            )
            efficiency = (
                _efficiency_scores(case.base, case.candidate, scoring_profile=scoring_profile)
                if base_status == "PASS" and candidate_status == "PASS" and workflow_comparable
                else None
            )
            scored_cases.append(
                CaseComparisonScore(
                    case_id=case.case_id,
                    base=_current_side_score(
                        case.base,
                        outcome_quality=base_outcome,
                        workflow_comparable=workflow_comparable,
                        efficiency=efficiency[0] if efficiency else None,
                        case_hard_gates=base_gates,
                        scoring_profile=scoring_profile,
                    ),
                    candidate=_current_side_score(
                        case.candidate,
                        outcome_quality=candidate_outcome,
                        workflow_comparable=workflow_comparable,
                        efficiency=efficiency[1] if efficiency else None,
                        case_hard_gates=candidate_gates,
                        scoring_profile=scoring_profile,
                    ),
                    candidate_new_hard_gates=candidate_new_hard_gates,
                )
            )
            continue
        if not case.assessment_compatible:
            reasons = case.incompatibility_reasons or ("Assessment evidence is incompatible",)
            scored_cases.append(
                CaseComparisonScore(
                    case_id=case.case_id,
                    base=_incompatible_side_score(case.base),
                    candidate=_incompatible_side_score(case.candidate),
                    incompatibility_reasons=reasons,
                    candidate_new_hard_gates=candidate_new_hard_gates,
                )
            )
            continue
        if (
            case.base.outcome_quality is None
            or case.candidate.outcome_quality is None
            or case.base.framework_workflow_quality is None
            or case.candidate.framework_workflow_quality is None
        ):
            raise ValueError(f"{case.case_id} comparable Case requires Outcome and Workflow Quality")

        efficiency = _efficiency_scores(case.base, case.candidate, scoring_profile=scoring_profile)
        base_score = _side_score(
            case.base,
            efficiency=efficiency[0] if efficiency else None,
            scoring_profile=scoring_profile,
        )
        candidate_score = _side_score(
            case.candidate,
            efficiency=efficiency[1] if efficiency else None,
            scoring_profile=scoring_profile,
        )
        scored_cases.append(
            CaseComparisonScore(
                case_id=case.case_id,
                base=base_score,
                candidate=candidate_score,
                candidate_new_hard_gates=candidate_new_hard_gates,
            )
        )

    comparable = tuple(item for item in scored_cases if not item.incompatibility_reasons)
    comparable_count = len(comparable)
    coverage = round(comparable_count / len(cases), 4)
    sorted_global_gates = tuple(sorted(global_gates))
    if sorted_global_gates:
        status = "BLOCKED"
    elif comparable_count == 0:
        status = "NOT_COMPARABLE"
    elif comparable_count < len(cases):
        status = "PARTIALLY_COMPARABLE"
    else:
        status = "FULLY_COMPARABLE"

    base_outcome = (
        _aggregate_case_scores(
            (_required_number(item.base.outcome_quality) for item in comparable),
            scoring_profile=scoring_profile,
        )
        if comparable
        else None
    )
    candidate_outcome = (
        _aggregate_case_scores(
            (_required_number(item.candidate.outcome_quality) for item in comparable),
            scoring_profile=scoring_profile,
        )
        if comparable
        else None
    )
    base_unified = (
        _aggregate_case_scores(
            (_required_number(item.base.unified_score) for item in comparable),
            scoring_profile=scoring_profile,
        )
        if comparable
        else None
    )
    candidate_unified = (
        _aggregate_case_scores(
            (_required_number(item.candidate.unified_score) for item in comparable),
            scoring_profile=scoring_profile,
        )
        if comparable
        else None
    )
    outcome_delta = _difference(base_outcome, candidate_outcome)
    unified_delta = _difference(base_unified, candidate_unified)
    base_gates = tuple(sorted(base_case_gates))
    candidate_gates = tuple(sorted(candidate_case_gates))
    recommendation = None
    if (
        status in {"FULLY_COMPARABLE", "PARTIALLY_COMPARABLE"}
        and (not current_policy or business_inconclusive_cases == 0)
    ):
        recommendation = _promotion_decision(
            unified_delta=unified_delta,
            outcome_delta=outcome_delta,
            candidate_business_regression_count=candidate_business_regressions,
            scoring_profile=scoring_profile,
        )
    return RunAggregation(
        status=status,
        selected_case_count=len(cases),
        comparable_case_count=comparable_count,
        comparable_coverage=coverage,
        shared_business_failure_case_count=shared_business_failures,
        candidate_business_regression_case_count=candidate_business_regressions,
        business_inconclusive_case_count=business_inconclusive_cases,
        cases=tuple(scored_cases),
        base_outcome_quality=base_outcome,
        candidate_outcome_quality=candidate_outcome,
        outcome_delta=outcome_delta,
        base_unified_score=base_unified,
        candidate_unified_score=candidate_unified,
        unified_delta=unified_delta,
        base_case_hard_gates=base_gates,
        candidate_case_hard_gates=candidate_gates,
        global_hard_gates=sorted_global_gates,
        recommendation=recommendation,
    )


def _side_score(
    side: SideEvaluation,
    *,
    efficiency: float | None,
    scoring_profile: ScoringProfile,
) -> SideCaseScore:
    """Compose one comparable side using only the frozen Scoring Profile."""

    unified = _compose_unified_score(
        outcome_quality=_required_number(side.outcome_quality),
        workflow_quality=_required_number(side.framework_workflow_quality),
        efficiency=efficiency,
        scoring_profile=scoring_profile,
    )
    return SideCaseScore(
        outcome_quality=round(_required_number(side.outcome_quality), 2),
        framework_workflow_quality=round(_required_number(side.framework_workflow_quality), 2),
        efficiency=round(efficiency, 2) if efficiency is not None else None,
        unified_score=unified,
        case_hard_gates=tuple(sorted(set(side.case_hard_gates))),
    )


def _current_side_score(
    side: SideEvaluation,
    *,
    outcome_quality: float,
    workflow_comparable: bool,
    efficiency: float | None,
    case_hard_gates: tuple[str, ...],
    scoring_profile: ScoringProfile,
) -> SideCaseScore:
    """Compose one v7 side from the components mutually available to the pair."""

    workflow_quality = side.framework_workflow_quality
    unified = _compose_unified_score(
        outcome_quality=outcome_quality,
        workflow_quality=(
            _required_number(workflow_quality) if workflow_comparable else None
        ),
        efficiency=efficiency,
        scoring_profile=scoring_profile,
    )
    return SideCaseScore(
        outcome_quality=round(outcome_quality, 2),
        framework_workflow_quality=(
            round(workflow_quality, 2) if workflow_quality is not None else None
        ),
        efficiency=round(efficiency, 2) if efficiency is not None else None,
        unified_score=unified,
        case_hard_gates=case_hard_gates,
    )


def _compose_unified_score(
    *,
    outcome_quality: float,
    workflow_quality: float | None,
    efficiency: float | None,
    scoring_profile: ScoringProfile,
) -> float:
    """Execute the frozen weighted-components formula."""

    if scoring_profile.unified_formula not in {
        "renormalize-mutually-available-components.v2",
    }:
        raise ValueError(f"Unsupported unified formula: {scoring_profile.unified_formula}")
    weighted_sum = outcome_quality * scoring_profile.outcome_weight
    applied_weight = scoring_profile.outcome_weight
    if workflow_quality is not None:
        weighted_sum += workflow_quality * scoring_profile.workflow_weight
        applied_weight += scoring_profile.workflow_weight
    if efficiency is not None:
        weighted_sum += efficiency * scoring_profile.efficiency_weight
        applied_weight += scoring_profile.efficiency_weight
    elif scoring_profile.efficiency_na_policy != "renormalize-available-components.v1":
        raise ValueError(f"Unsupported efficiency N/A policy: {scoring_profile.efficiency_na_policy}")
    if applied_weight == 0:
        raise ValueError("Unified score has no available weighted component")
    return round(weighted_sum / applied_weight, 2)


def _incompatible_side_score(side: SideEvaluation) -> SideCaseScore:
    """Preserve reliable Outcome facts while withholding incompatible scores."""

    return SideCaseScore(
        outcome_quality=round(side.outcome_quality, 2) if side.outcome_quality is not None else None,
        framework_workflow_quality=None,
        efficiency=None,
        unified_score=None,
        case_hard_gates=tuple(sorted(set(side.case_hard_gates))),
    )


def _excluded_side_score(
    side: SideEvaluation,
    *,
    outcome_quality: float | None = None,
    case_hard_gates: tuple[str, ...],
) -> SideCaseScore:
    """Preserve every trustworthy single-side component for an excluded pair."""

    resolved_outcome = side.outcome_quality if outcome_quality is None else outcome_quality
    workflow = side.framework_workflow_quality
    return SideCaseScore(
        outcome_quality=round(resolved_outcome, 2) if resolved_outcome is not None else None,
        framework_workflow_quality=round(workflow, 2) if workflow is not None else None,
        efficiency=None,
        unified_score=None,
        case_hard_gates=case_hard_gates,
    )


def merge_workflow_completion_gates(gates: Iterable[str]) -> tuple[str, ...]:
    """Collapse missing final evidence into the single workflow-incomplete fact."""

    normalized = set(gates)
    if "CRITICAL_EVIDENCE_MISSING" in normalized:
        normalized.remove("CRITICAL_EVIDENCE_MISSING")
        normalized.add("WORKFLOW_TERMINAL_FAILURE")
    return tuple(sorted(normalized))


def _efficiency_scores(
    base: SideEvaluation,
    candidate: SideEvaluation,
    *,
    scoring_profile: ScoringProfile,
) -> tuple[float, float] | None:
    """Score only attributable costs when both sides reached valid terminals."""

    if scoring_profile.efficiency_formula != "symmetric-relative-cost.v1":
        raise ValueError(f"Unsupported efficiency formula: {scoring_profile.efficiency_formula}")
    if (
        not base.reached_comparable_business_terminal
        or not candidate.reached_comparable_business_terminal
        or base.case_hard_gates
        or candidate.case_hard_gates
        or base.resources is None
        or candidate.resources is None
    ):
        return None
    base_metrics = (
        base.resources.agent_tokens,
        base.resources.active_execution_seconds,
        base.resources.workflow_rework_actions,
    )
    candidate_metrics = (
        candidate.resources.agent_tokens,
        candidate.resources.active_execution_seconds,
        candidate.resources.workflow_rework_actions,
    )
    weights = (
        scoring_profile.token_efficiency_weight / 100,
        scoring_profile.active_time_efficiency_weight / 100,
        scoring_profile.rework_efficiency_weight / 100,
    )
    candidate_advantage = sum(
        _relative_cost_advantage(base_value, candidate_value) * weight
        for base_value, candidate_value, weight in zip(base_metrics, candidate_metrics, weights)
    )
    return (
        round(50.0 - candidate_advantage * 50.0, 2),
        round(50.0 + candidate_advantage * 50.0, 2),
    )


def _relative_cost_advantage(base: float, candidate: float) -> float:
    """Return Candidate cost improvement on a symmetric -1 to 1 scale."""

    denominator = max(base, candidate)
    return 0.0 if denominator == 0 else (base - candidate) / denominator


def _promotion_decision(
    *,
    unified_delta: float | None,
    outcome_delta: float | None,
    candidate_business_regression_count: int = 0,
    scoring_profile: ScoringProfile,
) -> PromotionRecommendation:
    """Apply score thresholds to every non-empty comparable Case subset."""

    if unified_delta is None or outcome_delta is None:
        raise ValueError("Comparable Run is missing aggregate scores")
    if (
        scoring_profile.case_aggregation_formula
        == "equal-business-comparable-cases-tolerant-regression.v3"
        and candidate_business_regression_count
        > scoring_profile.candidate_business_regression_tolerance
    ):
        return "DO_NOT_RECOMMEND"
    if (
        unified_delta <= scoring_profile.do_not_recommend_unified_delta
        or outcome_delta <= scoring_profile.do_not_recommend_outcome_delta
    ):
        return "DO_NOT_RECOMMEND"
    if (
        unified_delta >= scoring_profile.recommend_unified_delta
        and outcome_delta >= scoring_profile.recommend_minimum_outcome_delta
    ):
        return "RECOMMEND"
    return "NO_CLEAR_GAIN"


def _validate_side(
    evaluation: SideEvaluation,
    *,
    case_id: str,
    side_name: str,
    scoring_profile: ScoringProfile,
) -> None:
    """Validate scores, resources, and gates against the frozen profile."""

    if evaluation.business_verification_status not in {"PASS", "FAIL", "INCONCLUSIVE"}:
        raise ValueError(f"{case_id} {side_name} business verification status is invalid")
    scores: list[tuple[str, float]] = []
    if evaluation.outcome_quality is not None:
        scores.append(("outcome_quality", evaluation.outcome_quality))
    if evaluation.framework_workflow_quality is not None:
        scores.append(("framework_workflow_quality", evaluation.framework_workflow_quality))
    for label, score in scores:
        if not 0 <= score <= 100:
            raise ValueError(f"{case_id} {side_name} {label} must be between 0 and 100")
    if evaluation.resources is not None and any(
        value < 0
        for value in (
            evaluation.resources.agent_tokens,
            evaluation.resources.active_execution_seconds,
            evaluation.resources.workflow_rework_actions,
        )
    ):
        raise ValueError(f"{case_id} {side_name} resource observations must be non-negative")
    undeclared_case_gates = set(evaluation.case_hard_gates) - set(scoring_profile.case_hard_gates)
    if (
        "CRITICAL_EVIDENCE_MISSING" in undeclared_case_gates
        and "WORKFLOW_TERMINAL_FAILURE" in scoring_profile.case_hard_gates
    ):
        undeclared_case_gates.remove("CRITICAL_EVIDENCE_MISSING")
    undeclared_global_gates = set(evaluation.global_hard_gates) - set(scoring_profile.global_hard_gates)
    if undeclared_case_gates or undeclared_global_gates:
        raise ValueError(
            f"{case_id} {side_name} uses gates not declared by the Scoring Profile: "
            + ", ".join(sorted(undeclared_case_gates | undeclared_global_gates))
        )


def _average(values: Iterable[float]) -> float:
    """Return a stable equal-weight aggregate."""

    materialized = tuple(values)
    return round(sum(materialized) / len(materialized), 2)


def _aggregate_case_scores(
    values: Iterable[float],
    *,
    scoring_profile: ScoringProfile,
) -> float:
    """Execute the frozen equal-selected-Cases aggregation formula."""

    if scoring_profile.case_aggregation_formula not in {
        "equal-business-comparable-cases-tolerant-regression.v3",
    }:
        raise ValueError(
            f"Unsupported case aggregation formula: {scoring_profile.case_aggregation_formula}"
        )
    return _average(values)


def _difference(base: float | None, candidate: float | None) -> float | None:
    """Return Candidate minus Base when both aggregates exist."""

    if base is None or candidate is None:
        return None
    return round(candidate - base, 2)


def _required_number(value: float | None) -> float:
    """Narrow a score already guaranteed by comparable-case selection."""

    if value is None:
        raise ValueError("Comparable Case is missing a required score")
    return value
