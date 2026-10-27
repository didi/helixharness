"""Explicit feedback receipt and operator-facing read-only views."""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from web_ui.deps import get_roots
from web_ui.routes.eval import _render_page
from web_ui.routes.telemetry_report import _report_payload

router = APIRouter()
CATEGORIES = {"bug": "Bug", "docs_unclear": "文档不清楚", "process_stuck": "流程卡住",
              "suggestion": "建议", "uncategorized": "未分类"}

_FEEDBACK_PAGE_SIZE = 20
_FEEDBACK_PAGE_SIZES = (10, 20, 50, 100)


@router.post("/feedback/report")
async def report(request: Request) -> JSONResponse:
    try:
        payload = await _report_payload(request)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) > 100:
        return JSONResponse({"detail": "items must be an array of at most 100 entries"}, status_code=400)
    acknowledgements = []
    for item in items:
        identifier = str(item.get("feedback_id") or "") if isinstance(item, dict) else ""
        try:
            result = request.app.state.feedback_store.ingest(item)
        except sqlite3.Error:
            result = {"status": "retryable", "error": {"code": "storage_unavailable"}}
        acknowledgements.append({"feedback_id": identifier, **result})
    return JSONResponse({"items": acknowledgements})


def _feedback_filters(
    category: str | None,
    project: str | None,
    plugin_version: str | None,
    feature: str | None,
) -> dict:
    return {
        key: value
        for key, value in {
            "category": category,
            "project": project,
            "plugin_version": plugin_version,
            "feature": feature,
        }.items()
        if value
    }


def _filter_query(filters: dict) -> str:
    """Filters-only query string (no page) for pagination links."""
    params = {key: value for key, value in filters.items() if value}
    return ("?" + urlencode(params)) if params else ""


def _normalized_page_size(value: str | int) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return _FEEDBACK_PAGE_SIZE
    return size if size in _FEEDBACK_PAGE_SIZES else _FEEDBACK_PAGE_SIZE


def _page_window(page: int, total_pages: int) -> list:
    """Page numbers with ellipsis markers, e.g. [1, '…', 4, 5, 6, '…', 12]."""
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    keep = {1, 2, total_pages - 1, total_pages, page - 1, page, page + 1}
    keep = {item for item in keep if 1 <= item <= total_pages}
    window: list = []
    previous = 0
    for item in sorted(keep):
        if item - previous > 1:
            window.append("…")
        window.append(item)
        previous = item
    return window


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_list(
    request: Request,
    category: str = "",
    project: str = "",
    plugin_version: str = "",
    feature: str = "",
    page: int = 1,
    page_size: str = "",
):
    store = request.app.state.feedback_store
    filters = _feedback_filters(
        category if category in CATEGORIES else "",
        project, plugin_version, feature,
    )
    size = _normalized_page_size(page_size or _FEEDBACK_PAGE_SIZE)
    total = store.count(**filters)
    total_pages = max(1, (total + size - 1) // size)
    page = max(1, min(page, total_pages))
    return _render_page(request, "/feedback", "eval/feedback_list.html", {
        "rows": store.list_feedback(**filters, limit=size, offset=(page - 1) * size),
        "stats": store.stats(),
        "filters": filters,
        "base_query": _filter_query(filters),
        "category_labels": CATEGORIES,
        "page": page,
        "page_size": size,
        "page_sizes": _FEEDBACK_PAGE_SIZES,
        "pages": _page_window(page, total_pages),
        "total": total,
    })


@router.get("/feedback/partial/table", response_class=HTMLResponse)
async def feedback_table_partial(
    request: Request,
    category: str = "",
    project: str = "",
    plugin_version: str = "",
    feature: str = "",
    page: int = 1,
    page_size: str = "",
):
    """HTMX 片段：过滤/翻页/换每页条数只刷新表格区。"""
    store = request.app.state.feedback_store
    filters = _feedback_filters(
        category if category in CATEGORIES else "",
        project, plugin_version, feature,
    )
    size = _normalized_page_size(page_size or _FEEDBACK_PAGE_SIZE)
    total = store.count(**filters)
    total_pages = max(1, (total + size - 1) // size)
    page = max(1, min(page, total_pages))
    return _render_page(request, "/feedback", "eval/feedback_table.html", {
        "rows": store.list_feedback(**filters, limit=size, offset=(page - 1) * size),
        "filters": filters,
        "base_query": _filter_query(filters),
        "category_labels": CATEGORIES,
        "page": page,
        "page_size": size,
        "page_sizes": _FEEDBACK_PAGE_SIZES,
        "pages": _page_window(page, total_pages),
        "total": total,
    })


def _parse_transcript(markdown: str) -> list[dict]:
    """Parse the transcript line format ("## HH:MM:SS user|assistant" groups)."""
    header = re.compile(r"^## (\d{2}:\d{2}:\d{2}) (user|assistant)$")
    groups: list[dict] = []
    for line in (markdown or "").splitlines():
        matched = header.match(line)
        if matched:
            groups.append({"time": matched.group(1), "role": matched.group(2), "lines": []})
            continue
        if not groups:
            continue
        groups[-1]["lines"].append(line)
    return [
        {**group, "lines": [line for line in group["lines"] if line.strip()]}
        for group in groups
    ]


def _related_workflow_entries(request: Request, document: dict) -> tuple[list[dict], str | None]:
    """按 related_entry_ids → feature → session 三级回退，从 usage 账本找关联流程。

    复用看板总览的 usage_loader 账本解析层；未配置数据源或解析失败时返回空关联，
    页面展示空态，不影响详情其余部分。
    """
    related_ids = {str(value) for value in (document.get("related_entry_ids") or [])}
    feature = str(document.get("feature") or "")
    session = str(document.get("session") or "")
    if not related_ids and not feature and session in ("", "unknown"):
        return [], None
    try:
        from eval.usage_loader import load_usage_dashboard_data
        from eval.usage_sync import _current_usage_roots

        data = load_usage_dashboard_data(_current_usage_roots(get_roots()))
    except Exception:
        return [], None
    exact: list[dict] = []
    by_feature: list[dict] = []
    by_session: list[dict] = []
    for project in data.projects:
        for log in project.get("features", []):
            for entry in log.get("entries") or []:
                record = {
                    "entry_id": entry.get("entry_id"),
                    "command": entry.get("command"),
                    "phase": entry.get("phase"),
                    "stage_kind": entry.get("stage_kind"),
                    "status": entry.get("status"),
                    "started_at": entry.get("started_at"),
                    "ended_at": entry.get("ended_at"),
                }
                if related_ids and record["entry_id"] in related_ids:
                    exact.append(record)
                if feature and log.get("feature") == feature:
                    by_feature.append(record)
                if session and session != "unknown" and entry.get("session_hash") == session:
                    by_session.append(record)
    if exact:
        return exact, "related_entry_ids"
    if by_feature:
        return by_feature, "feature"
    if by_session:
        return by_session, "session"
    return [], None


@router.get("/feedback/detail/{feedback_id}", response_class=HTMLResponse)
async def feedback_detail(request: Request, feedback_id: str):
    """反馈详情页：完整字段 + 关联工作流时间线（usage 账本 join）+ 会话上下文（实时上报）。"""
    item = request.app.state.feedback_store.get(feedback_id)
    if item is None:
        return HTMLResponse("未找到该反馈（可能已被删除或未同步）", status_code=404)
    document = item["document"]
    entries, association = _related_workflow_entries(request, document)
    row = {key: document.get(key) for key in (
        "feedback_id", "created_at", "category", "text", "feature", "session",
        "project", "plugin_version", "host", "os", "python_version", "git_head", "user_name",
    )}
    row["source_root"] = "realtime"
    return _render_page(request, "/feedback", "eval/feedback_detail.html", {
        "row": row,
        "entries": entries,
        "association": association,
        "transcript_groups": _parse_transcript(item.get("transcript_md") or ""),
        "transcript_stats": item.get("transcript_stats") or {},
        "category_labels": CATEGORIES,
    })
