"""共享依赖：roots 解析、模板引擎、单例管理器。

所有路由模块通过这里的 helper 获取引擎层的入口和共享配置，
避免在各路由里重复解析环境变量或创建多个管理器实例。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def format_beijing_datetime(value: object) -> str:
    """Format an ISO timestamp for the operator-facing Beijing timezone."""

    if value is None:
        return "—"
    raw_value = str(value).strip()
    if not raw_value:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return raw_value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def format_duration_ms(value: object) -> str:
    """Format milliseconds without implying a value when evidence is absent."""

    if value is None:
        return "—"
    milliseconds = max(0, int(value))
    if milliseconds < 1_000:
        return f"{milliseconds} 毫秒"
    seconds = milliseconds / 1_000
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes = int(seconds // 60)
    remaining_seconds = int(round(seconds - minutes * 60))
    return f"{minutes} 分 {remaining_seconds} 秒"


def format_byte_size(value: object) -> str:
    """Format byte counts as an operator-readable storage size."""

    if value is None:
        return "—"
    size = max(0, int(value))
    if size < 1_024:
        return f"{size} bytes"
    if size < 1_024**2:
        return f"{size / 1_024:.1f} KB"
    return f"{size / 1_024**2:.2f} MB"

# Jinja2 模板引擎（全局单例）
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["beijing_datetime"] = format_beijing_datetime
templates.env.filters["duration_ms"] = format_duration_ms
templates.env.filters["byte_size"] = format_byte_size


def get_roots(helixharness_roots: list[str] | None = None) -> list[str]:
    """解析 HelixHarness 项目根列表。

    优先使用传入的 roots，否则回退到 HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS 环境变量，
    没有配置时返回空列表，由数据源选择器在需要时生成隔离示例。
    """
    if helixharness_roots:
        return helixharness_roots
    roots_str = os.environ.get("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", "")
    roots = [r.strip() for r in roots_str.split(",") if r.strip()]
    if roots:
        return roots
    return []


class AppContext:
    """应用级共享上下文，持有只读数据源配置。

    路由通过 ``request.app.context`` 访问。
    """

    def __init__(self) -> None:
        self.helixharness_roots: list[str] = get_roots()
        self.usage_data_source: str = os.environ.get("HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE", "demo")

    def current_roots(self) -> list[str]:
        return self.helixharness_roots


def render_template(name: str, context: dict[str, Any]) -> str:
    """直接渲染模板为字符串（不经过 Request，用于 HTMX 片段响应）。

    Jinja2Templates 需要 Request 对象，这里直接用底层 Environment 渲染。
    """

    # 直接用 templates 的 env 渲染模板为字符串
    template = templates.env.get_template(name)
    return template.render(**context)
