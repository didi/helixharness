"""数据看板路由：复用现有 usage_loader 纯函数。

数据源：
- demo 模式 → 内置 sample_usage/
- sync / auto 模式 → 读取 eval/.cache/roots.txt（Git 同步缓存）
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web_ui.deps import get_roots, render_template, templates

if TYPE_CHECKING:
    from eval.usage_loader import UsageDashboardData

router = APIRouter()

_SIDEBAR_ITEMS = [
    ("数据看板", [
        ("/", "看板总览", True),
        ("/config", "看板配置", False),
        ("/feedback", "问题反馈", False),
    ]),
]


def _sidebar(active: str):
    result = []
    for group_title, items in _SIDEBAR_ITEMS:
        marked = [(path, label, path == active) for path, label, _ in items]
        result.append((group_title, marked))
    return result


def _resolve_roots() -> list[str]:
    """解析数据源：复用旧 app.py 的 _current_usage_roots 逻辑。

    - demo 模式 → 内置 sample_usage/
    - sync / auto 模式 → 读 eval/.cache/roots.txt（同步缓存）
    - auto 且无缓存 → 回退 demo
    """
    from eval.usage_sync import _current_usage_roots

    return _current_usage_roots(get_roots())


def _render_page(request: Request, active_path: str, template_name: str, extra: dict | None = None):
    """通用页面渲染：HTMX 请求返回片段，否则返回完整 base.html。"""
    ctx = {
        "sidebar_items": _sidebar(active_path),
        "active_section": "dashboard",
    }
    if extra:
        ctx.update(extra)
    html = render_template(template_name, ctx)
    if request.headers.get("HX-Request"):
        return HTMLResponse(html)
    return templates.TemplateResponse(request, "base.html", {
        "sidebar_items": _sidebar(active_path),
        "active_section": "dashboard",
        "content_html": html,
    })


def _resolve_view_mode(view: str) -> str:
    """将英文 view 参数映射为 usage_loader 期望的中文 view_mode。"""
    return "按用户维度" if view == "user" else "按需求维度"


def _evolution_entry_context(request: Request) -> dict[str, int | str] | None:
    """Expose only Evolution records originating from uploaded telemetry."""

    store = request.app.state.evolution_store
    source_counts = {
        "eligible_eval": 0,
    }
    for row in store.list_hypotheses():
        payload = _json_object(row.get("payload_json"))
        if not _is_data_report_hypothesis(payload):
            continue
        source_counts["eligible_eval"] += 1
    hypothesis_count = sum(source_counts.values())
    experiment_count = sum(
        _is_data_report_hypothesis(_json_object(row.get("hypothesis_json")))
        for row in store.list_experiments()
    )
    if hypothesis_count <= 0 and experiment_count <= 0:
        return None
    return {
        "hypothesis_count": hypothesis_count,
        "experiment_count": experiment_count,
        **source_counts,
        "href": (
            "/evolution/hypotheses?source=EVAL"
            if hypothesis_count > 0
            else "/evolution/experiments?source=EVAL"
        ),
    }


def _json_object(value: object) -> dict:
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _is_data_report_hypothesis(payload: dict) -> bool:
    return (payload.get("source_kind") == "EVAL_LOG" or payload.get("semantic_source") in {"MODEL", "RULE"}) and not (
        str(payload.get("source_kind") or "") == "BENCH_RUN"
        or str(payload.get("semantic_source") or "") in {"BENCH_REMEDIATION", "PARTIAL_REHEARSAL"}
    )


def _refresh_quality_gate(request: Request, roots: list[str]) -> tuple[set[str], list, UsageDashboardData]:
    """Refresh fixed/async linkage before rebuilding the consumer Gate."""

    from eval import usage_sync
    from eval.usage_first_seen import UsageFirstSeenStore
    from eval.usage_loader import load_usage_dashboard_data

    store = request.app.state.telemetry_quality_store
    if not hasattr(request.app.state, "usage_first_seen_store"):
        request.app.state.usage_first_seen_store = UsageFirstSeenStore(store.path.parent)
    admissions = request.app.state.usage_first_seen_store
    supplement_store = request.app.state.stage_supplement_store
    if usage_sync.is_demo_roots(roots):
        from eval.stage_supplements import StageSupplementStore
        from eval.telemetry_quality import TelemetryQualityStore

        directory = usage_sync.USAGE_SYNC_CACHE / ".demo"
        store = TelemetryQualityStore(directory / "telemetry.db")
        supplement_store = StageSupplementStore(directory / "stage_supplements.db")
        admissions = UsageFirstSeenStore(directory, database_path=directory / "usage_first_seen.db")
        request.state.usage_demo = True
    request.state.usage_first_seen_store = admissions
    admissions.bootstrap(roots, supplement_store.path)
    supplement_store.refresh_links(roots)
    linked_supplements = supplement_store.active_linked_records()
    store.refresh(roots, linked_supplements)
    rejected = store.invalid_source_paths()
    dashboard_data = load_usage_dashboard_data(roots, rejected, linked_supplements)
    admissions.observe(dashboard_data.aggregate["features"])
    return rejected, linked_supplements, dashboard_data


@router.get("/", response_class=HTMLResponse)
async def dashboard_overview(
    request: Request, view: str = "project", runtime_period: str = "all",
    runtime_start: str = "", runtime_end: str = "", recent_days: int = 1,
):
    """数据看板总览：demo 数据源，复用 usage_loader 渲染。"""
    from eval.usage_loader import (
        render_usage_global_overview_html,
        render_usage_overview_html,
    )

    roots = _resolve_roots()
    rejected_paths, linked_supplements, dashboard_data = _refresh_quality_gate(request, roots)
    global_html = render_usage_global_overview_html(
        roots, rejected_paths, linked_supplements,
        runtime_period=runtime_period, runtime_start=runtime_start, runtime_end=runtime_end,
        runtime_expanded="runtime_period" in request.query_params, view=view, recent_days=recent_days,
        first_seen=request.state.usage_first_seen_store.snapshot(), dashboard_data=dashboard_data,
    )
    detail_html = render_usage_overview_html(
        roots, _resolve_view_mode(view), rejected_paths, linked_supplements, dashboard_data=dashboard_data
    )

    return _render_page(request, "/", "eval/overview.html", {
        "global_html": global_html,
        "detail_html": detail_html,
        "view_mode": view,
        "recent_days": recent_days if recent_days in {1, 3, 7, 30} else 1,
        "runtime_period": runtime_period,
        "runtime_start": runtime_start,
        "runtime_end": runtime_end,
        "evolution_entry": _evolution_entry_context(request),
    })


@router.get("/partial/runtime", response_class=HTMLResponse)
async def dashboard_runtime_partial(
    request: Request, runtime_period: str = "all", runtime_start: str = "", runtime_end: str = "",
    runtime_expanded: bool = False, view: str = "project", recent_days: int = 1,
):
    """Refresh stage duration and version statistics while preserving disclosure state."""
    from eval.usage_loader import render_runtime_overview_html

    roots = _resolve_roots()
    rejected_paths, linked_supplements, _ = _refresh_quality_gate(request, roots)
    return HTMLResponse(render_runtime_overview_html(
        roots, rejected_paths, linked_supplements,
        period=runtime_period, start=runtime_start, end=runtime_end, expanded=runtime_expanded, view=view, recent_days=recent_days,
    ))


@router.get("/partial/detail", response_class=HTMLResponse)
async def dashboard_detail_partial(
    request: Request, view: str = "project",
    runtime_period: str = "all", runtime_start: str = "", runtime_end: str = "", recent_days: int = 1,
):
    """维度切换的 HTMX 片段：只返回展示维度标题 + 明细表，不整页刷新。"""
    from eval.usage_loader import render_usage_overview_html

    roots = _resolve_roots()
    rejected_paths, linked_supplements, dashboard_data = _refresh_quality_gate(request, roots)
    detail_html = render_usage_overview_html(
        roots, _resolve_view_mode(view), rejected_paths, linked_supplements, dashboard_data=dashboard_data
    )
    return HTMLResponse(render_template("eval/detail_section.html", {
        "detail_html": detail_html,
        "view_mode": view,
        "recent_days": recent_days if recent_days in {1, 3, 7, 30} else 1,
        "runtime_period": runtime_period,
        "runtime_start": runtime_start,
        "runtime_end": runtime_end,
    }))


def _repo_list() -> list[dict]:
    """读取已配置的仓库列表（供模板渲染）。"""
    from pathlib import Path

    from eval.cli.sync_helixharness_usage import _eval_inventory
    from eval.feature_exclusions import FeatureExclusionStore
    from eval.usage_sync import _load_usage_sync_config, _load_usage_sync_state, _usage_repo_name

    cfg = _load_usage_sync_config()
    state = _load_usage_sync_state()
    state_repos = state.get("repos", {}) if isinstance(state.get("repos"), dict) else {}
    repos = [r for r in cfg.get("repos", []) if isinstance(r, dict)]
    exclusions = FeatureExclusionStore().active_pairs()
    _sync_map = {"cloned": "已克隆", "updated": "有更新", "unchanged": "已是最新"}
    result = []
    for repo in repos:
        name = str(repo.get("name") or _usage_repo_name(str(repo.get("url") or "")))
        st = state_repos.get(name, {}) if isinstance(state_repos.get(name), dict) else {}
        inventory = None
        cached_path_value = str(st.get("path") or "").strip()
        cached_path = Path(cached_path_value) if cached_path_value else None
        if cached_path is not None and cached_path.is_dir():
            inventory = _eval_inventory(
                cached_path,
                str(repo.get("eval_dir") or "docs/eval"),
                repository=name,
                excluded_features=exclusions,
            )
        eval_count = int((inventory or st).get("eval_log_count", 0) or 0)
        invalid_count = int((inventory or st).get("invalid_log_count", 0) or 0)
        valid_count = (
            int((inventory or st).get("valid_log_count", 0) or 0)
            if "valid_log_count" in (inventory or st)
            else max(eval_count - invalid_count, 0)
        )
        result.append({
            "name": name,
            "url": str(repo.get("url") or ""),
            "branch": str(repo.get("branch") or "master"),
            "eval_dir": str(repo.get("eval_dir") or "docs/eval"),
            "enabled": bool(repo.get("enabled", True)),
            "sync_status": _sync_map.get(str(st.get("status") or ""), "未同步"),
            "sync_cls": str(st.get("status") or "none"),
            "last_commit": str(st.get("last_commit") or "")[:12] or "—",
            "last_commit_author": str(st.get("last_commit_author") or ""),
            "last_checked": str(st.get("last_checked_at") or "—"),
            "eval_count": eval_count,
            "valid_eval_count": valid_count,
            "invalid_count": invalid_count,
        })
    return result


def _repo_table_context(*, flash: str = "") -> dict:
    """Build repository-table metrics from the current per-repository sync inventory."""

    repos = _repo_list()
    eval_repo_count = sum(1 for repo in repos if repo["valid_eval_count"] > 0)
    context = {
        "repos": repos,
        "last_run": _last_run_summary(),
        "eval_summary": {
            "repo_count": eval_repo_count,
            "valid_count": sum(repo["valid_eval_count"] for repo in repos),
            "invalid_count": sum(repo["invalid_count"] for repo in repos),
        },
    }
    if flash:
        context["flash"] = flash
    return context


def _last_run_summary() -> dict:
    """读取最近一次同步运行的汇总结果（来自 sync_state.json 的 last_run）。"""
    from eval.usage_sync import _load_usage_sync_state

    state = _load_usage_sync_state()
    lr = state.get("last_run") if isinstance(state.get("last_run"), dict) else {}
    return {
        "finished_at": str(lr.get("finished_at") or ""),
        "started_at": str(lr.get("started_at") or ""),
        "total": int(lr.get("total") or 0),
        "succeeded": int(lr.get("succeeded") or 0),
        "updated": int(lr.get("updated") or 0),
        "cloned": int(lr.get("cloned") or 0),
        "unchanged": int(lr.get("unchanged") or 0),
        "with_eval": int(lr.get("with_eval") or 0),
        "failed": int(lr.get("failed") or 0),
        "has_data": bool(lr),
    }


def _cron_value() -> str:
    from eval.usage_sync import _usage_cron_value

    return _usage_cron_value()


@router.get("/config", response_class=HTMLResponse)
async def dashboard_config(request: Request):
    """数据看板配置页：仓库管理 + 同步策略 + 立即同步 + AI 重生成。"""
    # 刷新恢复：若有后台任务正在运行，把其进度状态 HTML 注入 #action-status，
    # HTMX 接管后自动继续轮询，用户刷新页面也能看到进度。
    from eval.usage_sync import _TASKS

    action_status_html = ""
    for tid in ("sync", "ai_regenerate"):
        if _TASKS.status(tid).get("running"):
            action_status_html = _TASKS.status_html(tid)
            break
    context = _repo_table_context()
    context.update({
        "cron_value": _cron_value(),
        "action_status_html": action_status_html,
    })
    return _render_page(request, "/config", "eval/config.html", context)


@router.get("/config/repo-table", response_class=HTMLResponse)
async def config_repo_table():
    """HTMX 片段：刷新仓库表（增删改后 swap 回来）。"""
    return HTMLResponse(render_template("eval/config_repo_table.html", _repo_table_context()))


@router.post("/config/add-repo", response_class=HTMLResponse)
async def config_add_repo(request: Request):
    """新增/更新项目库。"""
    from eval.usage_sync import (
        _load_usage_sync_config,
        _save_usage_sync_config,
        _usage_repo_name,
    )

    form = await request.form()
    name = (form.get("name") or "").strip()
    url = (form.get("url") or "").strip()
    branch = (form.get("branch") or "master").strip()
    eval_dir = (form.get("eval_dir") or "docs/eval").strip().strip("/")
    enabled = form.get("enabled") in ("true", "on", "1", "yes")

    msg = ""
    if not url:
        msg = "请先填写 Git 仓库地址。"
    else:
        name = name or _usage_repo_name(url)
        cfg = _load_usage_sync_config()
        repos = [r for r in cfg.get("repos", []) if isinstance(r, dict)]
        repo = {"name": name, "url": url, "branch": branch, "eval_dir": eval_dir, "enabled": enabled}
        replaced = False
        for idx, existing in enumerate(repos):
            if existing.get("name") == name or existing.get("url") == url:
                repos[idx] = repo
                replaced = True
                break
        if not replaced:
            repos.append(repo)
        cfg["repos"] = repos
        cfg.setdefault("schedule", {"cron": "0 * * * *", "enabled": False})
        _save_usage_sync_config(cfg)
        msg = f"已保存项目库 {name}。"

    return HTMLResponse(render_template("eval/config_repo_table.html", _repo_table_context(flash=msg)))


@router.post("/config/delete-repo", response_class=HTMLResponse)
async def config_delete_repo(request: Request):
    """删除项目库。"""
    from eval.usage_sync import (
        _find_usage_repo,
        _load_usage_sync_config,
        _save_usage_sync_config,
    )

    form = await request.form()
    name = (form.get("name") or "").strip()
    idx, _ = _find_usage_repo(name)
    msg = ""
    if idx is not None:
        cfg = _load_usage_sync_config()
        repos = [r for r in cfg.get("repos", []) if isinstance(r, dict)]
        repos.pop(idx)
        cfg["repos"] = repos
        _save_usage_sync_config(cfg)
        msg = f"已删除项目库 {name}。"
    else:
        msg = f"未找到项目库 {name}。"

    return HTMLResponse(render_template("eval/config_repo_table.html", _repo_table_context(flash=msg)))


@router.post("/config/save-cron", response_class=HTMLResponse)
async def config_save_cron(request: Request):
    """保存并安装全局同步计划。"""
    from eval.usage_sync import _install_usage_cron_with_summary

    form = await request.form()
    cron_expr = (form.get("cron") or "0 * * * *").strip()
    _, status_html = _install_usage_cron_with_summary(cron_expr)
    return HTMLResponse(status_html)


@router.post("/config/sync", response_class=HTMLResponse)
async def config_sync(request: Request):
    """立即同步：后台执行 sync 脚本，立即返回轮询状态（不阻塞）。"""
    from eval.usage_sync import _TASKS, _run_usage_sync

    started = _TASKS.start("sync", _run_usage_sync, "按需求维度", get_roots())
    return HTMLResponse(_TASKS.status_html("sync", just_started=started))


@router.post("/config/ai-regenerate", response_class=HTMLResponse)
async def config_ai_regenerate(request: Request):
    """重新生成 AI 分析：后台执行，立即返回轮询状态（不阻塞）。"""
    from eval.usage_sync import _TASKS, _run_usage_ai_regenerate, _run_usage_ai_team_regenerate

    runner = _run_usage_ai_team_regenerate if request.query_params.get("scope") == "team" else _run_usage_ai_regenerate
    started = _TASKS.start("ai_regenerate", runner, "按需求维度", get_roots())
    return HTMLResponse(_TASKS.status_html("ai_regenerate", just_started=started))


@router.get("/config/task-status/{task_id}", response_class=HTMLResponse)
async def config_task_status(task_id: str):
    """轮询后台任务状态（HTMX 每 3s 调用一次，完成后自动停止）。"""
    from eval.usage_sync import _TASKS

    return HTMLResponse(_TASKS.status_html(task_id))
