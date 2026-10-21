"""Stable contracts shared by the evolution application services."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HypothesisKind(str, Enum):
    """How a proposed change relates to the current capability contract."""

    REPAIR = "REPAIR"
    GROWTH = "GROWTH"


class ExperimentState(str, Enum):
    """Persisted lifecycle of one single-hypothesis experiment."""

    PROPOSED = "PROPOSED"
    AWAITING_HUMAN_SELECTION = "AWAITING_HUMAN_SELECTION"
    BUILDING_CANDIDATE = "BUILDING_CANDIDATE"
    AWAITING_EVALUATION = "AWAITING_EVALUATION"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Capability:
    """Human-owned description of one HelixHarness capability."""

    capability_id: str
    name: str
    description: str
    selectors: tuple[str, ...]
    owned_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    case_version_ids: tuple[str, ...]
    case_selection_mode: str = "EXPLICIT"
    case_selection_max_cases: int = 3
    allowed_case_profiles: tuple[str, ...] = field(default_factory=tuple)
    auto_workflow_confirmation: bool = False
    auto_repair: bool = False
    growth_confirmed: bool = False


@dataclass(frozen=True)
class EvolutionEvidence:
    """Sanitized telemetry fact admitted by the eval consumer Gate."""

    evidence_id: str
    source_path: str
    source_commit: str | None
    source_hash: str
    feature: str
    project_name: str | None
    helixharness_version: str | None
    command: str | None
    stage_kind: str | None
    status: str | None
    outcome_reason: str | None
    gate_result: str | None
    evidence_complete: bool | None
    occurred_at: str | None
    analysis_scope: str = "CURRENT"
    admitted_at: str | None = None
    fixed_evidence: dict[str, Any] = field(default_factory=dict)
    async_evidence: dict[str, Any] = field(default_factory=dict)
    code_evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "EvolutionEvidence":
        source_hash = str(value.get("file_hash") or "")
        entry_id = str(value.get("entry_id") or "")
        return cls(
            evidence_id=f"{source_hash}:{entry_id}",
            source_path=str(value.get("source_path") or ""),
            source_commit=_optional_string(value.get("source_commit")),
            source_hash=source_hash,
            feature=str(value.get("feature") or ""),
            project_name=_optional_string(value.get("project_name")),
            helixharness_version=_optional_string(value.get("helixharness_version")),
            command=_optional_string(value.get("command")),
            stage_kind=_optional_string(value.get("stage_kind")),
            status=_optional_string(value.get("status")),
            outcome_reason=_optional_string(value.get("outcome_reason")),
            gate_result=_optional_string(value.get("gate_result")),
            evidence_complete=value.get("evidence_complete") if isinstance(value.get("evidence_complete"), bool) else None,
            occurred_at=_optional_string(value.get("ended_at") or value.get("started_at")),
            admitted_at=_optional_string(value.get("admitted_at")),
            fixed_evidence=dict(value.get("fixed_evidence") or {})
            if isinstance(value.get("fixed_evidence"), dict)
            else {},
            async_evidence=dict(value.get("async_evidence") or {})
            if isinstance(value.get("async_evidence"), dict)
            else {},
            code_evidence=dict(value.get("code_evidence") or {})
            if isinstance(value.get("code_evidence"), dict)
            else {},
        )


@dataclass(frozen=True)
class AcceptanceContract:
    """Non-scoring contract that decides whether a hypothesis is testable."""

    capability_id: str
    case_version_ids: tuple[str, ...]
    expected_effect: str
    regression_scope: tuple[str, ...]
    protected_paths: tuple[str, ...]
    case_selection_mode: str = "EXPLICIT"
    case_selection_max_cases: int = 3
    allowed_case_profiles: tuple[str, ...] = field(default_factory=tuple)
    auto_workflow_confirmation: bool = False


@dataclass(frozen=True)
class Hypothesis:
    """One evidence-backed, independently evaluable change proposal."""

    hypothesis_id: str
    kind: HypothesisKind
    capability_id: str
    title: str
    rationale: str
    evidence_ids: tuple[str, ...]
    confidence: float
    acceptance: AcceptanceContract
    requires_human_selection: bool
    semantic_source: str
    created_at: str
    source_quality_issues: tuple[str, ...] = field(default_factory=tuple)
    context_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
    analysis_context_id: str | None = None
    retry_of_experiment_id: str | None = None
    source_kind: str = "EVAL_LOG"
    source_run_id: str | None = None
    source_root_run_id: str | None = None
    source_base_commit: str | None = None
    source_candidate_commit: str | None = None
    intervention: str = ""
    falsification_condition: str = ""
    validation_plan: str = ""
    generation_basis: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EvaluationFinding:
    """One immutable, candidate-attributed fact from a terminal Benchmark Run."""

    finding_id: str
    case_id: str
    category: str
    summary: str
    evidence_ids: tuple[str, ...]
    repair_target: str = ""
    observed_difference: str = ""
    diagnosis: str = ""


@dataclass(frozen=True)
class BenchmarkRemediationEvidence:
    """Sanitized Bench DTO used to draft one human-confirmed repair plan."""

    source_run_id: str
    root_run_id: str
    profile: str
    base_commit: str
    candidate_commit: str
    case_ids: tuple[str, ...]
    decision_status: str
    decision_reason: str
    findings: tuple[EvaluationFinding, ...]
    created_at: str


@dataclass(frozen=True)
class CandidateVersion:
    """Immutable Git identity emitted by an isolated candidate build."""

    candidate_id: str
    hypothesis_id: str
    base_commit: str
    candidate_commit: str
    candidate_ref: str
    worktree_path: str
    changed_paths: tuple[str, ...]
    diff_summary: str = ""
    diff_patch: str = ""
    diff_truncated: bool = False


@dataclass(frozen=True)
class EvaluationSubmission:
    """Reference from evolution to an immutable Benchmark Run."""

    run_id: str
    state: str
    plan: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationVerdict:
    """Benchmark-owned terminal decision consumed without re-scoring."""

    decision_status: str
    decision_reason: str
    calculation_id: str | None = None


@dataclass(frozen=True)
class CycleOutcome:
    """Observable result of one scheduler tick."""

    cycle_id: str
    state: str
    reason: str
    hypothesis_ids: tuple[str, ...] = field(default_factory=tuple)
    experiment_id: str | None = None


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None
