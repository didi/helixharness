"""Explicit feedback receipt and operator-facing read-only views."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from web_ui.routes.eval import _render_page
from web_ui.routes.telemetry_report import _report_payload

router = APIRouter()
CATEGORIES = {"bug": "Bug", "docs_unclear": "文档不清楚", "process_stuck": "流程卡住",
              "suggestion": "建议", "uncategorized": "未分类"}


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


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_list(request: Request, category: str = "", page: int = 1):
    store = request.app.state.feedback_store
    category = category if category in CATEGORIES else ""
    total = store.count(category)
    pages = max(1, (total + 19) // 20)
    page = max(1, min(page, pages))
    return _render_page(request, "/feedback", "eval/feedback_list.html", {
        "rows": store.list_feedback(category=category, offset=(page - 1) * 20),
        "total": total, "page": page, "pages": pages, "category": category, "categories": CATEGORIES,
    })


@router.get("/feedback/detail/{feedback_id}", response_class=HTMLResponse)
async def feedback_detail(request: Request, feedback_id: str):
    item = request.app.state.feedback_store.get(feedback_id)
    if item is None:
        return HTMLResponse("未找到该反馈", status_code=404)
    return _render_page(request, "/feedback", "eval/feedback_detail.html", {
        "item": item, "document": item["document"], "categories": CATEGORIES,
        "fields": {"feedback_id": "反馈编号", "host": "编码助手", "plugin_version": "插件版本",
                   "os": "操作系统", "python_version": "Python 版本", "project": "匿名项目编号",
                   "session": "匿名会话编号", "feature": "关联需求", "git_head": "代码版本",
                   "related_entry_ids": "关联阶段编号"},
    })
