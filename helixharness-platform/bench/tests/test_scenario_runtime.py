from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from bench.evaluation_contracts import (
    UNIFIED_SCORING_PROFILE,
    ArtifactSource,
    ExecutionStrategy,
    StrategyStage,
    freeze_evaluation_contract,
)
from bench.scenario_runtime import (
    MockScenarioExecutionAdapter,
    ScenarioRetryableExecutionError,
    ScenarioRunCreateRequest,
    ScenarioRunService,
    ScenarioRuntimeError,
    ScenarioWorker,
    _claim_next_job,
    _cleanup_terminal_run_workspaces,
    _fail_job,
    _side_evaluation,
)
from bench.scenario_storage import ScenarioStore
from bench.scenarios import load_scenario_source
from bench.workload_policy import EVOLUTION_EVALUATION, FORMAL_EVALUATION


def _write_scenario(root: Path) -> Path:
    source = root / "runtime-scenario"
    source.mkdir()
    manifest = {
        "schema_version": "framework-eval-scenario.v2",
        "case_id": "runtime-scenario",
        "title": "Runtime scenario",
        "case_kind": "DEVELOPMENT_FIXTURE",
        "execution_intent": "FLOW_THROUGH",
        "initial_request_path": "initial-request.md",
        "gold_path": "gold.json",
        "clarification_policy_path": "clarification-policy.json",
        "workspace_path": "workspace",
        "runtime": {"language": "PYTHON", "python_version": "3.10"},
    }
    (source / "case.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "initial-request.md").write_text("Create result.txt with complete.\n", encoding="utf-8")
    (source / "clarification-policy.json").write_text(
        json.dumps(
            {
                "schema_version": "framework-eval-clarification-policy.v1",
                "execution_intent": "FLOW_THROUGH",
                "external_confirmation_required": False,
                "facts": [{"fact_id": "F-1", "statement": "The target text is complete."}],
            }
        ),
        encoding="utf-8",
    )
    (source / "gold.json").write_text(
        json.dumps(
            {
                "schema_version": "framework-eval-scenario-gold.v2",
                "requirements": [
                    {
                        "requirement_id": "REQ-1",
                        "statement": "The result is observable.",
                        "evidence_ids": ["E-1"],
                        "judge_ids": ["F2P-1", "P2P-1"],
                    }
                ],
                "source_evidence": [
                    {
                        "evidence_id": "E-1",
                        "path": "initial-request.md",
                        "line_start": 1,
                        "line_end": 1,
                        "quote": "Create result.txt with complete.",
                    }
                ],
                "execution_tests": [
                    {
                        "check_id": "F2P-1",
                        "kind": "FAIL_TO_PASS",
                        "argv": ["python3", "-c", "raise SystemExit(0)"],
                        "timeout_seconds": 30,
                    },
                    {
                        "check_id": "P2P-1",
                        "kind": "PASS_TO_PASS",
                        "argv": ["python3", "-c", "raise SystemExit(0)"],
                        "timeout_seconds": 30,
                    },
                ],
                "solution_checks": [
                    {
                        "check_id": "SC-1",
                        "scope": "SOLUTION",
                        "argv": ["python3", "-c", "raise SystemExit(0)"],
                        "timeout_seconds": 30,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    workspace = source / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
    return source


def _strategy(prefix: str) -> ExecutionStrategy:
    return ExecutionStrategy(
        strategy_id=f"{prefix}-strategy",
        version="1",
        adapter_id="known-helixharness",
        adapter_version="1",
        source_commit=("a" if prefix == "base" else "b") * 40,
        source_document_hashes=(("commands.md", "c" * 64),),
        commands=(f"/{prefix}:implement",),
        stages=(StrategyStage("IMPLEMENT", "IMPLEMENTATION", f"/{prefix}:implement", "COMPLETED", True),),
        completion_states=("COMPLETED",),
        artifact_sources=(ArtifactSource("completed-stage-artifacts", f"docs/specs/completed/{prefix}"),),
        finding_policy="NONE",
        finding_closures=(),
    )


def _contract() -> dict[str, object]:
    return freeze_evaluation_contract(
        base_strategy=_strategy("base"),
        candidate_strategy=_strategy("candidate"),
    )


def test_run_limits_case_selection_and_concurrency(tmp_path: Path) -> None:
    service = ScenarioRunService(ScenarioStore(tmp_path / "evaluation.db"))
    common = {
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract(),
        "created_by": "limit-test",
    }

    with pytest.raises(ScenarioRuntimeError, match="at most 10 Scenarios"):
        service.preview(
            ScenarioRunCreateRequest(
                scenario_ids=tuple(f"scenario-{index}" for index in range(11)),
                **common,
            )
        )

    with pytest.raises(ScenarioRuntimeError, match="between 1 and 4"):
        service.preview(
            ScenarioRunCreateRequest(
                scenario_ids=("runtime-scenario",),
                runtime_policy={"max_concurrency": 5},
                **common,
            )
        )


def test_mock_worker_persists_jobs_calculation_and_immutable_report(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="deployment-smoke",
    )

    preview = service.preview(request)
    run = service.create(request, expected_preview_hash=preview.preview_hash)
    worker = ScenarioWorker(store, MockScenarioExecutionAdapter())
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "FINALIZED"

    completed = service.get_run(run.run_id)
    report = service.get_report(run.run_id)
    assert completed.status == "COMPLETED"
    assert report["schema_version"] == "framework-eval-report.v2"
    assert report["calculation"]["status"] == "FULLY_COMPARABLE"
    assert report["calculation"]["recommendation"] == "RECOMMEND"
    assert report["calculation"]["candidate_unified_score"] > report["calculation"]["base_unified_score"]
    assert report["report_hash"] == sha256(
        json.dumps(
            {key: value for key, value in report.items() if key != "report_hash"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    recalculated = service.recalculate(run.run_id, reason="deployment recalculation smoke")
    assert recalculated.calculation_id != report["calculation"]["calculation_id"]
    assert len(store.list_run_calculations(run.run_id)) == 2
    assert service.get_report(run.run_id)["calculation"]["calculation_id"] == recalculated.calculation_id


def test_base_only_run_executes_once_and_completes_without_paired_score(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="base-verification",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "execution_scope": "BASE_ONLY",
        },
    )
    preview = service.preview(request)
    run = service.create(request, expected_preview_hash=preview.preview_hash)
    worker = ScenarioWorker(store, MockScenarioExecutionAdapter())

    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "FINALIZED"
    completed = service.get_run(run.run_id)
    assert completed.status == "COMPLETED"
    assert completed.current_calculation_id is None
    with sqlite3.connect(store.database_path) as connection:
        jobs = connection.execute(
            "SELECT side, status FROM scenario_jobs WHERE run_id = ?",
            (run.run_id,),
        ).fetchall()
    assert jobs == [("BASE", "COMPLETED")]
    with pytest.raises(ScenarioRuntimeError, match="no calculation report"):
        service.get_report(run.run_id)


def test_business_failure_remains_comparable_with_zero_outcome_and_workflow_score(tmp_path: Path) -> None:
    class BaseBusinessFailureAdapter:
        name = "MOCK"

        def execute(self, job: object) -> dict[str, object]:
            outcome = MockScenarioExecutionAdapter().execute(job)  # type: ignore[arg-type]
            if getattr(job, "side") == "BASE":
                outcome["outcome_quality"] = 0.0
                outcome["outcome_axis_scores"] = {}
                outcome["case_hard_gates"] = ["CORE_BUSINESS_BEHAVIOR_FAILURE"]
                outcome["reached_comparable_business_terminal"] = False
                outcome["verification"] = {"status": "FAIL", "checks": []}
            return outcome

    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="business-failure-scoring",
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    worker = ScenarioWorker(store, BaseBusinessFailureAdapter())

    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "FINALIZED"

    calculation = service.get_report(run.run_id)["calculation"]
    case = calculation["cases"][0]
    assert calculation["status"] == "FULLY_COMPARABLE"
    assert case["base"]["outcome_quality"] == 0
    assert case["candidate"]["outcome_quality"] == 80
    assert case["base"]["framework_workflow_quality"] == 60
    assert case["candidate"]["framework_workflow_quality"] == 80
    assert case["base"]["efficiency"] is None
    assert case["candidate"]["efficiency"] is None
    assert case["base"]["unified_score"] == 15
    assert case["candidate"]["unified_score"] == 80
    assert calculation["recommendation"] == "RECOMMEND"


def test_completed_job_persists_bounded_turn_evidence_before_workspace_cleanup(
    tmp_path: Path,
) -> None:
    class TurnEvidenceAdapter:
        name = "MOCK"

        def execute(self, job: object) -> dict[str, object]:
            outcome = MockScenarioExecutionAdapter().execute(job)  # type: ignore[arg-type]
            execution: dict[str, object] = {
                "completed_stage_ids": [],
            }
            outcome["execution"] = execution
            execution["terminal_reason"] = "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"
            execution["turn_evidence"] = [
                {
                    "turn_sequence": 1,
                    "turn_id": "turn-1",
                    "input_kind": "WORKFLOW_STEP_RERUN",
                    "workflow_step_id": "APPLY",
                    "milestone_id": "IMPLEMENTED",
                    "classification": "FAILED",
                    "started_at_ms": 1_000,
                    "completed_at_ms": 2_000,
                    "duration_ms": 1_000,
                    "final_response": "编译仍失败。",
                    "error": None,
                    "events": [
                        {
                            "event_type": "workflow/closure",
                            "category": "workflow",
                            "status": "FAILED",
                            "summary": "阶段返工次数耗尽",
                            "payload": {
                                "reason": "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"
                            },
                            "occurred_at_ms": 2_000,
                        }
                    ],
                }
            ]
            return outcome

    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="turn-evidence-test",
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    worker = ScenarioWorker(store, TurnEvidenceAdapter())

    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    job = store.list_run_jobs(run.run_id)[0]
    turns = store.list_job_turn_evidence(job.job_id)

    assert job.terminal_reason == "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"
    assert len(turns) == 1
    assert turns[0]["attempt_number"] == 1
    assert turns[0]["workflow_step_id"] == "APPLY"
    assert turns[0]["classification"] == "FAILED"
    assert turns[0]["final_response"] == "编译仍失败。"
    assert turns[0]["events"][0]["payload"]["reason"] == "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"


class _RetryOnceAdapter:
    name = "MOCK"

    def __init__(self) -> None:
        self.delegate = MockScenarioExecutionAdapter()
        self.calls = 0

    def execute(self, job: object) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise ScenarioRetryableExecutionError("stream disconnected")
        return self.delegate.execute(job)  # type: ignore[arg-type]


class _AlwaysRetryAdapter:
    name = "MOCK"

    def execute(self, job: object) -> dict[str, object]:
        del job
        raise ScenarioRetryableExecutionError("temporarily unavailable")


class _PauseOnceAdapter:
    name = "MOCK"

    def __init__(self) -> None:
        self.delegate = MockScenarioExecutionAdapter()
        self.calls = 0

    def execute(self, job: object) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise OSError(122, "Disk quota exceeded")
        return self.delegate.execute(job)  # type: ignore[arg-type]


class _RecordingCleanupAdapter:
    name = "MOCK"

    def __init__(self) -> None:
        self.cleaned: list[tuple[str, tuple[str, ...]]] = []

    def execute(self, job: object) -> dict[str, object]:
        return MockScenarioExecutionAdapter().execute(job)  # type: ignore[arg-type]

    def cleanup_run_workspace(self, run_id: str, job_ids: tuple[str, ...]) -> None:
        self.cleaned.append((run_id, job_ids))


def _development_run(store: ScenarioStore, scenario_root: Path) -> tuple[ScenarioRunService, str]:
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(scenario_root)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="retry-test",
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    return service, run.run_id


def test_worker_retries_transient_infrastructure_failure_then_completes(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)
    worker = ScenarioWorker(store, _RetryOnceAdapter())

    assert worker.run_once(run_id=run_id).status == "RETRY_PENDING"
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT status, attempt_number, error, started_at, completed_at FROM scenario_jobs "
            "WHERE run_id = ? AND side = 'BASE'",
            (run_id,),
        ).fetchone()
    assert row is not None
    assert row[:2] == ("PENDING", 1)
    assert "stream disconnected" in str(row[2])
    assert row[3:] == (None, None)

    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    completed_base = next(
        job for job in store.list_run_jobs(run_id) if job.side == "BASE"
    )
    assert completed_base.status == "COMPLETED"
    assert completed_base.error is None
    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run_id).status == "FINALIZED"
    assert service.get_run(run_id).status == "COMPLETED"


def test_store_initialization_preserves_existing_evidence(
    tmp_path: Path,
) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    _, run_id = _development_run(store, tmp_path)
    worker = ScenarioWorker(store, _RetryOnceAdapter())

    assert worker.run_once(run_id=run_id).status == "RETRY_PENDING"
    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    completed_base = next(
        job for job in store.list_run_jobs(run_id) if job.side == "BASE"
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE scenario_jobs SET error = ? WHERE job_id = ?",
            ("ScenarioRetryableExecutionError: Reconnecting... 1/5", completed_base.job_id),
        )

    store.initialize()

    repaired_base = next(
        job for job in store.list_run_jobs(run_id) if job.side == "BASE"
    )
    attempts = store.list_job_attempt_evidence(completed_base.job_id)
    assert repaired_base.status == "COMPLETED"
    assert repaired_base.error == "ScenarioRetryableExecutionError: Reconnecting... 1/5"
    assert attempts[0]["disposition"] == "RETRY_PENDING"
    assert "stream disconnected" in str(attempts[0]["error"])


def test_retry_attempt_resources_are_persisted_and_separated_from_scoring_cost(
    tmp_path: Path,
) -> None:
    class RetryEvidenceAdapter(MockScenarioExecutionAdapter):
        def execute(self, job: object) -> dict[str, object]:
            attempt_number = int(getattr(job, "attempt_number"))
            side = str(getattr(job, "side"))
            if side == "BASE" and attempt_number == 1:
                raise ScenarioRetryableExecutionError(
                    "stream disconnected",
                    attempt_record={
                        "schema_version": "framework-eval-attempt-resource.v1",
                        "terminal_status": "FAILED",
                        "started_at_ms": 1_000,
                        "completed_at_ms": 7_000,
                        "duration_ms": 6_000,
                        "usage": {"total_tokens": 700},
                        "evidence_complete": True,
                        "turn_evidence": [],
                    },
                )
            outcome = super().execute(job)  # type: ignore[arg-type]
            resources = dict(outcome["resources"])  # type: ignore[arg-type]
            resources["agent_tokens"] = 500
            resources["active_execution_seconds"] = 3.0
            outcome["resources"] = resources
            outcome["execution"] = {
                "attempt_record": {
                    "schema_version": "framework-eval-attempt-resource.v1",
                    "terminal_status": "COMPLETED",
                    "started_at_ms": 10_000,
                    "completed_at_ms": 14_000,
                    "duration_ms": 4_000,
                    "usage": {"total_tokens": 300},
                    "evidence_complete": True,
                    "turn_evidence": [],
                }
            }
            return outcome

    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)
    worker = ScenarioWorker(store, RetryEvidenceAdapter())

    assert worker.run_once(run_id=run_id).status == "RETRY_PENDING"
    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run_id).status == "FINALIZED"

    report = service.get_report(run_id)
    base_outcome = next(
        item["outcome"]
        for item in report["execution_outcomes"]
        if item["side"] == "BASE"
    )
    resources = base_outcome["resources"]
    assert resources["accounting_status"] == "COMPLETE"
    assert resources["attributable"]["agent_tokens"] == 500
    assert resources["attributable"]["active_execution_seconds"] == 3.0
    assert resources["actual"] == {
        "agent_tokens": 1_000,
        "execution_seconds": 10.0,
        "attempt_elapsed_seconds": 13.0,
        "observed_attempts": 2,
        "expected_attempts": 2,
    }
    assert resources["excluded"]["non_attributable_tokens"] == 500
    attempts = store.list_job_attempt_evidence(
        next(item.job_id for item in store.list_run_jobs(run_id) if item.side == "BASE")
    )
    assert [item["disposition"] for item in attempts] == ["RETRY_PENDING", "COMPLETED"]


def test_fresh_store_has_current_usage_evidence_columns(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()

    with sqlite3.connect(store.database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        turn_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(scenario_job_turn_evidence)"
            ).fetchall()
        }

    assert "scenario_job_attempt_evidence" in tables
    assert "usage_json" in turn_columns


def test_unversioned_resource_shape_is_rejected() -> None:
    outcome = MockScenarioExecutionAdapter().execute(
        type(
            "ExampleJob",
            (),
            {
                "contract": _contract(),
                "side": "BASE",
                "run_id": "example-run",
                "case_id": "example-case",
            },
        )()
    )
    outcome["resources"] = {
        "agent_tokens": 321,
        "active_execution_seconds": 12.5,
        "workflow_rework_actions": 2,
    }

    with pytest.raises(ScenarioRuntimeError, match="attributable resources are invalid"):
        _side_evaluation(outcome, _contract())


def test_worker_pauses_after_three_transient_attempts_and_can_resume(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)
    worker = ScenarioWorker(store, _AlwaysRetryAdapter())

    assert worker.run_once(run_id=run_id).status == "RETRY_PENDING"
    assert worker.run_once(run_id=run_id).status == "RETRY_PENDING"
    assert worker.run_once(run_id=run_id).status == "PAUSED"
    assert service.get_run(run_id).status == "PAUSED"
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT status, attempt_number, error FROM scenario_jobs WHERE run_id = ? AND side = 'BASE'",
            (run_id,),
        ).fetchone()
    assert row is not None
    assert row[:2] == ("PAUSED", 3)
    assert "temporarily unavailable" in str(row[2])

    resumed = service.resume(run_id)

    assert resumed.status == "RUNNING"
    jobs = store.list_run_jobs(run_id)
    assert [(item.side, item.status, item.attempt_number) for item in jobs] == [
        ("BASE", "PENDING", 3),
        ("CANDIDATE", "PENDING", 0),
    ]
    assert jobs[0].error is None

    assert worker.run_once(run_id=run_id).status == "RETRY_PENDING"
    assert store.list_run_jobs(run_id)[0].attempt_number == 4
    assert worker.run_once(run_id=run_id).status == "RETRY_PENDING"
    assert store.list_run_jobs(run_id)[0].attempt_number == 5
    assert worker.run_once(run_id=run_id).status == "PAUSED"
    assert store.list_run_jobs(run_id)[0].attempt_number == 6

    service.resume(run_id)
    recovering_worker = ScenarioWorker(store, MockScenarioExecutionAdapter())
    assert recovering_worker.run_once(run_id=run_id).status == "PROCESSED"
    assert store.list_run_jobs(run_id)[0].attempt_number == 7
    assert recovering_worker.run_once(run_id=run_id).status == "PROCESSED"
    assert recovering_worker.run_once(run_id=run_id).status == "FINALIZED"
    assert service.get_run(run_id).status == "COMPLETED"


def test_worker_pauses_capacity_failure_and_resumes_same_job(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)
    worker = ScenarioWorker(store, _PauseOnceAdapter())

    assert worker.run_once(run_id=run_id).status == "PAUSED"
    assert service.get_run(run_id).status == "PAUSED"
    jobs = store.list_run_jobs(run_id)
    assert [(item.side, item.status, item.attempt_number) for item in jobs] == [
        ("BASE", "PAUSED", 1),
        ("CANDIDATE", "PENDING", 0),
    ]
    assert "Disk quota exceeded" in str(jobs[0].error)

    resumed = service.resume(run_id)
    assert resumed.status == "RUNNING"
    jobs = store.list_run_jobs(run_id)
    assert [(item.side, item.status, item.attempt_number) for item in jobs] == [
        ("BASE", "PENDING", 1),
        ("CANDIDATE", "PENDING", 0),
    ]
    assert jobs[0].error is None

    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    assert store.list_run_jobs(run_id)[0].attempt_number == 2
    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run_id).status == "FINALIZED"
    assert service.get_run(run_id).status == "COMPLETED"


def test_operator_suspends_queued_run_without_deleting_jobs_and_resumes_queue(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)
    jobs_before = store.list_run_jobs(run_id)

    suspended = service.suspend(run_id)

    assert suspended.status == "SUSPENDED"
    assert store.list_run_jobs(run_id) == jobs_before
    assert store.health()["queued_jobs"] == 0
    assert ScenarioWorker(store, MockScenarioExecutionAdapter()).run_once(run_id=run_id).status == "IDLE"

    resumed = service.resume(run_id)

    assert resumed.run_id == run_id
    assert resumed.status == "QUEUED"
    assert store.list_run_jobs(run_id) == jobs_before
    assert store.health()["queued_jobs"] == 2


def test_suspended_fifo_head_does_not_block_a_later_queued_run(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, first_run_id = _development_run(store, tmp_path)
    service.suspend(first_run_id)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="later-run-test",
    )
    second = service.create(request, expected_preview_hash=service.preview(request).preview_hash)

    result = ScenarioWorker(store, MockScenarioExecutionAdapter()).run_once()

    assert result.status == "PROCESSED"
    assert service.get_run(first_run_id).status == "SUSPENDED"
    assert service.get_run(second.run_id).status == "RUNNING"


def test_resume_rejects_non_paused_run(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)

    with pytest.raises(ScenarioRuntimeError, match="只能恢复暂停排队或异常暂停"):
        service.resume(run_id)


def test_concurrent_finalize_is_idempotent(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="concurrency-test",
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    worker = ScenarioWorker(store, MockScenarioExecutionAdapter())
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        calculations = tuple(executor.map(lambda _: service.finalize(run.run_id), range(2)))

    assert calculations[0].calculation_id == calculations[1].calculation_id
    assert len(store.list_run_calculations(run.run_id)) == 1


def test_two_workers_can_claim_both_sides_of_the_same_run(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = tuple(
            executor.map(
                lambda worker_id: _claim_next_job(
                    store,
                    worker_id,
                    adapter="MOCK",
                    run_id=run_id,
                ),
                ("worker-base", "worker-candidate"),
            )
        )

    assert all(job is not None for job in claimed)
    assert {job.side for job in claimed if job is not None} == {"BASE", "CANDIDATE"}
    assert len({job.job_id for job in claimed if job is not None}) == 2
    assert service.get_run(run_id).status == "RUNNING"


def test_failed_run_workspace_is_not_cleaned_while_another_side_is_running(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    _, run_id = _development_run(store, tmp_path)
    failed = _claim_next_job(store, "worker-failed", adapter="MOCK", run_id=run_id)
    running = _claim_next_job(store, "worker-running", adapter="MOCK", run_id=run_id)
    assert failed is not None
    assert running is not None
    _fail_job(store, failed, "checkpoint validation failed")
    cleaner = _RecordingCleanupAdapter()

    _cleanup_terminal_run_workspaces(store, cleaner, run_id=run_id)

    assert cleaner.cleaned == []
    with sqlite3.connect(store.database_path) as connection:
        status = connection.execute(
            "SELECT status FROM scenario_jobs WHERE job_id = ?",
            (running.job_id,),
        ).fetchone()
    assert status == ("RUNNING",)


def test_workers_only_claim_their_workload_fifo(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, formal_run_id = _development_run(store, tmp_path)
    evolution_request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="evolution",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "workload_kind": EVOLUTION_EVALUATION,
            "origin": "EVOLUTION_DISCOVERY",
            "max_concurrency": 1,
        },
    )
    evolution_run = service.create(
        evolution_request,
        expected_preview_hash=service.preview(evolution_request).preview_hash,
    )
    formal_job = _claim_next_job(
        store,
        "formal-worker",
        adapter="MOCK",
        run_id=None,
        workload_kind=FORMAL_EVALUATION,
    )
    evolution_job = _claim_next_job(
        store,
        "evolution-worker",
        adapter="MOCK",
        run_id=None,
        workload_kind=EVOLUTION_EVALUATION,
    )

    assert formal_job is not None and formal_job.run_id == formal_run_id
    assert evolution_job is not None and evolution_job.run_id == evolution_run.run_id
    assert _claim_next_job(
        store,
        "second-evolution-worker",
        adapter="MOCK",
        run_id=None,
        workload_kind=EVOLUTION_EVALUATION,
    ) is None


def test_stale_worker_cannot_fail_released_job_or_run(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="lease-test",
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    stale = _claim_next_job(store, "worker-old", adapter="MOCK", run_id=run.run_id)
    assert stale is not None
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE scenario_jobs SET lease_expires_at = ? WHERE job_id = ?",
            (expired, stale.job_id),
        )
    current = _claim_next_job(store, "worker-new", adapter="MOCK", run_id=run.run_id)
    assert current is not None and current.job_id == stale.job_id

    _fail_job(store, stale, "stale worker failure")

    assert service.get_run(run.run_id).status == "RUNNING"
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT status, worker_id, lease_token FROM scenario_jobs WHERE job_id = ?",
            (current.job_id,),
        ).fetchone()
    assert row == ("RUNNING", "worker-new", current.lease_token)


def test_preview_rejects_self_hashed_untrusted_scoring_fields(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    forged = deepcopy(_contract())
    forged["scoring_profile"]["weights"]["efficiency"] = 99  # type: ignore[index]
    forged["contract_hash"] = sha256(
        json.dumps(
            {key: value for key, value in forged.items() if key != "contract_hash"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=forged,
        created_by="forgery-test",
    )

    with pytest.raises(ValueError, match="untrusted|invalid"):
        ScenarioRunService(store).preview(request)


def test_formal_run_rejects_uncalibrated_scenario(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)

    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="FORMAL",
        execution_adapter="CODEX",
        evaluation_contract=_contract(),
        created_by="operator",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "execution_model": "codex-test",
            "judge_model": "judge-test",
            "reasoning_effort": "high",
            "judge_reasoning_effort": "high",
            "wall_timeout_seconds": 600,
            "token_budget": 100_000,
            "tool_call_budget": 1_000,
            "disk_budget_mb": 1_024,
        },
    )

    preview = service.preview(request)
    assert preview.ready is False
    assert "runtime-scenario is not a calibrated REPOSITORY_CASE" in preview.blockers
    assert "Formal Run requires the deployed HelixHarness Strategy repository" in preview.blockers


def test_delete_queued_run_cancels_jobs_without_erasing_audit_rows(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)

    service.delete(run_id)

    assert service.list_runs() == []
    assert service.get_run(run_id).status == "CANCELLED"
    with sqlite3.connect(store.database_path) as connection:
        run_row = connection.execute(
            "SELECT status, deleted_at FROM scenario_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        job_rows = connection.execute(
            "SELECT status FROM scenario_jobs WHERE run_id = ? ORDER BY side",
            (run_id,),
        ).fetchall()
    assert run_row is not None and run_row[0] == "CANCELLED" and run_row[1]
    assert job_rows == [("CANCELLED",), ("CANCELLED",)]
    assert store.health()["queued_jobs"] == 0


def test_list_runs_returns_all_visible_runs_without_default_cap(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, first_run_id = _development_run(store, tmp_path)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="list-runs-test",
    )
    preview_hash = service.preview(request).preview_hash

    for _ in range(50):
        service.create(request, expected_preview_hash=preview_hash)

    runs = service.list_runs()

    assert len(runs) == 51
    assert first_run_id in {run.run_id for run in runs}


def test_delete_running_run_is_blocked(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)
    assert _claim_next_job(store, "active-worker", adapter="MOCK", run_id=run_id) is not None

    with pytest.raises(ScenarioRuntimeError, match="执行中的评测任务不能删除"):
        service.delete(run_id)


def test_failed_run_orphan_jobs_are_not_reported_as_waiting(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    service, run_id = _development_run(store, tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE scenario_runs SET status = 'FAILED', error = 'fixture failure' WHERE run_id = ?",
            (run_id,),
        )

    health = store.health()

    assert health["queued_jobs"] == 0


def test_recalculation_reuses_frozen_contract_and_same_evidence_without_new_jobs(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=_contract(),
        created_by="recalculation-smoke",
    )
    preview = service.preview(request)
    run = service.create(request, expected_preview_hash=preview.preview_hash)
    worker = ScenarioWorker(store, MockScenarioExecutionAdapter())
    while worker.run_once(run_id=run.run_id).status not in {"FINALIZED", "IDLE"}:
        pass

    first = service.get_report(run.run_id)
    recalculated = service.recalculate(run.run_id, reason="Rebuild the deterministic aggregate")
    second = service.get_report(run.run_id)

    assert first["calculation"]["status"] == "FULLY_COMPARABLE"
    assert second["calculation"]["status"] == "FULLY_COMPARABLE"
    assert recalculated.contract_hash == _contract()["contract_hash"]
    assert len(store.list_run_calculations(run.run_id)) == 2


def test_recalculation_can_upgrade_only_scoring_profile_without_rerunning_jobs(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    store.publish(load_scenario_source(_write_scenario(tmp_path)))
    legacy_profile = replace(
        UNIFIED_SCORING_PROFILE,
        version="unified-evaluation.v6-test",
        candidate_business_regression_tolerance=0,
    )
    legacy_contract = freeze_evaluation_contract(
        base_strategy=_strategy("base"),
        candidate_strategy=_strategy("candidate"),
        scoring_profile=legacy_profile,
    )
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=("runtime-scenario",),
        run_kind="DEVELOPMENT",
        execution_adapter="MOCK",
        evaluation_contract=legacy_contract,
        created_by="score-profile-upgrade",
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    worker = ScenarioWorker(store, MockScenarioExecutionAdapter())
    while worker.run_once(run_id=run.run_id).status not in {"FINALIZED", "IDLE"}:
        pass

    with sqlite3.connect(store.database_path) as connection:
        jobs_before = connection.execute(
            "SELECT job_id, status, attempt_number, outcome_json FROM scenario_jobs WHERE run_id = ? ORDER BY sequence_number",
            (run.run_id,),
        ).fetchall()
    first = service.get_report(run.run_id)
    recalculated = service.recalculate(
        run.run_id,
        reason="Apply current scoring rules to immutable evidence",
        use_current_scoring_profile=True,
    )
    second = service.get_report(run.run_id)
    with sqlite3.connect(store.database_path) as connection:
        jobs_after = connection.execute(
            "SELECT job_id, status, attempt_number, outcome_json FROM scenario_jobs WHERE run_id = ? ORDER BY sequence_number",
            (run.run_id,),
        ).fetchall()

    assert jobs_after == jobs_before
    assert first["scoring_profile"]["version"] == "unified-evaluation.v6-test"
    assert second["scoring_profile"]["version"] == "unified-evaluation.v7"
    assert recalculated.contract_hash != legacy_contract["contract_hash"]
    assert second["calculation"]["sequence_number"] == 2
    assert len(store.list_run_calculations(run.run_id)) == 2
