"""Evidence-bounded, on-demand hard-gate analysis for Scenario v2 Runs."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from bench.hard_gate_analysis import (
    HardGateAnalysisError,
    HardGateAnalyzer,
    validate_hard_gate_analysis_output,
)
from bench.scenario_runtime import ScenarioRunService, ScenarioRuntimeError
from bench.scenario_storage import ScenarioStore
from bench.scoring import merge_workflow_completion_gates

PROMPT_VERSION = "framework-eval-scenario-hard-gate-analysis.v1"
_MAX_TURNS_PER_JOB = 48
_MAX_TEXT_CHARS = 800


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _excerpt(value: object, *, limit: int = _MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.replace("```", " ").split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1].rstrip()}…"


def _mechanical_attribution(side: str) -> str:
    if side == "CANDIDATE":
        return "TESTED_FRAMEWORK"
    if side == "BASE":
        return "BASE_FRAMEWORK"
    return "SHARED_CONTRACT"


def _strategy_stages(contract: dict[str, Any], side: str) -> tuple[dict[str, str], ...]:
    """Project the frozen ordered stages used for one side's completion diagnosis."""

    strategy = _object(_object(contract.get("strategies")).get(side))
    stages: list[dict[str, str]] = []
    for item in _list(strategy.get("stages")):
        if not isinstance(item, dict) or not str(item.get("stage_id", "")).strip():
            continue
        stages.append(
            {
                "stage_id": str(item["stage_id"]),
                "command": str(item.get("command", "")),
                "semantic_role": str(item.get("semantic_role", "")),
            }
        )
    return tuple(stages)


def _workflow_completion_fact(
    stages: tuple[dict[str, str], ...],
    *,
    completed_stage_ids: tuple[str, ...],
    terminal_reason: object,
    missing_required_evidence: list[str],
) -> dict[str, Any]:
    """Identify the first frozen stage not completed without parsing error prose."""

    completed = set(completed_stage_ids)
    first_incomplete_index = next(
        (index for index, stage in enumerate(stages) if stage["stage_id"] not in completed),
        None,
    )
    last_completed_stage = None
    first_incomplete_stage = None
    subsequent_incomplete_stages: list[dict[str, str]] = []
    if first_incomplete_index is not None:
        first_incomplete_stage = stages[first_incomplete_index]
        last_completed_stage = next(
            (
                stage
                for stage in reversed(stages[:first_incomplete_index])
                if stage["stage_id"] in completed
            ),
            None,
        )
        subsequent_incomplete_stages = [
            stage
            for stage in stages[first_incomplete_index + 1 :]
            if stage["stage_id"] not in completed
        ]
    elif stages:
        last_completed_stage = stages[-1]
    return {
        "declared_stage_count": len(stages),
        "completed_stage_count": sum(stage["stage_id"] in completed for stage in stages),
        "last_completed_stage": last_completed_stage,
        "first_incomplete_stage": first_incomplete_stage,
        "subsequent_incomplete_stages": subsequent_incomplete_stages,
        "all_declared_stages_completed": bool(stages) and first_incomplete_index is None,
        "missing_required_evidence": missing_required_evidence,
        "terminal_reason": str(terminal_reason or ""),
    }


class ScenarioHardGateAnalysisService:
    """Expose deterministic facts and append explicitly requested AI advice."""

    def __init__(self, store: ScenarioStore, *, analyzer: HardGateAnalyzer | None = None) -> None:
        self.store = store
        self.analyzer = analyzer
        self.run_service = ScenarioRunService(store)

    def facts(self, run_id: str) -> dict[str, Any]:
        """Build the current immutable evidence packet without invoking a model."""

        try:
            run = self.run_service.get_run(run_id)
            report = self.run_service.get_report(run_id)
        except ScenarioRuntimeError as exc:
            raise HardGateAnalysisError("只有已形成最终报告的任务才能查看门禁事实") from exc
        if run.status != "COMPLETED" or not run.current_calculation_id:
            raise HardGateAnalysisError("任务尚未结束，暂不能形成门禁事实包")

        calculation = _object(report.get("calculation"))
        run_fact = {
            "run_id": run_id,
            "run_kind": run.run_kind,
            "status": run.status,
            "calculation_id": str(calculation.get("calculation_id", "")),
            "calculation_sequence": calculation.get("sequence_number"),
            "calculation_status": calculation.get("status"),
            "recommendation": calculation.get("recommendation"),
            "comparable_case_count": calculation.get("comparable_case_count"),
            "selected_case_count": calculation.get("selected_case_count"),
            "report_hash": report.get("report_hash"),
            "contract_hash": _object(report.get("run")).get("contract_hash"),
        }
        evidence: list[dict[str, Any]] = [
            {"ref_id": "run:result", "kind": "RUN_RESULT", "fact": run_fact}
        ]
        hard_gates: list[dict[str, str]] = []
        side_facts: list[dict[str, Any]] = []
        seen_gates: set[tuple[str, str, str]] = set()
        jobs = self.store.list_run_jobs(run_id)
        jobs_by_case_side = {(job.case_id, job.side): job for job in jobs}
        contract = self.store.get_run_contract(run_id) or {}
        stages_by_side = {
            side: _strategy_stages(contract, side)
            for side in ("BASE", "CANDIDATE")
        }

        for item in _list(report.get("execution_outcomes")):
            if not isinstance(item, dict):
                continue
            case_id = str(item.get("case_id", ""))
            side = str(item.get("side", ""))
            outcome = _object(item.get("outcome"))
            if not case_id or side not in {"BASE", "CANDIDATE"}:
                continue
            verification = _object(outcome.get("verification"))
            initialization = _object(outcome.get("initialization"))
            execution = _object(outcome.get("execution"))
            artifact_evidence = [value for value in _list(outcome.get("evidence")) if isinstance(value, dict)]
            normalized_case_gates = merge_workflow_completion_gates(
                str(value) for value in _list(outcome.get("case_hard_gates"))
            )
            missing_required_evidence = [
                str(value.get("evidence_id", ""))
                for value in artifact_evidence
                if value.get("required") is True and value.get("status") != "PRESENT"
            ]
            completed_stage_ids = tuple(
                str(value)
                for value in _list(execution.get("completed_stage_ids"))
                if isinstance(value, str) and value
            )
            if not completed_stage_ids:
                job = jobs_by_case_side.get((case_id, side))
                completed_stage_ids = job.completed_stage_ids if job is not None else ()
            side_fact = {
                "case_id": case_id,
                "side": side,
                "terminal_status": outcome.get("terminal_status"),
                "terminal_reason": execution.get("terminal_reason"),
                "outcome_quality": outcome.get("outcome_quality"),
                "framework_workflow_quality": outcome.get("framework_workflow_quality"),
                "assessment_compatible": outcome.get("assessment_compatible"),
                "incompatibility_reasons": [str(value) for value in _list(outcome.get("incompatibility_reasons"))],
                "verification_status": verification.get("status"),
                "verification_check_count": len(_list(verification.get("checks"))),
                "initialization_status": initialization.get("status"),
                "artifact_evidence_count": len(artifact_evidence),
                "missing_required_evidence": missing_required_evidence,
                "case_hard_gates": list(normalized_case_gates),
            }
            if "WORKFLOW_TERMINAL_FAILURE" in normalized_case_gates:
                side_fact["workflow_completion"] = _workflow_completion_fact(
                    stages_by_side.get(side, ()),
                    completed_stage_ids=completed_stage_ids,
                    terminal_reason=execution.get("terminal_reason"),
                    missing_required_evidence=missing_required_evidence,
                )
            side_facts.append(side_fact)
            side_ref = f"side:{side}:{case_id}"
            evidence.append({"ref_id": side_ref, "kind": "SIDE_RESULT", "fact": side_fact})

            for check_index, check in enumerate(_list(verification.get("checks")), start=1):
                if not isinstance(check, dict):
                    continue
                check_fact = {
                    key: check.get(key)
                    for key in ("check_id", "kind", "status", "returncode", "timed_out")
                    if key in check
                }
                check_fact["stdout_excerpt"] = _excerpt(check.get("stdout"))
                check_fact["stderr_excerpt"] = _excerpt(check.get("stderr"))
                evidence.append(
                    {
                        "ref_id": f"check:{side}:{case_id}:{check_index}",
                        "kind": "BUSINESS_VERIFICATION_CHECK",
                        "fact": check_fact,
                    }
                )
            for artifact_index, artifact in enumerate(artifact_evidence, start=1):
                artifact_fact = {
                    key: artifact.get(key)
                    for key in ("evidence_id", "kind", "path", "required", "status", "sha256", "size_bytes")
                    if key in artifact
                }
                evidence.append(
                    {
                        "ref_id": f"artifact:{side}:{case_id}:{artifact_index}",
                        "kind": "ARTIFACT_EVIDENCE",
                        "fact": artifact_fact,
                    }
                )
            for gate_id in side_fact["case_hard_gates"]:
                self._append_gate(
                    hard_gates,
                    evidence,
                    seen_gates,
                    gate_id=gate_id,
                    side=side,
                    case_id=case_id,
                    related_ref=side_ref,
                )

        for gate_id in _list(calculation.get("global_hard_gates")):
            self._append_gate(
                hard_gates,
                evidence,
                seen_gates,
                gate_id=str(gate_id),
                side="GLOBAL",
                case_id="ALL",
                related_ref="run:result",
            )

        for job in jobs:
            for turn in self.store.list_job_turn_evidence(job.job_id)[:_MAX_TURNS_PER_JOB]:
                turn_sequence = int(turn.get("turn_sequence", 0))
                fact = {
                    "attempt_number": turn.get("attempt_number"),
                    "turn_sequence": turn_sequence,
                    "input_kind": turn.get("input_kind"),
                    "workflow_step_id": turn.get("workflow_step_id"),
                    "milestone_id": turn.get("milestone_id"),
                    "classification": turn.get("classification"),
                    "started_at_ms": turn.get("started_at_ms"),
                    "completed_at_ms": turn.get("completed_at_ms"),
                    "duration_ms": turn.get("duration_ms"),
                    "final_response_excerpt": _excerpt(turn.get("final_response")),
                    "error": _object(turn.get("error")).get("message"),
                    "event_summaries": [
                        _excerpt(event.get("summary"), limit=240)
                        for event in _list(turn.get("events"))[:8]
                        if isinstance(event, dict) and event.get("summary")
                    ],
                }
                evidence.append(
                    {
                        "ref_id": f"turn:{job.side}:{job.case_id}:{job.attempt_number}:{turn_sequence}",
                        "kind": "INTERACTION_TURN",
                        "fact": fact,
                    }
                )

        return {
            "schema_version": "framework-eval-scenario-hard-gate-facts.v1",
            "analysis_scope": {
                "advisory_only": True,
                "may_not_change_evaluation_decision": True,
                "model_not_invoked": True,
                "full_dialogue_excluded": True,
                "workspace_source_excluded": True,
            },
            "run": run_fact,
            "hard_gates": hard_gates,
            "side_facts": side_facts,
            "evidence": evidence,
        }

    @staticmethod
    def _append_gate(
        hard_gates: list[dict[str, str]],
        evidence: list[dict[str, Any]],
        seen: set[tuple[str, str, str]],
        *,
        gate_id: str,
        side: str,
        case_id: str,
        related_ref: str,
    ) -> None:
        identity = (gate_id, side, case_id)
        if not gate_id or identity in seen:
            return
        seen.add(identity)
        ref_id = f"gate:{len(hard_gates) + 1}"
        gate = {
            "gate_id": gate_id,
            "side": side,
            "case_id": case_id,
            "mechanical_attribution": _mechanical_attribution(side),
        }
        hard_gates.append(gate)
        evidence.append(
            {
                "ref_id": ref_id,
                "kind": "HARD_GATE_TRIGGER",
                "fact": {**gate, "related_result_ref": related_ref},
            }
        )

    def latest(self, run_id: str) -> dict[str, object] | None:
        """Load the latest attempt; callers decide whether its calculation is current."""

        return self.store.get_latest_scenario_hard_gate_analysis(run_id)

    def analyze(self, run_id: str) -> dict[str, object]:
        """Append one analysis only after an explicit user action."""

        if self.analyzer is None:
            raise HardGateAnalysisError("AI 分析服务尚未配置")
        request = self.facts(run_id)
        if not request["hard_gates"]:
            raise HardGateAnalysisError("本次任务没有可分析的硬门禁")
        runtime_policy = self.store.get_run_runtime_policy(run_id) or {}
        model = str(runtime_policy.get("judge_model", "")).strip()
        reasoning_effort = str(
            runtime_policy.get("judge_reasoning_effort", runtime_policy.get("reasoning_effort", "medium"))
        ).strip() or "medium"
        if not model:
            raise HardGateAnalysisError("冻结任务未记录 Judge 模型，无法执行 AI 分析")
        request["analysis_scope"]["model_not_invoked"] = False
        serialized = json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        input_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        analysis_id = f"shga_{uuid.uuid4().hex[:24]}"
        created_at = datetime.now(timezone.utc).isoformat()
        calculation_id = str(request["run"]["calculation_id"])
        common: dict[str, object] = {
            "analysis_id": analysis_id,
            "run_id": run_id,
            "calculation_id": calculation_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "prompt_version": PROMPT_VERSION,
            "input_sha256": input_sha256,
            "input": request,
            "created_at": created_at,
        }
        try:
            output = validate_hard_gate_analysis_output(
                self.analyzer.analyze(request, model=model, reasoning_effort=reasoning_effort),
                request,
            )
        except Exception as exc:
            safe_error = str(exc) if isinstance(exc, HardGateAnalysisError) else "AI 分析服务调用失败，请稍后重试。"
            self.store.record_scenario_hard_gate_analysis(
                {**common, "status": "FAILED", "output": {}, "error": safe_error}
            )
            raise HardGateAnalysisError(safe_error) from exc
        self.store.record_scenario_hard_gate_analysis(
            {**common, "status": "READY", "output": output, "error": None}
        )
        stored = self.latest(run_id)
        if stored is None:  # pragma: no cover - protected by the append above
            raise RuntimeError("Scenario hard-gate analysis was not persisted")
        return stored
