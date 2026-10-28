"""Scenario v2 hard-gate facts and explicit AI-analysis contracts."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from bench.hard_gate_analysis import HardGateAnalysisError
from bench.scenario_hard_gate_analysis import (
    ScenarioHardGateAnalysisService,
    _workflow_completion_fact,
)
from bench.scenario_runtime import (
    MockScenarioExecutionAdapter,
    ScenarioRunCreateRequest,
    ScenarioRunService,
    ScenarioWorker,
)
from bench.scenario_storage import ScenarioStore
from bench.scenarios import load_scenario_source
from bench.tests.test_scenario_runtime import _contract, _write_scenario


class _CandidateGateAdapter(MockScenarioExecutionAdapter):
    def execute(self, job: object) -> dict[str, object]:
        outcome = super().execute(job)  # type: ignore[arg-type]
        if getattr(job, "side") == "CANDIDATE":
            outcome["case_hard_gates"] = ["CORE_BUSINESS_BEHAVIOR_FAILURE"]
            outcome["outcome_quality"] = 0.0
            outcome["outcome_axis_scores"] = {}
            outcome["verification"] = {
                "status": "FAIL",
                "checks": [
                    {
                        "check_id": "F2P-1",
                        "kind": "FAIL_TO_PASS",
                        "status": "FAIL",
                        "returncode": 1,
                        "timed_out": False,
                        "stderr": "expected complete, got incomplete",
                    }
                ],
            }
        return outcome


def test_workflow_completion_fact_identifies_frozen_stage_gap() -> None:
    stages = tuple(
        {
            "stage_id": f"STEP_{index:02d}",
            "command": command,
            "semantic_role": role,
        }
        for index, command, role in (
            (1, "/helix:propose feature request.md", "PROPOSE"),
            (2, "/helix:propose feature --continue", "PROPOSE_CONTINUE"),
            (3, "/helix:apply feature", "APPLY"),
            (4, "/helix:verify feature", "VERIFY"),
            (5, "/helix:archive feature", "ARCHIVE"),
        )
    )

    fact = _workflow_completion_fact(
        stages,
        completed_stage_ids=("STEP_01", "STEP_02", "STEP_03"),
        terminal_reason="WORKFLOW_BLOCKED",
        missing_required_evidence=[],
    )

    assert fact["last_completed_stage"]["stage_id"] == "STEP_03"
    assert fact["first_incomplete_stage"]["stage_id"] == "STEP_04"
    assert [stage["stage_id"] for stage in fact["subsequent_incomplete_stages"]] == ["STEP_05"]
    assert fact["terminal_reason"] == "WORKFLOW_BLOCKED"


def _completed_gated_run(tmp_path: Path) -> tuple[ScenarioStore, ScenarioRunService, str]:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="hard-gate-analysis-test",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "judge_model": "frozen-judge-model",
            "judge_reasoning_effort": "high",
        },
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    worker = ScenarioWorker(store, _CandidateGateAdapter())
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "FINALIZED"
    return store, service, run.run_id


class _RecordingAnalyzer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def analyze(
        self,
        request: dict[str, object],
        *,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, object]:
        self.calls.append(
            {"request": request, "model": model, "reasoning_effort": reasoning_effort}
        )
        gate = request["hard_gates"][0]  # type: ignore[index]
        return {
            "schema_version": "framework-eval-hard-gate-analysis.v1",
            "overall_summary": "Candidate 的 F2P 业务检查失败，建议先复核实现结果。",
            "findings": [
                {
                    "gate_id": gate["gate_id"],
                    "side": gate["side"],
                    "case_id": gate["case_id"],
                    "attribution": "TESTED_FRAMEWORK",
                    "confidence": "MEDIUM",
                    "summary": "业务结果与冻结检查不一致。",
                    "root_causes": ["Candidate 未满足 F2P 检查。"],
                    "evidence_refs": [
                        {
                            "ref_id": "check:CANDIDATE:runtime-scenario:1",
                            "claim": "F2P-1 返回失败。",
                        }
                    ],
                    "counterevidence": ["Base 同一检查未触发门禁。"],
                    "recommended_actions": ["复核 Candidate 输出并重跑 F2P-1。"],
                }
            ],
            "decision_impact": "ANALYSIS_ONLY",
        }


def test_facts_are_deterministic_and_do_not_invoke_analyzer(tmp_path: Path) -> None:
    store, _, run_id = _completed_gated_run(tmp_path)
    analyzer = _RecordingAnalyzer()

    facts = ScenarioHardGateAnalysisService(store, analyzer=analyzer).facts(run_id)

    assert analyzer.calls == []
    assert facts["analysis_scope"]["model_not_invoked"] is True
    assert facts["hard_gates"] == [
        {
            "gate_id": "CORE_BUSINESS_BEHAVIOR_FAILURE",
            "side": "CANDIDATE",
            "case_id": "runtime-scenario",
            "mechanical_attribution": "TESTED_FRAMEWORK",
        }
    ]
    check = next(item for item in facts["evidence"] if item["kind"] == "BUSINESS_VERIFICATION_CHECK")
    assert check["fact"]["status"] == "FAIL"
    assert "expected complete" in check["fact"]["stderr_excerpt"]


def test_explicit_analysis_is_append_only_and_cannot_change_report(tmp_path: Path) -> None:
    store, run_service, run_id = _completed_gated_run(tmp_path)
    analyzer = _RecordingAnalyzer()
    service = ScenarioHardGateAnalysisService(store, analyzer=analyzer)
    report_before = deepcopy(run_service.get_report(run_id))

    analysis = service.analyze(run_id)

    assert analysis["status"] == "READY"
    assert analysis["calculation_id"] == report_before["calculation"]["calculation_id"]
    assert analysis["output"]["decision_impact"] == "ANALYSIS_ONLY"
    assert analyzer.calls[0]["model"] == "frozen-judge-model"
    assert analyzer.calls[0]["reasoning_effort"] == "high"
    assert run_service.get_report(run_id) == report_before
    assert store.get_latest_scenario_hard_gate_analysis(run_id)["analysis_id"] == analysis["analysis_id"]


def test_invalid_analysis_is_recorded_as_failed(tmp_path: Path) -> None:
    store, _, run_id = _completed_gated_run(tmp_path)

    class InvalidAnalyzer:
        @staticmethod
        def analyze(request, *, model, reasoning_effort):
            return {
                "schema_version": "framework-eval-hard-gate-analysis.v1",
                "overall_summary": "invalid",
                "findings": [],
                "decision_impact": "ANALYSIS_ONLY",
            }

    with pytest.raises(HardGateAnalysisError, match="未完整对应"):
        ScenarioHardGateAnalysisService(store, analyzer=InvalidAnalyzer()).analyze(run_id)

    latest = store.get_latest_scenario_hard_gate_analysis(run_id)
    assert latest is not None
    assert latest["status"] == "FAILED"
    assert latest["output"] == {}


def test_analysis_refuses_runs_without_hard_gates_before_model_call(tmp_path: Path) -> None:
    store, _, _ = _completed_gated_run(tmp_path)
    analyzer = _RecordingAnalyzer()

    # A second clean Run is used because trusted report rows are immutable.
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="no-gate-test",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "judge_model": "frozen-judge-model",
        },
    )
    run_service = ScenarioRunService(store)
    clean_run = run_service.create(request, expected_preview_hash=run_service.preview(request).preview_hash)
    worker = ScenarioWorker(store, MockScenarioExecutionAdapter())
    assert worker.run_once(run_id=clean_run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=clean_run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=clean_run.run_id).status == "FINALIZED"

    with pytest.raises(HardGateAnalysisError, match="没有可分析"):
        ScenarioHardGateAnalysisService(store, analyzer=analyzer).analyze(clean_run.run_id)
    assert analyzer.calls == []
