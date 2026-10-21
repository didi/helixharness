"""Current-only Web routes for the workflow-neutral evaluation center."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import zipfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

from bench.platform_config import platform_config_path, save_platform_config
from bench.rulebook import (
    current_evaluation_rulebook,
    score_anchor_rules,
)
from bench.runtime_secrets import evaluation_provider_config
from bench.scenario_storage import PublishedScenario, ScenarioStore
from bench.task_configuration import PLATFORM_CONFIG_SCHEMA_VERSION, task_platform_configuration
from bench.workload_policy import (
    EVOLUTION_EVALUATION,
    FORMAL_EVALUATION,
    MAX_WORKLOAD_CONCURRENCY,
)
from web_ui.deps import templates
from web_ui.markdown import render_markdown
from web_ui.scenario_views import scenario_case_detail_view, scenario_case_view

router = APIRouter(prefix="/framework-evaluation", tags=["framework-evaluation"])

_CASE_CREATOR_PLUGIN_NAME = "helixharness-case-creator"
_CASE_CREATOR_MARKETPLACE_NAME = "helixharness-platform"
_CASE_CREATOR_GIT_URL = ""
_CASE_CREATOR_GIT_REF = ""


def _store(request: Request) -> ScenarioStore:
    store = getattr(request.app.state, "scenario_store", None)
    if not isinstance(store, ScenarioStore):  # pragma: no cover - composition-root contract
        raise HTTPException(status_code=503, detail="新版评测数据不可用")
    return store


def _scenario_repositories() -> dict[str, Path]:
    """Resolve Bench-owned repository checkouts used by frozen Case intervals."""

    root = Path(
        os.environ.get(
            "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REPOSITORY_ROOT",
            "bench/.cache/repositories",
        )
    ).expanduser()
    repositories = {
        path.name: path.resolve()
        for path in sorted(root.iterdir())
        if path.is_dir() and (path / ".git").exists()
    } if root.is_dir() else {}
    configured = os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REPOSITORIES", "")
    for item in (value.strip() for value in configured.split(",") if value.strip()):
        repository_id, separator, raw_path = item.partition("=")
        path = Path(raw_path).expanduser()
        if separator and repository_id.strip() and path.is_dir() and (path / ".git").exists():
            repositories[repository_id.strip()] = path.resolve()
    return repositories


def _scenario_case_views(items: list[PublishedScenario]) -> list[dict[str, object]]:
    repositories = _scenario_repositories()
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(items)))) as executor:
        return list(
            executor.map(
                lambda item: scenario_case_view(item, repositories=repositories),
                items,
            )
        )


def _framework_sidebar(active_path: str) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    """Return the one supported evaluation navigation model."""

    return [
        (
            "评测中心",
            [
                ("/framework-evaluation", "评测概览", active_path == "/framework-evaluation"),
                ("/framework-evaluation/cases", "评测Case", active_path == "/framework-evaluation/cases"),
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
                ("/framework-evaluation/rules", "评测规则", active_path == "/framework-evaluation/rules"),
                ("/framework-evaluation/config", "评测配置", active_path == "/framework-evaluation/config"),
            ],
        )
    ]


def _case_creator_install_context() -> dict[str, str]:
    git_url = os.environ.get("HELIXHARNESS_PLATFORM_CASE_CREATOR_GIT_URL", _CASE_CREATOR_GIT_URL).strip()
    git_ref = os.environ.get("HELIXHARNESS_PLATFORM_CASE_CREATOR_GIT_REF", _CASE_CREATOR_GIT_REF).strip()
    marketplace_command = f"codex plugin marketplace add {shlex.quote(git_url)}"
    if git_ref:
        marketplace_command += f" --ref {shlex.quote(git_ref)}"
    return {
        "case_creator_plugin_name": _CASE_CREATOR_PLUGIN_NAME,
        "case_creator_install_command": (
            f"{marketplace_command} && "
            f"codex plugin add {_CASE_CREATOR_PLUGIN_NAME}@{_CASE_CREATOR_MARKETPLACE_NAME}"
        ) if git_url else "",
        "case_creator_codex_install_url": os.environ.get(
            "HELIXHARNESS_PLATFORM_CASE_CREATOR_CODEX_INSTALL_URL", ""
        ).strip(),
        "case_creator_download_url": "/framework-evaluation/skill/download",
    }


def _case_creator_plugin_root() -> Path:
    configured = os.environ.get("HELIXHARNESS_PLATFORM_CASE_CREATOR_PLUGIN_ROOT", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path(__file__).parents[1] / "static" / "plugins" / _CASE_CREATOR_PLUGIN_NAME
    )


def _case_creator_plugin_zip() -> bytes:
    plugin_root = _case_creator_plugin_root()
    if not (plugin_root / ".codex-plugin" / "plugin.json").is_file():
        raise FileNotFoundError("Case Creator Plugin 不存在")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(plugin_root.rglob("*")):
            if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
                continue
            relative_path = Path(_CASE_CREATOR_PLUGIN_NAME) / path.relative_to(plugin_root)
            info = zipfile.ZipInfo(relative_path.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, path.read_bytes())
    return archive.getvalue()


def _poll_signature(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _resource_pool_views(store: ScenarioStore) -> tuple[dict[str, object], ...]:
    """Project independent v2 workload FIFOs and Worker pools."""

    configuration = task_platform_configuration()
    definitions = (
        (
            FORMAL_EVALUATION,
            "正式评测",
            "人工确认发起的 Base/Candidate 正式比较",
            configuration.formal_evaluation.max_concurrency,
        ),
        (
            EVOLUTION_EVALUATION,
            "自进化评测",
            "新 Candidate 首次验证与评测结论驱动的修复重评",
            configuration.evolution_evaluation.max_concurrency,
        ),
    )
    pools = []
    for workload_kind, label, description, configured_concurrency in definitions:
        summary = store.resource_summary(workload_kind=workload_kind)
        worker_count = int(summary["worker_count"])
        pending_work_count = int(summary["pending_work_count"])
        if worker_count == 0:
            state, state_label = "UNAVAILABLE", "暂无可用 Worker"
        elif worker_count < configured_concurrency:
            state, state_label = "DEGRADED", "在线容量低于配置"
        elif pending_work_count:
            state, state_label = "WAITING", "新任务需要等待"
        else:
            state, state_label = "AVAILABLE", "当前可立即使用"
        head_run = summary.get("head_run")
        pools.append(
            {
                "lane": workload_kind,
                "label": label,
                "description": description,
                "state": state,
                "state_label": state_label,
                "worker_count": worker_count,
                "configured_concurrency": configured_concurrency,
                "effective_concurrency": min(worker_count, configured_concurrency),
                "busy_worker_count": int(summary["busy_worker_count"]),
                "idle_worker_count": int(summary["idle_worker_count"]),
                "active_work_count": int(summary["active_work_count"]),
                "pending_work_count": pending_work_count,
                "ahead_count": int(summary["unfinished_run_count"]),
                "current_item": f"任务 · {head_run['run_id']}" if isinstance(head_run, dict) else "无",
            }
        )
    return tuple(pools)


def _overview_metrics(store: ScenarioStore) -> dict[str, int]:
    metrics = store.overview_metrics()
    origins = store.run_metrics_by_origin()
    manual = origins["MANUAL"]
    evolution = origins["EVOLUTION"]
    return {
        **metrics,
        **manual,
        "evolution_runs": evolution["runs"],
        "evolution_completed_runs": evolution["completed_runs"],
        "evolution_failed_runs": evolution["failed_runs"],
        "evolution_active_runs": evolution["active_runs"],
        "evolution_reported_runs": evolution["reported_runs"],
    }


@router.get("")
async def framework_evaluation_home(request: Request) -> Response:
    overview_metrics = await asyncio.to_thread(_overview_metrics, _store(request))
    return templates.TemplateResponse(
        request,
        "bench/index.html",
        {
            "active_section": "framework_evaluation",
            **_case_creator_install_context(),
            "overview_metrics": overview_metrics,
            "tutorial_video_available": _tutorial_video_path() is not None,
            "sidebar_items": _framework_sidebar("/framework-evaluation"),
        },
    )


def _tutorial_video_path() -> Path | None:
    """Expose only the operator-configured local tutorial, never a request path."""
    configured = os.environ.get("HELIXHARNESS_PLATFORM_EVALUATION_TUTORIAL_VIDEO_PATH", "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser().resolve()
    return path if path.suffix.lower() in {".mp4", ".webm"} and path.is_file() else None


@router.get("/tutorial-video")
async def evaluation_tutorial_video() -> Response:
    path = _tutorial_video_path()
    if path is None:
        raise HTTPException(status_code=404, detail="评测教学视频尚未配置或文件不可用")
    media_type = "video/webm" if path.suffix.lower() == ".webm" else "video/mp4"
    return FileResponse(path, media_type=media_type)


def _platform_config_context(
    request: Request,
    *,
    error: str = "",
) -> dict[str, object]:
    provider = evaluation_provider_config("codex")
    configuration = task_platform_configuration(provider)
    return {
        "configuration": configuration,
        "provider_ready": bool(provider.base_url and provider.api_key),
        "config_saved": request.query_params.get("saved") == "1",
        "config_error": error,
        "config_path": str(platform_config_path()),
        "reasoning_efforts": ("low", "medium", "high", "xhigh"),
        "permission_modes": ("workspace-write", "read-only"),
    }


@router.get("/config")
async def framework_evaluation_config(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "bench/config.html",
        {
            **_platform_config_context(request),
            "active_section": "framework_evaluation",
            "sidebar_items": _framework_sidebar("/framework-evaluation/config"),
        },
    )


@router.post("/config")
async def save_framework_evaluation_config(request: Request) -> Response:
    form = await request.form()

    def text(name: str, fallback: str = "") -> str:
        return str(form.get(name, fallback)).strip()

    def integer(name: str, minimum: int, maximum: int) -> int:
        try:
            value = int(text(name))
        except ValueError as exc:
            raise ValueError(f"{name} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
        return value

    def token_budget(prefix: str) -> int:
        """Convert the form's k Token display value back to the stored Token count."""

        kilotoken_name = f"{prefix}_token_budget_k"
        raw_kilotokens = text(kilotoken_name)
        if not raw_kilotokens:
            return integer(
                f"{prefix}_token_budget",
                1_000,
                2_000_000,
            )
        try:
            tokens = Decimal(raw_kilotokens) * 1_000
        except InvalidOperation as exc:
            raise ValueError(f"{kilotoken_name} 必须是数字") from exc
        if not tokens.is_finite():
            raise ValueError(f"{kilotoken_name} 必须是有限数字")
        if tokens != tokens.to_integral_value():
            raise ValueError(f"{kilotoken_name} 最多支持 3 位小数")
        value = int(tokens)
        if not 1_000 <= value <= 2_000_000:
            raise ValueError(f"{kilotoken_name} 必须在 1 到 2000 之间")
        return value

    models: dict[str, object] = {
        "execution_model": text("execution_model"),
        "judge_model": text("judge_model"),
        "analysis_model": text("analysis_model"),
        "candidate_model": text("candidate_model"),
        "execution_reasoning_effort": text("execution_reasoning_effort"),
        "judge_reasoning_effort": text("judge_reasoning_effort"),
        "analysis_reasoning_effort": text("analysis_reasoning_effort"),
        "candidate_reasoning_effort": text("candidate_reasoning_effort"),
    }
    values: dict[str, object] = {
        "schema_version": PLATFORM_CONFIG_SCHEMA_VERSION,
        "reference_ref": text("reference_ref"),
        "models": models,
        "permission_mode": text("permission_mode"),
    }
    try:
        if not values["reference_ref"]:
            raise ValueError("Reference 引用不能为空")
        if any(not models[key] for key in ("execution_model", "judge_model", "analysis_model", "candidate_model")):
            raise ValueError("执行、Judge、分析和 Candidate 模型不能为空")
        for role in ("execution", "judge", "analysis", "candidate"):
            if models[f"{role}_reasoning_effort"] not in {"low", "medium", "high", "xhigh"}:
                raise ValueError(f"{role} reasoning effort 不合法")
        if values["permission_mode"] not in {"workspace-write", "read-only"}:
            raise ValueError("权限模式不合法")
        def resource(prefix: str) -> dict[str, int]:
            return {
                "wall_timeout_seconds": integer(
                    f"{prefix}_wall_timeout_seconds",
                    60,
                    604_800,
                ),
                "token_budget": token_budget(prefix),
                "tool_call_budget": integer(
                    f"{prefix}_tool_call_budget",
                    1,
                    10_000,
                ),
                "disk_budget_mb": integer(
                    f"{prefix}_disk_budget_mb",
                    128,
                    102_400,
                ),
                "max_concurrency": integer(
                    f"{prefix}_max_concurrency",
                    1,
                    MAX_WORKLOAD_CONCURRENCY,
                ),
            }

        values["resources"] = {
            "formal_evaluation": resource("formal"),
            "evolution_analysis": resource("analysis"),
            "evolution_candidate": resource("candidate"),
            "evolution_evaluation": resource("evolution_evaluation"),
        }
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "bench/config.html",
            {
                **_platform_config_context(request, error=str(exc)),
                "active_section": "framework_evaluation",
                "sidebar_items": _framework_sidebar("/framework-evaluation/config"),
            },
            status_code=422,
        )
    save_platform_config(values)
    request.app.state.evolution_scheduler.candidate_max_concurrency = int(
        values["resources"]["evolution_candidate"]["max_concurrency"]
    )
    return RedirectResponse("/framework-evaluation/config?saved=1", status_code=303)


def _positive_int(value: str, default: int) -> int:
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _cases_url(filters: dict[str, str], page: int) -> str:
    params = {**{key: value for key, value in filters.items() if value}, "page": str(page)}
    return f"/framework-evaluation/cases?{urlencode(params)}"


def _pagination_items(current_page: int, total_pages: int) -> tuple[int | None, ...]:
    visible = {1, total_pages, *range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1)}
    items: list[int | None] = []
    previous = 0
    for page in sorted(visible):
        if page - previous > 1:
            items.append(None)
        items.append(page)
        previous = page
    return tuple(items)


@router.get("/cases")
async def framework_evaluation_cases(request: Request) -> Response:
    store = _store(request)
    scenarios, formally_verified_versions = await asyncio.gather(
        asyncio.to_thread(store.list_current),
        asyncio.to_thread(store.formally_verified_versions),
    )
    entries = [
        entry
        for entry in await asyncio.to_thread(_scenario_case_views, scenarios)
        if entry["case_kind"] == "REPOSITORY_CASE" or getattr(request.app.state, "demo_mode", False)
    ]
    for entry in entries:
        if entry["case_kind"] != "REPOSITORY_CASE":
            entry.update(selectable=False, readiness="DEMO", readiness_label="合成示例 · 仅供查看")
        entry["formal_run_verified"] = (
            str(entry["case_id"]),
            str(entry["source_hash"]),
        ) in formally_verified_versions
    query = request.query_params.get("q", "").strip()
    readiness = request.query_params.get("readiness", "").strip()
    repository_mode = request.query_params.get("repository_mode", "").strip()
    sort_order = request.query_params.get("sort", "availability").strip()
    page_size = _positive_int(request.query_params.get("page_size", "20"), 20)
    page_size = page_size if page_size in {20, 50, 100} else 20
    requested_page = _positive_int(request.query_params.get("page", "1"), 1)
    if sort_order not in {"availability", "published_desc", "title", "case_id"}:
        sort_order = "availability"
    if repository_mode not in {"", "SINGLE_REPOSITORY", "MULTI_REPOSITORY"}:
        repository_mode = ""
    normalized_query = query.casefold()
    entries = [
        entry
        for entry in entries
        if (not readiness or entry["readiness"] == readiness)
        and (not repository_mode or entry["repository_mode"] == repository_mode)
        and (
            not normalized_query
            or normalized_query in str(entry["case_id"]).casefold()
            or normalized_query in str(entry["title"]).casefold()
            or any(normalized_query in str(repository_id).casefold() for repository_id in entry["repository_ids"])
        )
    ]
    if sort_order == "published_desc":
        entries.sort(key=lambda entry: (str(entry["published_at"]), str(entry["case_id"])), reverse=True)
    elif sort_order == "title":
        entries.sort(key=lambda entry: (str(entry["title"]).casefold(), str(entry["case_id"])))
    elif sort_order == "case_id":
        entries.sort(key=lambda entry: str(entry["case_id"]))
    else:
        entries.sort(
            key=lambda entry: (
                0 if entry["selectable"] else 1,
                str(entry["title"]).casefold(),
                str(entry["case_id"]),
            )
        )
    total_items = len(entries)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = min(requested_page, total_pages)
    offset = (page - 1) * page_size
    filters = {
        "q": query,
        "readiness": readiness,
        "sort": sort_order,
        "page_size": str(page_size),
    }
    return templates.TemplateResponse(
        request,
        "bench/cases.html",
        {
            "active_section": "framework_evaluation",
            "case_entries": entries[offset : offset + page_size],
            "case_count": total_items,
            "selectable_count": sum(bool(entry["selectable"]) for entry in entries),
            "filters": filters,
            "deleted_case_id": request.query_params.get("deleted", "").strip(),
            "page_size_options": (20, 50, 100),
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "first_item": offset + 1 if total_items else 0,
                "last_item": min(offset + page_size, total_items),
                "page_items": tuple(
                    None if item is None else {"page": item, "url": _cases_url(filters, item)}
                    for item in _pagination_items(page, total_pages)
                ),
                "previous_url": _cases_url(filters, page - 1) if page > 1 else None,
                "next_url": _cases_url(filters, page + 1) if page < total_pages else None,
            },
            "sidebar_items": _framework_sidebar("/framework-evaluation/cases"),
        },
    )


@router.get("/cases/{case_id}")
async def framework_evaluation_case_detail(request: Request, case_id: str) -> Response:
    store = _store(request)
    scenario, formally_verified_versions = await asyncio.gather(
        asyncio.to_thread(store.get, case_id),
        asyncio.to_thread(store.formally_verified_versions),
    )
    if scenario is None:
        raise HTTPException(status_code=404, detail="Case 不存在")
    detail = await asyncio.to_thread(
        scenario_case_detail_view,
        scenario,
        repositories=_scenario_repositories(),
    )
    if getattr(request.app.state, "demo_mode", False) and detail["case_kind"] != "REPOSITORY_CASE":
        detail.update(selectable=False, readiness="DEMO", readiness_label="合成示例 · 仅供查看")
    detail["formal_run_verified"] = (scenario.case_id, scenario.source_hash) in formally_verified_versions
    return templates.TemplateResponse(
        request,
        "bench/case_detail.html",
        {
            "active_section": "framework_evaluation",
            "detail": detail,
            "initial_request_html": render_markdown(str(detail["initial_request"])),
            "delete_error": request.query_params.get("delete_error", "").strip(),
            "sidebar_items": _framework_sidebar("/framework-evaluation/cases"),
        },
    )


@router.post("/cases/{case_id}/delete")
async def delete_framework_evaluation_case(request: Request, case_id: str) -> Response:
    form = await request.form()
    if str(form.get("confirmation") or "").strip() != case_id:
        raise HTTPException(status_code=422, detail="Case 删除确认不匹配")
    try:
        await asyncio.to_thread(_store(request).delete_current, case_id)
    except ValueError as exc:
        query = urlencode({"delete_error": str(exc)})
        return RedirectResponse(
            f"/framework-evaluation/cases/{case_id}?{query}",
            status_code=303,
        )
    return RedirectResponse(
        f"/framework-evaluation/cases?{urlencode({'deleted': case_id})}",
        status_code=303,
    )


@router.get("/rules")
async def framework_evaluation_rules(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "bench/rules.html",
        {
            "active_section": "framework_evaluation",
            "rulebook": current_evaluation_rulebook(),
            "score_anchor_rules": score_anchor_rules(),
            "sidebar_items": _framework_sidebar("/framework-evaluation/rules"),
        },
    )


@router.get("/resource-status")
async def framework_evaluation_resource_status(request: Request) -> Response:
    pools = await asyncio.to_thread(_resource_pool_views, _store(request))
    return templates.TemplateResponse(
        request,
        "bench/run_resource_status.html",
        {"resource_pools": pools, "resource_pool_signature": _poll_signature(pools)},
    )


@router.get("/api/summary")
async def framework_evaluation_summary(request: Request) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(_overview_metrics, _store(request)))


@router.get("/skill/download")
async def download_case_creator_skill(request: Request) -> Response:
    del request
    try:
        content = _case_creator_plugin_zip()
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail="Plugin 文件不存在") from exc
    return Response(
        content,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="helixharness-case-creator-plugin.zip"',
            "X-Content-Type-Options": "nosniff",
        },
    )
