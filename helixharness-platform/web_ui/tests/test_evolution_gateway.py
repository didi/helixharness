from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from bench.runtime_secrets import EvaluationProviderConfig
from bench.scenario_runtime import ClaimedScenarioJob, MockScenarioExecutionAdapter, ScenarioWorker
from bench.scenario_storage import ScenarioStore
from bench.scenarios import load_scenario_source
from bench.task_configuration import (
    CalibrationResourcePolicy,
    ModelConfiguration,
    TaskPlatformConfiguration,
    WorkloadResourcePolicy,
)
from bench.tests.test_scenario_runtime import _write_scenario
from bench.workload_policy import EVOLUTION_EVALUATION
from evolution.models import AcceptanceContract, Hypothesis, HypothesisKind
from web_ui.evolution_evaluation import ScenarioEvolutionGateway
from web_ui.tests.test_scenario import _write_helixharness_repository


def _hypothesis(case_ids: tuple[str, ...] = ("runtime-scenario",)) -> Hypothesis:
    return Hypothesis(
        hypothesis_id="hyp-1",
        kind=HypothesisKind.REPAIR,
        capability_id="workflow",
        title="修复工作流",
        rationale="合格证据显示归档失败",
        evidence_ids=("e-1",),
        confidence=0.9,
        acceptance=AcceptanceContract(
            "workflow",
            case_ids,
            "归档门禁恢复通过",
            case_ids,
            ("eval/",),
            auto_workflow_confirmation=True,
        ),
        requires_human_selection=False,
        semantic_source="RULE",
        created_at="2026-08-01T00:00:00Z",
    )


def _gateway(tmp_path: Path) -> tuple[ScenarioEvolutionGateway, ScenarioStore, str, str]:
    repository, base_commit, candidate_commit = _write_helixharness_repository(tmp_path)
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    scenario = store.publish(load_scenario_source(_write_scenario(source_root)))
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE scenarios SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
        connection.execute(
            "UPDATE scenario_versions SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
    store.record_calibration(scenario.case_id, scenario.source_hash, state="CALIBRATED")
    configuration = TaskPlatformConfiguration(
        helixharness_repository=str(repository),
        reference_ref=base_commit,
        models=ModelConfiguration(
            execution_model="gpt-5.6-sol",
            judge_model="gpt-5.6-sol",
            analysis_model="analysis-model",
            candidate_model="candidate-model",
            execution_reasoning_effort="low",
        ),
        permission_mode="workspace-write",
        formal_evaluation=WorkloadResourcePolicy(600, 50_000, 200, 2_048, 2),
        evolution_analysis=WorkloadResourcePolicy(180, 20_000, 40, 512, 1),
        evolution_candidate=WorkloadResourcePolicy(900, 80_000, 400, 4_096, 1),
        evolution_evaluation=WorkloadResourcePolicy(1_200, 100_000, 500, 5_120, 1),
        case_calibration=CalibrationResourcePolicy(1_800, 8_192, 1, 4_096),
    )
    provider = EvaluationProviderConfig("codex", "openai", "", "", "gpt-5.6-sol", "TEST_KEY")
    return (
        ScenarioEvolutionGateway(
            store,
            repository=repository,
            configuration=configuration,
            provider=provider,
        ),
        store,
        base_commit,
        candidate_commit,
    )


def test_gateway_submits_experiment_to_scenario_v2(tmp_path: Path) -> None:
    gateway, store, base_commit, candidate_commit = _gateway(tmp_path)

    submission = gateway.submit(_hypothesis(), candidate_commit, base_commit)
    policy = store.get_run_runtime_policy(submission.run_id)

    assert submission.state == "QUEUED"
    assert submission.plan["schema_version"] == "evolution-evaluation-plan.v2"
    assert submission.plan["execution_runtime"] == "SCENARIO_V2"
    assert policy is not None
    assert policy["origin"] == "EVOLUTION_DISCOVERY"
    assert policy["workload_kind"] == "EVOLUTION_EVALUATION"
    assert policy["token_budget"] == 100_000
    assert policy["max_concurrency"] == 1
    assert store.run_counts_by_origin()["EVOLUTION_DISCOVERY"] == 1


class _CodexMockAdapter(MockScenarioExecutionAdapter):
    name = "CODEX"


def test_gateway_reads_v2_verdict_after_parallel_jobs_finish(tmp_path: Path) -> None:
    gateway, store, base_commit, candidate_commit = _gateway(tmp_path)
    submission = gateway.submit(_hypothesis(), candidate_commit, base_commit)
    worker = ScenarioWorker(
        store,
        _CodexMockAdapter(),
        workload_kind=EVOLUTION_EVALUATION,
    )
    while worker.run_once(run_id=submission.run_id).status != "FINALIZED":
        pass

    verdict = gateway.verdict(submission.run_id)

    assert verdict is not None
    assert verdict.calculation_id is not None
    assert verdict.decision_status in {"RECOMMEND", "NO_CLEAR_GAIN", "DO_NOT_RECOMMEND", "BLOCKED"}


class _CandidateGateAdapter(_CodexMockAdapter):
    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        outcome = super().execute(job)
        if job.side == "CANDIDATE":
            outcome["case_hard_gates"] = ["WORKFLOW_TERMINAL_FAILURE"]
        return outcome


def test_gateway_projects_candidate_only_v2_gate_for_remediation(tmp_path: Path) -> None:
    gateway, store, base_commit, candidate_commit = _gateway(tmp_path)
    submission = gateway.submit(_hypothesis(), candidate_commit, base_commit)
    worker = ScenarioWorker(
        store,
        _CandidateGateAdapter(),
        workload_kind=EVOLUTION_EVALUATION,
    )
    while worker.run_once(run_id=submission.run_id).status != "FINALIZED":
        pass

    evidence = gateway.remediation_candidates()

    assert len(evidence) == 1
    assert evidence[0].source_run_id == submission.run_id
    assert evidence[0].findings[0].repair_target == "WORKFLOW_TERMINAL_FAILURE"
    assert evidence[0].profile == "SCENARIO_V2"
    assert evidence[0].decision_status == "RECOMMEND"


def test_gateway_routes_initial_and_remediation_runs_to_one_evolution_pool(tmp_path: Path) -> None:
    gateway, store, base_commit, candidate_commit = _gateway(tmp_path)
    initial = gateway.submit(_hypothesis(), candidate_commit, base_commit)
    worker = ScenarioWorker(store, _CodexMockAdapter(), workload_kind=EVOLUTION_EVALUATION)
    while worker.run_once(run_id=initial.run_id).status != "FINALIZED":
        pass
    remediation = gateway.submit_remediation(
        replace(
            _hypothesis(),
            semantic_source="BENCH_REMEDIATION",
            source_kind="BENCH_RUN",
            source_run_id=initial.run_id,
            source_root_run_id=initial.run_id,
            source_base_commit=base_commit,
            source_candidate_commit=candidate_commit,
        ),
        candidate_commit,
    )

    initial_policy = store.get_run_runtime_policy(initial.run_id)
    remediation_policy = store.get_run_runtime_policy(remediation.run_id)

    assert initial_policy is not None and remediation_policy is not None
    assert initial_policy["workload_kind"] == "EVOLUTION_EVALUATION"
    assert remediation_policy["workload_kind"] == "EVOLUTION_EVALUATION"
    assert initial_policy["origin"] == "EVOLUTION_DISCOVERY"
    assert remediation_policy["origin"] == "EVOLUTION_REMEDIATION"


def test_ai_case_selection_can_decline_pool_without_creating_a_run(tmp_path: Path) -> None:
    import pytest

    gateway, store, base_commit, candidate_commit = _gateway(tmp_path)
    hypothesis = _hypothesis(())
    hypothesis = replace(hypothesis, acceptance=replace(hypothesis.acceptance, case_selection_mode="AI"))

    class DecliningRanker:
        def rank(self, request, **kwargs):
            candidate = request["candidates"][0]
            assert candidate["requirements"][0]["judge_ids"] == ["F2P-1", "P2P-1"]
            checks = {item["check_id"]: item for item in candidate["verification_checks"]}
            assert checks["F2P-1"]["argv"] == ["python3", "-c", "raise SystemExit(0)"]
            assert checks["SC-1"]["scope"] == "SOLUTION"
            assert checks["SC-1"]["argv"] == ["python3", "-c", "raise SystemExit(0)"]
            return {"schema_version": "evolution-case-selection.v2", "case_ids": [],
                    "rationale": "现有 Case 没有覆盖缺失证据的恢复行为", "confidence": 0.0}

    gateway.case_ranker = DecliningRanker()
    before = store.run_counts_by_origin()
    with pytest.raises(ValueError, match="没有覆盖当前假设的可评测 Case"):
        gateway.submit(hypothesis, candidate_commit, base_commit)
    assert store.run_counts_by_origin() == before


def test_ranker_schema_allows_no_matching_case() -> None:
    from types import SimpleNamespace

    from web_ui.evolution_evaluation import OpenAIEvolutionCaseRanker

    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output_text='{"schema_version":"evolution-case-selection.v2","case_ids":[],"rationale":"unrelated","confidence":0}')

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    provider = EvaluationProviderConfig("codex", "openai", "", "", "test-model", "TEST_KEY")
    ranker = OpenAIEvolutionCaseRanker(provider_config=provider, client_factory=lambda: client)
    result = ranker.rank({"candidates": [{"case_id": "unrelated"}], "max_cases": 1}, model="test-model", reasoning_effort="low")
    assert result["case_ids"] == []
    assert captured["text"]["format"]["schema"]["properties"]["case_ids"]["minItems"] == 0
