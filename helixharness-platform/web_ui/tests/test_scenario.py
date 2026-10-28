from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bench.scenario_runtime import (
    ClaimedScenarioJob,
    MockScenarioExecutionAdapter,
    ScenarioPauseExecutionError,
    ScenarioRunService,
    ScenarioWorker,
)
from bench.scenario_storage import ScenarioStore
from bench.scenarios import load_scenario_source
from bench.tests.test_scenario_runtime import _contract, _write_scenario
from web_ui.main import create_app
from web_ui.routes.scenario import (
    _display_source_ref,
    _hard_gate_display_label,
    _hard_gate_facts_view,
    _pause_error_view,
    _preferred_candidate_ref,
)


class _CandidateGateAdapter(MockScenarioExecutionAdapter):
    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        outcome = super().execute(job)
        if job.side == "CANDIDATE":
            outcome["case_hard_gates"] = ["CRITICAL_EVIDENCE_MISSING", "WORKFLOW_TERMINAL_FAILURE"]
        return outcome


class _CandidateRegressionAdapter(MockScenarioExecutionAdapter):
    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        outcome = super().execute(job)
        if job.side != "CANDIDATE":
            return outcome
        outcome["outcome_quality"] = 30.0
        outcome["framework_workflow_quality"] = 30.0
        outcome["outcome_axis_scores"] = {
            axis: 30.0 for axis in dict(outcome["outcome_axis_scores"])
        }
        outcome["workflow_axis_scores"] = {
            axis: 30.0 for axis in dict(outcome["workflow_axis_scores"])
        }
        return outcome


class _PauseOnceAdapter(MockScenarioExecutionAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise ScenarioPauseExecutionError("[Errno 28] No space left on device")
        return super().execute(job)


class _TurnEvidenceAdapter(MockScenarioExecutionAdapter):
    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        outcome = super().execute(job)
        outcome["execution"] = {
            "completed_stage_ids": [],
            "terminal_reason": "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED",
            "turn_evidence": [
                {
                    "turn_sequence": 1,
                    "turn_id": f"turn-{job.side.lower()}",
                    "input_kind": "WORKFLOW_STEP_RERUN",
                    "input_text": "/helix:apply runtime-scenario",
                    "workflow_step_id": "APPLY",
                    "milestone_id": "IMPLEMENTED",
                    "classification": "FAILED",
                    "started_at_ms": 1_000,
                    "completed_at_ms": 2_000,
                    "duration_ms": 1_000,
                    "final_response": "Apply 返工后仍编译失败。",
                    "error": None,
                    "events": [],
                }
            ],
        }
        return outcome


@pytest.mark.parametrize(
    ("error", "expected_title", "expected_summary"),
    (
        (
            'ScenarioRetryableExecutionError: {"message":"Reconnecting... 1/5"}',
            "上游连接中断",
            "Codex 上游连接",
        ),
        (
            "ScenarioPauseExecutionError: [Errno 28] No space left on device",
            "执行环境容量不足",
            "磁盘空间或执行配额",
        ),
        (
            "ScenarioPauseExecutionError: recoverable worker interruption",
            "执行异常已暂停",
            "可恢复异常",
        ),
    ),
)
def test_pause_error_view_distinguishes_recovery_reason(
    error: str,
    expected_title: str,
    expected_summary: str,
) -> None:
    view = _pause_error_view(error)

    assert view["title"] == expected_title
    assert expected_summary in view["summary"]


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_helixharness_repository(root: Path) -> tuple[Path, str, str]:
    repository = root / "helixharness"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "branch", "-M", "master")
    _git(repository, "config", "user.name", "Scenario UI Test")
    _git(repository, "config", "user.email", "scenario-ui@example.test")
    (repository / ".claude-plugin").mkdir()
    (repository / ".claude-plugin/plugin.json").write_text('{"name":"helix"}\n')
    _git(repository, "add", ".claude-plugin")
    commands = repository / "commands"
    commands.mkdir()
    for name in ("init", "propose", "apply", "archive"):
        content = "propose workflow supports --loop\n" if name == "propose" else f"{name} workflow\n"
        (commands / f"{name}.md").write_text(content, encoding="utf-8")
    _git(repository, "add", "commands")
    _git(repository, "commit", "-m", "base workflow")
    base_commit = _git(repository, "rev-parse", "HEAD")
    (commands / "apply.md").write_text("candidate apply workflow\n", encoding="utf-8")
    (commands / "propose.md").write_text("candidate workflow supports --loop --candidate-only\n", encoding="utf-8")
    _git(repository, "add", "commands/apply.md", "commands/propose.md")
    _git(repository, "commit", "-m", "candidate workflow")
    candidate_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "branch", "candidate", candidate_commit)
    _git(repository, "reset", "--hard", base_commit)
    origin = root / "helixharness-origin.git"
    _git(root, "clone", "--bare", str(repository), str(origin))
    _git(repository, "remote", "add", "origin", str(origin))
    _git(repository, "fetch", "origin")
    return repository, base_commit, candidate_commit


def _contract_with_source_refs() -> dict[str, object]:
    contract = _contract()
    strategies = contract["strategies"]
    assert isinstance(strategies, dict)
    for side, source_ref in (("BASE", "master"), ("CANDIDATE", "feature/task-list")):
        strategy = strategies[side]
        assert isinstance(strategy, dict)
        adapter = strategy["adapter"]
        assert isinstance(adapter, dict)
        adapter["source_ref"] = source_ref
    contract_without_hash = dict(contract)
    contract_without_hash.pop("contract_hash")
    serialized = json.dumps(
        contract_without_hash,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    contract["contract_hash"] = sha256(serialized.encode("utf-8")).hexdigest()
    return contract


def test_internal_candidate_ref_displays_the_readable_branch_name() -> None:
    commit = "a" * 40

    assert _preferred_candidate_ref(
        ["refs/helixharness/evolution/codex/multi-host-architecture"]
    ) == "codex/multi-host-architecture"
    assert _display_source_ref(
        "refs/helixharness/evolution/manual-integrity-refresh-1e739b9",
        commit,
        side="CANDIDATE",
        candidate_refs={commit: "codex/multi-host-architecture"},
    ) == "codex/multi-host-architecture"


@pytest.fixture
def v2_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, object]:
    repository, _, _ = _write_helixharness_repository(tmp_path)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH", str(tmp_path / "evaluation-v1.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH", str(tmp_path / "evaluation.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_HELIXHARNESS_REPOSITORY", str(repository))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(tmp_path / "platform-config.json"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVALUATION_CODEX_MODEL", "test-codex-model")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_TELEMETRY_DB_PATH", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH", str(tmp_path / "stage-supplements.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_DB_PATH", str(tmp_path / "evolution.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_SCHEDULER_ENABLED", "false")
    monkeypatch.delenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_LEGACY_WRITE_ENABLED", raising=False)
    app = create_app()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    app.state.scenario_store.publish(load_scenario_source(_write_scenario(source_root)))
    return TestClient(app), app


def _record_successful_formal_side(store: ScenarioStore, *, case_id: str, source_hash: str) -> None:
    outcome = {
        "schema_version": "framework-eval-execution-outcome.v2",
        "terminal_status": "COMPLETED",
        "case_hard_gates": [],
        "global_hard_gates": [],
        "reached_comparable_business_terminal": True,
        "assessment_compatible": True,
        "verification": {
            "status": "PASS",
            "checks": [
                {"check_id": "F2P-1", "kind": "FAIL_TO_PASS", "status": "PASS"},
                {"check_id": "P2P-1", "kind": "PASS_TO_PASS", "status": "PASS"},
            ],
        },
    }
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            INSERT INTO scenario_runs(
                run_id, status, run_kind, execution_adapter, contract_json, contract_hash,
                preview_hash, created_by, created_at, completed_at, current_calculation_id
            ) VALUES ('formal-success', 'COMPLETED', 'FORMAL', 'CODEX', '{}', 'contract',
                      'preview', 'test', '2026-08-31T00:00:00+00:00',
                      '2026-08-31T00:01:00+00:00', 'calculation-success')
            """
        )
        connection.execute(
            """
            INSERT INTO scenario_run_cases(run_id, case_id, sequence_number, source_hash, snapshot_json)
            VALUES ('formal-success', ?, 1, ?, '{}')
            """,
            (case_id, source_hash),
        )
        connection.execute(
            """
            INSERT INTO scenario_jobs(
                job_id, run_id, case_id, side, sequence_number, status, outcome_json, completed_at
            ) VALUES ('formal-success-base', 'formal-success', ?, 'BASE', 1, 'COMPLETED', ?,
                      '2026-08-31T00:01:00+00:00')
            """,
            (case_id, json.dumps(outcome)),
        )
def _record_successful_base_only_formal_side(
    store: ScenarioStore,
    *,
    case_id: str,
    source_hash: str,
) -> None:
    outcome = {
        "schema_version": "framework-eval-execution-outcome.v2",
        "terminal_status": "COMPLETED",
        "case_hard_gates": [],
        "global_hard_gates": [],
        "reached_comparable_business_terminal": True,
        "assessment_compatible": True,
        "verification": {
            "status": "PASS",
            "checks": [
                {"check_id": "F2P-1", "kind": "FAIL_TO_PASS", "status": "PASS"},
                {"check_id": "P2P-1", "kind": "PASS_TO_PASS", "status": "PASS"},
            ],
        },
    }
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            INSERT INTO scenario_runs(
                run_id, status, run_kind, execution_adapter, contract_json, contract_hash,
                runtime_policy_json, preview_hash, created_by, created_at, completed_at
            ) VALUES ('base-only-success', 'COMPLETED', 'FORMAL', 'CODEX', ?, 'contract',
                      ?, 'preview', 'test',
                      '2026-08-31T00:00:00+00:00', '2026-08-31T00:01:00+00:00')
            """,
            (json.dumps(_contract()), json.dumps({"execution_scope": "BASE_ONLY"})),
        )
        connection.execute(
            """
            INSERT INTO scenario_run_cases(run_id, case_id, sequence_number, source_hash, snapshot_json)
            VALUES ('base-only-success', ?, 1, ?, '{}')
            """,
            (case_id, source_hash),
        )
        connection.execute(
            """
            INSERT INTO scenario_jobs(
                job_id, run_id, case_id, side, sequence_number, status, outcome_json, completed_at
            ) VALUES ('base-only-success-base', 'base-only-success', ?, 'BASE', 1, 'COMPLETED', ?,
                      '2026-08-31T00:01:00+00:00')
            """,
            (case_id, json.dumps(outcome)),
        )


@pytest.mark.parametrize(("workload", "label"), [
    ("FORMAL_EVALUATION", "开发验证"),
    ("EVOLUTION_EVALUATION", "自进化评测"),
])
def test_run_detail_identifies_its_worker_queue(v2_client, workload, label):
    client, _ = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"], "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK", "evaluation_contract": _contract(),
        "runtime_policy": {"schema_version": "framework-eval-runtime-policy.v2", "workload_kind": workload},
        "created_by": "synthetic-queue-label",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload)
    assert preview.status_code == 200
    payload["expected_preview_hash"] = preview.json()["preview_hash"]
    created = client.post("/framework-evaluation/api/runs", json=payload)
    assert created.status_code == 201
    detail = client.get(f"/framework-evaluation/runs/{created.json()['run_id']}/view")
    assert detail.status_code == 200
    assert f'{label} · 统一评分体系' in detail.text


def test_v2_http_create_worker_report_and_recalculate(v2_client: tuple[TestClient, object]) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract(),
        "created_by": "server-smoke",
    }

    overview = client.get("/framework-evaluation")
    health = client.get("/framework-evaluation/api/health")
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload)
    assert overview.status_code == 200
    assert "评测概览" in overview.text
    assert health.json() == {
        "status": "ok",
        "database_integrity": "ok",
        "published_scenarios": 1,
        "queued_jobs": 0,
        "running_jobs": 0,
    }
    assert preview.status_code == 200
    assert preview.json()["ready"] is True

    payload["expected_preview_hash"] = preview.json()["preview_hash"]
    created = client.post("/framework-evaluation/api/runs", json=payload)
    assert created.status_code == 201
    run_id = created.json()["run_id"]

    run_list = client.get("/framework-evaluation/runs")
    assert run_list.status_code == 200
    assert 'data-v2-run-list' in run_list.text
    assert run_id in run_list.text
    assert 'data-run-ledger-header="true"' in run_list.text
    assert 'data-run-task-info="true">1 个 Case</div>' in run_list.text
    assert "创建时间" in run_list.text
    assert 'data-run-created-at="true"' in run_list.text
    assert 'data-evaluation-status="true"' in run_list.text
    assert "评测状态" in run_list.text

    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")
    assert detail.status_code == 200
    assert 'data-new-run-detail="true"' in detail.text
    assert f'id="v2-run-detail-{run_id}"' in detail.text
    assert 'data-poll-state-key="workflow:candidate"' in detail.text
    assert 'data-poll-state-key="workflow:base"' in detail.text
    assert "stablePollDetailStates" in detail.text
    assert "details[data-poll-state-key]" in detail.text
    assert 'data-task-content-layout="horizontal"' in detail.text
    assert 'data-frozen-model="execution"' in detail.text
    assert 'data-frozen-model="judge"' in detail.text
    assert 'data-workflow-comparison="base-left-candidate-right"' in detail.text
    assert detail.text.index('data-task-content-layout="horizontal"') < detail.text.index(
        'data-workflow-comparison="base-left-candidate-right"'
    )
    assert detail.text.index('data-workflow-side="BASE"') < detail.text.index(
        'data-workflow-side="CANDIDATE"'
    )
    assert 'class="grid gap-px bg-slate-200 lg:grid-cols-2"' in detail.text
    assert 'data-pending-execution-table="full-width"' in detail.text
    assert 'style="width:100%;table-layout:fixed"' in detail.text
    assert "统一评分体系" in detail.text
    assert 'hx-trigger="every 5s"' in detail.text

    pending_report = client.get(f"/framework-evaluation/api/runs/{run_id}/report")
    assert pending_report.status_code == 409
    worker = ScenarioWorker(app.state.scenario_store, MockScenarioExecutionAdapter())
    while worker.run_once(run_id=run_id).status not in {"FINALIZED", "IDLE"}:
        pass

    report = client.get(f"/framework-evaluation/api/runs/{run_id}/report")
    recalculated = client.post(
        f"/framework-evaluation/api/runs/{run_id}/recalculate",
        json={"reason": "HTTP recalculation smoke"},
    )
    assert report.status_code == 200
    assert report.json()["calculation"]["status"] == "FULLY_COMPARABLE"
    base_resources = next(
        item["outcome"]["resources"]
        for item in report.json()["execution_outcomes"]
        if item["side"] == "BASE"
    )
    assert base_resources["schema_version"] == "framework-eval-resource-observation.v2"
    assert base_resources["accounting_status"] == "COMPLETE"
    assert base_resources["attributable"]["agent_tokens"] == 1_000
    assert base_resources["actual"]["agent_tokens"] == 1_000
    assert recalculated.status_code == 201
    assert recalculated.json()["sequence_number"] == 2

    completed_detail = client.get(f"/framework-evaluation/runs/{run_id}/view")
    assert "建议采用候选版本" in completed_detail.text
    assert "业务结果质量" in completed_detail.text
    assert "框架工作流质量" in completed_detail.text
    assert "执行效率" in completed_detail.text
    assert "执行效率 · B / C" in completed_detail.text
    case_result = report.json()["calculation"]["cases"][0]
    assert 'data-overall-score-priority="true"' in completed_detail.text
    assert 'data-overall-score="BASE"' in completed_detail.text
    assert 'data-overall-score="CANDIDATE"' in completed_detail.text
    assert 'data-overall-component-scores="true"' in completed_detail.text
    assert 'data-case-comparison-layout="cards"' in completed_detail.text
    assert 'data-case-total-score-row="true"' in completed_detail.text
    assert 'data-case-component-score-row="true"' in completed_detail.text
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in completed_detail.text
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in completed_detail.text
    assert 'data-case-efficiency="true"' in completed_detail.text
    assert 'data-efficiency-score-basis="true"' in completed_detail.text
    assert 'data-efficiency-metrics="true"' in completed_detail.text
    assert 'data-efficiency-metric="agent_tokens"' in completed_detail.text
    assert 'data-efficiency-metric="active_execution_seconds"' in completed_detail.text
    assert 'data-efficiency-metric="workflow_rework_actions"' in completed_detail.text
    assert 'data-efficiency-summary="compact"' in completed_detail.text
    assert 'data-efficiency-formula="true"' not in completed_detail.text
    assert 'data-resource-accounting="true"' not in completed_detail.text
    assert "0.0 M Token" in completed_detail.text
    assert "1.7 分钟" in completed_detail.text
    assert "1.3 分钟" in completed_detail.text
    assert "100 秒" not in completed_detail.text
    assert "Agent Token · 50%" in completed_detail.text
    assert "有效执行时间 · 40%" in completed_detail.text
    assert "工作流返工 · 10%" in completed_detail.text
    assert str(case_result["base"]["efficiency"]) in completed_detail.text
    assert str(case_result["candidate"]["efficiency"]) in completed_detail.text
    assert completed_detail.text.index('data-side-score-details="CANDIDATE"') < completed_detail.text.index(
        'data-efficiency-score-basis="true"'
    )
    assert 'colspan="7"' not in completed_detail.text
    assert "逐 Case 执行对比" not in completed_detail.text
    assert 'data-paired-case-jobs="true"' not in completed_detail.text
    assert 'data-execution-summary="BASE">已完成 · 1 次尝试' in completed_detail.text
    assert 'data-execution-summary="CANDIDATE">已完成 · 1 次尝试' in completed_detail.text
    assert 'data-case-score-details="runtime-scenario"' in completed_detail.text
    assert "评分与 Judge 判定依据" in completed_detail.text
    assert "本次没有可展示的 Judge 文字分析" in completed_detail.text
    assert "Requirement 满足度" in completed_detail.text
    assert 'data-score-axis="requirement_fulfillment"' in completed_detail.text
    assert "该历史结果只保留了单项分数" in completed_detail.text
    assert completed_detail.text.index('data-detail-progress="') < completed_detail.text.index(
        'data-trusted-evaluation-result="true"'
    )
    assert completed_detail.text.index('data-overall-score-priority="true"') < completed_detail.text.index(
        'data-overall-component-scores="true"'
    )
    assert completed_detail.text.index('data-overall-component-scores="true"') < completed_detail.text.index(
        'data-case-comparison-layout="cards"'
    )
    assert completed_detail.text.index('data-case-comparison-layout="cards"') < completed_detail.text.index(
        'data-task-content-layout="horizontal"'
    )
    assert completed_detail.text.index('data-task-content-layout="horizontal"') < completed_detail.text.index(
        'data-workflow-comparison="base-left-candidate-right"'
    )
    completed_list = client.get("/framework-evaluation/runs")
    assert 'data-run-score-summary="split"' in completed_list.text
    assert 'data-comparison-coverage="FULLY_COMPARABLE">全部 Case 可对比' in completed_list.text
    assert 'data-run-unified-delta="true"' in completed_list.text
    assert 'data-comparison-warning="FULLY_COMPARABLE"' not in completed_list.text


def test_base_only_formal_validation_remains_visible_after_web_source_sync(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    scenario = app.state.scenario_store.get("runtime-scenario")
    assert scenario is not None
    with sqlite3.connect(app.state.scenario_store.database_path) as connection:
        connection.execute(
            "UPDATE scenarios SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
    _record_successful_base_only_formal_side(
        app.state.scenario_store,
        case_id=scenario.case_id,
        source_hash=scenario.source_hash,
    )

    cases = client.get("/framework-evaluation/cases")
    detail = client.get("/framework-evaluation/runs/base-only-success/view")
    runs = client.get("/framework-evaluation/runs", params={"task_type": "BASE_ONLY"})

    assert cases.status_code == 200
    assert detail.status_code == 200
    assert runs.status_code == 200
    assert 'data-formal-run-verified="true"' in cases.text
    assert 'data-base-verification-completed="true"' in runs.text
    assert 'data-workflow-side="BASE"' in detail.text
    assert 'data-workflow-side="CANDIDATE"' not in detail.text
    assert 'data-execution-summary="BASE"' in detail.text
    assert 'data-execution-summary="CANDIDATE"' not in detail.text


def test_partial_comparison_uses_plain_visible_warning_badge(
    v2_client: tuple[TestClient, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract(),
        "created_by": "partial-comparison-display",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]
    worker = ScenarioWorker(app.state.scenario_store, MockScenarioExecutionAdapter())
    while worker.run_once(run_id=run_id).status not in {"FINALIZED", "IDLE"}:
        pass
    partial_report = client.get(f"/framework-evaluation/api/runs/{run_id}/report").json()
    partial_report["calculation"]["status"] = "PARTIALLY_COMPARABLE"
    partial_report["calculation"]["comparable_case_count"] = 0
    partial_report["calculation"]["cases"][0]["incompatibility_reasons"] = [
        "CANDIDATE: BUSINESS_GATE_NOT_PASSED",
        "CANDIDATE: WORKFLOW_ARTIFACT_EVIDENCE_UNAVAILABLE",
    ]
    candidate_outcome = next(
        item["outcome"]
        for item in partial_report["execution_outcomes"]
        if item["side"] == "CANDIDATE"
    )
    candidate_outcome["judge"] = {
        "workflow": {
            "axis_scores": [
                {
                    "axis": "goal_requirement_decomposition",
                    "score": 80,
                    "rationale": "需求拆解完整，但缺少可复验的业务结果。",
                    "deductions": ["业务结果证据不足"],
                    "limitations": [],
                    "evidence_ids": ["artifact-1"],
                }
            ]
        }
    }
    monkeypatch.setattr(
        ScenarioRunService,
        "get_report",
        lambda self, requested_run_id, calculation_id=None: partial_report,
    )

    run_list = client.get("/framework-evaluation/runs")
    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")

    assert 'data-comparison-warning="PARTIALLY_COMPARABLE"' in run_list.text
    assert "部分 Case 无法有效对比" in run_list.text
    assert 'data-run-score-summary="split"' in run_list.text
    assert 'data-run-unified-delta="true"' in run_list.text
    assert "全部 Case 可对比" not in run_list.text
    assert 'data-comparison-warning="PARTIALLY_COMPARABLE"' in detail.text
    assert 'data-incompatibility-summary="true"' in detail.text
    assert "暂不计入综合比较" in detail.text
    assert "Candidate：业务校验未通过，旧版结果未形成业务分数" not in detail.text
    assert "Candidate：缺少可评分的工作流产物证据" not in detail.text
    assert 'data-diagnostic-code="CANDIDATE: BUSINESS_GATE_NOT_PASSED"' not in detail.text
    assert "默认折叠 · 展开查看 AI 评分理由、扣分原因与证据" in detail.text
    assert "需求拆解完整，但缺少可复验的业务结果。" in detail.text


def test_run_detail_exposes_persisted_bounded_turn_diagnostics(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract(),
        "created_by": "turn-diagnostics",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]
    worker = ScenarioWorker(app.state.scenario_store, _TurnEvidenceAdapter())
    while worker.run_once(run_id=run_id).status not in {"FINALIZED", "IDLE"}:
        pass

    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")

    assert detail.status_code == 200
    assert 'data-job-diagnostics="BASE"' in detail.text
    assert 'data-job-diagnostics="CANDIDATE"' in detail.text
    assert 'data-poll-state-key="job-diagnostics:runtime-scenario:BASE"' in detail.text
    assert 'data-poll-state-key="job-diagnostics:runtime-scenario:CANDIDATE"' in detail.text
    assert 'data-terminal-reason="STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"' in detail.text
    assert 'data-turn-evidence="1"' in detail.text
    assert "Apply 返工后仍编译失败。" in detail.text


def test_task_list_is_branch_first_filterable_and_links_to_detailed_progress(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract_with_source_refs(),
        "created_by": "branch-operator",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]

    matched = client.get("/framework-evaluation/runs", params={"branch": "task-list"})
    missing = client.get("/framework-evaluation/runs", params={"branch": "does-not-exist"})

    assert matched.status_code == 200
    assert run_id in matched.text
    assert 'data-primary-branch="true">feature/task-list</p>' in matched.text
    assert 'data-candidate-ref="feature/task-list"' in matched.text
    assert 'data-run-actions="true"' in matched.text
    assert f'action="/framework-evaluation/runs/{run_id}/delete"' in matched.text
    assert 'data-stable-poll="true"' in matched.text
    assert f'data-task-id="{run_id}">任务 ID · {run_id}</p>' in matched.text
    assert 'data-run-task-info="true">1 个 Case</div>' in matched.text
    assert "执行进度</span>" not in matched.text
    assert "等待评测结果" in matched.text
    assert '@keyframes evaluation-run-spin' in matched.text
    assert 'data-live-observer-spinner="true"' in matched.text
    assert 'data-run-state-spinner="queued"' in matched.text
    assert (
        'data-evaluation-status-heading="true">'
        '<span>等待评测结果</span>'
        '<i class="evaluation-run-spinner'
    ) in matched.text
    assert 'data-run-live-status="true">排队中</span>' in matched.text
    assert run_id not in missing.text

    worker = ScenarioWorker(app.state.scenario_store, MockScenarioExecutionAdapter())
    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    running = client.get("/framework-evaluation/runs", params={"branch": "task-list"})
    assert 'data-run-state-spinner="running"' in running.text
    assert 'data-run-live-status="true">执行中</span>' in running.text
    while worker.run_once(run_id=run_id).status not in {"FINALIZED", "IDLE"}:
        pass

    completed = client.get(
        "/framework-evaluation/runs",
        params={"branch": "feature/task-list", "recommendation": "RECOMMEND"},
    )
    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")

    assert run_id in completed.text
    assert "建议采用候选版本" in completed.text
    assert 'data-live-observer-spinner="true"' not in completed.text
    assert 'data-run-state-spinner="completed"' not in completed.text
    assert "feature/task-list" in detail.text
    assert "master" in detail.text
    assert 'data-case-comparison-layout="cards"' in detail.text
    assert 'data-case-total-score-row="true"' in detail.text
    assert 'data-case-component-score-row="true"' in detail.text
    assert "runtime-scenario" in detail.text


def test_cancelled_task_list_shows_terminal_state_without_spinner(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract_with_source_refs(),
        "created_by": "cancelled-list-state",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]
    with sqlite3.connect(app.state.scenario_store.database_path) as connection:
        connection.execute(
            "UPDATE scenario_jobs SET status = 'CANCELLED' WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            "UPDATE scenario_runs SET status = 'CANCELLED', error = ? WHERE run_id = ?",
            ("任务已按用户要求取消", run_id),
        )

    page = client.get("/framework-evaluation/runs", params={"branch": "task-list"})
    row = re.search(
        rf'<article[^>]+data-run-row="{run_id}".*?</article>',
        page.text,
        re.DOTALL,
    )

    assert row is not None
    assert "任务已取消" in row.group(0)
    assert "任务已按用户要求取消" in row.group(0)
    assert 'data-run-state-spinner="cancelled"' not in row.group(0)
    assert 'data-run-live-status="true">排队中</span>' not in row.group(0)


def test_task_list_exposes_rejected_candidate_remediation_state_before_linking(
    v2_client: tuple[TestClient, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract_with_source_refs(),
        "created_by": "rejection-operator",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]

    worker = ScenarioWorker(app.state.scenario_store, _CandidateRegressionAdapter())
    while worker.run_once(run_id=run_id).status not in {"FINALIZED", "IDLE"}:
        pass

    pending = client.get("/framework-evaluation/runs")

    assert "不建议采用候选版本" in pending.text
    assert 'data-run-remediation-status="PENDING"' in pending.text
    assert "正在检查改进机会" in pending.text
    assert f'data-run-evolution-link="{run_id}"' not in pending.text
    assert 'hx-trigger="every 5s"' in pending.text

    monkeypatch.setattr(
        app.state.scenario_store,
        "get_remediation_projection",
        lambda requested_run_id: {
            "run_id": requested_run_id,
            "status": "NOT_ELIGIBLE",
            "reason": "没有 Candidate 独有且证据充分的可修复问题",
            "hypothesis_id": None,
            "updated_at": "2026-08-31T00:00:00Z",
        },
    )
    ineligible = client.get("/framework-evaluation/runs")

    assert 'data-run-remediation-status="NOT_ELIGIBLE"' not in ineligible.text
    assert "无可修复假设" not in ineligible.text
    assert "没有 Candidate 独有且证据充分的可修复问题" not in ineligible.text
    assert f'data-run-evolution-link="{run_id}"' not in ineligible.text

    monkeypatch.setattr(
        app.state.scenario_store,
        "get_remediation_projection",
        lambda requested_run_id: {
            "run_id": requested_run_id,
            "status": "READY",
            "reason": "具体修复假设已生成",
            "hypothesis_id": "hyp_bench_ready",
            "updated_at": "2026-08-31T00:00:01Z",
        },
    )
    ready = client.get("/framework-evaluation/runs")

    assert f'data-run-evolution-link="{run_id}"' in ready.text
    assert 'data-run-remediation-status="READY"' in ready.text
    assert f'href="/evolution/hypotheses?source=BENCH&amp;source_run_id={run_id}"' in ready.text
    assert "查看进化假设" in ready.text


def test_task_delete_hides_queued_run_and_rejects_wrong_confirmation(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract_with_source_refs(),
        "created_by": "delete-operator",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]

    mismatch = client.post(
        f"/framework-evaluation/runs/{run_id}/delete",
        data={"confirmation": "wrong-run"},
        follow_redirects=False,
    )
    deleted = client.post(
        f"/framework-evaluation/runs/{run_id}/delete",
        data={"confirmation": run_id},
        follow_redirects=False,
    )

    assert mismatch.status_code == 400
    assert deleted.status_code == 303
    assert run_id not in client.get("/framework-evaluation/runs").text
    assert app.state.scenario_store.health()["queued_jobs"] == 0


def test_queued_task_can_suspend_without_deletion_and_resume_same_run(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract_with_source_refs(),
        "created_by": "queue-suspend-operator",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]

    queued = client.get("/framework-evaluation/runs")
    assert f'action="/framework-evaluation/runs/{run_id}/suspend"' in queued.text
    assert "暂停排队" in queued.text

    suspended = client.post(
        f"/framework-evaluation/runs/{run_id}/suspend",
        follow_redirects=False,
    )

    assert suspended.status_code == 303
    listing = client.get("/framework-evaluation/runs")
    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")
    assert run_id in listing.text
    assert "已暂停排队" in listing.text
    assert "任务和 Case 已保留" in listing.text
    assert f'action="/framework-evaluation/runs/{run_id}/resume"' in listing.text
    assert "恢复排队" in listing.text
    assert "任务 ID、冻结配置和全部 Case 均已保留" in detail.text
    assert app.state.scenario_store.health()["queued_jobs"] == 0
    assert ScenarioWorker(
        app.state.scenario_store,
        MockScenarioExecutionAdapter(),
    ).run_once(run_id=run_id).status == "IDLE"

    resumed = client.post(
        f"/framework-evaluation/runs/{run_id}/resume",
        follow_redirects=False,
    )

    assert resumed.status_code == 303
    assert resumed.headers["location"] == "/framework-evaluation/runs"
    current = client.get(f"/framework-evaluation/api/runs/{run_id}")
    assert current.json()["run_id"] == run_id
    assert current.json()["status"] == "QUEUED"
    assert app.state.scenario_store.health()["queued_jobs"] == 2

    api_suspended = client.post(f"/framework-evaluation/api/runs/{run_id}/suspend")
    api_resumed = client.post(f"/framework-evaluation/api/runs/{run_id}/resume")
    assert api_suspended.json()["status"] == "SUSPENDED"
    assert api_resumed.json()["status"] == "QUEUED"


def test_paused_run_can_resume_from_detail_and_api(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract_with_source_refs(),
        "created_by": "resume-operator",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]
    worker = ScenarioWorker(app.state.scenario_store, _PauseOnceAdapter())

    assert worker.run_once(run_id=run_id).status == "PAUSED"
    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")
    listing = client.get("/framework-evaluation/runs")
    assert "已暂停，可重试" in detail.text
    assert f'action="/framework-evaluation/runs/{run_id}/resume"' in detail.text
    assert "从断点重试" in detail.text
    assert "优先从最近有效 checkpoint 继续" in detail.text
    assert "checkpoint 不存在时，会从失败侧起点重新执行" in detail.text
    assert "执行环境容量不足" in listing.text
    assert "处理容量异常后可从检查点恢复" not in listing.text

    resumed = client.post(f"/framework-evaluation/api/runs/{run_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "RUNNING"
    duplicate = client.post(f"/framework-evaluation/api/runs/{run_id}/resume")
    assert duplicate.status_code == 409

    assert worker.run_once(run_id=run_id).status == "PROCESSED"
    assert app.state.scenario_store.list_run_jobs(run_id)[0].attempt_number == 2


def test_run_detail_defaults_workflows_open_and_tracks_completed_stages(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    contract = _contract_with_source_refs()
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": contract,
        "created_by": "workflow-observer",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]
    candidate_stages = contract["strategies"]["CANDIDATE"]["stages"]
    assert candidate_stages
    candidate_job = next(
        item for item in app.state.scenario_store.list_run_jobs(run_id) if item.side == "CANDIDATE"
    )
    lease_token = "workflow-progress-test"
    with sqlite3.connect(app.state.scenario_store.database_path) as connection:
        connection.execute(
            "UPDATE scenario_runs SET status = 'RUNNING' WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            """
            UPDATE scenario_jobs
            SET status = 'RUNNING', lease_token = ?, attempt_number = 1
            WHERE job_id = ?
            """,
            (lease_token, candidate_job.job_id),
        )
    first_stage_id = candidate_stages[0]["stage_id"]
    running_detail = client.get(f"/framework-evaluation/runs/{run_id}/view")
    app.state.scenario_store.record_job_workflow_progress(
        candidate_job.job_id,
        lease_token,
        (first_stage_id,),
    )

    completed_detail = client.get(f"/framework-evaluation/runs/{run_id}/view")

    assert running_detail.status_code == 200
    assert '<details open class="mt-3' in running_detail.text
    assert f'data-workflow-stage="{first_stage_id}" data-stage-status="RUNNING"' in running_detail.text
    assert f'data-workflow-stage="{first_stage_id}" data-stage-status="COMPLETED"' in completed_detail.text
    assert "阶段产物从双方各自的 completed feature 目录递归收集" in completed_detail.text


def test_chinese_creation_flow(v2_client: tuple[TestClient, object]) -> None:
    client, _ = v2_client

    creation = client.get("/framework-evaluation/runs/new")

    assert creation.status_code == 200
    assert 'data-evaluation-wizard="true"' in creation.text
    assert 'href="/framework-evaluation"' in creation.text
    assert 'href="/framework-evaluation/cases"' in creation.text
    assert "评测Case" in creation.text
    assert re.search(
        r'data-sidebar-path="/framework-evaluation/cases"[^>]*>\s*评测Case\s*</a>',
        creation.text,
    )
    assert "平台管理" not in creation.text
    assert "当前评测资源占用" in creation.text
    assert 'data-resource-pools="workload-v2"' in creation.text
    assert 'data-resource-lane="FORMAL_EVALUATION"' in creation.text
    assert 'data-resource-lane="EVOLUTION_EVALUATION"' in creation.text
    assert 'data-resource-lane="EVOLUTION_DISCOVERY"' not in creation.text
    assert 'data-resource-lane="EVOLUTION_REMEDIATION_EVALUATION"' not in creation.text
    assert creation.text.index("评测规则") < creation.text.index("评测配置")
    assert "所有工作流统一使用同一套证据与评分规则" in creation.text
    assert "同体系工作流比较" not in creation.text
    assert "跨体系能力比较" not in creation.text
    assert "标准模式" not in creation.text
    assert "精简模式" not in creation.text
    assert "基准工作流命令" in creation.text
    assert "候选工作流命令" in creation.text
    assert "每行一条完整命令，最多 20 条" in creation.text
    assert "每个任务最多选择 10 个 Case" in creation.text
    assert "一个评测任务最多选择 ${maxScenarioSelection} 个 Case" in creation.text
    assert "后续命令可按该版本规则省略 feature" in creation.text
    assert 'data-command-workspace-help="true"' in creation.text
    assert "每个 Case 都从隔离后的业务代码库根目录执行" in creation.text
    assert ".helixharness_case/initial-request.md" in creation.text
    assert "propose --loop" not in creation.text
    assert 'name="base_workflow_commands"' in creation.text
    assert 'name="candidate_workflow_commands"' in creation.text
    assert 'data-copy-workflow' in creation.text
    assert "基准版本" in creation.text
    assert "候选版本" in creation.text
    assert "test-codex-model" in creation.text
    assert "工作流：<code>test-codex-model</code> · low" in creation.text
    assert "裁判：<code>test-codex-model</code> · low" in creation.text
    assert "工作流修改" not in creation.text
    assert "工作流重构" not in creation.text
    assert "/framework-evaluation/api/runs/draft" in creation.text
    assert 'data-confirm-create' in creation.text
    assert 'data-artifact-sources' in creation.text
    assert '<details class="sv2c-technical" data-technical-details>' in creation.text
    assert 'data-map-native' not in creation.text
    assert "requestedRevision !== formRevision" in creation.text
    assert 'data-repository-mode-selector="true"' in creation.text
    assert 'name="repository_mode" value="SINGLE_REPOSITORY"' in creation.text
    assert 'name="repository_mode" value="MULTI_REPOSITORY"' in creation.text
    assert 'data-mode-count="SINGLE_REPOSITORY">1 个可用</strong>' in creation.text
    assert 'data-mode-count="MULTI_REPOSITORY">0 个可用</strong>' in creation.text
    assert 'data-visible-scenario-count>0</b>' in creation.text
    assert "visibleCount.textContent = currentVisibleCount" in creation.text
    assert "repositoryMode ? '没有匹配的 Case' : '请选择 Case 类型'" in creation.text
    assert 'data-scenario-filters="difficulty"' in creation.text
    assert 'data-scenario-filters="readiness"' not in creation.text
    assert 'data-filter-value="HARD">困难' in creation.text
    assert 'data-scenario-difficulty="UNASSESSED"' in creation.text
    assert "难度 · 待评估" in creation.text
    assert 'href="/framework-evaluation/cases/runtime-scenario">查看详情 →</a>' in creation.text


def test_case_library_uses_current_scenarios_with_readable_details(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    scenario = app.state.scenario_store.get("runtime-scenario")
    assert scenario is not None
    with sqlite3.connect(app.state.scenario_store.database_path) as connection:
        connection.execute(
            "UPDATE scenarios SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
        connection.execute(
            "UPDATE scenario_versions SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
    app.state.scenario_store.record_calibration(
        scenario.case_id,
        scenario.source_hash,
        state="CALIBRATED",
    )
    _record_successful_formal_side(
        app.state.scenario_store,
        case_id=scenario.case_id,
        source_hash=scenario.source_hash,
    )
    cases = client.get("/framework-evaluation/cases")
    detail = client.get("/framework-evaluation/cases/runtime-scenario")
    history = client.get("/framework-evaluation/legacy/cases")
    history_detail = client.get("/framework-evaluation/legacy/cases/dev-chain-behavior-minimal")
    blocked_edit = client.get("/framework-evaluation/cases/runtime-scenario/edit")
    blocked_publish = client.post("/framework-evaluation/cases/runtime-scenario/publish")
    preselected = client.get(
        "/framework-evaluation/runs/new",
        params={"scenario_id": "runtime-scenario"},
    )
    app.state.scenario_store.record_calibration(
        scenario.case_id,
        scenario.source_hash,
        state="FAILED",
    )
    failed_calibration = client.get("/framework-evaluation/cases")

    assert cases.status_code == 200
    assert 'data-scenario-case-library="true"' in cases.text
    assert "runtime-scenario" in cases.text
    assert "场景类型" not in cases.text
    assert "开发验证场景" not in cases.text
    assert "Create result.txt with complete." not in cases.text
    assert 'href="/framework-evaluation/legacy/cases"' not in cases.text
    assert "历史 Case" not in cases.text
    assert 'href="/framework-evaluation/cases/runtime-scenario"' in cases.text
    assert ">详情</a>" in cases.text
    assert 'action="/framework-evaluation/cases/runtime-scenario/delete"' in cases.text
    assert ">删除</button>" in cases.text
    assert 'data-formal-run-verified="true"' in cases.text
    assert "已验证可跑通" in cases.text
    assert "使用此 Case" not in cases.text
    assert "判定内容" not in cases.text
    assert "校准状态：" not in cases.text
    assert scenario.source_hash[:12] not in cases.text
    assert 'data-case-difficulty-column="true"' in cases.text
    assert 'data-case-difficulty-rule="business-code-delta"' in cases.text
    assert ".case-difficulty--easy" in cases.text
    assert "难度 · 待评估" not in cases.text
    assert 'href="/framework-evaluation/runs/new?scenario_id=runtime-scenario"' not in cases.text
    assert "Reference 工作流命令" not in cases.text

    assert detail.status_code == 200
    assert 'data-scenario-case-detail="true"' in detail.text
    assert "评测目标" in detail.text
    assert "执行侧看到的完整任务说明" in detail.text
    assert 'data-case-code-interval="inline"' in detail.text
    assert "可评分的验收标准" in detail.text
    assert "需求依据（来源证据）" in detail.text
    assert "业务验收与硬门禁" in detail.text
    assert "Case 使用了哪些文件" in detail.text
    assert "已知事实与缺失处理" in detail.text
    assert "待评估难度" in detail.text
    assert 'data-case-contract-map="latest-v2"' in detail.text
    assert 'data-case-requirement-map="true"' in detail.text
    assert 'data-case-verification-matrix="true"' in detail.text
    assert 'data-case-technical-appendix="true"' in detail.text
    assert ">隐藏校验</h3>" not in detail.text
    assert "来源记录只用于审计" in detail.text
    assert "SHA-256</th>" not in detail.text
    assert "契约版本</span>" not in detail.text
    assert "最新 Case v2" not in detail.text
    assert 'data-case-check-group="hidden"' not in detail.text
    assert 'data-case-check-group="execution"' in detail.text
    assert 'data-case-check-group="solution"' in detail.text
    assert "使用此 Case" not in detail.text
    assert 'data-formal-run-verified="true"' in detail.text
    assert "已验证可跑通" in detail.text

    assert history.status_code == 404
    assert history_detail.status_code == 404
    assert blocked_edit.status_code == 404
    assert blocked_publish.status_code == 404
    assert 'value="runtime-scenario" checked' in preselected.text
    assert "待确定性校准" in failed_calibration.text
    assert "校准状态：校准中" not in failed_calibration.text
    assert "最近一次校准未通过" not in failed_calibration.text
    assert '<option value="FAILED"' not in failed_calibration.text


def test_case_library_does_not_inherit_verified_label_from_an_old_source_hash(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    scenario = app.state.scenario_store.get("runtime-scenario")
    assert scenario is not None
    with sqlite3.connect(app.state.scenario_store.database_path) as connection:
        connection.execute(
            "UPDATE scenarios SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
    _record_successful_formal_side(
        app.state.scenario_store,
        case_id=scenario.case_id,
        source_hash="0" * 64,
    )

    cases = client.get("/framework-evaluation/cases")
    detail = client.get("/framework-evaluation/cases/runtime-scenario")

    assert cases.status_code == 200
    assert detail.status_code == 200
    assert 'data-formal-run-verified="true"' not in cases.text
    assert 'data-formal-run-verified="true"' not in detail.text
    assert "已验证可跑通" not in cases.text
    assert "已验证可跑通" not in detail.text


def test_case_library_accepts_successful_base_only_formal_validation(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    scenario = app.state.scenario_store.get("runtime-scenario")
    assert scenario is not None
    with sqlite3.connect(app.state.scenario_store.database_path) as connection:
        connection.execute(
            "UPDATE scenarios SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
        connection.execute(
            "UPDATE scenario_versions SET case_kind = 'REPOSITORY_CASE' WHERE case_id = ?",
            (scenario.case_id,),
        )
    app.state.scenario_store.record_calibration(
        scenario.case_id,
        scenario.source_hash,
        state="CALIBRATED",
    )
    _record_successful_base_only_formal_side(
        app.state.scenario_store,
        case_id=scenario.case_id,
        source_hash=scenario.source_hash,
    )

    cases = client.get("/framework-evaluation/cases")
    detail = client.get("/framework-evaluation/cases/runtime-scenario")
    default_runs = client.get("/framework-evaluation/runs")
    runs = client.get("/framework-evaluation/runs", params={"task_type": "BASE_ONLY"})
    all_runs = client.get("/framework-evaluation/runs", params={"task_type": "ALL"})
    run_detail = client.get("/framework-evaluation/runs/base-only-success/view")

    assert cases.status_code == 200
    assert detail.status_code == 200
    assert default_runs.status_code == 200
    assert runs.status_code == 200
    assert all_runs.status_code == 200
    assert run_detail.status_code == 200
    assert 'data-formal-run-verified="true"' in cases.text
    assert 'data-formal-run-verified="true"' in detail.text
    assert "已验证可跑通" in cases.text
    assert "已验证可跑通" in detail.text
    assert "base-only-success" not in default_runs.text
    assert 'data-run-task-type-filter="true"' in default_runs.text
    assert 'data-run-advanced-filters="true">' in default_runs.text
    assert '<option value="PAIRED" selected>对比评测</option>' in default_runs.text
    assert 'data-base-verification-completed="true"' in runs.text
    assert "Base 验证已完成" in runs.text
    assert 'data-run-advanced-filters="true" open>' in runs.text
    assert 'data-run-task-type-active="true">任务类型：Base 验证</span>' in runs.text
    assert "base-only-success" in all_runs.text
    assert 'data-workflow-side="BASE"' in run_detail.text
    assert 'data-workflow-side="CANDIDATE"' not in run_detail.text
    assert 'data-execution-summary="BASE"' in run_detail.text
    assert 'data-execution-summary="CANDIDATE"' not in run_detail.text


def test_case_delete_removes_current_case_but_keeps_immutable_version(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    scenario = app.state.scenario_store.get("runtime-scenario")
    assert scenario is not None

    mismatch = client.post(
        "/framework-evaluation/cases/runtime-scenario/delete",
        data={"confirmation": "wrong-case"},
    )
    deleted = client.post(
        "/framework-evaluation/cases/runtime-scenario/delete",
        data={"confirmation": "runtime-scenario"},
    )

    assert mismatch.status_code == 422
    assert deleted.status_code == 200
    assert "已从当前 Case 库删除" in deleted.text
    assert app.state.scenario_store.get("runtime-scenario") is None
    assert app.state.scenario_store.list_versions("runtime-scenario") == [scenario]
    assert client.get("/framework-evaluation/cases/runtime-scenario").status_code == 404


def test_case_library_is_fixed_to_repository_cases(
    v2_client: tuple[TestClient, object],
) -> None:
    client, _ = v2_client

    response = client.get(
        "/framework-evaluation/cases",
        params={"case_kind": "DEVELOPMENT_FIXTURE"},
    )

    assert response.status_code == 200, response.text
    assert "runtime-scenario" not in response.text
    assert "场景类型" not in response.text
    assert "共 0 个当前 Case" in response.text


def test_canonical_case_library_does_not_follow_legacy_write_switch(
    v2_client: tuple[TestClient, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = v2_client
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_LEGACY_WRITE_ENABLED", "true")

    response = client.get("/framework-evaluation/cases")

    assert response.status_code == 200, response.text
    assert 'data-scenario-case-library="true"' in response.text
    assert 'data-case-library="true"' not in response.text


def test_creation_draft_rejects_more_than_ten_cases(
    v2_client: tuple[TestClient, object],
) -> None:
    client, _ = v2_client

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": [f"case-{index}" for index in range(11)],
            "candidate_ref": "candidate",
            "base_workflow_commands": ["/helix:archive evaluation-scenario"],
            "candidate_workflow_commands": ["/helix:archive evaluation-scenario"],
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "一个评测任务最多选择 10 个 Case"


def test_base_only_draft_freezes_one_execution_scope(v2_client: tuple[TestClient, object]) -> None:
    client, _app = v2_client
    base_commands = [
        "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
        "/helix:apply evaluation-scenario",
        "/helix:archive evaluation-scenario",
    ]

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "base_only": True,
            "base_workflow_commands": base_commands,
            "created_by": "Base 验证",
        },
    )

    assert response.status_code == 200, response.text
    request_payload = response.json()["request"]
    assert request_payload["run_kind"] == "FORMAL"
    assert request_payload["runtime_policy"]["execution_scope"] == "BASE_ONLY"
    assert request_payload["runtime_policy"]["origin"] == "BASE_VERIFICATION"
    contract = request_payload["evaluation_contract"]
    assert contract["strategies"]["BASE"]["adapter"]["source_commit"] == contract["strategies"][
        "CANDIDATE"
    ]["adapter"]["source_commit"]
    assert contract["strategies"]["BASE"]["commands"] == base_commands
    assert contract["strategies"]["CANDIDATE"]["commands"] == base_commands


def test_creation_flow_builds_server_owned_contract_preview(v2_client: tuple[TestClient, object]) -> None:
    client, app = v2_client
    base_commands = [
        "/helix:init",
        "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
        "/helix:apply evaluation-scenario",
        "/helix:archive evaluation-scenario",
    ]
    candidate_commands = [
        "/helix:init",
        "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
        "/helix:apply evaluation-scenario",
        "/helix:archive evaluation-scenario",
    ]

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": base_commands,
            "candidate_workflow_commands": candidate_commands,
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    request_payload = payload["request"]
    contract = request_payload["evaluation_contract"]
    assert request_payload["run_kind"] == "FORMAL"
    assert request_payload["execution_adapter"] == "CODEX"
    assert request_payload["runtime_policy"]["max_business_clarifications"] == 3
    assert "采用推荐" in request_payload["runtime_policy"]["universal_user_response"]
    assert "comparison" not in contract
    assert contract["scoring_profile"]["weights"] == {
        "outcome_quality": 60,
        "framework_workflow_quality": 20,
        "efficiency": 20,
    }
    assert contract["strategies"]["BASE"]["adapter"]["source_commit"] != contract["strategies"]["CANDIDATE"]["adapter"]["source_commit"]
    assert payload["strategy_summary"]["BASE"]["version_label"] == "master"
    assert payload["strategy_summary"]["CANDIDATE"]["version_label"] == "candidate"
    assert "strategy_label" not in payload["strategy_summary"]["BASE"]
    assert contract["strategies"]["BASE"]["commands"] == base_commands
    assert contract["strategies"]["CANDIDATE"]["commands"] == candidate_commands
    assert contract["strategies"]["BASE"]["adapter"]["source_ref"] == "master"
    assert contract["strategies"]["CANDIDATE"]["adapter"]["source_ref"] == "candidate"
    base_recovery = contract["strategies"]["BASE"]["recovery_policy"]
    assert base_recovery["schema_version"] == "framework-eval-strategy-recovery.v1"
    assert base_recovery["feature"] == "evaluation-scenario"
    goal_control = base_recovery["workflow_goal_control"]
    assert goal_control["mode"] == "BOUNDED_GOAL_DIRECTED"
    assert goal_control["limits"] == {
        "max_archive_attempts": 5,
        "max_inferred_actions": 12,
        "max_stage_rework_cycles": 5,
    }
    assert set(goal_control["actions"]) == {"HANDLE_STAGE_FINDINGS", "ARCHIVE"}

    assert contract["strategies"]["CANDIDATE"]["recovery_policy"]["workflow_goal_control"]["limits"][
        "max_archive_attempts"
    ] == 5
    assert payload["strategy_summary"]["BASE"]["commands"] == base_commands
    assert payload["strategy_summary"]["CANDIDATE"]["commands"] == candidate_commands
    assert payload["artifact_summary"] == [
        {
            "side": "BASE",
            "root": "docs/specs/completed/evaluation-scenario",
            "include_glob": "**/*.md",
        },
        {
            "side": "CANDIDATE",
            "root": "docs/specs/completed/evaluation-scenario",
            "include_glob": "**/*.md",
        },
    ]
    assert payload["preview"]["ready"] is False
    assert any("calibrated REPOSITORY_CASE" in item for item in payload["preview"]["blockers"])
    assert "尚未完成真实仓库校准" in payload["blocker_labels"][0]
    assert app.state.scenario_strategy_validator is not None


def test_creation_draft_refreshes_latest_remote_master(
    v2_client: tuple[TestClient, object],
    tmp_path: Path,
) -> None:
    client, app = v2_client
    repository = app.state.scenario_strategy_validator.repository
    publisher = tmp_path / "publisher"
    _git(tmp_path, "clone", str(repository.parent / "helixharness-origin.git"), str(publisher))
    _git(publisher, "config", "user.name", "Scenario Publisher")
    _git(publisher, "config", "user.email", "publisher@example.test")
    (publisher / "commands" / "archive.md").write_text("new remote archive workflow\n", encoding="utf-8")
    _git(publisher, "add", "commands/archive.md")
    _git(publisher, "commit", "-m", "advance remote master")
    remote_master = _git(publisher, "rev-parse", "HEAD")
    _git(publisher, "push", "origin", "master")

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": ["/helix:archive evaluation-scenario"],
            "candidate_workflow_commands": ["/helix:archive evaluation-scenario"],
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text
    contract = response.json()["request"]["evaluation_contract"]
    assert contract["strategies"]["BASE"]["adapter"]["source_commit"] == remote_master
    assert _git(repository, "rev-parse", "master") != remote_master


def test_creation_flow_resolves_remote_tracking_candidate_without_local_branch(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    repository = app.state.scenario_strategy_validator.repository
    base_commit = _git(repository, "rev-parse", "master")
    candidate_commit = _git(repository, "rev-parse", "candidate")
    _git(repository, "push", "origin", "candidate:refs/heads/feature_oe_workflow")
    _git(
        repository,
        "update-ref",
        "refs/remotes/origin/feature_oe_workflow",
        base_commit,
    )
    _git(repository, "branch", "-D", "candidate")

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "feature_oe_workflow",
            "base_workflow_commands": [
                "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
                "/helix:apply evaluation-scenario",
                "/helix:archive evaluation-scenario",
            ],
            "candidate_workflow_commands": [
                "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
                "/helix:apply evaluation-scenario",
                "/helix:archive evaluation-scenario",
            ],
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text
    candidate = response.json()["request"]["evaluation_contract"]["strategies"]["CANDIDATE"]
    assert candidate["adapter"]["source_ref"] == "feature_oe_workflow"
    assert candidate["adapter"]["source_commit"] == candidate_commit


def test_unified_draft_accepts_different_strategy_capabilities(v2_client: tuple[TestClient, object]) -> None:
    client, _ = v2_client

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": [
                "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
                "/helix:apply evaluation-scenario",
                "/helix:archive evaluation-scenario",
            ],
            "candidate_workflow_commands": [
                "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
                "/helix:apply evaluation-scenario",
                "/helix:archive evaluation-scenario",
            ],
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text
    assert "comparison" not in response.json()["request"]["evaluation_contract"]


def test_unified_draft_accepts_standard_workflow_without_verify(
    v2_client: tuple[TestClient, object],
) -> None:
    client, _ = v2_client
    commands = [
        "/helix:propose evaluation-scenario .helixharness_case/initial-request.md",
        "/helix:apply evaluation-scenario",
        "/helix:archive evaluation-scenario",
    ]

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": commands,
            "candidate_workflow_commands": commands,
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text
    contract = response.json()["request"]["evaluation_contract"]
    assert contract["strategies"]["BASE"]["commands"] == commands
    assert contract["strategies"]["CANDIDATE"]["commands"] == commands


def test_unified_draft_accepts_workflow_without_propose_or_apply(
    v2_client: tuple[TestClient, object],
) -> None:
    client, _ = v2_client
    commands = ["/helix:archive evaluation-scenario"]

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": commands,
            "candidate_workflow_commands": commands,
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text
    contract = response.json()["request"]["evaluation_contract"]
    assert contract["strategies"]["BASE"]["commands"] == commands
    assert contract["strategies"]["CANDIDATE"]["commands"] == commands


def test_unified_draft_uses_user_commands_instead_of_commit_capability_guessing(
    v2_client: tuple[TestClient, object],
) -> None:
    client, _ = v2_client
    commands = [
        "/helix:propose custom-evaluation .helixharness_case/initial-request.md",
        "/helix:apply custom-evaluation",
        "/helix:archive custom-evaluation",
    ]

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": commands,
            "candidate_workflow_commands": commands,
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text
    contract = response.json()["request"]["evaluation_contract"]
    assert "comparison" not in contract
    assert contract["strategies"]["BASE"]["commands"] == commands
    assert contract["strategies"]["CANDIDATE"]["commands"] == commands
    assert contract["strategies"]["BASE"]["artifact_sources"][0]["root"] == (
        "docs/specs/completed/custom-evaluation"
    )


def test_draft_rejects_non_helixharness_workflow_commands(v2_client: tuple[TestClient, object]) -> None:
    client, _ = v2_client

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": ["python dangerous.py"],
            "candidate_workflow_commands": [
                "/helix:archive evaluation-scenario",
            ],
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 400
    assert "必须是单条受支持的 HelixHarness 命令" in response.json()["detail"]


def test_draft_accepts_propose_loop(v2_client: tuple[TestClient, object]) -> None:
    client, _ = v2_client

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": [
                "/helix:propose evaluation-scenario .helixharness_case/initial-request.md --loop",
            ],
            "candidate_workflow_commands": [
                "/helix:archive evaluation-scenario",
            ],
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 200, response.text


def test_draft_rejects_command_option_missing_from_frozen_version(
    v2_client: tuple[TestClient, object],
) -> None:
    client, _ = v2_client

    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={
            "scenario_ids": ["runtime-scenario"],
            "candidate_ref": "candidate",
            "base_workflow_commands": [
                "/helix:propose evaluation-scenario .helixharness_case/initial-request.md --candidate-only",
                "/helix:apply evaluation-scenario",
                "/helix:archive evaluation-scenario",
            ],
            "candidate_workflow_commands": [
                "/helix:propose evaluation-scenario .helixharness_case/initial-request.md --candidate-only",
                "/helix:apply evaluation-scenario",
                "/helix:archive evaluation-scenario",
            ],
            "created_by": "测试操作员",
        },
    )

    assert response.status_code == 400
    assert "当前工作流不支持这些选项" in response.json()["detail"]


def test_formal_creation_requires_server_verified_quota_confirmation(v2_client: tuple[TestClient, object]) -> None:
    client, _ = v2_client

    response = client.post(
        "/framework-evaluation/api/runs",
        json={
            "scenario_ids": ["runtime-scenario"],
            "run_kind": "FORMAL",
            "execution_adapter": "CODEX",
            "evaluation_contract": _contract(),
            "created_by": "测试操作员",
            "runtime_policy": {},
            "expected_preview_hash": "0" * 64,
        },
    )

    assert response.status_code == 400
    assert "模型额度" in response.json()["detail"]


def test_run_detail_localizes_hard_gate_codes(v2_client: tuple[TestClient, object]) -> None:
    client, app = v2_client
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract(),
        "created_by": "测试操作员",
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]
    worker = ScenarioWorker(app.state.scenario_store, _CandidateGateAdapter())
    while worker.run_once(run_id=run_id).status not in {"FINALIZED", "IDLE"}:
        pass

    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")

    assert "Candidate 工作流未完成" in detail.text
    assert "Candidate 新增门禁" not in detail.text
    assert 'data-limited-recommendation-risks="true"' not in detail.text
    assert "推荐范围与 Case 风险" not in detail.text
    assert 'data-case-hard-gate-facts="runtime-scenario"' in detail.text
    assert detail.text.count('data-factual-hard-gate-group="CANDIDATE"') == 1
    assert detail.text.count('data-factual-hard-gate-side="CANDIDATE"') == 1
    assert "Candidate 问题 · 1 项" in detail.text
    assert "平台确定性事实" in detail.text
    assert 'data-workflow-incomplete-stage="CANDIDATE"' not in detail.text
    assert "首个未完成阶段" not in detail.text
    assert 'data-key-execution-evidence="true"' in detail.text
    assert "CRITICAL_EVIDENCE_MISSING" not in detail.text
    assert "WORKFLOW_TERMINAL_FAILURE" not in detail.text
    assert "初始化：" not in detail.text
    assert "NOT_APPLICABLE" not in detail.text
    assert 'data-efficiency-score-basis="true"' not in detail.text


@pytest.mark.parametrize(
    ("gate_id", "side", "expected"),
    [
        ("CORE_BUSINESS_BEHAVIOR_FAILURE", "BASE", "Base 目标业务能力验证失败（F2P）"),
        ("PROTECTED_REGRESSION_FAILURE", "CANDIDATE", "Candidate 既有业务能力验证失败（P2P）"),
        ("INIT_OUTPUT_INVALID", "CANDIDATE", "Candidate 初始化产物验证失败（INIT）"),
        ("CRITICAL_EVIDENCE_MISSING", "BASE", "Base 工作流未完成"),
        ("WORKFLOW_TERMINAL_FAILURE", "CANDIDATE", "Candidate 工作流未完成"),
        ("UNACCEPTABLE_SECURITY_REGRESSION", "GLOBAL", "安全检查发现不可接受的回归"),
        ("EVIDENCE_CONTAMINATION", "GLOBAL", "评测证据被访问或污染"),
        ("CHEATING", "GLOBAL", "检测到规避正常实现或操纵评测结果"),
        ("FALSE_COMPLETION", "GLOBAL", "完成声明与实际执行结果不一致"),
    ],
)
def test_hard_gate_display_labels_are_business_readable(gate_id: str, side: str, expected: str) -> None:
    assert _hard_gate_display_label(gate_id, side) == expected


def test_hard_gate_facts_group_case_problems_by_side() -> None:
    facts = {
        "run": {},
        "side_facts": [
            {"case_id": "case-one", "side": "BASE"},
            {"case_id": "case-one", "side": "CANDIDATE"},
        ],
        "hard_gates": [
            {"case_id": "case-one", "side": "CANDIDATE", "gate_id": "CRITICAL_EVIDENCE_MISSING"},
            {"case_id": "case-one", "side": "BASE", "gate_id": "CRITICAL_EVIDENCE_MISSING"},
            {"case_id": "case-one", "side": "CANDIDATE", "gate_id": "WORKFLOW_TERMINAL_FAILURE"},
            {"case_id": "case-one", "side": "BASE", "gate_id": "WORKFLOW_TERMINAL_FAILURE"},
        ],
        "evidence": [],
    }

    view = _hard_gate_facts_view(facts)
    groups = view["case_gate_groups"]["case-one"]

    assert [group["side"] for group in groups] == ["BASE", "CANDIDATE"]
    assert [[gate["gate_id"] for gate in group["gates"]] for group in groups] == [
        ["CRITICAL_EVIDENCE_MISSING", "WORKFLOW_TERMINAL_FAILURE"],
        ["CRITICAL_EVIDENCE_MISSING", "WORKFLOW_TERMINAL_FAILURE"],
    ]


def test_run_detail_shows_facts_before_explicit_ai_analysis(
    v2_client: tuple[TestClient, object],
) -> None:
    client, app = v2_client
    calls: list[dict[str, object]] = []

    class Analyzer:
        @staticmethod
        def analyze(request, *, model, reasoning_effort):
            calls.append({"request": request, "model": model, "reasoning_effort": reasoning_effort})
            return {
                "schema_version": "framework-eval-hard-gate-analysis.v1",
                "overall_summary": "Candidate 同时缺少关键证据且未可信完成。",
                "findings": [
                    {
                        "gate_id": gate["gate_id"],
                        "side": gate["side"],
                        "case_id": gate["case_id"],
                        "attribution": "TESTED_FRAMEWORK",
                        "confidence": "MEDIUM",
                        "summary": "冻结侧结果触发了该门禁。",
                        "root_causes": ["需结合执行轮次继续复核。"],
                        "evidence_refs": [
                            {
                                "ref_id": f"side:{gate['side']}:{gate['case_id']}",
                                "claim": "侧结果记录了该门禁。",
                            }
                        ],
                        "counterevidence": ["当前事实包不包含工作区源码。"],
                        "recommended_actions": ["复核对应执行诊断和产物收集。"],
                    }
                    for gate in request["hard_gates"]
                ],
                "decision_impact": "ANALYSIS_ONLY",
            }

    app.state.scenario_hard_gate_analyzer = Analyzer()
    app.state.scenario_hard_gate_analysis_enabled = True
    payload = {
        "scenario_ids": ["runtime-scenario"],
        "run_kind": "DEVELOPMENT",
        "execution_adapter": "MOCK",
        "evaluation_contract": _contract(),
        "created_by": "按需分析测试",
        "runtime_policy": {
            "schema_version": "framework-eval-runtime-policy.v2",
            "judge_model": "frozen-judge-model",
            "judge_reasoning_effort": "high",
        },
    }
    preview = client.post("/framework-evaluation/api/runs/preview", json=payload).json()
    payload["expected_preview_hash"] = preview["preview_hash"]
    run_id = client.post("/framework-evaluation/api/runs", json=payload).json()["run_id"]
    worker = ScenarioWorker(app.state.scenario_store, _CandidateGateAdapter())
    while worker.run_once(run_id=run_id).status not in {"FINALIZED", "IDLE"}:
        pass
    report_before = client.get(f"/framework-evaluation/api/runs/{run_id}/report").json()

    detail = client.get(f"/framework-evaluation/runs/{run_id}/view")

    assert calls == []
    assert 'data-case-hard-gate-facts="runtime-scenario"' in detail.text
    assert 'data-key-execution-evidence="true"' in detail.text
    assert "平台确定性事实" in detail.text
    assert "尚未调用 AI" in detail.text
    assert "AI 分析根因" in detail.text

    analysis = client.post(
        f"/framework-evaluation/runs/{run_id}/ai-analysis",
        headers={"HX-Request": "true"},
    )

    assert analysis.status_code == 200
    assert len(calls) == 1
    assert calls[0]["model"] == "frozen-judge-model"
    assert 'data-ai-analysis-ready="true"' in analysis.text
    assert "Candidate 同时缺少关键证据且未可信完成" in analysis.text
    assert "仅供辅助判断" in analysis.text
    assert client.get(f"/framework-evaluation/api/runs/{run_id}/report").json() == report_before


def test_v2_http_rejects_stale_preview(v2_client: tuple[TestClient, object]) -> None:
    client, _ = v2_client
    response = client.post(
        "/framework-evaluation/api/runs",
        json={
            "scenario_ids": ["runtime-scenario"],
            "run_kind": "DEVELOPMENT",
            "execution_adapter": "MOCK",
            "evaluation_contract": _contract(),
            "created_by": "server-smoke",
            "expected_preview_hash": "0" * 64,
        },
    )

    assert response.status_code == 409
    assert "preview changed" in response.json()["detail"]


def test_creation_draft_uses_configured_main_and_local_candidate_without_origin(
    v2_client: tuple[TestClient, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = v2_client
    repository = app.state.scenario_strategy_validator.repository
    base = _git(repository, "rev-parse", "master")
    candidate = _git(repository, "rev-parse", "candidate")
    _git(repository, "remote", "remove", "origin")
    _git(repository, "branch", "main", base)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REFERENCE_REF", "main")
    app = create_app()
    assert app.state.scenario_strategy_validator.reference_ref == "main"
    client = TestClient(app)
    commands = ["/helix:archive evaluation-scenario"]
    response = client.post(
        "/framework-evaluation/api/runs/draft",
        json={"scenario_ids": ["runtime-scenario"], "candidate_ref": "candidate", "created_by": "Public acceptance",
              "base_workflow_commands": commands, "candidate_workflow_commands": commands},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    strategies = payload["request"]["evaluation_contract"]["strategies"]
    assert strategies["BASE"]["adapter"]["source_ref"] == "main"
    assert strategies["BASE"]["adapter"]["source_commit"] == base
    assert strategies["CANDIDATE"]["adapter"]["source_commit"] == candidate
    assert payload["strategy_summary"]["BASE"]["version_label"] == "main"
    app.state.scenario_strategy_validator.validate(payload["request"]["evaluation_contract"])


def test_nested_plugin_configuration_is_used_by_web_draft(tmp_path, monkeypatch) -> None:
    from bench.task_configuration import task_platform_configuration
    from bench.tests.test_helixharness_source import _plugin

    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init")
    _plugin(repository / "one")
    _plugin(repository / "tools/two")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "two nested plugins")
    commit = _git(repository, "rev-parse", "HEAD")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_HELIXHARNESS_REPOSITORY", str(repository / "tools"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_PLUGIN_PATH", "tools/two")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REFERENCE_REF", commit)
    configuration = task_platform_configuration()
    assert Path(configuration.helixharness_repository) == repository.resolve()
    assert configuration.repository_ready
    app = create_app()
    source_root = tmp_path / "cases"
    source_root.mkdir()
    app.state.scenario_store.publish(load_scenario_source(_write_scenario(source_root)))
    with TestClient(app) as client:
        response = client.get("/framework-evaluation/config")
        assert response.status_code == 200
        assert "插件相对路径：<code>tools/two</code>" in response.text
        payload = {
            "scenario_ids": ["runtime-scenario"], "candidate_ref": commit,
            "base_workflow_commands": ["/helix:archive sample"],
            "candidate_workflow_commands": ["/helix:archive sample"],
            "created_by": "Layout Test",
        }
        response = client.post("/framework-evaluation/api/runs/draft", json=payload)
        assert response.status_code == 200, response.text
        contract = response.json()["request"]["evaluation_contract"]
        assert all(strategy["adapter"]["source_plugin_path"] == "tools/two"
                   for strategy in contract["strategies"].values())
        app.state.scenario_strategy_validator.plugin_path = "missing"
        invalid = client.post("/framework-evaluation/api/runs/draft", json=payload)
        assert invalid.status_code == 400
        assert "Selected plugin path" in invalid.text
