from __future__ import annotations

from dataclasses import replace

import pytest

from bench.evaluation_contracts import UNIFIED_SCORING_PROFILE
from bench.scoring import (
    CaseEvaluation,
    ResourceObservation,
    SideEvaluation,
    aggregate_run,
)


def _side(
    outcome: float,
    workflow: float | None,
    *,
    tokens: float = 100,
    seconds: float = 100,
    rework: float = 2,
    case_gates: tuple[str, ...] = (),
    global_gates: tuple[str, ...] = (),
    reached_business_terminal: bool = True,
    business_status: str = "PASS",
) -> SideEvaluation:
    return SideEvaluation(
        outcome_quality=outcome,
        framework_workflow_quality=workflow,
        resources=ResourceObservation(
            agent_tokens=tokens,
            active_execution_seconds=seconds,
            workflow_rework_actions=rework,
        ),
        case_hard_gates=case_gates,
        global_hard_gates=global_gates,
        reached_comparable_business_terminal=reached_business_terminal,
        business_verification_status=business_status,  # type: ignore[arg-type]
    )


def test_case_score_combines_outcome_workflow_and_attributable_efficiency() -> None:
    result = aggregate_run(
        (
            CaseEvaluation(
                case_id="case-a",
                base=_side(80, 70),
                candidate=_side(85, 80, tokens=50, seconds=80, rework=1),
            ),
        )
    )

    case = result.cases[0]
    assert case.base.efficiency == 31.0
    assert case.candidate.efficiency == 69.0
    assert case.base.unified_score == 68.2
    assert case.candidate.unified_score == 80.8
    assert result.unified_delta == 12.6
    assert result.recommendation == "RECOMMEND"


def test_workflow_evidence_gap_keeps_business_case_and_uses_outcome_only() -> None:
    result = aggregate_run(
        (
            CaseEvaluation("case-a", _side(70, 70), _side(80, 80)),
            CaseEvaluation(
                "case-b",
                replace(_side(90, 90), framework_workflow_quality=None),
                replace(_side(10, 10), framework_workflow_quality=None),
                assessment_compatible=False,
                incompatibility_reasons=("COMPLETION_RECORD mapping is missing",),
            ),
        )
    )

    assert result.status == "FULLY_COMPARABLE"
    assert result.comparable_case_count == 2
    assert result.selected_case_count == 2
    assert result.comparable_coverage == 1.0
    assert result.base_unified_score == 78.0
    assert result.candidate_unified_score == 42.0
    assert result.recommendation == "DO_NOT_RECOMMEND"
    assert result.cases[1].base.outcome_quality == 90
    assert result.cases[1].candidate.outcome_quality == 10
    assert result.cases[1].base.framework_workflow_quality is None
    assert result.cases[1].candidate.unified_score == 10
    assert result.cases[1].incompatibility_reasons == ()


def test_base_business_failure_scores_zero_outcome_and_preserves_workflow_quality() -> None:
    result = aggregate_run(
        (
            CaseEvaluation(
                "case-a",
                _side(
                    0,
                    70,
                    case_gates=("CORE_BUSINESS_BEHAVIOR_FAILURE",),
                    reached_business_terminal=False,
                    business_status="FAIL",
                ),
                _side(100, 90, tokens=1, seconds=1, rework=0),
            ),
        )
    )

    case = result.cases[0]
    assert result.status == "FULLY_COMPARABLE"
    assert case.base.outcome_quality == 0
    assert case.candidate.outcome_quality == 100
    assert case.base.framework_workflow_quality == 70
    assert case.candidate.framework_workflow_quality == 90
    assert case.base.efficiency is None
    assert case.candidate.efficiency is None
    assert case.base.unified_score == 17.5
    assert case.candidate.unified_score == 97.5
    assert result.base_case_hard_gates == ("CORE_BUSINESS_BEHAVIOR_FAILURE",)
    assert result.candidate_case_hard_gates == ()
    assert case.candidate_new_hard_gates == ()
    assert result.recommendation == "RECOMMEND"


def test_retired_efficiency_formula_is_rejected() -> None:
    from bench.evaluation_contracts import ContractValidationError, validate_scoring_profile
    profile=replace(UNIFIED_SCORING_PROFILE, efficiency_na_policy="neutral-50.v1")
    with pytest.raises(ContractValidationError, match="Unsupported"):
        validate_scoring_profile(profile)


def test_workflow_gain_cannot_hide_any_outcome_regression() -> None:
    result = aggregate_run(
        (
            CaseEvaluation(
                "case-a",
                _side(80, 50),
                _side(78, 100, tokens=10, seconds=10, rework=0),
            ),
        )
    )

    assert result.unified_delta > 5
    assert result.outcome_delta == -2
    assert result.recommendation == "NO_CLEAR_GAIN"


def test_global_integrity_gate_blocks_the_whole_run() -> None:
    result = aggregate_run(
        (
            CaseEvaluation(
                "case-a",
                _side(80, 80),
                _side(90, 90, global_gates=("EVIDENCE_CONTAMINATION",)),
            ),
        )
    )

    assert result.status == "BLOCKED"
    assert result.global_hard_gates == ("EVIDENCE_CONTAMINATION",)
    assert result.recommendation is None


def test_selected_cases_are_aggregated_with_equal_weight() -> None:
    result = aggregate_run(
        (
            CaseEvaluation("small", _side(10, 10), _side(20, 20)),
            CaseEvaluation("large", _side(90, 90), _side(100, 100)),
        )
    )

    assert result.base_outcome_quality == 50.0
    assert result.candidate_outcome_quality == 60.0
    assert result.outcome_delta == 10.0


def test_candidate_case_failure_is_reported_without_vetoing_subset_recommendation() -> None:
    result = aggregate_run(
        (
            CaseEvaluation(
                "case-a",
                _side(
                    50,
                    50,
                    case_gates=("CORE_BUSINESS_BEHAVIOR_FAILURE",),
                    business_status="FAIL",
                ),
                _side(100, 100),
            ),
            CaseEvaluation(
                "case-b",
                _side(100, 100),
                _side(
                    100,
                    100,
                    case_gates=("CORE_BUSINESS_BEHAVIOR_FAILURE",),
                    business_status="FAIL",
                ),
            ),
        )
    )

    assert result.unified_delta > 5
    assert result.outcome_delta == 0
    assert result.recommendation == "RECOMMEND"
    assert result.cases[0].candidate_new_hard_gates == ()
    assert result.cases[1].candidate_new_hard_gates == ("CORE_BUSINESS_BEHAVIOR_FAILURE",)


def test_shared_business_failure_is_excluded_from_aggregation() -> None:
    result = aggregate_run(
        (
            CaseEvaluation(
                "shared-failure",
                _side(0, 70, business_status="FAIL"),
                _side(0, 80, business_status="FAIL"),
            ),
            CaseEvaluation("passing", _side(80, 70), _side(90, 80)),
        )
    )

    assert result.status == "PARTIALLY_COMPARABLE"
    assert result.shared_business_failure_case_count == 1
    assert result.comparable_case_count == 1
    assert result.cases[0].incompatibility_reasons == ("SHARED_BUSINESS_FAILURE",)
    assert result.base_outcome_quality == 80
    assert result.candidate_outcome_quality == 90


def test_one_side_without_workflow_preserves_visible_score_but_omits_it_from_both_composites() -> None:
    result = aggregate_run(
        (
            CaseEvaluation(
                "partial-workflow",
                _side(80, 60, case_gates=("WORKFLOW_TERMINAL_FAILURE",)),
                _side(90, None, case_gates=("CRITICAL_EVIDENCE_MISSING",)),
            ),
        )
    )

    case = result.cases[0]
    assert case.base.framework_workflow_quality == 60
    assert case.candidate.framework_workflow_quality is None
    assert case.base.unified_score == 80
    assert case.candidate.unified_score == 90
    assert case.base.efficiency is None
    assert case.candidate.efficiency is None
    assert result.base_case_hard_gates == ("WORKFLOW_TERMINAL_FAILURE",)
    assert result.candidate_case_hard_gates == ("WORKFLOW_TERMINAL_FAILURE",)


def test_candidate_business_regression_tolerance_only_vetoes_above_one_case() -> None:
    tolerant = replace(
        UNIFIED_SCORING_PROFILE,
        do_not_recommend_unified_delta=-101,
        do_not_recommend_outcome_delta=-101,
        recommend_unified_delta=101,
    )
    def regression(case_id: str) -> CaseEvaluation:
        return CaseEvaluation(
            case_id,
            _side(100, 100),
            _side(0, 100, business_status="FAIL"),
        )

    one = aggregate_run((regression("one"),), scoring_profile=tolerant)
    two = aggregate_run((regression("one"), regression("two")), scoring_profile=tolerant)

    assert one.candidate_business_regression_case_count == 1
    assert one.recommendation == "NO_CLEAR_GAIN"
    assert two.candidate_business_regression_case_count == 2
    assert two.recommendation == "DO_NOT_RECOMMEND"


def test_scoring_consumes_the_frozen_profile_instead_of_module_constants() -> None:
    outcome_only = replace(
        UNIFIED_SCORING_PROFILE,
        version="outcome-only.test",
        outcome_weight=100,
        workflow_weight=0,
        efficiency_weight=0,
    )

    result = aggregate_run(
        (CaseEvaluation("case-a", _side(40, 100), _side(60, 0)),),
        scoring_profile=outcome_only,
    )

    assert result.base_unified_score == 40
    assert result.candidate_unified_score == 60
    assert result.unified_delta == 20


def test_scoring_rejects_gates_not_declared_by_the_frozen_profile() -> None:
    with pytest.raises(ValueError, match="UNDECLARED_GATE"):
        aggregate_run(
            (
                CaseEvaluation(
                    "case-a",
                    _side(80, 80),
                    _side(90, 90, case_gates=("UNDECLARED_GATE",)),
                ),
            )
        )


def test_scoring_rejects_unknown_formula_identifiers() -> None:
    unsupported = replace(
        UNIFIED_SCORING_PROFILE,
        version="unsupported-formula.test",
        efficiency_formula="unknown.v9",
    )

    with pytest.raises(ValueError, match="unknown.v9"):
        aggregate_run(
            (CaseEvaluation("case-a", _side(80, 80), _side(90, 90)),),
            scoring_profile=unsupported,
        )


def test_scoring_rejects_formula_identifier_used_for_the_wrong_purpose() -> None:
    swapped = replace(
        UNIFIED_SCORING_PROFILE,
        version="swapped-formula.test",
        unified_formula="equal-selected-cases.v1",
        case_aggregation_formula="weighted-components.v1",
    )

    with pytest.raises(ValueError, match="unified formula"):
        aggregate_run(
            (CaseEvaluation("case-a", _side(80, 80), _side(90, 90)),),
            scoring_profile=swapped,
        )
