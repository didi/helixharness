"""HTTP and HTML adapters for the isolated workflow-neutral evaluation v2 pilot."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, RedirectResponse, Response

from bench.hard_gate_analysis import ATTRIBUTION_LABELS, HardGateAnalysisError
from bench.rulebook import current_evaluation_rulebook
from bench.runtime_secrets import evaluation_provider_config
from bench.scenario_execution_support import ScenarioExecutionSupportError, json_object, snapshot_file
from bench.scenario_hard_gate_analysis import ScenarioHardGateAnalysisService
from bench.scenario_runtime import (
    BASE_ONLY_EXECUTION_SCOPE,
    GitScenarioStrategyValidator,
    ScenarioRunCreateRequest,
    ScenarioRunService,
    ScenarioRuntimeError,
    ScenarioStrategyValidator,
    calculation_from_stored,
)
from bench.scenario_storage import ScenarioStore
from bench.scenario_strategy import KnownHelixHarnessStrategyResolver, ScenarioStrategyDraftError
from bench.scenario_workspace import MULTI_REPOSITORY, REPOSITORY_MODES, SINGLE_REPOSITORY, scenario_workspace
from bench.task_configuration import task_platform_configuration
from bench.turn_control import (
    DEFAULT_MAX_BUSINESS_CLARIFICATIONS,
    EVIDENCE_BOUND_USER_RESPONSE,
    EVIDENCE_BOUND_USER_RESPONSE_VERSION,
)
from bench.workload_policy import EVOLUTION_EVALUATION, FORMAL_EVALUATION, MAX_SCENARIOS_PER_RUN
from web_ui.deps import templates
from web_ui.routes.bench import _poll_signature, _resource_pool_views, _scenario_case_views

router = APIRouter(prefix="/framework-evaluation", tags=["framework-evaluation-v2"])

_RUN_STATUS_LABELS = {
    "QUEUED": "等待执行",
    "SUSPENDED": "已暂停排队",
    "PENDING": "等待执行",
    "RUNNING": "执行中",
    "PAUSED": "已暂停，可重试",
    "COMPLETED": "已完成",
    "FAILED": "执行失败",
    "CANCELLED": "已取消",
}
_RUN_KIND_LABELS = {
    "DEVELOPMENT": "开发验证",
    "FORMAL": "正式评测",
}
_RUN_TASK_TYPE_LABELS = {
    "PAIRED": "对比评测",
    "BASE_ONLY": "Base 验证",
    "ALL": "全部任务",
}
_COMPARISON_STATUS_LABELS = {
    "FULLY_COMPARABLE": "全部可比较",
    "PARTIALLY_COMPARABLE": "部分 Case 无法有效对比",
    "NOT_COMPARABLE": "没有 Case 可以有效对比",
    "BLOCKED": "已被强制门禁阻断",
}
_RECOMMENDATION_LABELS = {
    "RECOMMEND": "建议采用候选版本",
    "DO_NOT_RECOMMEND": "不建议采用候选版本",
    "NO_CLEAR_GAIN": "尚无明确收益",
}
_HARD_GATE_LABELS = {
    "UNACCEPTABLE_SECURITY_REGRESSION": "安全检查发现不可接受的回归",
    "EVIDENCE_CONTAMINATION": "评测证据被访问或污染",
    "CHEATING": "检测到规避正常实现或操纵评测结果",
    "FALSE_COMPLETION": "完成声明与实际执行结果不一致",
    "CORE_BUSINESS_BEHAVIOR_FAILURE": "目标业务能力验证失败（F2P）",
    "PROTECTED_REGRESSION_FAILURE": "既有业务能力验证失败（P2P）",
    "INIT_OUTPUT_INVALID": "初始化产物验证失败（INIT）",
    "CRITICAL_EVIDENCE_MISSING": "工作流未完成",
    "WORKFLOW_TERMINAL_FAILURE": "工作流未完成",
}


def _pause_error_view(error: object) -> dict[str, str]:
    """Return an operator-facing paused reason without mislabeling every fault as capacity."""

    detail = str(error or "")
    lowered = detail.lower()
    if (
        "reconnecting..." in lowered
        or "stream disconnected" in lowered
        or "apiconnectionerror" in lowered
        or "connection reset" in lowered
        or "connection closed" in lowered
        or "temporarily unavailable" in lowered
        or "rate limit" in lowered
        or re.search(r"\b(?:429|502|503|504)\b", lowered)
    ):
        return {
            "title": "上游连接中断",
            "summary": "Codex 上游连接在多次自动重试后仍未恢复；可从最近有效检查点继续。",
        }
    if (
        re.search(r"\[errno\s+(?:28|122)\]", lowered)
        or "no space left on device" in lowered
        or "disk quota exceeded" in lowered
    ):
        return {
            "title": "执行环境容量不足",
            "summary": "磁盘空间或执行配额不足；处理容量后可从最近有效检查点继续。",
        }
    return {
        "title": "执行异常已暂停",
        "summary": "执行遇到可恢复异常；确认下方技术原因后可从最近有效检查点继续。",
    }
_FACT_STATUS_LABELS = {
    "PASS": "通过",
    "FAIL": "失败",
    "ERROR": "执行异常",
    "INCONCLUSIVE": "无法判定",
    "NOT_RUN": "未执行",
}
_INCOMPATIBILITY_REASON_LABELS = {
    "SHARED_BUSINESS_FAILURE": "Base 与 Candidate 业务验证均失败，本 Case 作为共同失败不计入汇总",
    "BUSINESS_GATE_NOT_PASSED": "业务校验未通过，未形成业务分数",
    "BUSINESS_VERIFICATION_INCONCLUSIVE": "业务验证发生错误或超时，暂时无法形成可信结论",
    "WORKFLOW_ARTIFACT_EVIDENCE_UNAVAILABLE": "缺少可评分的工作流产物证据",
    "Assessment evidence is incompatible": "评测证据不兼容，无法形成有效对比",
    "Outcome or Workflow evidence is not scoreable": "业务结果或工作流证据不足，无法评分",
}
_INCOMPATIBILITY_SIDE_LABELS = {
    "BASE": "Base",
    "CANDIDATE": "Candidate",
}
def _sidebar(active_path: str) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    return [
        (
            "评测中心",
            [
                ("/framework-evaluation", "评测概览", active_path == "/framework-evaluation"),
                (
                    "/framework-evaluation/cases",
                    "评测Case",
                    active_path == "/framework-evaluation/cases",
                ),
                (
                    "/framework-evaluation/runs/new",
                    "发起评测",
                    active_path == "/framework-evaluation/runs/new",
                ),
                (
                    "/framework-evaluation/runs",
                    "评测任务",
                    active_path == "/framework-evaluation/runs",
                ),
                (
                    "/framework-evaluation/rules",
                    "评测规则",
                    active_path == "/framework-evaluation/rules",
                ),
                (
                    "/framework-evaluation/config",
                    "评测配置",
                    active_path == "/framework-evaluation/config",
                ),
            ],
        ),
    ]


@router.get("/runs")
async def scenario_v2_runs(request: Request) -> Response:
    """Render current v2 Runs only."""

    store = _store(request)
    service = await run_in_threadpool(_service, request)
    runs = await run_in_threadpool(service.list_runs)
    query = request.query_params.get("q", "").strip()
    branch = request.query_params.get("branch", "").strip()
    status = request.query_params.get("status", "").strip()
    run_kind = request.query_params.get("run_kind", "").strip()
    recommendation = request.query_params.get("recommendation", "").strip()
    requested_task_type = request.query_params.get("task_type", "PAIRED").strip().upper()
    task_type = requested_task_type if requested_task_type in _RUN_TASK_TYPE_LABELS else "PAIRED"
    requested_page_size = _positive_int(request.query_params.get("page_size", "10"), 10)
    page_size = requested_page_size if requested_page_size in {5, 10, 20, 50} else 10
    requested_page = _positive_int(request.query_params.get("page", "1"), 1)
    repository = _strategy_repository(request)
    candidate_refs = await run_in_threadpool(_candidate_ref_map, repository) if repository else {}

    run_views = []
    for run in runs:
        view = await run_in_threadpool(_run_view, store, run, candidate_refs=candidate_refs)
        jobs = [asdict(item) for item in await run_in_threadpool(store.list_run_jobs, run.run_id)]
        view["jobs"] = jobs
        view["progress"] = _run_progress(jobs, run_status=run.status)
        view["result"] = None
        if run.current_calculation_id:
            report = await run_in_threadpool(service.get_report, run.run_id)
            view["result"] = _report_view(report)
        if _matches_v2_filters(
            view,
            query=query,
            branch=branch,
            status=status,
            run_kind=run_kind,
            recommendation=recommendation,
            task_type=task_type,
        ):
            run_views.append(view)
    total_items = len(run_views)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = min(requested_page, total_pages)
    offset = (page - 1) * page_size
    run_views = run_views[offset : offset + page_size]
    filters = {
        "q": query,
        "branch": branch,
        "status": status,
        "run_kind": run_kind,
        "recommendation": recommendation,
        "task_type": task_type,
        "page_size": str(page_size),
    }
    has_pending_remediations = any(
        item.get("remediation", {}).get("status") == "PENDING"
        for item in run_views
    )
    return templates.TemplateResponse(
        request,
        "bench/runs.html",
        {
            "active_section": "framework_evaluation",
            "runs": run_views,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "previous_url": _v2_runs_url(filters, page - 1) if page > 1 else None,
                "next_url": (
                    _v2_runs_url(filters, page + 1)
                    if page < total_pages
                    else None
                ),
            },
            "current_url": _v2_runs_url(filters, page),
            "filters": filters,
            "run_poll_signature": _poll_signature(run_views),
            "status_options": tuple(_RUN_STATUS_LABELS),
            "status_labels": _RUN_STATUS_LABELS,
            "run_kind_options": ("FORMAL", "DEVELOPMENT"),
            "run_kind_labels": _RUN_KIND_LABELS,
            "task_type_options": tuple(_RUN_TASK_TYPE_LABELS),
            "task_type_labels": _RUN_TASK_TYPE_LABELS,
            "recommendation_options": tuple(_RECOMMENDATION_LABELS),
            "recommendation_labels": _RECOMMENDATION_LABELS,
            "has_live_runs": any(item["status"] in {"QUEUED", "RUNNING"} for item in run_views),
            "has_pending_remediations": has_pending_remediations,
            "sidebar_items": _sidebar("/framework-evaluation/runs"),
        },
    )


@router.get("/runs/new")
async def scenario_v2_create(request: Request) -> Response:
    """Render the Chinese-first creation flow for the frozen v2 contract."""

    store = _store(request)
    published_scenarios = await run_in_threadpool(store.list_current)
    scenarios = await run_in_threadpool(_scenario_case_views, published_scenarios)
    scenarios = [
        scenario
        for scenario in scenarios
        if scenario["publication_status"] == "PUBLISHED" and scenario["selectable"]
    ]
    repository_mode_counts = {
        SINGLE_REPOSITORY: sum(
            scenario["repository_mode"] == SINGLE_REPOSITORY for scenario in scenarios
        ),
        MULTI_REPOSITORY: sum(
            scenario["repository_mode"] == MULTI_REPOSITORY for scenario in scenarios
        ),
    }
    selected_scenario_ids = set(request.query_params.getlist("scenario_id"))
    for scenario in scenarios:
        scenario["selected"] = bool(
            scenario["selectable"] and scenario["case_id"] in selected_scenario_ids
        )
    configuration = await run_in_threadpool(
        task_platform_configuration,
        evaluation_provider_config("codex"),
    )
    resource_pools = await run_in_threadpool(
        _resource_pool_views,
        store,
    )
    return templates.TemplateResponse(
        request,
        "bench/create.html",
        {
            "active_section": "framework_evaluation",
            "scenarios": scenarios,
            "calibrated_count": sum(item["calibration_state"] == "CALIBRATED" for item in scenarios),
            "selectable_count": sum(bool(item["selectable"]) for item in scenarios),
            "repository_mode_counts": repository_mode_counts,
            "pending_calibration_count": sum(
                item["case_kind"] == "REPOSITORY_CASE" and not item["selectable"]
                for item in scenarios
            ),
            "max_scenarios_per_run": MAX_SCENARIOS_PER_RUN,
            "reference_ref": configuration.reference_ref,
            "execution_model": configuration.execution_model,
            "judge_model": configuration.judge_model,
            "execution_reasoning_effort": configuration.execution_reasoning_effort,
            "judge_reasoning_effort": configuration.judge_reasoning_effort,
            "resource_pools": resource_pools,
            "resource_pool_signature": _poll_signature(resource_pools),
            "sidebar_items": _sidebar("/framework-evaluation/runs/new"),
        },
    )


@router.get("/runs/{run_id}/view")
async def scenario_v2_run_view(run_id: str, request: Request) -> Response:
    """Render one localized v2 Run and its latest trusted calculation."""

    store = _store(request)
    service = await run_in_threadpool(_service, request)
    try:
        run = await run_in_threadpool(service.get_run, run_id)
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    report: dict[str, Any] | None = None
    if run.current_calculation_id:
        report = await run_in_threadpool(service.get_report, run_id)
    repository = _strategy_repository(request)
    candidate_refs = await run_in_threadpool(_candidate_ref_map, repository) if repository else {}
    run_view = await run_in_threadpool(
        _run_view,
        store,
        run,
        candidate_refs=candidate_refs,
    )
    jobs = [asdict(item) for item in await run_in_threadpool(store.list_run_jobs, run_id)]
    await run_in_threadpool(_attach_turn_evidence, store, jobs)
    run_view["jobs"] = jobs
    paired_case_jobs = _paired_case_jobs(jobs)
    run_view["case_jobs"] = paired_case_jobs
    run_view["case_jobs_by_id"] = {
        str(row["case_id"]): row
        for row in paired_case_jobs
    }
    run_view["progress"] = _run_progress(jobs, run_status=run.status)
    _attach_workflow_progress(run_view, jobs)
    report_view = _report_view(report) if report else None
    hard_gate_facts: dict[str, object] | None = None
    hard_gate_analysis: dict[str, object] | None = None
    if report is not None:
        analysis_service = _hard_gate_analysis_service(request)
        facts = await run_in_threadpool(analysis_service.facts, run_id)
        if facts["hard_gates"]:
            hard_gate_facts = _hard_gate_facts_view(facts)
            latest = await run_in_threadpool(analysis_service.latest, run_id)
            hard_gate_analysis = _hard_gate_analysis_view(
                latest,
                current_calculation_id=run.current_calculation_id,
                enabled=_hard_gate_analysis_enabled(request),
                model_available=bool(run_view["runtime"]["judge_model"]),
            )
    return templates.TemplateResponse(
        request,
        "bench/run_detail.html",
        {
            "active_section": "framework_evaluation",
            "run": run_view,
            "report": report_view,
            "hard_gate_facts": hard_gate_facts,
            "hard_gate_analysis": hard_gate_analysis,
            "status_labels": _RUN_STATUS_LABELS,
            "detail_poll_signature": _poll_signature({"run": run_view, "report": report_view}),
            "sidebar_items": _sidebar("/framework-evaluation/runs"),
        },
    )


@router.post("/runs/{run_id}/ai-analysis")
async def scenario_v2_hard_gate_analysis(run_id: str, request: Request) -> Response:
    """Run advisory diagnosis only after an explicit click."""

    service = _hard_gate_analysis_service(request)
    error: str | None = None
    if not _hard_gate_analysis_enabled(request):
        error = "AI 分析服务未配置可用的模型凭据"
    else:
        try:
            await run_in_threadpool(service.analyze, run_id)
        except HardGateAnalysisError as exc:
            error = str(exc)
    run = await run_in_threadpool(_service(request).get_run, run_id)
    latest = await run_in_threadpool(service.latest, run_id)
    view = _hard_gate_analysis_view(
        latest,
        current_calculation_id=run.current_calculation_id,
        enabled=_hard_gate_analysis_enabled(request),
        model_available=bool((service.store.get_run_runtime_policy(run_id) or {}).get("judge_model")),
        request_error=error,
    )
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "bench/partials/hard_gate_ai_analysis.html",
            {"run_id": run_id, "hard_gate_analysis": view},
        )
    return RedirectResponse(f"/framework-evaluation/runs/{run_id}/view", status_code=303)


@router.post("/runs/{run_id}/delete")
async def scenario_v2_delete_run(run_id: str, request: Request) -> Response:
    """Remove one non-running task from the current ledger without erasing audit data."""

    form = await request.form()
    if str(form.get("confirmation", "")).strip() != run_id:
        raise HTTPException(status_code=400, detail="删除确认与任务 ID 不一致")
    try:
        await run_in_threadpool(_service(request).delete, run_id)
    except ScenarioRuntimeError as exc:
        status = 409 if "不能" in str(exc) else 404
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return RedirectResponse("/framework-evaluation/runs", status_code=303)


@router.post("/runs/{run_id}/resume")
async def scenario_v2_resume_run(run_id: str, request: Request) -> Response:
    """Resume one held queue entry or recoverable task without replacing it."""

    try:
        run = await run_in_threadpool(_service(request).resume, run_id)
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    target = (
        "/framework-evaluation/runs"
        if run.status == "QUEUED"
        else f"/framework-evaluation/runs/{run_id}/view"
    )
    return RedirectResponse(target, status_code=303)


@router.post("/runs/{run_id}/suspend")
async def scenario_v2_suspend_run(run_id: str, request: Request) -> Response:
    """Hold one queued task while preserving its frozen identity and jobs."""

    try:
        await run_in_threadpool(_service(request).suspend, run_id)
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/framework-evaluation/runs", status_code=303)


@router.get("/api/scenarios")
async def list_scenarios(request: Request) -> JSONResponse:
    """List published Scenario versions and calibration state."""

    scenarios = await run_in_threadpool(_store(request).list_current)
    return JSONResponse([asdict(item) for item in scenarios])


@router.post("/api/runs/draft")
async def draft_run(request: Request) -> JSONResponse:
    """Resolve platform-owned strategies and return a confirmable task preview."""

    payload = await _json_object(request)
    scenario_ids = payload.get("scenario_ids")
    if not isinstance(scenario_ids, list) or not scenario_ids or any(not isinstance(item, str) for item in scenario_ids):
        raise HTTPException(status_code=400, detail="请至少选择一个评测场景")
    if len(scenario_ids) > MAX_SCENARIOS_PER_RUN:
        raise HTTPException(status_code=400, detail=f"一个评测任务最多选择 {MAX_SCENARIOS_PER_RUN} 个 Case")
    repository_mode = _validate_repository_mode_selection(_store(request), payload)
    base_only = payload.get("base_only", False)
    if not isinstance(base_only, bool):
        raise HTTPException(status_code=400, detail="base_only must be a boolean")
    candidate_ref = payload.get("candidate_ref")
    created_by = payload.get("created_by")
    base_workflow_commands = payload.get("base_workflow_commands")
    candidate_workflow_commands = payload.get("candidate_workflow_commands")
    if base_only:
        candidate_ref = task_platform_configuration(evaluation_provider_config("codex")).reference_ref
        candidate_workflow_commands = base_workflow_commands
    if not all(isinstance(item, str) for item in (candidate_ref, created_by)):
        raise HTTPException(status_code=400, detail="候选版本和创建人均为必填项")
    if (
        not isinstance(base_workflow_commands, list)
        or not isinstance(candidate_workflow_commands, list)
        or not all(isinstance(item, str) for item in (*base_workflow_commands, *candidate_workflow_commands))
    ):
        raise HTTPException(status_code=400, detail="基准侧和候选侧工作流命令均为必填项")
    validator = getattr(request.app.state, "scenario_strategy_validator", None)
    if not isinstance(validator, GitScenarioStrategyValidator):
        raise HTTPException(status_code=503, detail="平台尚未配置可用的 HelixHarness 版本仓库")
    try:
        draft = await run_in_threadpool(
            KnownHelixHarnessStrategyResolver(
                validator.repository, plugin_path=validator.plugin_path,
            ).draft,
            candidate_ref=candidate_ref,
            base_ref=validator.reference_ref,
            base_workflow_commands=base_workflow_commands,
            candidate_workflow_commands=candidate_workflow_commands,
        )
        configuration = await run_in_threadpool(
            task_platform_configuration,
            evaluation_provider_config("codex"),
        )
        policy = configuration.resource_policy(FORMAL_EVALUATION)
        run_payload: dict[str, Any] = {
            "scenario_ids": scenario_ids,
            "repository_mode": repository_mode,
            "run_kind": "FORMAL",
            "execution_adapter": "CODEX",
            "evaluation_contract": draft.evaluation_contract,
            "created_by": created_by,
            "runtime_policy": {
                "schema_version": "framework-eval-runtime-policy.v2",
                "workload_kind": FORMAL_EVALUATION,
                "execution_model": configuration.execution_model,
                "judge_model": configuration.judge_model,
                "reasoning_effort": configuration.execution_reasoning_effort,
                "judge_reasoning_effort": configuration.judge_reasoning_effort,
                **policy.runtime_fields(),
                "token_budget_enforced": False,
                "max_api_step_retries": 5,
                "judge_max_retries": 1,
                "max_business_clarifications": DEFAULT_MAX_BUSINESS_CLARIFICATIONS,
                "universal_user_response_version": EVIDENCE_BOUND_USER_RESPONSE_VERSION,
                "universal_user_response": EVIDENCE_BOUND_USER_RESPONSE,
                "origin": "BASE_VERIFICATION" if base_only else "MANUAL",
                **({"execution_scope": BASE_ONLY_EXECUTION_SCOPE} if base_only else {}),
            },
        }
        service = await run_in_threadpool(_service, request)
        preview = await run_in_threadpool(service.preview, _create_request(run_payload))
    except (ScenarioStrategyDraftError, ScenarioRuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "request": run_payload,
            "preview": asdict(preview),
            "blocker_labels": [_preview_blocker_label(item) for item in preview.blockers],
            "strategy_summary": draft.strategy_summary,
            "artifact_summary": _artifact_summary(draft.evaluation_contract),
        }
    )


@router.get("/api/health")
async def scenario_v2_health(request: Request) -> JSONResponse:
    """Expose DB integrity and queue state for deployment acceptance."""

    health = await run_in_threadpool(_store(request).health)
    return JSONResponse(health, status_code=200 if health["status"] == "ok" else 503)


@router.get("/api/runs")
async def list_runs(request: Request) -> JSONResponse:
    """List isolated v2 Runs."""

    service = await run_in_threadpool(_service, request)
    runs = await run_in_threadpool(service.list_runs)
    return JSONResponse([asdict(run) for run in runs])


@router.post("/api/runs/preview")
async def preview_run(request: Request) -> JSONResponse:
    """Validate and hash one exact prospective Run."""

    payload = await _json_object(request)
    try:
        service = await run_in_threadpool(_service, request)
        preview = await run_in_threadpool(service.preview, _create_request(payload))
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(asdict(preview))


@router.post("/api/runs")
async def create_run(request: Request) -> JSONResponse:
    """Create only when the client confirms the current deterministic preview."""

    payload = await _json_object(request)
    _validate_repository_mode_selection(_store(request), payload)
    if payload.get("run_kind") == "FORMAL" and payload.get("confirm_model_quota") is not True:
        raise HTTPException(status_code=400, detail="创建正式评测前必须明确确认会消耗模型额度")
    expected = payload.get("expected_preview_hash")
    if not isinstance(expected, str) or not expected:
        raise HTTPException(status_code=400, detail="expected_preview_hash is required")
    try:
        service = await run_in_threadpool(_service, request)
        run = await run_in_threadpool(
            service.create,
            _create_request(payload),
            expected_preview_hash=expected,
        )
    except ScenarioRuntimeError as exc:
        status = 409 if "preview changed" in str(exc) or "not ready" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return JSONResponse(asdict(run), status_code=201)


def _validate_repository_mode_selection(store: ScenarioStore, payload: dict[str, Any]) -> str | None:
    """Validate the requested topology against the selected current Cases."""

    mode = payload.get("repository_mode")
    if mode is not None and mode not in REPOSITORY_MODES:
        raise HTTPException(status_code=400, detail="请选择单仓或多仓")
    scenario_ids = payload.get("scenario_ids")
    if not isinstance(scenario_ids, list):
        return mode if isinstance(mode, str) else None
    actual_modes: set[str] = set()
    for case_id in scenario_ids:
        scenario = store.get(str(case_id))
        if scenario is None:
            continue
        try:
            snapshot = json.loads(scenario.snapshot_json)
            manifest = json_object(snapshot_file(snapshot, "case.json"), label="Scenario manifest")
            actual_mode = (
                scenario_workspace(manifest).repository_mode
                if manifest.get("case_kind") == "REPOSITORY_CASE"
                else SINGLE_REPOSITORY
            )
        except (ScenarioExecutionSupportError, ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"Case 仓库模式无效：{case_id}") from None
        actual_modes.add(actual_mode)
        if mode is not None and actual_mode != mode:
            raise HTTPException(status_code=400, detail=f"Case 仓库模式与当前选择不一致：{case_id}")
    if mode is None and len(actual_modes) > 1:
        raise HTTPException(status_code=400, detail="单仓与多仓 Case 不能混合创建任务")
    return mode if isinstance(mode, str) else next(iter(actual_modes), None)


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> JSONResponse:
    """Return Run status and immutable calculation history."""

    store = _store(request)
    try:
        service = await run_in_threadpool(_service, request)
        run = await run_in_threadpool(service.get_run, run_id)
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    stored_calculations = await run_in_threadpool(store.list_run_calculations, run_id)
    calculations = [calculation_from_stored(item) for item in stored_calculations]
    return JSONResponse(
        {
            **asdict(run),
            "calculations": [asdict(item) for item in calculations],
        }
    )


@router.get("/api/runs/{run_id}/report")
async def get_report(run_id: str, request: Request, calculation_id: str | None = None) -> JSONResponse:
    """Return the current or explicitly selected trusted report."""

    try:
        service = await run_in_threadpool(_service, request)
        report = await run_in_threadpool(service.get_report, run_id, calculation_id=calculation_id)
    except ScenarioRuntimeError as exc:
        status = 409 if "no calculation" in str(exc) else 404
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return JSONResponse(report)


@router.post("/api/runs/{run_id}/recalculate")
async def recalculate_run(run_id: str, request: Request) -> JSONResponse:
    """Append a calculation while preserving every execution outcome."""

    payload = await _json_object(request)
    reason = payload.get("reason")
    if not isinstance(reason, str):
        raise HTTPException(status_code=400, detail="reason is required")
    use_current_scoring_profile = payload.get("use_current_scoring_profile", False)
    if not isinstance(use_current_scoring_profile, bool):
        raise HTTPException(status_code=400, detail="use_current_scoring_profile must be a boolean")
    try:
        service = await run_in_threadpool(_service, request)
        calculation = await run_in_threadpool(
            service.recalculate,
            run_id,
            reason=reason,
            use_current_scoring_profile=use_current_scoring_profile,
        )
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(asdict(calculation), status_code=201)


@router.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str, request: Request) -> JSONResponse:
    """Resume one held or recoverable Run and preserve its execution history."""

    try:
        run = await run_in_threadpool(_service(request).resume, run_id)
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(asdict(run))


@router.post("/api/runs/{run_id}/suspend")
async def suspend_run(run_id: str, request: Request) -> JSONResponse:
    """Hold one queued Run without deleting or rewriting its frozen Jobs."""

    try:
        run = await run_in_threadpool(_service(request).suspend, run_id)
    except ScenarioRuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(asdict(run))


def _preview_blocker_label(blocker: str) -> str:
    if "is not a calibrated REPOSITORY_CASE" in blocker:
        return f"评测场景 {blocker.split(' is not', 1)[0]} 尚未完成真实仓库校准"
    labels = {
        "Formal Run requires the CODEX execution adapter": "正式评测需要可用的模型执行通道",
        "Formal Run requires the deployed HelixHarness Strategy repository": "平台尚未部署可解析执行策略的版本仓库",
    }
    return labels.get(blocker, blocker)


def _artifact_summary(contract: dict[str, object]) -> list[dict[str, object]]:
    strategies = contract.get("strategies") if isinstance(contract.get("strategies"), dict) else {}
    summary: list[dict[str, object]] = []
    for side in ("BASE", "CANDIDATE"):
        strategy = strategies.get(side) if isinstance(strategies.get(side), dict) else {}
        sources = strategy.get("artifact_sources") if isinstance(strategy.get("artifact_sources"), list) else []
        for source in sources:
            if isinstance(source, dict):
                summary.append(
                    {
                        "side": side,
                        "root": str(source.get("root", "")),
                        "include_glob": str(source.get("include_glob", "")),
                    }
                )
    return summary


def _run_view(
    store: ScenarioStore,
    run: Any,
    *,
    candidate_refs: dict[str, str] | None = None,
) -> dict[str, object]:
    contract = store.get_run_contract(run.run_id) or {}
    runtime_policy = store.get_run_runtime_policy(run.run_id) or {}
    remediation = store.get_remediation_projection(run.run_id)
    strategies = contract.get("strategies") if isinstance(contract.get("strategies"), dict) else {}
    pause_error = _pause_error_view(run.error)
    execution_scope = str(runtime_policy.get("execution_scope", "PAIRED"))
    return {
        "run_id": run.run_id,
        "status": run.status,
        "status_label": _RUN_STATUS_LABELS.get(run.status, run.status),
        "run_kind": run.run_kind,
        "run_kind_label": _RUN_KIND_LABELS.get(run.run_kind, run.run_kind),
        "purpose_label": (
            "自进化评测" if run.workload_kind == EVOLUTION_EVALUATION
            else _RUN_KIND_LABELS.get(run.run_kind, run.run_kind)
        ),
        "comparison_label": "统一评分体系",
        "base_version": _strategy_version_view(
            strategies.get("BASE"),
            side="BASE",
            candidate_refs=candidate_refs,
        ),
        "candidate_version": _strategy_version_view(
            strategies.get("CANDIDATE"),
            side="CANDIDATE",
            candidate_refs=candidate_refs,
        ),
        "created_by": run.created_by,
        "created_at": run.created_at,
        "scenario_ids": run.scenario_ids,
        "current_calculation_id": run.current_calculation_id,
        "execution_scope": execution_scope,
        "base_only": execution_scope == BASE_ONLY_EXECUTION_SCOPE,
        "error": run.error,
        "pause_error": pause_error,
        "remediation": _remediation_view(remediation),
        "runtime": {
            "execution_model": str(runtime_policy.get("execution_model", "")),
            "execution_reasoning_effort": str(runtime_policy.get("reasoning_effort", "")),
            "judge_model": str(runtime_policy.get("judge_model", "")),
            "judge_reasoning_effort": str(
                runtime_policy.get("judge_reasoning_effort", runtime_policy.get("reasoning_effort", ""))
            ),
        },
    }


def _remediation_view(projection: dict[str, object] | None) -> dict[str, object]:
    """Translate the durable remediation projection into one operator-facing state."""

    status = str(projection.get("status") or "UNAVAILABLE") if projection else "UNAVAILABLE"
    reason = str(projection.get("reason") or "") if projection else ""
    labels = {
        "PENDING": ("正在检查改进机会", "后台正在判断该任务是否存在证据充分的 Candidate 独有问题。"),
        "READY": ("查看进化假设", reason or "具体修复假设已生成。"),
        "NOT_ELIGIBLE": ("无可修复假设", reason or "该任务没有形成证据充分的 Candidate 独有问题。"),
        "FAILED": ("进化检查失败", reason or "后台处理该任务的进化证据时发生异常。"),
        "UNAVAILABLE": ("无进化检查记录", "该历史任务没有可用的进化回收记录。"),
    }
    label, description = labels.get(status, ("进化状态待确认", reason or "未识别的进化回收状态。"))
    return {
        "status": status,
        "label": label,
        "description": description,
        "hypothesis_id": str(projection.get("hypothesis_id") or "") if projection else "",
    }


def _strategy_version_view(
    value: object,
    *,
    side: str,
    candidate_refs: dict[str, str] | None = None,
) -> dict[str, object]:
    strategy = value if isinstance(value, dict) else {}
    adapter = strategy.get("adapter") if isinstance(strategy.get("adapter"), dict) else {}
    commit = str(adapter.get("source_commit", ""))
    source_ref = str(adapter.get("source_ref", "")).strip()
    display_ref = _display_source_ref(source_ref, commit, side=side, candidate_refs=candidate_refs)
    fallback = f"提交 {commit[:12]}" if commit else ("基准版本" if side == "BASE" else "候选版本")
    commands = strategy.get("commands") if isinstance(strategy.get("commands"), list) else []
    raw_stages = strategy.get("stages") if isinstance(strategy.get("stages"), list) else []
    return {
        "ref": display_ref,
        "label": display_ref or fallback,
        "commit": commit,
        "short_commit": commit[:12],
        "commands": [str(item) for item in commands],
        "workflow_stages": [
            {
                "stage_id": str(item.get("stage_id", "")),
                "command": str(item.get("command", "")),
            }
            for item in raw_stages
            if isinstance(item, dict)
        ],
    }


def _strategy_repository(request: Request) -> Path | None:
    validator = getattr(request.app.state, "scenario_strategy_validator", None)
    if not isinstance(validator, GitScenarioStrategyValidator):
        return None
    return validator.repository


def _display_source_ref(
    source_ref: str,
    commit: str,
    *,
    side: str,
    candidate_refs: dict[str, str] | None,
) -> str:
    if side != "CANDIDATE":
        return source_ref
    technical_ref = re.fullmatch(r"[0-9a-f]{40}", source_ref) is not None or source_ref.startswith("refs/")
    if not technical_ref:
        return source_ref
    if candidate_refs is not None and re.fullmatch(r"[0-9a-f]{40}", commit) is not None:
        resolved = candidate_refs.get(commit)
        if resolved:
            return resolved
    return _readable_candidate_ref(source_ref)


def _candidate_ref_map(repository: Path) -> dict[str, str]:
    completed = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "for-each-ref",
            "--format=%(objectname)%09%(refname)",
            "refs/heads",
            "refs/remotes",
            "refs/helixharness/evolution",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {}
    refs_by_commit: dict[str, list[str]] = {}
    for line in completed.stdout.splitlines():
        commit, separator, ref = line.partition("\t")
        if separator and re.fullmatch(r"[0-9a-f]{40}", commit):
            refs_by_commit.setdefault(commit, []).append(ref)
    return {
        commit: _preferred_candidate_ref(refs)
        for commit, refs in refs_by_commit.items()
        if _preferred_candidate_ref(refs)
    }


def _preferred_candidate_ref(refs: list[str]) -> str:
    preferred = next((item for item in refs if item.startswith("refs/heads/")), "")
    preferred = preferred or next(
        (item for item in refs if item.startswith("refs/helixharness/evolution/")),
        "",
    )
    preferred = preferred or next(
        (item for item in refs if item.startswith("refs/remotes/") and not item.endswith("/HEAD")),
        "",
    )
    return _readable_candidate_ref(preferred)


def _readable_candidate_ref(value: str) -> str:
    for prefix in (
        "refs/heads/",
        "refs/remotes/origin/",
        "refs/helixharness/evolution/",
        "refs/helixharness/",
    ):
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return value


def _attach_workflow_progress(
    run: dict[str, object],
    jobs: list[dict[str, Any]],
) -> None:
    for side, version_key in (("BASE", "base_version"), ("CANDIDATE", "candidate_version")):
        version = run.get(version_key)
        if not isinstance(version, dict):
            continue
        raw_stages = version.get("workflow_stages")
        stages = raw_stages if isinstance(raw_stages, list) else []
        side_jobs = [item for item in jobs if item.get("side") == side]
        total_jobs = len(side_jobs)
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            stage_id = str(stage.get("stage_id", ""))
            completed_jobs = 0
            running_jobs = 0
            failed_jobs = 0
            for job in side_jobs:
                completed_ids = tuple(str(item) for item in job.get("completed_stage_ids", ()))
                if stage_id in completed_ids:
                    completed_jobs += 1
                elif job.get("status") == "RUNNING" and len(completed_ids) == index:
                    running_jobs += 1
                elif job.get("status") == "FAILED":
                    failed_jobs += 1
            if total_jobs and completed_jobs == total_jobs:
                status = "COMPLETED"
            elif running_jobs:
                status = "RUNNING"
            elif failed_jobs:
                status = "FAILED"
            elif completed_jobs:
                status = "PARTIAL"
            else:
                status = "PENDING"
            stage.update(
                {
                    "status": status,
                    "completed_jobs": completed_jobs,
                    "total_jobs": total_jobs,
                }
            )


def _attach_turn_evidence(store: ScenarioStore, jobs: list[dict[str, Any]]) -> None:
    """Attach bounded, persisted diagnostics without exposing mutable workspaces."""

    for job in jobs:
        job_id = job.get("job_id")
        evidence = (
            store.list_job_turn_evidence(job_id)
            if isinstance(job_id, str) and job_id
            else []
        )
        exceptional_turn = any(
            item.get("classification") != "COMPLETED"
            or "FINDING" in str(item.get("input_kind", "")).upper()
            or "RERUN" in str(item.get("input_kind", "")).upper()
            for item in evidence
        )
        terminal_reason = job.get("terminal_reason")
        job["turn_evidence"] = (
            evidence
            if exceptional_turn
            or job.get("status") in {"FAILED", "PAUSED"}
            or terminal_reason not in {None, "COMPLETED"}
            else []
        )


def _paired_case_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, object]]:
    """Group Base and Candidate execution summaries into one row per Case."""

    grouped: dict[str, dict[str, object]] = {}
    for job in jobs:
        case_id = str(job.get("case_id", ""))
        row = grouped.setdefault(case_id, {"case_id": case_id, "BASE": None, "CANDIDATE": None})
        side = str(job.get("side", ""))
        if side in {"BASE", "CANDIDATE"}:
            row[side] = job
    return list(grouped.values())


def _run_progress(jobs: list[dict[str, Any]], *, run_status: str) -> dict[str, object]:
    total = len(jobs)
    completed = sum(item.get("status") == "COMPLETED" for item in jobs)
    failed = sum(item.get("status") == "FAILED" for item in jobs)
    finished = completed + failed
    sides: dict[str, dict[str, int]] = {}
    for side in ("BASE", "CANDIDATE"):
        side_jobs = [item for item in jobs if item.get("side") == side]
        sides[side] = {
            "completed": sum(item.get("status") == "COMPLETED" for item in side_jobs),
            "running": sum(item.get("status") == "RUNNING" for item in side_jobs),
            "failed": sum(item.get("status") == "FAILED" for item in side_jobs),
            "total": len(side_jobs),
        }
    if run_status == "COMPLETED":
        active_label = "执行完成，评测结果已生成"
    elif run_status == "FAILED":
        active_label = "执行失败，请进入详情查看原因"
    elif run_status == "PAUSED":
        active_label = "可恢复异常已暂停；可优先从最近有效检查点重试"
    elif run_status == "SUSPENDED":
        active_label = "任务已暂停排队；恢复后将重新进入原执行通道"
    elif sides["BASE"]["running"] and sides["CANDIDATE"]["running"]:
        active_label = "Base 与 Candidate 正在并行执行"
    elif sides["BASE"]["running"]:
        active_label = "Base 正在执行"
    elif sides["CANDIDATE"]["running"]:
        active_label = "Candidate 正在执行"
    else:
        active_label = "等待 Worker 领取任务"
    return {
        "percentage": round(finished * 100 / total) if total else 0,
        "finished": finished,
        "completed": completed,
        "failed": failed,
        "total": total,
        "sides": sides,
        "active_label": active_label,
    }


def _report_view(report: dict[str, Any]) -> dict[str, object]:
    calculation = report.get("calculation") if isinstance(report.get("calculation"), dict) else {}
    status = str(calculation.get("status", ""))
    recommendation = calculation.get("recommendation")
    cases = calculation.get("cases", []) if isinstance(calculation.get("cases"), list) else []
    scoring_profile = report.get("scoring_profile") if isinstance(report.get("scoring_profile"), dict) else {}
    outcomes_by_case_side = _execution_outcomes_by_case_side(report.get("execution_outcomes"))
    rulebook = current_evaluation_rulebook()
    axis_labels = {
        axis.key: axis.label
        for axis in (
            *rulebook.outcome_axes,
            *rulebook.workflow_axes,
        )
    }
    case_views = [
        _case_result_view(
            item,
            outcomes_by_case_side=outcomes_by_case_side,
            scoring_profile=scoring_profile,
            axis_labels=axis_labels,
        )
        for item in cases
        if isinstance(item, dict)
    ]
    candidate_new_hard_failures = [
        {
            "case_id": str(item["case_id"]),
            "hard_gates": list(item["candidate_new_hard_gates"]),
        }
        for item in case_views
        if item["candidate_new_hard_gates"]
    ]
    ignored_cases = [
        {
            "case_id": str(item["case_id"]),
            "reasons": list(item["incompatibility_reasons"]),
        }
        for item in case_views
        if item["incompatibility_reasons"]
    ]
    return {
        "comparison_label": "统一评分体系",
        "status": status,
        "status_label": _COMPARISON_STATUS_LABELS.get(status, status),
        "status_badge": (
            None
            if status == "FULLY_COMPARABLE"
            else {
                "label": _COMPARISON_STATUS_LABELS.get(status, status),
                "tone": "warning" if status == "PARTIALLY_COMPARABLE" else "danger",
            }
        ),
        "recommendation": recommendation,
        "recommendation_label": _RECOMMENDATION_LABELS.get(str(recommendation), "暂不生成正式建议"),
        "selected_case_count": calculation.get("selected_case_count"),
        "comparable_case_count": calculation.get("comparable_case_count"),
        "comparable_coverage": calculation.get("comparable_coverage"),
        "base_outcome_quality": calculation.get("base_outcome_quality"),
        "candidate_outcome_quality": calculation.get("candidate_outcome_quality"),
        "outcome_delta": calculation.get("outcome_delta"),
        "base_unified_score": calculation.get("base_unified_score"),
        "candidate_unified_score": calculation.get("candidate_unified_score"),
        "unified_delta": calculation.get("unified_delta"),
        "base_workflow_quality": _average_case_component(cases, "base", "framework_workflow_quality"),
        "candidate_workflow_quality": _average_case_component(cases, "candidate", "framework_workflow_quality"),
        "base_efficiency": _average_case_component(cases, "base", "efficiency"),
        "candidate_efficiency": _average_case_component(cases, "candidate", "efficiency"),
        "base_case_hard_gates": _hard_gate_labels(calculation.get("base_case_hard_gates")),
        "candidate_case_hard_gates": _hard_gate_labels(calculation.get("candidate_case_hard_gates")),
        "global_hard_gates": _hard_gate_labels(calculation.get("global_hard_gates")),
        "candidate_new_hard_failures": candidate_new_hard_failures,
        "ignored_cases": ignored_cases,
        "cases": case_views,
        "sequence_number": calculation.get("sequence_number"),
        "reason": calculation.get("reason"),
    }


def _average_case_component(cases: list[object], side: str, component: str) -> float | None:
    values: list[float] = []
    for item in cases:
        side_score = item.get(side) if isinstance(item, dict) else None
        value = side_score.get(component) if isinstance(side_score, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return round(sum(values) / len(values), 2) if values else None


def _case_result_view(
    item: dict[str, Any],
    *,
    outcomes_by_case_side: dict[tuple[str, str], dict[str, Any]],
    scoring_profile: dict[str, Any],
    axis_labels: dict[str, str],
) -> dict[str, object]:
    case_id = str(item.get("case_id", ""))
    base_raw = item.get("base") if isinstance(item.get("base"), dict) else {}
    candidate_raw = item.get("candidate") if isinstance(item.get("candidate"), dict) else {}
    base_gate_ids = {str(gate) for gate in base_raw.get("case_hard_gates", [])}
    candidate_gate_ids = {str(gate) for gate in candidate_raw.get("case_hard_gates", [])}
    incompatibility_reason_details = _incompatibility_reason_details(
        item.get("incompatibility_reasons", [])
    )
    result: dict[str, object] = {
        "case_id": case_id,
        "incompatibility_reasons": [str(reason["label"]) for reason in incompatibility_reason_details],
        "incompatibility_reason_details": incompatibility_reason_details,
        "shared_hard_gates": _hard_gate_labels(sorted(base_gate_ids & candidate_gate_ids)),
        "base_only_hard_gates": _hard_gate_labels(sorted(base_gate_ids - candidate_gate_ids)),
        "candidate_new_hard_gates": [
            _HARD_GATE_LABELS.get(gate, gate)
            for gate in sorted(candidate_gate_ids - base_gate_ids)
        ],
    }
    for side in ("base", "candidate"):
        raw = item.get(side) if isinstance(item.get(side), dict) else {}
        outcome = outcomes_by_case_side.get((case_id, side.upper()), {})
        result[side] = {
            "outcome_quality": raw.get("outcome_quality"),
            "framework_workflow_quality": raw.get("framework_workflow_quality"),
            "efficiency": raw.get("efficiency"),
            "resources": _resource_accounting_view(outcome.get("resources")),
            "unified_score": raw.get("unified_score"),
            "case_hard_gates": _hard_gate_labels(raw.get("case_hard_gates")),
            "score_components": _score_components(raw, scoring_profile),
            "score_details": _score_axis_details(
                outcome,
                scoring_profile=scoring_profile,
                axis_labels=axis_labels,
            ),
        }
    result["efficiency_basis"] = _efficiency_basis_view(result, scoring_profile)
    score_details: list[dict[str, object]] = []
    for side in ("base", "candidate"):
        side_result = result.get(side)
        side_details = side_result.get("score_details") if isinstance(side_result, dict) else None
        if isinstance(side_details, list):
            score_details.extend(detail for detail in side_details if isinstance(detail, dict))
    result["ai_judgment_available"] = any(
        detail.get("rationale") or detail.get("deductions") or detail.get("limitations")
        for detail in score_details
    )
    return result


def _efficiency_basis_view(
    case_result: dict[str, object],
    scoring_profile: dict[str, Any],
) -> dict[str, object] | None:
    """Explain one persisted relative Efficiency score from its frozen resource inputs."""

    base = case_result.get("base")
    candidate = case_result.get("candidate")
    if not isinstance(base, dict) or not isinstance(candidate, dict):
        return None
    base_score = _finite_number(base.get("efficiency"))
    candidate_score = _finite_number(candidate.get("efficiency"))
    base_resources = base.get("resources")
    candidate_resources = candidate.get("resources")
    if (
        base_score is None
        or candidate_score is None
        or not isinstance(base_resources, dict)
        or not isinstance(candidate_resources, dict)
    ):
        return None
    base_attributable = base_resources.get("attributable")
    candidate_attributable = candidate_resources.get("attributable")
    weights = scoring_profile.get("efficiency_weights")
    if (
        not isinstance(base_attributable, dict)
        or not isinstance(candidate_attributable, dict)
        or not isinstance(weights, dict)
    ):
        return None

    metric_specs = (
        ("agent_tokens", "Agent Token", "M Token", 1_000_000, 1),
        ("active_execution_seconds", "有效执行时间", "分钟", 60, 1),
        ("workflow_rework_actions", "工作流返工", "次", 1, None),
    )
    metrics: list[dict[str, object]] = []
    for key, label, unit, divisor, precision in metric_specs:
        base_value = _finite_number(base_attributable.get(key))
        candidate_value = _finite_number(candidate_attributable.get(key))
        weight = _finite_number(weights.get(key))
        if base_value is None or candidate_value is None or weight is None:
            return None
        metrics.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "weight": _stable_display_number(weight),
                "base_value": _efficiency_display_number(base_value, divisor=divisor, precision=precision),
                "candidate_value": _efficiency_display_number(candidate_value, divisor=divisor, precision=precision),
            }
        )
    return {
        "metrics": metrics,
        "base_score": _stable_display_number(base_score),
        "candidate_score": _stable_display_number(candidate_score),
    }


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if number == number and number not in {float("inf"), float("-inf")} else None


def _stable_display_number(value: float, *, precision: int = 3) -> int | float:
    rounded = round(value, precision)
    return int(rounded) if rounded.is_integer() else rounded


def _efficiency_display_number(value: float, *, divisor: int, precision: int | None) -> int | float | str:
    scaled = value / divisor
    return f"{scaled:.{precision}f}" if precision is not None else _stable_display_number(scaled)


def _resource_accounting_view(value: object) -> dict[str, object]:
    """Normalize v6 dual accounting and retain an honest historical fallback."""

    if not isinstance(value, dict):
        return {
            "accounting_status": "UNAVAILABLE",
            "accounting_status_label": "资源证据不可用",
            "attributable": {},
            "actual": None,
            "excluded": None,
        }
    attributable = value.get("attributable", value)
    actual = value.get("actual")
    excluded = value.get("excluded")
    status = str(value.get("accounting_status", "HISTORICAL"))
    status_labels = {
        "COMPLETE": "完整核算",
        "PARTIAL": "部分核算",
        "HISTORICAL": "历史单口径",
    }
    return {
        "accounting_status": status,
        "accounting_status_label": status_labels.get(status, status),
        "attributable": attributable if isinstance(attributable, dict) else {},
        "actual": actual if isinstance(actual, dict) else None,
        "excluded": excluded if isinstance(excluded, dict) else None,
    }


def _execution_outcomes_by_case_side(value: object) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(value, list):
        return {}
    outcomes: dict[tuple[str, str], dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("outcome"), dict):
            continue
        case_id = str(item.get("case_id", ""))
        side = str(item.get("side", ""))
        if case_id and side in {"BASE", "CANDIDATE"}:
            outcomes[(case_id, side)] = item["outcome"]
    return outcomes


def _score_components(raw: dict[str, Any], scoring_profile: dict[str, Any]) -> list[dict[str, object]]:
    weights = scoring_profile.get("weights") if isinstance(scoring_profile.get("weights"), dict) else {}
    labels = {
        "outcome_quality": "业务结果质量",
        "framework_workflow_quality": "框架工作流质量",
        "efficiency": "执行效率",
    }
    return [
        {
            "key": key,
            "label": label,
            "score": raw.get(key),
            "weight": weights.get(key),
        }
        for key, label in labels.items()
    ]


def _score_axis_details(
    outcome: dict[str, Any],
    *,
    scoring_profile: dict[str, Any],
    axis_labels: dict[str, str],
) -> list[dict[str, object]]:
    judge = outcome.get("judge") if isinstance(outcome.get("judge"), dict) else {}
    details: list[dict[str, object]] = []
    for judge_key, score_key, weights_key, category in (
        ("outcome", "outcome_axis_scores", "outcome_weights", "业务结果评分"),
        ("workflow", "workflow_axis_scores", "workflow_axes", "工作流质量评分"),
    ):
        raw_scores = outcome.get(score_key) if isinstance(outcome.get(score_key), dict) else {}
        weights = scoring_profile.get(weights_key) if isinstance(scoring_profile.get(weights_key), dict) else {}
        judgment = judge.get(judge_key) if isinstance(judge.get(judge_key), dict) else {}
        raw_items = judgment.get("axis_scores") if isinstance(judgment.get("axis_scores"), list) else []
        judgment_by_axis = {
            str(item.get("axis", "")): item
            for item in raw_items
            if isinstance(item, dict) and item.get("axis")
        }
        for axis in weights:
            item = judgment_by_axis.get(str(axis), {})
            score = item.get("score", raw_scores.get(axis))
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                continue
            details.append(
                {
                    "axis": str(axis),
                    "label": axis_labels.get(str(axis), str(axis)),
                    "category": category,
                    "score": float(score),
                    "weight": weights.get(axis),
                    "rationale": str(item.get("rationale", "")).strip(),
                    "deductions": [str(value) for value in item.get("deductions", []) if isinstance(value, str)],
                    "limitations": [str(value) for value in item.get("limitations", []) if isinstance(value, str)],
                    "evidence_ids": [str(value) for value in item.get("evidence_ids", []) if isinstance(value, str)],
                }
            )
    return details


def _hard_gate_labels(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_HARD_GATE_LABELS.get(str(item), str(item)) for item in value]


def _incompatibility_reason_details(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    details: list[dict[str, str]] = []
    for item in value:
        code = str(item)
        side_label = ""
        reason_code = code
        prefix, separator, remainder = code.partition(": ")
        if separator and prefix in _INCOMPATIBILITY_SIDE_LABELS:
            side_label = _INCOMPATIBILITY_SIDE_LABELS[prefix]
            reason_code = remainder
        reason_label = _INCOMPATIBILITY_REASON_LABELS.get(reason_code, reason_code)
        label = f"{side_label}：{reason_label}" if side_label else reason_label
        details.append({"code": code, "label": label})
    return details


def _hard_gate_facts_view(facts: dict[str, Any]) -> dict[str, object]:
    side_facts = {
        (str(item.get("side", "")), str(item.get("case_id", ""))): item
        for item in facts.get("side_facts", [])
        if isinstance(item, dict)
    }
    gates: list[dict[str, object]] = []
    for item in facts.get("hard_gates", []):
        if not isinstance(item, dict):
            continue
        side = str(item.get("side", ""))
        case_id = str(item.get("case_id", ""))
        gate_id = str(item.get("gate_id", ""))
        side_fact = dict(side_facts.get((side, case_id), {}))
        terminal_status = str(side_fact.get("terminal_status") or "")
        verification_status = str(side_fact.get("verification_status") or "")
        initialization_status = str(side_fact.get("initialization_status") or "")
        side_fact["terminal_status_label"] = _RUN_STATUS_LABELS.get(terminal_status, terminal_status or "未记录")
        side_fact["verification_status_label"] = _FACT_STATUS_LABELS.get(
            verification_status,
            verification_status or "未记录",
        )
        side_fact["initialization_status_label"] = (
            None
            if initialization_status in {"", "NOT_APPLICABLE"}
            else _FACT_STATUS_LABELS.get(initialization_status, initialization_status)
        )
        gates.append(
            {
                "gate_id": gate_id,
                "label": _hard_gate_display_label(gate_id, side),
                "side": side,
                "side_label": _INCOMPATIBILITY_SIDE_LABELS.get(side, "全局"),
                "case_id": case_id,
                "side_fact": side_fact,
            }
        )
    case_gate_groups: dict[str, list[dict[str, object]]] = {}
    groups_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for gate in gates:
        case_id = str(gate["case_id"])
        side = str(gate["side"])
        if not case_id:
            continue
        key = (case_id, side)
        group = groups_by_key.get(key)
        if group is None:
            group = {
                "side": side,
                "side_label": gate["side_label"],
                "side_fact": gate["side_fact"],
                "gates": [],
            }
            groups_by_key[key] = group
            case_gate_groups.setdefault(case_id, []).append(group)
        group_gates = group["gates"]
        assert isinstance(group_gates, list)
        group_gates.append(gate)
    side_order = {"BASE": 0, "CANDIDATE": 1, "GLOBAL": 2}
    for groups in case_gate_groups.values():
        groups.sort(key=lambda item: side_order.get(str(item["side"]), 3))
    evidence: list[dict[str, str]] = []
    for item in facts.get("evidence", []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", ""))
        fact = item.get("fact") if isinstance(item.get("fact"), dict) else {}
        summary = ""
        if kind == "BUSINESS_VERIFICATION_CHECK":
            summary = (
                f"{fact.get('kind') or '业务检查'} {fact.get('check_id') or ''}："
                f"{fact.get('status') or '未知'}"
            )
            detail = fact.get("stderr_excerpt") or fact.get("stdout_excerpt")
            if detail:
                summary = f"{summary} · {detail}"
        elif kind == "ARTIFACT_EVIDENCE" and fact.get("status") != "PRESENT":
            summary = f"产物 {fact.get('evidence_id') or fact.get('path') or '未命名'}：{fact.get('status') or '缺失'}"
        elif kind == "INTERACTION_TURN" and fact.get("classification") not in {
            "COMPLETED",
            "PASS",
            "PASSED",
        }:
            summary = (
                f"工作流轮次 {fact.get('workflow_step_id') or fact.get('turn_sequence')}："
                f"{fact.get('classification') or '异常'}"
            )
            detail = fact.get("error") or fact.get("final_response_excerpt")
            if detail:
                summary = f"{summary} · {detail}"
        if summary:
            evidence.append(
                {
                    "ref_id": str(item.get("ref_id", "")),
                    "kind": kind,
                    "summary": summary,
                }
            )
    return {
        "calculation_id": str(facts.get("run", {}).get("calculation_id", "")),
        "report_hash": str(facts.get("run", {}).get("report_hash", "")),
        "gates": gates,
        "case_gate_groups": case_gate_groups,
        "evidence": evidence,
        "evidence_count": len(facts.get("evidence", [])),
    }


def _hard_gate_display_label(gate_id: str, side: str) -> str:
    label = _HARD_GATE_LABELS.get(gate_id, gate_id)
    side_label = _INCOMPATIBILITY_SIDE_LABELS.get(side)
    return f"{side_label} {label}" if side_label else label


def _hard_gate_analysis_view(
    analysis: dict[str, object] | None,
    *,
    current_calculation_id: str | None,
    enabled: bool,
    model_available: bool,
    request_error: str | None = None,
) -> dict[str, object]:
    unavailable_reason = None
    if not enabled:
        unavailable_reason = "平台尚未配置 AI 分析模型凭据"
    elif not model_available:
        unavailable_reason = "该冻结任务未记录 Judge 模型"
    output = analysis.get("output") if isinstance(analysis, dict) and isinstance(analysis.get("output"), dict) else {}
    findings: list[dict[str, object]] = []
    for item in output.get("findings", []):
        if not isinstance(item, dict):
            continue
        gate_id = str(item.get("gate_id", ""))
        side = str(item.get("side", ""))
        findings.append(
            {
                **item,
                "gate_label": _HARD_GATE_LABELS.get(gate_id, gate_id),
                "side_label": _INCOMPATIBILITY_SIDE_LABELS.get(side, "全局"),
                "attribution_label": ATTRIBUTION_LABELS.get(
                    str(item.get("attribution", "")),
                    str(item.get("attribution", "")),
                ),
                "confidence_label": {
                    "HIGH": "高",
                    "MEDIUM": "中",
                    "LOW": "低",
                }.get(str(item.get("confidence", "")), str(item.get("confidence", ""))),
            }
        )
    return {
        "exists": analysis is not None,
        "status": str(analysis.get("status", "")) if analysis else "",
        "analysis_id": str(analysis.get("analysis_id", "")) if analysis else "",
        "calculation_id": str(analysis.get("calculation_id", "")) if analysis else "",
        "is_current": bool(
            analysis
            and current_calculation_id
            and analysis.get("calculation_id") == current_calculation_id
        ),
        "model": str(analysis.get("model", "")) if analysis else "",
        "reasoning_effort": str(analysis.get("reasoning_effort", "")) if analysis else "",
        "created_at": str(analysis.get("created_at", "")) if analysis else "",
        "overall_summary": str(output.get("overall_summary", "")),
        "findings": findings,
        "stored_error": str(analysis.get("error", "")) if analysis and analysis.get("error") else "",
        "request_error": request_error or "",
        "can_analyze": enabled and model_available,
        "unavailable_reason": unavailable_reason,
    }


def _positive_int(value: str, default: int) -> int:
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _v2_runs_url(filters: dict[str, str], page: int) -> str:
    params = {
        **{key: value for key, value in filters.items() if value},
        "page": str(page),
    }
    return f"/framework-evaluation/runs?{urlencode(params)}"


def _matches_v2_filters(
    run: dict[str, object],
    *,
    query: str,
    branch: str,
    status: str,
    run_kind: str,
    recommendation: str,
    task_type: str,
) -> bool:
    if task_type == "PAIRED" and bool(run["base_only"]):
        return False
    if task_type == "BASE_ONLY" and not bool(run["base_only"]):
        return False
    if status and run["status"] != status:
        return False
    if run_kind and run["run_kind"] != run_kind:
        return False
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    if recommendation and result.get("recommendation") != recommendation:
        return False
    versions = (run["base_version"], run["candidate_version"])
    if branch:
        branch_query = branch.casefold()
        if not any(
            branch_query in str(version.get("ref", "")).casefold()
            for version in versions
            if isinstance(version, dict)
        ):
            return False
    if query:
        searchable = " ".join(
            (
                str(run["run_id"]),
                str(run["created_by"]),
                *(str(item) for item in run["scenario_ids"]),
                *(
                    str(version.get("commit", ""))
                    for version in versions
                    if isinstance(version, dict)
                ),
            )
        ).casefold()
        if query.casefold() not in searchable:
            return False
    return True


def _store(request: Request) -> ScenarioStore:
    store = getattr(request.app.state, "scenario_store", None)
    if not isinstance(store, ScenarioStore):  # pragma: no cover - composition-root contract
        raise HTTPException(status_code=503, detail="Scenario v2 store is unavailable")
    return store


def _hard_gate_analysis_service(request: Request) -> ScenarioHardGateAnalysisService:
    analyzer = getattr(request.app.state, "scenario_hard_gate_analyzer", None)
    return ScenarioHardGateAnalysisService(_store(request), analyzer=analyzer)


def _hard_gate_analysis_enabled(request: Request) -> bool:
    return bool(getattr(request.app.state, "scenario_hard_gate_analysis_enabled", False))


def _service(request: Request) -> ScenarioRunService:
    validator = getattr(request.app.state, "scenario_strategy_validator", None)
    if validator is not None and not isinstance(validator, ScenarioStrategyValidator):
        raise HTTPException(status_code=503, detail="Scenario v2 Strategy validator is unavailable")
    return ScenarioRunService(
        _store(request),
        strategy_validator=validator,
    )


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return value


def _create_request(payload: dict[str, Any]) -> ScenarioRunCreateRequest:
    scenario_ids = payload.get("scenario_ids")
    contract = payload.get("evaluation_contract")
    if not isinstance(scenario_ids, list) or any(not isinstance(item, str) for item in scenario_ids):
        raise HTTPException(status_code=400, detail="scenario_ids must be a list of strings")
    if not isinstance(contract, dict):
        raise HTTPException(status_code=400, detail="evaluation_contract must be an object")
    try:
        return ScenarioRunCreateRequest(
            scenario_ids=tuple(scenario_ids),
            run_kind=str(payload.get("run_kind", "")),  # type: ignore[arg-type]
            execution_adapter=str(payload.get("execution_adapter", "")),  # type: ignore[arg-type]
            evaluation_contract=contract,
            created_by=str(payload.get("created_by", "")),
            runtime_policy=(
                payload["runtime_policy"]
                if isinstance(payload.get("runtime_policy"), dict)
                else {}
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
