"""Run-owned execution and scoring contracts for Scenario v2."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Literal, TypeAlias

from bench.helixharness_source import HelixHarnessSourceError, validate_plugin_path

FindingPolicy: TypeAlias = Literal["NONE", "DECLARED"]
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ContractValidationError(ValueError):
    """Raised when a Run-owned v2 contract cannot be frozen safely."""


@dataclass(frozen=True)
class ArtifactSource:
    """One configurable Markdown package collected as unified workflow evidence."""

    source_id: str
    root: str
    include_glob: str = "**/*.md"
    artifact_schema: str = "helixharness-completed-markdown.v1"
    deterministic_checks: tuple[str, ...] = ("AT_LEAST_ONE", "NON_EMPTY")
    required: bool = True
    max_files: int = 50
    max_total_bytes: int = 500_000


@dataclass(frozen=True)
class StrategyStage:
    """One native stage with explicit semantics and completion checkpoint."""

    stage_id: str
    semantic_role: str
    command: str
    completion_state: str
    checkpoint: bool


@dataclass(frozen=True)
class FindingClosure:
    """One bounded conditional remediation loop declared by the Strategy."""

    finding_type: str
    remediation_command: str
    return_stage: str
    max_rounds: int


@dataclass(frozen=True)
class ExecutionStrategy:
    """One side's fully resolved, versioned native workflow contract."""

    strategy_id: str
    version: str
    adapter_id: str
    adapter_version: str
    source_commit: str
    source_document_hashes: tuple[tuple[str, str], ...]
    commands: tuple[str, ...]
    stages: tuple[StrategyStage, ...]
    completion_states: tuple[str, ...]
    artifact_sources: tuple[ArtifactSource, ...]
    finding_policy: FindingPolicy
    finding_closures: tuple[FindingClosure, ...]
    recovery_policy: dict[str, object] | None = None
    source_ref: str | None = None
    source_plugin_path: str | None = None


@dataclass(frozen=True)
class ScoringProfile:
    """Versioned common scoring policy, independent from Scenario and Strategy."""

    profile_id: str
    version: str
    outcome_weight: int
    workflow_weight: int
    efficiency_weight: int
    requirement_fulfillment_weight: int
    implementation_quality_weight: int
    token_efficiency_weight: int
    active_time_efficiency_weight: int
    rework_efficiency_weight: int
    workflow_axes: tuple[tuple[str, int], ...]
    global_hard_gates: tuple[str, ...]
    case_hard_gates: tuple[str, ...]
    candidate_business_regression_tolerance: int = 0
    unified_formula: str = "renormalize-mutually-available-components.v2"
    outcome_formula: str = "requirements-90-implementation-quality-10.v1"
    efficiency_formula: str = "symmetric-relative-cost.v1"
    efficiency_na_policy: str = "renormalize-available-components.v1"
    case_aggregation_formula: str = "equal-business-comparable-cases-tolerant-regression.v3"
    recommend_unified_delta: float = 5
    recommend_minimum_outcome_delta: float = 0
    do_not_recommend_unified_delta: float = -5
    do_not_recommend_outcome_delta: float = -5


UNIFIED_SCORING_PROFILE = ScoringProfile(
    profile_id="UNIFIED_EVALUATION",
    version="unified-evaluation.v7",
    outcome_weight=60,
    workflow_weight=20,
    efficiency_weight=20,
    requirement_fulfillment_weight=90,
    implementation_quality_weight=10,
    token_efficiency_weight=50,
    active_time_efficiency_weight=40,
    rework_efficiency_weight=10,
    workflow_axes=(
        ("goal_requirement_decomposition", 25),
        ("decision_planning_quality", 25),
        ("implementation_traceability", 25),
        ("verification_completion_quality", 25),
    ),
    global_hard_gates=(
        "UNACCEPTABLE_SECURITY_REGRESSION",
        "EVIDENCE_CONTAMINATION",
        "CHEATING",
        "FALSE_COMPLETION",
    ),
    case_hard_gates=(
        "CORE_BUSINESS_BEHAVIOR_FAILURE",
        "PROTECTED_REGRESSION_FAILURE",
        "INIT_OUTPUT_INVALID",
        "WORKFLOW_TERMINAL_FAILURE",
    ),
    candidate_business_regression_tolerance=1,
    unified_formula="renormalize-mutually-available-components.v2",
    case_aggregation_formula="equal-business-comparable-cases-tolerant-regression.v3",
)


def freeze_evaluation_contract(
    *,
    base_strategy: ExecutionStrategy,
    candidate_strategy: ExecutionStrategy,
    scoring_profile: ScoringProfile = UNIFIED_SCORING_PROFILE,
) -> dict[str, object]:
    """Validate and serialize the complete Run-owned v2 evaluation contract."""

    _validate_strategy(base_strategy, side="BASE")
    _validate_strategy(candidate_strategy, side="CANDIDATE")
    validate_scoring_profile(scoring_profile)

    payload: dict[str, object] = {
        "schema_version": "framework-eval-run-contract.v2",
        "strategies": {
            "BASE": _strategy_payload(base_strategy),
            "CANDIDATE": _strategy_payload(candidate_strategy),
        },
        "scoring_profile": _scoring_profile_payload(scoring_profile),
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    payload["contract_hash"] = sha256(serialized.encode("utf-8")).hexdigest()
    return payload


def _validate_strategy(strategy: ExecutionStrategy, *, side: str) -> None:
    """Validate one native Strategy and its unified artifact sources."""

    if not strategy.strategy_id.strip() or not strategy.version.strip():
        raise ContractValidationError(f"{side} strategy_id and version are required")
    if not strategy.adapter_id.strip() or not strategy.adapter_version.strip():
        raise ContractValidationError(f"{side} Strategy must freeze Adapter identity and version")
    if _COMMIT_PATTERN.fullmatch(strategy.source_commit) is None:
        raise ContractValidationError(f"{side} Strategy must freeze an exact source Commit")
    if strategy.source_plugin_path is not None:
        try:
            validate_plugin_path(strategy.source_plugin_path)
        except HelixHarnessSourceError as exc:
            raise ContractValidationError(f"{side} Strategy plugin path is invalid: {exc}") from exc
    if strategy.source_ref is not None and (
        not strategy.source_ref.strip()
        or strategy.source_ref != strategy.source_ref.strip()
        or len(strategy.source_ref) > 255
        or any(character.isspace() or ord(character) < 32 for character in strategy.source_ref)
    ):
        raise ContractValidationError(f"{side} Strategy source Ref is invalid")
    if not strategy.source_document_hashes or any(
        not path.strip() or _SHA256_PATTERN.fullmatch(document_hash) is None
        for path, document_hash in strategy.source_document_hashes
    ):
        raise ContractValidationError(f"{side} Strategy must freeze source document hashes")
    if not strategy.commands or any(not command.strip() for command in strategy.commands):
        raise ContractValidationError(f"{side} strategy must declare executable commands")
    if not strategy.completion_states or any(not state.strip() for state in strategy.completion_states):
        raise ContractValidationError(f"{side} strategy must declare verifiable completion states")
    stage_ids: set[str] = set()
    for stage in strategy.stages:
        if (
            not stage.stage_id.strip()
            or stage.stage_id in stage_ids
            or not stage.semantic_role.strip()
            or stage.command not in strategy.commands
            or stage.completion_state not in strategy.completion_states
        ):
            raise ContractValidationError(f"{side} Strategy stage semantics are invalid")
        stage_ids.add(stage.stage_id)
    if not stage_ids:
        raise ContractValidationError(f"{side} Strategy must declare stage semantics")
    if not strategy.artifact_sources:
        raise ContractValidationError(f"{side} Strategy must declare an artifact source")
    source_ids: set[str] = set()
    for source in strategy.artifact_sources:
        root = PurePosixPath(source.root)
        if (
            not source.source_id.strip()
            or source.source_id in source_ids
            or root.is_absolute()
            or ".." in root.parts
            or not source.root.strip()
            or not source.include_glob.strip()
            or source.include_glob.startswith("/")
            or ".." in PurePosixPath(source.include_glob).parts
            or not source.artifact_schema.strip()
            or source.max_files < 1
            or source.max_total_bytes < 1
            or any(not check.strip() for check in source.deterministic_checks)
        ):
            raise ContractValidationError(f"{side} Strategy artifact source is invalid")
        source_ids.add(source.source_id)
    if strategy.finding_policy not in {"NONE", "DECLARED"}:
        raise ContractValidationError(f"{side} Strategy finding_policy is invalid")
    if strategy.finding_policy == "NONE" and strategy.finding_closures:
        raise ContractValidationError(f"{side} Strategy with NONE finding policy cannot declare closures")
    if strategy.finding_policy == "DECLARED" and not strategy.finding_closures:
        raise ContractValidationError(f"{side} Strategy must declare Finding closures")
    finding_types: set[str] = set()
    for closure in strategy.finding_closures:
        if (
            not closure.finding_type.strip()
            or closure.finding_type in finding_types
            or not closure.remediation_command.strip()
            or closure.return_stage not in stage_ids
            or closure.max_rounds < 1
        ):
            raise ContractValidationError(f"{side} Strategy Finding closure is invalid")
        finding_types.add(closure.finding_type)


def validate_scoring_profile(profile: ScoringProfile) -> None:
    """Validate all frozen scoring and decision-policy weight contracts."""

    if profile.outcome_weight + profile.workflow_weight + profile.efficiency_weight != 100:
        raise ContractValidationError("Scoring Profile top-level weights must total 100")
    if profile.requirement_fulfillment_weight + profile.implementation_quality_weight != 100:
        raise ContractValidationError("Scoring Profile Outcome weights must total 100")
    if sum(weight for _, weight in profile.workflow_axes) != 100:
        raise ContractValidationError("Scoring Profile workflow weights must total 100")
    if len(profile.workflow_axes) != 4 or any(weight != 25 for _, weight in profile.workflow_axes):
        raise ContractValidationError("Scoring Profile requires four equally weighted workflow axes")
    if (
        profile.token_efficiency_weight
        + profile.active_time_efficiency_weight
        + profile.rework_efficiency_weight
        != 100
    ):
        raise ContractValidationError("Scoring Profile efficiency weights must total 100")
    supported_formulas = (
        (
            "unified",
            profile.unified_formula,
            {"renormalize-mutually-available-components.v2"},
        ),
        ("outcome", profile.outcome_formula, "requirements-90-implementation-quality-10.v1"),
        ("efficiency", profile.efficiency_formula, "symmetric-relative-cost.v1"),
        (
            "case aggregation",
            profile.case_aggregation_formula,
            {
                "equal-business-comparable-cases-tolerant-regression.v3",
            },
        ),
    )
    for purpose, formula, supported in supported_formulas:
        supported_values = {supported} if isinstance(supported, str) else supported
        if formula not in supported_values:
            raise ContractValidationError(f"Unsupported Scoring Profile {purpose} formula: {formula}")
    if profile.candidate_business_regression_tolerance < 0:
        raise ContractValidationError("Candidate business regression tolerance must be non-negative")
    supported_efficiency_na_policies = {
        "renormalize-available-components.v1",
    }
    if profile.efficiency_na_policy not in supported_efficiency_na_policies:
        raise ContractValidationError(
            f"Unsupported Scoring Profile efficiency N/A formula: {profile.efficiency_na_policy}"
        )
    if (
        profile.efficiency_na_policy == "renormalize-available-components.v1"
        and profile.outcome_weight + profile.workflow_weight == 0
    ):
        raise ContractValidationError("Scoring Profile cannot renormalize an empty available score")


def _strategy_payload(strategy: ExecutionStrategy) -> dict[str, object]:
    """Serialize one already validated native Strategy."""

    adapter: dict[str, object] = {
        "adapter_id": strategy.adapter_id,
        "adapter_version": strategy.adapter_version,
        "source_commit": strategy.source_commit,
        "source_document_hashes": dict(strategy.source_document_hashes),
    }
    if strategy.source_ref is not None:
        adapter["source_ref"] = strategy.source_ref
    if strategy.source_plugin_path is not None:
        adapter["source_plugin_path"] = strategy.source_plugin_path
    return {
        "schema_version": "framework-eval-execution-strategy.v2",
        "strategy_id": strategy.strategy_id,
        "version": strategy.version,
        "adapter": adapter,
        "commands": list(strategy.commands),
        "stages": [
            {
                "stage_id": stage.stage_id,
                "semantic_role": stage.semantic_role,
                "command": stage.command,
                "completion_state": stage.completion_state,
                "checkpoint": stage.checkpoint,
            }
            for stage in strategy.stages
        ],
        "completion_states": list(strategy.completion_states),
        "artifact_sources": [
            {
                "source_id": source.source_id,
                "root": source.root,
                "include_glob": source.include_glob,
                "artifact_schema": source.artifact_schema,
                "deterministic_checks": list(source.deterministic_checks),
                "required": source.required,
                "max_files": source.max_files,
                "max_total_bytes": source.max_total_bytes,
            }
            for source in strategy.artifact_sources
        ],
        "finding_policy": strategy.finding_policy,
        "finding_closures": [
            {
                "finding_type": closure.finding_type,
                "remediation_command": closure.remediation_command,
                "return_stage": closure.return_stage,
                "max_rounds": closure.max_rounds,
            }
            for closure in strategy.finding_closures
        ],
        "recovery_policy": strategy.recovery_policy or {},
    }


def _scoring_profile_payload(profile: ScoringProfile) -> dict[str, object]:
    """Serialize the single policy consumed by scoring and reporting."""

    return {
        "profile_id": profile.profile_id,
        "version": profile.version,
        "weights": {
            "outcome_quality": profile.outcome_weight,
            "framework_workflow_quality": profile.workflow_weight,
            "efficiency": profile.efficiency_weight,
        },
        "outcome_weights": {
            "requirement_fulfillment": profile.requirement_fulfillment_weight,
            "implementation_quality": profile.implementation_quality_weight,
        },
        "workflow_axes": dict(profile.workflow_axes),
        "efficiency_weights": {
            "agent_tokens": profile.token_efficiency_weight,
            "active_execution_seconds": profile.active_time_efficiency_weight,
            "workflow_rework_actions": profile.rework_efficiency_weight,
        },
        "resource_accounting": {
            "schema_version": "framework-eval-resource-observation.v2",
            "scoring_source": "attributable",
            "active_time_formula": "sum-included-turn-duration-ms.v1",
            "actual_cost_formula": "sum-observed-attempt-resource.v1",
            "incomplete_actual_evidence": "PARTIAL",
        },
        "formula_ids": {
            "unified": profile.unified_formula,
            "outcome": profile.outcome_formula,
            "efficiency": profile.efficiency_formula,
            "efficiency_na": profile.efficiency_na_policy,
            "case_aggregation": profile.case_aggregation_formula,
        },
        "hard_gates": {
            "GLOBAL": list(profile.global_hard_gates),
            "CASE": list(profile.case_hard_gates),
        },
        "promotion_policy": {
            "recommend_unified_delta": profile.recommend_unified_delta,
            "recommend_minimum_outcome_delta": profile.recommend_minimum_outcome_delta,
            "do_not_recommend_unified_delta": profile.do_not_recommend_unified_delta,
            "do_not_recommend_outcome_delta": profile.do_not_recommend_outcome_delta,
            "reject_new_candidate_case_gate": False,
            "candidate_business_regression_tolerance": profile.candidate_business_regression_tolerance,
        },
    }
