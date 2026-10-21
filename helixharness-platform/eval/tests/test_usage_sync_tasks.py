"""_BgTaskManager 后台任务进度与状态的单元测试。

覆盖「重新生成 AI 分析」进度条相关逻辑:
- progress 字段初始为空, update_progress 后可读
- status_html 运行中渲染 "已完成 N/总数 · 正在分析 xxx"
- status_html 运行中含 OOB 禁用按钮, 完成时恢复
- start() 防重入 (运行中再次 start 返回 False)
"""
from __future__ import annotations

import time
from threading import Event

from eval.usage_sync import _BgTaskManager


def _make_manager() -> _BgTaskManager:
    return _BgTaskManager()


def test_progress_initially_empty_and_update_reads_back() -> None:
    mgr = _make_manager()
    assert mgr.status("ai_regenerate").get("progress") == {}
    mgr._tasks["ai_regenerate"] = {  # 直接构造一个 running 任务便于断言
        "running": True,
        "started_at": "",
        "finished_at": "",
        "result_html": "",
        "progress": {"done": 0, "total": 0, "current": ""},
    }
    mgr.update_progress("ai_regenerate", 3, 7, "cdd-p1")
    st = mgr.status("ai_regenerate")
    assert st["progress"] == {"done": 3, "total": 7, "current": "cdd-p1"}


def test_status_html_running_shows_progress_with_current() -> None:
    mgr = _make_manager()
    mgr._tasks["ai_regenerate"] = {
        "running": True,
        "started_at": "",
        "finished_at": "",
        "result_html": "",
        "progress": {"done": 3, "total": 7, "current": "cdd-p1"},
    }
    html = mgr.status_html("ai_regenerate")
    assert "已完成 3/7" in html
    assert "正在分析 cdd-p1" in html
    # 轮询元素仍在 (刷新恢复后 HTMX 接管继续轮询)
    assert "hx-trigger=\"every 3s\"" in html
    # 运行中标记 (全局脚本据此禁用按钮)
    assert "helixharness-task-marker" in html
    assert 'data-task="ai_regenerate"' in html
    assert 'data-running="1"' in html


def test_status_html_running_without_progress_falls_back() -> None:
    mgr = _make_manager()
    mgr._tasks["ai_regenerate"] = {
        "running": True,
        "started_at": "",
        "finished_at": "",
        "result_html": "",
        "progress": {"done": 0, "total": 0, "current": ""},
    }
    html = mgr.status_html("ai_regenerate")
    assert "进行中" in html
    assert "已完成" not in html  # total=0 不显示 N/总数


def test_status_html_done_shows_result_and_enables_button() -> None:
    mgr = _make_manager()
    mgr._tasks["ai_regenerate"] = {
        "running": False,
        "started_at": "",
        "finished_at": "2026-07-23T16:00:00+08:00",
        "result_html": '<div class="soft-note ok">AI 分析已重新生成。</div>',
        "progress": {"done": 7, "total": 7, "current": ""},
    }
    html = mgr.status_html("ai_regenerate")
    assert "AI 分析已重新生成。" in html
    # 完成时无轮询元素
    assert "every 3s" not in html
    # 完成标记: 有 helixharness-task-marker 但无 data-running (全局脚本据此恢复按钮)
    assert "helixharness-task-marker" in html
    assert 'data-task="ai_regenerate"' in html
    assert 'data-running' not in html


def test_start_rejects_duplicate_while_running() -> None:
    mgr = _make_manager()

    def slow_fn(*_args):
        time.sleep(0.3)
        return ("<div>done</div>", "", "", "")

    first = mgr.start("ai_regenerate", slow_fn)
    assert first is True
    # 运行中再次启动同一个 task_id 应被拒绝
    second = mgr.start("ai_regenerate", slow_fn)
    assert second is False
    # 等待后台线程完成, 状态翻转为完成
    time.sleep(0.5)
    assert mgr.status("ai_regenerate")["running"] is False


def test_start_coalesces_one_rerun_when_requested_during_execution() -> None:
    mgr = _make_manager()
    first_started = Event()
    release_first = Event()
    calls: list[str] = []

    def slow_fn(label: str):
        calls.append(label)
        if len(calls) == 1:
            first_started.set()
            release_first.wait(timeout=1)
        return (f"<div>{label}</div>", "", "", "")

    assert mgr.start("ai_regenerate", slow_fn, "first", rerun_if_running=True) is True
    assert first_started.wait(timeout=1)
    assert mgr.start("ai_regenerate", slow_fn, "latest", rerun_if_running=True) is False
    release_first.set()

    deadline = time.time() + 2
    while mgr.status("ai_regenerate")["running"] and time.time() < deadline:
        time.sleep(0.01)

    assert calls == ["first", "latest"]
    assert mgr.status("ai_regenerate")["running"] is False


def test_start_with_progress_callback_updates_progress() -> None:
    """模拟 generate_usage_ai_insights 的 progress_cb 桥接:
    后台线程通过 update_progress 推进, 前端轮询 status 能读到推进。
    """
    mgr = _make_manager()

    def fn_with_progress(*_args):
        # 模拟 3 个单元
        for i, label in enumerate(["f1", "f2", "team"], start=1):
            mgr.update_progress("ai_regenerate", i, 3, label)
            time.sleep(0.05)
        return ("<div>ok</div>", "", "", "")

    mgr.start("ai_regenerate", fn_with_progress)
    # 等一下让线程跑起来, 读中间进度
    time.sleep(0.08)
    st = mgr.status("ai_regenerate")
    assert st["running"] is True
    # 至少推进过一次 (done >= 1)
    assert int(st["progress"]["done"]) >= 1
    assert int(st["progress"]["total"]) == 3
    time.sleep(0.3)
    assert mgr.status("ai_regenerate")["running"] is False
