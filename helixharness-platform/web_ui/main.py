"""FastAPI 应用入口 —— 替代旧 Gradio app.py。

单进程模型：uvicorn 一条命令起服务，FastAPI 同进程渲染 Jinja2 模板。
HTMX 做前端交互（行点击、局部刷新、轮询），Tailwind 做样式。
"""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from bench.hard_gate_analysis import OpenAIHardGateAnalyzer
from bench.runtime_secrets import codex_responses_base_url, evaluation_provider_config, load_runtime_secrets
from bench.scenario_runtime import GitScenarioStrategyValidator
from bench.scenario_storage import ScenarioStore
from bench.semantic_judging import OpenAISemanticJudgeAdapter
from bench.task_configuration import task_platform_configuration
from bench.workload_policy import EVOLUTION_ANALYSIS, EVOLUTION_CANDIDATE
from eval.feedback_store import FeedbackStore
from eval.stage_supplements import StageSupplementStore
from eval.telemetry_quality import TelemetryQualityStore
from evolution.candidate import CandidateBuilder, CodexCandidateRunner
from evolution.catalog import load_catalog
from evolution.codex_runtime import codex_runtime_available
from evolution.discovery import CodexSemanticAnalyzer, SemanticAnalyzer
from evolution.runtime_config import load_runtime_config
from evolution.scheduler import EvolutionScheduler
from evolution.service import EvaluationGateway, EvolutionService
from evolution.storage import EvolutionStore
from web_ui.deps import _STATIC_DIR, PROJECT_ROOT, AppContext
from web_ui.evolution_evaluation import build_benchmark_evolution_gateway, resolve_evolution_candidate_base
from web_ui.routes import (
    bench,
    eval,
    evolution,
    feedback_report,
    scenario,
    telemetry_report,
)


def create_app() -> FastAPI:
    """构建 FastAPI 应用：挂载静态资源并注册各功能路由。"""
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        scheduler = application.state.evolution_scheduler
        if _enabled("HELIXHARNESS_PLATFORM_EVOLUTION_SCHEDULER_ENABLED", default=False):
            scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(title="HelixHarness Platform", version="1.0.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1_000)
    app.context = AppContext()  # type: ignore[attr-defined]
    scenario_store = ScenarioStore(
        Path(
            os.environ.get(
                "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH",
                str(PROJECT_ROOT / "bench" / ".cache" / "evaluation.db"),
            )
        )
    )
    scenario_store.initialize()
    app.state.scenario_store = scenario_store
    task_configuration = task_platform_configuration(evaluation_provider_config("codex"))
    app.state.scenario_strategy_validator = GitScenarioStrategyValidator(
        Path(task_configuration.helixharness_repository),
        reference_ref=task_configuration.reference_ref,
        plugin_path=task_configuration.helixharness_plugin_path,
    )
    evaluation_provider = evaluation_provider_config("codex")
    app.state.scenario_judge_adapter = OpenAISemanticJudgeAdapter(
        provider_config=evaluation_provider
    )
    app.state.scenario_hard_gate_analyzer = OpenAIHardGateAnalyzer(
        provider_config=evaluation_provider
    )
    app.state.scenario_hard_gate_analysis_enabled = bool(evaluation_provider.api_key)
    telemetry_store = TelemetryQualityStore(
        os.environ.get(
            "HELIXHARNESS_PLATFORM_TELEMETRY_DB_PATH",
            str(PROJECT_ROOT / "eval" / ".cache" / "telemetry.db"),
        )
    )
    app.state.telemetry_quality_store = telemetry_store
    app.state.feedback_store = FeedbackStore(
        os.environ.get("HELIXHARNESS_PLATFORM_FEEDBACK_DB_PATH", str(PROJECT_ROOT / "eval" / ".cache" / "feedback.db"))
    )
    app.state.stage_supplement_store = StageSupplementStore(
        os.environ.get(
            "HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH",
            str(PROJECT_ROOT / "eval" / ".cache" / "stage_supplements.db"),
        )
    )
    evolution_store = EvolutionStore(
        os.environ.get(
            "HELIXHARNESS_PLATFORM_EVOLUTION_DB_PATH",
            str(PROJECT_ROOT / "evolution" / ".cache" / "evolution.db"),
        )
    )
    app.state.evolution_store = evolution_store
    runtime_configuration = load_runtime_config()
    catalog_path = Path(
        os.environ.get(
            "HELIXHARNESS_PLATFORM_EVOLUTION_CATALOG_PATH",
            str(PROJECT_ROOT / "evolution" / "catalog" / "capabilities.yaml"),
        )
    )
    evolution_service = _evolution_service(
        scenario_store,
        evolution_store,
        catalog_path=catalog_path,
        interval_hours=runtime_configuration.interval_hours,
    )
    app.state.evolution_service = evolution_service

    def evolution_runtime() -> tuple[SemanticAnalyzer | None, CandidateBuilder | None, EvaluationGateway | None]:
        refreshed = _evolution_service(
            scenario_store, evolution_store, catalog_path=catalog_path,
        )
        return refreshed.analyzer, refreshed.candidate_builder, refreshed.evaluation_gateway

    evolution_service.runtime_provider = evolution_runtime
    app.state.evolution_catalog_path = catalog_path
    configuration = task_platform_configuration(evaluation_provider_config("codex"))
    candidate_policy = configuration.resource_policy(EVOLUTION_CANDIDATE)
    app.state.evolution_scheduler = EvolutionScheduler(
        evolution_service,
        sync_source=lambda: _refresh_evolution_source(app, telemetry_store),
        evidence_provider=telemetry_store.automation_evidence,
        poll_seconds=runtime_configuration.poll_seconds,
        remediation_poll_seconds=runtime_configuration.remediation_poll_seconds,
        candidate_max_concurrency=candidate_policy.max_concurrency,
    )

    # 静态资源（HTMX JS、Tailwind CSS 编译产物）
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg")

    # 功能路由
    app.include_router(eval.router)
    app.include_router(telemetry_report.router)
    app.include_router(feedback_report.router)
    app.include_router(evolution.router)
    app.include_router(bench.router)
    app.include_router(scenario.router)

    return app


def _evolution_service(
    scenario_store: ScenarioStore,
    store: EvolutionStore,
    *,
    catalog_path: Path | None = None,
    interval_hours: int | None = None,
) -> EvolutionService:
    catalog_path = catalog_path or Path(
        os.environ.get(
            "HELIXHARNESS_PLATFORM_EVOLUTION_CATALOG_PATH",
            str(PROJECT_ROOT / "evolution" / "catalog" / "capabilities.yaml"),
        )
    )
    interval_hours = interval_hours or load_runtime_config().interval_hours
    capabilities = load_catalog(catalog_path)
    provider = evaluation_provider_config("codex")
    configuration = task_platform_configuration(provider)
    discovery_policy = configuration.resource_policy(EVOLUTION_ANALYSIS)
    candidate_policy = configuration.resource_policy(EVOLUTION_CANDIDATE)
    analyzer = None
    candidate_builder = None
    gateway = None
    if configuration.repository_ready and codex_runtime_available():
        if _enabled("HELIXHARNESS_PLATFORM_EVOLUTION_SEMANTIC_ANALYZER_ENABLED", default=True):
            analyzer = CodexSemanticAnalyzer(
                configuration.helixharness_repository,
                model=configuration.models.analysis_model or None,
                reasoning_effort=configuration.models.analysis_reasoning_effort,
                api_key=provider.api_key or None,
                base_url=codex_responses_base_url(provider.base_url) or None,
                timeout_seconds=discovery_policy.wall_timeout_seconds,
            )
        candidate_builder = CandidateBuilder(
            configuration.helixharness_repository,
            os.environ.get(
                "HELIXHARNESS_PLATFORM_EVOLUTION_WORKTREE_ROOT",
                str(PROJECT_ROOT / "evolution" / ".cache" / "worktrees"),
            ),
            CodexCandidateRunner(
                model=configuration.models.candidate_model or None,
                reasoning_effort=configuration.models.candidate_reasoning_effort,
                api_key=provider.api_key or None,
                base_url=codex_responses_base_url(provider.base_url) or None,
                timeout_seconds=candidate_policy.wall_timeout_seconds,
                execution_user=os.environ.get(
                    "HELIXHARNESS_PLATFORM_EVOLUTION_EXECUTION_USER",
                    "nobody" if hasattr(os, "geteuid") and os.geteuid() == 0 else "",
                ) or None,
            ),
            disk_budget_mb=candidate_policy.disk_budget_mb,
            base_commit_resolver=lambda repository: resolve_evolution_candidate_base(
                repository, configuration.reference_ref,
            ),
        )
    if configuration.ready and provider.api_key:
        gateway = build_benchmark_evolution_gateway(
            scenario_store,
            configuration,
            provider,
        )
    return EvolutionService(
        store,
        capabilities,
        analyzer=analyzer,
        candidate_builder=candidate_builder,
        evaluation_gateway=gateway,
        interval=timedelta(hours=interval_hours),
    )


def _refresh_evolution_source(app: FastAPI, telemetry_store: TelemetryQualityStore) -> None:
    from eval.usage_sync import _current_usage_roots, _run_usage_sync, is_demo_roots

    default_roots = app.context.current_roots()  # type: ignore[attr-defined]
    if app.context.usage_data_source in {"sync", "auto"}:  # type: ignore[attr-defined]
        _run_usage_sync("按需求维度", default_roots, skip_ai=True)
    current_roots = _current_usage_roots(default_roots)
    if is_demo_roots(current_roots):
        current_roots = []
    app_state = getattr(app, "state", None)
    supplement_store = getattr(app_state, "stage_supplement_store", None)
    linked_supplements = []
    if supplement_store is not None:
        supplement_store.refresh_links(current_roots)
        linked_supplements = supplement_store.active_linked_records()
        telemetry_store.refresh(current_roots, linked_supplements)
    else:
        telemetry_store.refresh(current_roots)


def _enabled(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def main() -> None:
    parser = argparse.ArgumentParser(description="HelixHarness Platform Server")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument(
        "--helixharness-roots",
        type=str,
        default="",
        help="逗号分隔的 HelixHarness 项目根路径。也可用环境变量 HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS。",
    )
    parser.add_argument(
        "--usage-data-source",
        choices=["demo", "sync", "auto"],
        default="demo",
        help="使用情况数据源。",
    )
    args = parser.parse_args()

    # 环境变量注入（供 deps 和 usage_loader 读取）
    load_runtime_secrets()
    os.environ["HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE"] = args.usage_data_source
    roots_str = args.helixharness_roots or os.environ.get("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", "")
    if roots_str:
        os.environ["HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS"] = roots_str

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
