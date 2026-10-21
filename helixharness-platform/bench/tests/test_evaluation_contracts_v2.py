from __future__ import annotations

from dataclasses import replace

import pytest

from bench.evaluation_contracts import (
    UNIFIED_SCORING_PROFILE,
    ArtifactSource,
    ContractValidationError,
    ExecutionStrategy,
    FindingClosure,
    StrategyStage,
    freeze_evaluation_contract,
)


def _strategy(strategy_id: str, *, prefix: str) -> ExecutionStrategy:
    return ExecutionStrategy(
        strategy_id=strategy_id,
        version="1",
        adapter_id="known-helixharness",
        adapter_version="1",
        source_commit="a" * 40,
        source_document_hashes=(("commands.md", "b" * 64),),
        commands=(f"/{prefix}:implement",),
        stages=(
            StrategyStage(
                stage_id="IMPLEMENT",
                semantic_role="IMPLEMENTATION",
                command=f"/{prefix}:implement",
                completion_state="COMPLETED",
                checkpoint=True,
            ),
        ),
        completion_states=("COMPLETED",),
        artifact_sources=(
            ArtifactSource(
                source_id="completed-stage-artifacts",
                root=f"docs/specs/completed/{prefix}",
            ),
        ),
        finding_policy="DECLARED",
        finding_closures=(
            FindingClosure("REVIEW_FINDING", f"/{prefix}:fix", "IMPLEMENT", 3),
        ),
    )


def test_run_contract_freezes_unified_scoring_and_configurable_artifact_sources() -> None:
    frozen = freeze_evaluation_contract(
        base_strategy=_strategy("legacy-chain", prefix="legacy"),
        candidate_strategy=_strategy("workflow-loop", prefix="workflow"),
    )

    assert frozen["schema_version"] == "framework-eval-run-contract.v2"
    assert "comparison" not in frozen
    assert frozen["scoring_profile"]["version"] == "unified-evaluation.v7"
    assert frozen["scoring_profile"]["resource_accounting"] == {
        "schema_version": "framework-eval-resource-observation.v2",
        "scoring_source": "attributable",
        "active_time_formula": "sum-included-turn-duration-ms.v1",
        "actual_cost_formula": "sum-observed-attempt-resource.v1",
        "incomplete_actual_evidence": "PARTIAL",
    }
    assert frozen["strategies"]["BASE"]["strategy_id"] == "legacy-chain"
    assert frozen["strategies"]["CANDIDATE"]["strategy_id"] == "workflow-loop"
    assert frozen["strategies"]["BASE"]["artifact_sources"] == [
        {
            "source_id": "completed-stage-artifacts",
            "root": "docs/specs/completed/legacy",
            "include_glob": "**/*.md",
            "artifact_schema": "helixharness-completed-markdown.v1",
            "deterministic_checks": ["AT_LEAST_ONE", "NON_EMPTY"],
            "required": True,
            "max_files": 50,
            "max_total_bytes": 500_000,
        }
    ]
    assert frozen["scoring_profile"]["weights"] == {
        "outcome_quality": 60,
        "framework_workflow_quality": 20,
        "efficiency": 20,
    }
    assert frozen["scoring_profile"]["outcome_weights"] == {
        "requirement_fulfillment": 90,
        "implementation_quality": 10,
    }
    assert frozen["scoring_profile"]["workflow_axes"] == {
        "goal_requirement_decomposition": 25,
        "decision_planning_quality": 25,
        "implementation_traceability": 25,
        "verification_completion_quality": 25,
    }
    assert frozen["scoring_profile"]["formula_ids"]["case_aggregation"] == (
        "equal-business-comparable-cases-tolerant-regression.v3"
    )
    assert frozen["scoring_profile"]["formula_ids"]["unified"] == (
        "renormalize-mutually-available-components.v2"
    )
    assert frozen["scoring_profile"]["promotion_policy"][
        "candidate_business_regression_tolerance"
    ] == 1
    assert frozen["scoring_profile"]["formula_ids"]["efficiency_na"] == (
        "renormalize-available-components.v1"
    )
    assert len(frozen["contract_hash"]) == 64


def test_run_contract_hash_is_stable_for_equivalent_inputs() -> None:
    first = freeze_evaluation_contract(
        base_strategy=_strategy("legacy", prefix="legacy"),
        candidate_strategy=_strategy("candidate", prefix="candidate"),
    )
    second = freeze_evaluation_contract(
        base_strategy=_strategy("legacy", prefix="legacy"),
        candidate_strategy=_strategy("candidate", prefix="candidate"),
    )

    assert first == second


def test_run_contract_rejects_unsafe_artifact_root() -> None:
    invalid = replace(
        _strategy("legacy", prefix="legacy"),
        artifact_sources=(ArtifactSource("completed", "../outside"),),
    )

    with pytest.raises(ContractValidationError, match="artifact source"):
        freeze_evaluation_contract(
            base_strategy=invalid,
            candidate_strategy=_strategy("candidate", prefix="candidate"),
        )


def test_run_contract_requires_four_equal_workflow_axes() -> None:
    invalid_profile = replace(
        UNIFIED_SCORING_PROFILE,
        workflow_axes=(("one", 50), ("two", 25), ("three", 25)),
    )

    with pytest.raises(ContractValidationError, match="four equally weighted"):
        freeze_evaluation_contract(
            base_strategy=_strategy("legacy", prefix="legacy"),
            candidate_strategy=_strategy("candidate", prefix="candidate"),
            scoring_profile=invalid_profile,
        )
