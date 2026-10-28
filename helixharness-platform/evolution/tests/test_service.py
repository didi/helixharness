from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from evolution.discovery import SemanticProposal
from evolution.models import (
    BenchmarkRemediationEvidence,
    CandidateVersion,
    Capability,
    EvaluationFinding,
    EvaluationSubmission,
    EvaluationVerdict,
    EvolutionEvidence,
)
from evolution.service import EvolutionService
from evolution.storage import EvolutionStore


def _capability(**overrides: object) -> Capability:
    values = {
        "capability_id": "workflow",
        "name": "工作流",
        "description": "工作流纪律",
        "selectors": ("archive",),
        "owned_paths": ("commands/",),
        "protected_paths": ("eval/",),
        "case_version_ids": ("case-1",),
        "case_selection_mode": "EXPLICIT",
        "case_selection_max_cases": 3,
        "allowed_case_profiles": (),
        "auto_workflow_confirmation": True,
        "auto_repair": True,
        "growth_confirmed": False,
    }
    values.update(overrides)
    return Capability(**values)


def _evidence(**overrides: object) -> EvolutionEvidence:
    values = {
        "evidence_id": "hash:entry",
        "source_path": "/tmp/eval-log.json",
        "source_commit": "base",
        "source_hash": "hash",
        "feature": "feature-a",
        "project_name": "demo",
        "helixharness_version": "2.0.0",
        "command": "/helix:archive",
        "stage_kind": "archive",
        "status": "blocked",
        "outcome_reason": "missing evidence",
        "gate_result": "fail",
        "evidence_complete": False,
        "occurred_at": "2026-08-01T00:00:00Z",
    }
    values.update(overrides)
    return EvolutionEvidence(**values)


class _Builder:
    def __init__(self) -> None:
        self.calls = 0
        self.base_commits: list[str | None] = []

    def build(self, experiment_id, hypothesis, capability, *, base_commit=None):
        self.calls += 1
        self.base_commits.append(base_commit)
        return CandidateVersion(
            candidate_id="cand-1",
            hypothesis_id=hypothesis.hypothesis_id,
            base_commit=base_commit or "base",
            candidate_commit="candidate",
            candidate_ref="refs/helixharness/evolution/cand-1",
            worktree_path="/tmp/cand-1",
            changed_paths=("commands/archive.md",),
        )


class _CandidateBeforeExperimentBuilder(_Builder):
    def __init__(self, store: EvolutionStore) -> None:
        super().__init__()
        self.store = store

    def build(self, experiment_id, hypothesis, capability, *, base_commit=None):
        assert self.store.list_experiments() == []
        return super().build(
            experiment_id,
            hypothesis,
            capability,
            base_commit=base_commit,
        )


class _Gateway:
    def __init__(self, decision_status: str = "RECOMMEND") -> None:
        self.calls = 0
        self.validation_calls = 0
        self.remediation_validation_calls = 0
        self.remediation_submission_calls = 0
        self.projection_completions: list[tuple[str, str, str | None]] = []
        self.decision_status = decision_status
        self.remediations: tuple[BenchmarkRemediationEvidence, ...] = ()

    def validate(self, hypothesis):
        self.validation_calls += 1

    def submit(self, hypothesis, candidate_commit, base_commit):
        self.calls += 1
        return EvaluationSubmission(
            "run-1",
            "QUEUED",
            {
                "schema_version": "evolution-evaluation-plan.v1",
                "case_ids": ["case-1"],
                "workflow_contract_hash": "contract-1",
            },
        )

    def remediation_candidates(self):
        return self.remediations

    def complete_remediation_projection(self, run_id, *, status, reason, hypothesis_id=None):
        completion = (run_id, status, hypothesis_id)
        if completion not in self.projection_completions:
            self.projection_completions.append(completion)

    def validate_remediation(self, hypothesis):
        self.remediation_validation_calls += 1

    def submit_remediation(self, hypothesis, candidate_commit):
        self.remediation_submission_calls += 1
        return EvaluationSubmission(
            "run-remediation-1",
            "QUEUED",
            {
                "schema_version": "evolution-remediation-evaluation-plan.v1",
                "source_run_id": hypothesis.source_run_id,
                "case_ids": list(hypothesis.acceptance.case_version_ids),
                "execution_lane": "EVOLUTION_REMEDIATION",
            },
        )

    def verdict(self, run_id):
        return EvaluationVerdict(self.decision_status, "benchmark decision", "calc-1")


class _GrowthAnalyzer:
    def analyze(self, capability, evidence, *, analysis_context=None):
        return (SemanticProposal("新能力", "成功样本显示可扩展", "覆盖新场景", 0.8),)


class _FailOnceAnalyzer:
    def __init__(self) -> None:
        self.should_fail = True
        self.contexts = []

    def analyze(self, capability, evidence, *, analysis_context=None):
        self.contexts.append(analysis_context)
        if self.should_fail:
            raise RuntimeError("temporary model failure")
        return (SemanticProposal("修复证据缺失", "触发事实与历史重复出现", "补齐归档证据", 0.8),)


class _BlockedGateway(_Gateway):
    def validate(self, hypothesis):
        raise ValueError("no current formal Case")


class _FailingSubmitGateway(_Gateway):
    def submit(self, hypothesis, candidate_commit, base_commit):
        raise ValueError("case selector schema rejected")


def test_cycle_waits_for_human_adoption_before_submitting_benchmark(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    builder = _CandidateBeforeExperimentBuilder(store)
    gateway = _Gateway()
    service = EvolutionService(store, (_capability(),), candidate_builder=builder, evaluation_gateway=gateway)

    result = service.run_cycle(
        (_evidence(), replace(_evidence(), evidence_id="hash:entry-2")),
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert result.state == "AWAITING_HUMAN_SELECTION"
    assert builder.calls == 0
    assert gateway.calls == 0
    assert gateway.validation_calls == 0
    assert store.list_experiments() == []

    hypothesis_id = result.hypothesis_ids[0]
    service.prepare_hypothesis_candidate(hypothesis_id)
    started = service.start_selected_hypothesis(hypothesis_id)

    assert started.state == "AWAITING_EVALUATION"
    assert builder.calls == 1
    assert gateway.calls == 1
    assert gateway.validation_calls == 1
    experiment = store.list_experiments()[0]
    assert experiment["candidate_id"] == "cand-1"
    assert experiment["benchmark_run_id"] == "run-1"
    assert json.loads(experiment["evaluation_plan_json"])["case_ids"] == ["case-1"]

    assert service.reconcile_evaluations() == 1
    experiment = store.list_experiments()[0]
    assert experiment["state"] == "ACCEPTED"
    promotion = store.get_promotion_bundle(experiment["experiment_id"])
    assert promotion["benchmark_decision"] == "RECOMMEND"
    assert promotion["release_mode"] == "HUMAN_ONLY"
    assert promotion["evaluation_plan"]["workflow_contract_hash"] == "contract-1"


def test_benchmark_remediation_draft_requires_human_selection_and_starts_from_candidate(
    tmp_path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    builder = _Builder()
    gateway = _Gateway()
    gateway.remediations = (
        BenchmarkRemediationEvidence(
            source_run_id="run-source-1",
            root_run_id="run-source-1",
            profile="WORKFLOW_REDESIGN",
            base_commit="base-commit",
            candidate_commit="failed-candidate-commit",
            case_ids=("case-1",),
            decision_status="BLOCKED",
            decision_reason="Candidate archive gate failed",
            findings=(
                EvaluationFinding(
                    finding_id="case-1:hard-gate:ARCHIVE",
                    case_id="case-1",
                    category="CANDIDATE_HARD_GATE",
                    summary="workflow Candidate archive hard gate failed",
                    evidence_ids=("evidence-archive-1",),
                    repair_target="归档终态",
                    observed_difference="Candidate 缺少归档终态，Base 已归档",
                    diagnosis="Candidate 没有形成可验证的 completed 归档产物",
                ),
            ),
            created_at="2026-08-10T00:00:00Z",
        ),
    )
    service = EvolutionService(
        store,
        (_capability(auto_repair=True),),
        candidate_builder=builder,
        evaluation_gateway=gateway,
    )

    assert service.discover_benchmark_remediations() == 1
    assert service.discover_benchmark_remediations() == 0
    assert service.is_due(datetime(2026, 8, 10, tzinfo=timezone.utc)) is True
    hypothesis_row = store.list_hypotheses()[0]
    payload = json.loads(hypothesis_row["payload_json"])
    assert payload["semantic_source"] == "BENCH_REMEDIATION"
    assert payload["source_run_id"] == "run-source-1"
    assert payload["source_candidate_commit"] == "failed-candidate-commit"
    assert payload["requires_human_selection"] is True
    assert payload["title"] == "提升工作流的流程完成度"
    operator_copy = " ".join(
        (
            payload["title"],
            payload["rationale"],
            payload["intervention"],
            payload["acceptance"]["expected_effect"],
            payload["validation_plan"],
            payload["falsification_condition"],
        )
    )
    assert "Candidate" not in operator_copy
    assert "Base" not in operator_copy
    assert "BLOCKED" not in operator_copy
    assert "RECOMMEND" not in operator_copy
    assert "case-1" not in operator_copy
    assert "commands/" not in operator_copy
    generation_basis = payload["generation_basis"][0]
    assert generation_basis["decision_status"] == "BLOCKED"
    assert generation_basis["decision_reason"] == "Candidate archive gate failed"
    assert generation_basis["findings"][0]["case_id"] == "case-1"
    assert generation_basis["findings"][0]["diagnosis"] == (
        "Candidate 没有形成可验证的 completed 归档产物"
    )
    assert gateway.projection_completions == [
        ("run-source-1", "READY", hypothesis_row["hypothesis_id"])
    ]
    assert store.list_experiments() == []
    assert service.pending_candidate_preparation_ids() == (
        hypothesis_row["hypothesis_id"],
    )

    with pytest.raises(ValueError, match="frozen Candidate"):
        service.start_selected_hypothesis(hypothesis_row["hypothesis_id"])
    candidate = service.prepare_hypothesis_candidate(hypothesis_row["hypothesis_id"])

    assert candidate.candidate_commit == "candidate"
    assert service.pending_candidate_preparation_ids() == ()
    assert builder.base_commits == ["failed-candidate-commit"]
    assert store.list_experiments() == []
    outcome = service.start_selected_hypothesis(hypothesis_row["hypothesis_id"])

    assert outcome.state == "AWAITING_EVALUATION"
    assert builder.base_commits == ["failed-candidate-commit"]
    assert gateway.remediation_validation_calls == 1
    assert gateway.remediation_submission_calls == 1
    experiment = store.list_experiments()[0]
    assert experiment["benchmark_run_id"] == "run-remediation-1"
    plan = json.loads(experiment["evaluation_plan_json"])
    assert plan["execution_lane"] == "EVOLUTION_REMEDIATION"
    assert plan["source_run_id"] == "run-source-1"


def test_missing_benchmark_gateway_is_recorded_after_human_adoption(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    builder = _Builder()
    service = EvolutionService(store, (_capability(),), candidate_builder=builder)

    result = service.run_cycle(
        (_evidence(),),
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert result.state == "AWAITING_HUMAN_SELECTION"
    hypothesis_id = result.hypothesis_ids[0]
    service.prepare_hypothesis_candidate(hypothesis_id)
    started = service.start_selected_hypothesis(hypothesis_id)

    assert started.state == "BLOCKED"
    assert started.reason == "Benchmark gateway is not configured"
    assert builder.calls == 1
    experiment = store.list_experiments()[0]
    assert experiment["state"] == "BLOCKED"
    assert experiment["benchmark_run_id"] is None


def test_benchmark_preflight_is_recorded_after_human_adoption(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    builder = _Builder()
    service = EvolutionService(
        store,
        (_capability(),),
        candidate_builder=builder,
        evaluation_gateway=_BlockedGateway(),
    )

    result = service.run_cycle(
        (_evidence(),),
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert result.state == "AWAITING_HUMAN_SELECTION"
    hypothesis_id = result.hypothesis_ids[0]
    service.prepare_hypothesis_candidate(hypothesis_id)
    started = service.start_selected_hypothesis(hypothesis_id)

    assert started.state == "BLOCKED"
    assert started.reason == "Benchmark preflight blocked: no current formal Case"
    assert builder.calls == 1


def test_same_evidence_is_noop_after_collection_window(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    service = EvolutionService(store, (_capability(auto_repair=False),))
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)

    service.run_cycle((_evidence(),), now=first)
    result = service.run_cycle((_evidence(),), now=first + timedelta(hours=49))

    assert result.state == "NOOP"
    assert result.reason == "no new qualified eval-log evidence"
    assert service.is_due(first + timedelta(hours=49, minutes=1)) is False
    assert service.run_cycle(
        (_evidence(),), now=first + timedelta(hours=49, minutes=1)
    ).state == "NOT_DUE"
    restarted = EvolutionService(EvolutionStore(store.path), (_capability(auto_repair=False),))
    assert restarted.is_due(first + timedelta(hours=49, minutes=1)) is False
    assert service.is_due(first + timedelta(hours=97)) is True


def test_failed_analysis_keeps_snapshot_and_does_not_advance_evidence_watermark(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    analyzer = _FailOnceAnalyzer()
    service = EvolutionService(
        store,
        (_capability(auto_repair=False),),
        analyzer=analyzer,
    )
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)

    with pytest.raises(RuntimeError, match="temporary model failure"):
        service.run_cycle((_evidence(),), now=first)

    failed_cycle = store.cycle_summary()["latest"]
    assert failed_cycle["state"] == "FAILED"
    assert store.known_evidence_ids() == set()
    contexts = store.list_analysis_contexts(failed_cycle["cycle_id"])
    assert len(contexts) == 1
    context_id = contexts[0]["analysis_context_id"]
    assert contexts[0]["status"] == "FAILED"
    assert store.list_analysis_attempts(context_id)[0]["status"] == "FAILED"

    analyzer.should_fail = False
    outcome = service.run_cycle(
        (_evidence(),),
        now=first + timedelta(minutes=1),
        force=True,
    )

    assert outcome.cycle_id == failed_cycle["cycle_id"]
    assert outcome.state == "AWAITING_HUMAN_SELECTION"
    assert store.known_evidence_ids() == {"hash:entry"}
    context = store.get_analysis_context(context_id)
    assert context is not None
    assert context["status"] == "ANALYZED"
    assert [item["status"] for item in store.list_analysis_attempts(context_id)] == [
        "FAILED",
        "ANALYZED",
    ]
    assert analyzer.contexts[0] == analyzer.contexts[1]
    hypothesis = json.loads(store.list_hypotheses()[0]["payload_json"])
    assert hypothesis["analysis_context_id"] == context_id


def test_growth_enters_experiment_only_after_human_adoption(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    capability = _capability(auto_repair=False, growth_confirmed=False)
    builder = _Builder()
    gateway = _Gateway()
    service = EvolutionService(
        store,
        (capability,),
        analyzer=_GrowthAnalyzer(),
        candidate_builder=builder,
        evaluation_gateway=gateway,
    )
    evidence = _evidence(status="completed", gate_result="pass", evidence_complete=True)

    cycle = service.run_cycle((evidence,), now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert cycle.state == "AWAITING_HUMAN_SELECTION"
    assert builder.calls == 0
    service.prepare_hypothesis_candidate(cycle.hypothesis_ids[0])
    outcome = service.start_selected_hypothesis(cycle.hypothesis_ids[0])

    assert outcome.state == "AWAITING_EVALUATION"
    assert builder.calls == 1
    assert gateway.calls == 1


def test_repair_can_be_selected_manually_while_auto_repair_is_disabled(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    builder = _Builder()
    gateway = _Gateway()
    service = EvolutionService(
        store,
        (_capability(auto_repair=False),),
        candidate_builder=builder,
        evaluation_gateway=gateway,
    )
    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    with pytest.raises(ValueError, match="frozen Candidate"):
        service.start_selected_hypothesis(cycle.hypothesis_ids[0])
    candidate = service.prepare_hypothesis_candidate(cycle.hypothesis_ids[0])

    assert candidate.candidate_commit == "candidate"
    assert store.list_experiments() == []
    result = service.start_selected_hypothesis(cycle.hypothesis_ids[0])

    assert cycle.state == "AWAITING_HUMAN_SELECTION"
    assert result.state == "AWAITING_EVALUATION"
    assert builder.calls == 1
    assert gateway.calls == 1


def test_non_automatic_eval_hypothesis_is_queued_for_candidate_preparation(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    service = EvolutionService(
        store,
        (_capability(auto_repair=False),),
        candidate_builder=_Builder(),
        evaluation_gateway=_Gateway(),
    )

    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert service.pending_candidate_preparation_ids() == (cycle.hypothesis_ids[0],)


def test_candidate_queue_does_not_require_experiment_prerequisites(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    service = EvolutionService(
        store,
        (_capability(auto_repair=False, case_version_ids=(), case_selection_mode="EXPLICIT"),),
        candidate_builder=_Builder(),
    )

    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert service.pending_candidate_preparation_ids() == (cycle.hypothesis_ids[0],)
    candidate = service.prepare_hypothesis_candidate(cycle.hypothesis_ids[0])
    assert candidate.candidate_commit == "candidate"


def test_missing_experiment_prerequisites_are_recorded_on_experiment(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    builder = _Builder()
    gateway = _Gateway()

    def reject_preflight(_hypothesis) -> None:
        raise ValueError("missing Published + Calibrated Case")

    gateway.validate = reject_preflight
    service = EvolutionService(
        store,
        (_capability(auto_repair=False, case_version_ids=(), case_selection_mode="EXPLICIT"),),
        candidate_builder=builder,
        evaluation_gateway=gateway,
    )
    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    service.prepare_hypothesis_candidate(cycle.hypothesis_ids[0])

    outcome = service.start_selected_hypothesis(cycle.hypothesis_ids[0])

    assert outcome.state == "BLOCKED"
    experiment = store.list_experiments()[0]
    assert experiment["state"] == "BLOCKED"
    assert "missing Published + Calibrated Case" in experiment["decision_reason"]


def test_repair_without_published_calibrated_case_does_not_auto_start(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    service = EvolutionService(store, (_capability(case_version_ids=()),), candidate_builder=_Builder())

    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert cycle.state == "AWAITING_HUMAN_SELECTION"
    assert store.list_experiments() == []


def test_ai_case_policy_still_requires_human_adoption(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    gateway = _Gateway()
    service = EvolutionService(
        store,
        (
            _capability(
                case_version_ids=(),
                case_selection_mode="AI",
                auto_workflow_confirmation=True,
            ),
        ),
        candidate_builder=_Builder(),
        evaluation_gateway=gateway,
    )

    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert cycle.state == "AWAITING_HUMAN_SELECTION"
    assert gateway.calls == 0
    hypothesis_id = cycle.hypothesis_ids[0]
    service.prepare_hypothesis_candidate(hypothesis_id)
    started = service.start_selected_hypothesis(hypothesis_id)

    assert started.state == "AWAITING_EVALUATION"
    assert gateway.calls == 1


def test_hypothesis_waits_for_human_adoption_regardless_of_workflow_authorization(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    service = EvolutionService(
        store,
        (
            _capability(
                case_version_ids=(),
                case_selection_mode="AI",
                auto_workflow_confirmation=False,
            ),
        ),
        candidate_builder=_Builder(),
        evaluation_gateway=_Gateway(),
    )

    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert cycle.state == "AWAITING_HUMAN_SELECTION"
    assert store.list_experiments() == []


def test_unrelated_new_evidence_does_not_retrigger_historical_hypothesis(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    service = EvolutionService(store, (_capability(auto_repair=False),))
    first = datetime(2026, 8, 1, tzinfo=timezone.utc)
    service.run_cycle((_evidence(),), now=first)
    unrelated = replace(
        _evidence(),
        evidence_id="other:entry",
        command="/helix:init",
        stage_kind="requirement",
        outcome_reason="different capability",
    )

    result = service.run_cycle((_evidence(), unrelated), now=first + timedelta(hours=49))

    assert result.state == "NOOP"
    assert result.reason == "no evidence-backed hypothesis discovered"
    assert len(store.list_hypotheses()) == 1


@pytest.mark.parametrize(
    ("decision_status", "expected_state"),
    (("NO_CLEAR_GAIN", "REJECTED"), ("INCONCLUSIVE", "INCONCLUSIVE")),
)
def test_benchmark_non_recommendation_never_creates_promotion_bundle(
    tmp_path,
    decision_status,
    expected_state,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    gateway = _Gateway(decision_status)
    service = EvolutionService(store, (_capability(),), candidate_builder=_Builder(), evaluation_gateway=gateway)
    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    hypothesis_id = cycle.hypothesis_ids[0]
    service.prepare_hypothesis_candidate(hypothesis_id)
    service.start_selected_hypothesis(hypothesis_id)

    assert service.reconcile_evaluations() == 1
    experiment = store.list_experiments()[0]
    assert experiment["state"] == expected_state
    assert store.get_promotion_bundle(experiment["experiment_id"]) is None


def test_deleted_experiment_still_reconciles_its_benchmark_terminal_state(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    gateway = _Gateway("NO_CLEAR_GAIN")
    service = EvolutionService(
        store,
        (_capability(),),
        candidate_builder=_Builder(),
        evaluation_gateway=gateway,
    )
    cycle = service.run_cycle((_evidence(),), now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    hypothesis_id = cycle.hypothesis_ids[0]
    service.prepare_hypothesis_candidate(hypothesis_id)
    service.start_selected_hypothesis(hypothesis_id)
    experiment_id = store.list_experiments()[0]["experiment_id"]

    service.delete_experiment(experiment_id, "人工从工作台删除")

    assert store.list_experiments() == []
    assert service.reconcile_evaluations() == 1
    experiment = store.get_experiment(experiment_id)
    assert experiment is not None
    assert experiment["state"] == "REJECTED"


def test_partial_rehearsal_runs_once_and_never_creates_promotion_bundle(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    gateway = _Gateway("RECOMMEND")
    service = EvolutionService(
        store,
        (_capability(auto_repair=False),),
        candidate_builder=_Builder(),
        evaluation_gateway=gateway,
    )
    tick = datetime(2026, 8, 1, tzinfo=timezone.utc)

    result = service.run_partial_rehearsal(
        (_evidence(),),
        source_hash="hash",
        issue_codes=("UNAVAILABLE_METRICS",),
        now=tick,
    )

    assert result.state == "AWAITING_EVALUATION"
    hypothesis = store.list_hypotheses()[0]
    payload = json.loads(hypothesis["payload_json"])
    assert payload["semantic_source"] == "PARTIAL_REHEARSAL"
    assert payload["evidence_ids"] == ["partial-rehearsal:hash:entry"]
    assert service.reconcile_evaluations() == 1
    experiment = store.list_experiments()[0]
    assert experiment["state"] == "INCONCLUSIVE"
    assert experiment["decision_reason"] == (
        "Benchmark RECOMMEND observed, but PARTIAL_REHEARSAL is non-promotable"
    )
    assert store.get_promotion_bundle(experiment["experiment_id"]) is None
    repeated = service.run_partial_rehearsal(
        (_evidence(),),
        source_hash="hash",
        issue_codes=("UNAVAILABLE_METRICS",),
        now=tick + timedelta(minutes=1),
    )
    assert repeated.state == "NOOP"


def test_partial_rehearsal_can_retry_one_pre_benchmark_blocked_attempt(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    capability = _capability(auto_repair=False, selectors=("archive", "test"))
    blocked_service = EvolutionService(
        store,
        (capability,),
        candidate_builder=_Builder(),
    )
    first = blocked_service.run_partial_rehearsal(
        (_evidence(command="/helix:test", stage_kind="testing"),),
        source_hash="hash",
        issue_codes=("UNAVAILABLE_METRICS",),
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert first.state == "BLOCKED"

    retry_service = EvolutionService(
        store,
        (capability,),
        candidate_builder=_Builder(),
        evaluation_gateway=_Gateway(),
    )
    retry = retry_service.run_partial_rehearsal(
        (_evidence(command="/helix:test", stage_kind="testing"),),
        source_hash="hash",
        issue_codes=("UNAVAILABLE_METRICS",),
        retry_of=first.experiment_id,
        now=datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
    )

    assert retry.state == "AWAITING_EVALUATION"
    payload = json.loads(store.get_hypothesis(retry.hypothesis_ids[0])["payload_json"])
    assert payload["title"].startswith("[PARTIAL 演练重试]")
    assert "stage=testing, command=/helix:test" in payload["rationale"]
    assert f"retry_of={first.experiment_id}" in payload["rationale"]
    with pytest.raises(ValueError, match="pre-Benchmark blocked rehearsal"):
        retry_service.run_partial_rehearsal(
            (_evidence(),),
            source_hash="hash",
            issue_codes=("UNAVAILABLE_METRICS",),
            retry_of=retry.experiment_id,
        )


def test_partial_rehearsal_reuses_frozen_candidate_after_submission_fix(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    capability = _capability(auto_repair=False)
    first = EvolutionService(
        store,
        (capability,),
        candidate_builder=_Builder(),
        evaluation_gateway=_FailingSubmitGateway(),
    ).run_partial_rehearsal(
        (_evidence(),),
        source_hash="hash",
        issue_codes=("UNAVAILABLE_METRICS",),
        now=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert first.state == "BLOCKED"
    blocked = store.get_experiment(first.experiment_id)
    assert blocked["candidate_id"] == "cand-1"
    assert blocked["benchmark_run_id"] is None

    resumed = EvolutionService(
        store,
        (capability,),
        candidate_builder=_Builder(),
        evaluation_gateway=_Gateway(),
    ).resume_partial_benchmark_submission(first.experiment_id)

    assert resumed.state == "AWAITING_EVALUATION"
    experiment = store.get_experiment(first.experiment_id)
    assert experiment["candidate_id"] == "cand-1"
    assert experiment["benchmark_run_id"] == "run-1"
    assert "from frozen Candidate" in experiment["decision_reason"]
