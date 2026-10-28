"""Git 数据源同步管理 —— 从 app.py 抽取的纯函数（配置读写/同步/AI重生成/cron）"""
from __future__ import annotations

import html
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml

from eval.usage_loader import render_usage_global_overview_html, render_usage_overview_html

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USAGE_SYNC_CONFIG = Path(os.environ.get("HELIXHARNESS_PLATFORM_USAGE_SYNC_CONFIG", str(PROJECT_ROOT / "eval/configs/usage_repos.yaml")))
USAGE_SYNC_CACHE = Path(os.environ.get("HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE", str(PROJECT_ROOT / "eval/.cache")))
USAGE_SYNC_STATE = USAGE_SYNC_CACHE / "sync_state.json"
USAGE_SYNC_SCRIPT = PROJECT_ROOT / "eval" / "cli" / "sync_helixharness_usage.py"
USAGE_CRON_BEGIN = "# BEGIN HelixHarness Observatory usage sync"
USAGE_CRON_END = "# END HelixHarness Observatory usage sync"


def _default_helixharness_roots() -> list[str]:
    """Generate the dashboard fallback without requiring shipped runtime files."""
    from eval.demo import ensure_usage_demo

    return [str(ensure_usage_demo(USAGE_SYNC_CACHE / ".demo"))]


def is_demo_roots(roots: list[str]) -> bool:
    """Identify the isolated generated fallback, never a user-configured project."""
    return roots == [str(USAGE_SYNC_CACHE / ".demo" / "project")]


def _usage_repo_name(url: str) -> str:
    value = (url or "").strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    value = value.split("/")[-1] if value else "repo"
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-") or "repo"


def _load_usage_sync_config() -> dict:
    if not USAGE_SYNC_CONFIG.exists():
        return {
            "schedule": {"cron": "0 * * * *", "enabled": False},
            "repos": [],
        }
    try:
        with USAGE_SYNC_CONFIG.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {"schedule": {"cron": "0 * * * *", "enabled": False}, "repos": []}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schedule", {"cron": "0 * * * *", "enabled": False})
    data.setdefault("repos", [])
    return data


def _load_usage_sync_state() -> dict:
    if not USAGE_SYNC_STATE.exists():
        return {"repos": {}}
    try:
        with USAGE_SYNC_STATE.open(encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return {"repos": {}}
    if not isinstance(data, dict):
        return {"repos": {}}
    data.setdefault("repos", {})
    return data


def _save_usage_sync_config(data: dict) -> None:
    USAGE_SYNC_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_SYNC_CONFIG.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _usage_repo_choices() -> list[str]:
    cfg = _load_usage_sync_config()
    repos = [r for r in cfg.get("repos", []) if isinstance(r, dict)]
    return [str(r.get("name") or _usage_repo_name(str(r.get("url") or ""))) for r in repos]


def _find_usage_repo(choice: str) -> tuple[int | None, dict]:
    cfg = _load_usage_sync_config()
    repos = [r for r in cfg.get("repos", []) if isinstance(r, dict)]
    for idx, repo in enumerate(repos):
        name = str(repo.get("name") or _usage_repo_name(str(repo.get("url") or "")))
        if name == choice:
            return idx, repo
    return None, {}


def _current_usage_roots(default_roots: list[str], data_source: str | None = None) -> list[str]:
    source = (data_source or os.environ.get("HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE") or "auto").strip().lower()
    if source == "demo":
        return default_roots or _default_helixharness_roots()

    roots_file = USAGE_SYNC_CACHE / "roots.txt"
    if roots_file.exists():
        roots = [r.strip() for r in roots_file.read_text(encoding="utf-8").split(",") if r.strip()]
        roots = [r for r in roots if os.path.isdir(r)]
        return roots
    if source == "sync":
        return []

    cfg = _load_usage_sync_config()
    repos = [r for r in cfg.get("repos", []) if isinstance(r, dict)]
    if repos:
        return []
    return default_roots or _default_helixharness_roots()


def _usage_html_pair(default_roots: list[str], view_mode: str) -> tuple[str, str]:
    roots = _current_usage_roots(default_roots)
    return render_usage_global_overview_html(roots), render_usage_overview_html(roots, view_mode)


def _usage_payload_script(payload: dict, confirm_text: str | None = None) -> str:
    confirm = ""
    if confirm_text:
        confirm = f"if(!window.confirm({json.dumps(confirm_text, ensure_ascii=False)})){{return;}}"
    payload_json = json.dumps(payload, ensure_ascii=False)
    script = (
        "(()=>{"
        f"{confirm}"
        f"const payload={payload_json};"
        "payload.nonce=Date.now();"
        "const el=document.querySelector('#usage-repo-action-payload textarea,#usage-repo-action-payload input');"
        "if(!el){return;}"
        "el.value=JSON.stringify(payload);"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "})()"
    )
    return html.escape(script, quote=True)


def _usage_row_action_button(action: str, name: str, label: str, css_class: str, row_key: str | None = None) -> str:
    if action == "edit" and row_key:
        script = (
            "(()=>{"
            f"const row=document.getElementById({json.dumps(row_key)});"
            "if(!row){return;}"
            "row.classList.add('is-editing');"
            "const first=row.querySelector('[data-usage-field=\"name\"]');"
            "if(first){first.focus(); first.select();}"
            "})()"
        )
        escaped_script = html.escape(script, quote=True)
    elif action == "cancel" and row_key:
        script = (
            "(()=>{"
            f"const row=document.getElementById({json.dumps(row_key)});"
            "if(row){row.classList.remove('is-editing');}"
            "})()"
        )
        escaped_script = html.escape(script, quote=True)
    else:
        escaped_script = _usage_payload_script(
            {"action": action, "choice": name},
            f"确认删除项目库 {name} 吗？同步配置会立即更新。" if action == "delete" else None,
        )
    return (
        f'<button type="button" class="usage-row-action {css_class}" '
        f'onclick="{escaped_script}">{html.escape(label)}</button>'
    )


def _usage_inline_save_button(row_key: str, choice: str) -> str:
    script = (
        "(()=>{"
        f"const row=document.getElementById({json.dumps(row_key)});"
        "if(!row){return;}"
        "const get=(field)=>{const el=row.querySelector(`[data-usage-field=\"${field}\"]`); return el ? el.value : '';};"
        "const enabled=row.querySelector('[data-usage-field=\"enabled\"]');"
        "const payload={"
        "\"action\":\"save_inline\","
        f"\"choice\":{json.dumps(choice, ensure_ascii=False)},"
        "\"name\":get('name'),"
        "\"url\":get('url'),"
        "\"branch\":get('branch'),"
        "\"eval_dir\":get('eval_dir'),"
        "\"enabled\":enabled ? enabled.checked : true,"
        "\"nonce\":Date.now()"
        "};"
        "const el=document.querySelector('#usage-repo-action-payload textarea,#usage-repo-action-payload input');"
        "if(!el){return;}"
        "el.value=JSON.stringify(payload);"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "})()"
    )
    return (
        '<button type="button" class="usage-row-action save" '
        f'onclick="{html.escape(script, quote=True)}">保存</button>'
    )


def _usage_sync_summary_html() -> str:
    cfg = _load_usage_sync_config()
    state = _load_usage_sync_state()
    state_repos = state.get("repos", {}) if isinstance(state.get("repos"), dict) else {}
    repos = [r for r in cfg.get("repos", []) if isinstance(r, dict)]
    enabled_repos = [r for r in repos if r.get("enabled", True)]
    cron = (cfg.get("schedule") or {}).get("cron", "0 * * * *")
    rows = ""
    for idx, repo in enumerate(repos[:8]):
        name = str(repo.get("name") or _usage_repo_name(repo.get("url", "")))
        url = str(repo.get("url") or "")
        branch = str(repo.get("branch") or "master")
        eval_dir = str(repo.get("eval_dir") or "docs/eval")
        repo_enabled = bool(repo.get("enabled", True))
        row_key = f"usage-repo-row-{idx}"
        st = state_repos.get(name, {}) if isinstance(state_repos.get(name), dict) else {}
        status = "启用" if repo_enabled else "停用"
        sync_status_map = {
            "cloned": "已克隆",
            "updated": "有更新",
            "unchanged": "已是最新",
        }
        sync_status = sync_status_map.get(str(st.get("status") or ""), "未同步")
        commit = str(st.get("last_commit") or "")
        commit_label = commit[:12] if commit else "-"
        checked_at = str(st.get("last_checked_at") or "-")
        eval_count = int(st.get("eval_log_count", 0) or 0)
        invalid_count = int(st.get("invalid_log_count", 0) or 0)
        valid_count = (
            int(st.get("valid_log_count", 0) or 0)
            if "valid_log_count" in st
            else max(eval_count - invalid_count, 0)
        )
        schema_note = f"有效 {valid_count}/{eval_count}"
        if invalid_count:
            schema_note += f" · 无效 {invalid_count}"
        edit_button = _usage_row_action_button("edit", name, "修改", "edit", row_key)
        delete_button = _usage_row_action_button("delete", name, "删除", "delete")
        save_button = _usage_inline_save_button(row_key, name)
        cancel_button = _usage_row_action_button("cancel", name, "取消", "cancel", row_key)
        checked_attr = "checked" if repo_enabled else ""
        rows += (
            f'<tr id="{html.escape(row_key)}" class="usage-repo-row">'
            "<td>"
            f'<div class="usage-view"><b>{html.escape(name)}</b><br><code>{html.escape(url)}</code></div>'
            '<div class="usage-edit-stack">'
            f'<label>仓库名称<input data-usage-field="name" value="{html.escape(name, quote=True)}"></label>'
            f'<label>Git 地址<input data-usage-field="url" value="{html.escape(url, quote=True)}"></label>'
            "</div>"
            "</td>"
            "<td>"
            f'<span class="usage-view">{html.escape(branch)}</span>'
            f'<input class="usage-edit" data-usage-field="branch" value="{html.escape(branch, quote=True)}">'
            "</td>"
            "<td>"
            f'<span class="usage-view">{html.escape(eval_dir)}</span>'
            f'<input class="usage-edit" data-usage-field="eval_dir" value="{html.escape(eval_dir, quote=True)}">'
            "</td>"
            "<td>"
            f'<span class="usage-view">{status}</span>'
            f'<label class="usage-edit usage-inline-check"><input type="checkbox" data-usage-field="enabled" {checked_attr}>启用</label>'
            "</td>"
            f"<td><span class=\"sync-pill sync-{html.escape(str(st.get('status') or 'none'))}\">{html.escape(sync_status)}</span><br>"
            f"<small>{html.escape(checked_at)} · {html.escape(schema_note)}</small></td>"
            f"<td><code>{html.escape(commit_label)}</code></td>"
            '<td class="usage-row-actions">'
            f'<span class="usage-view">{edit_button}{delete_button}</span>'
            f'<span class="usage-edit">{save_button}{cancel_button}</span>'
            "</td>"
            "</tr>"
        )
    if not rows:
        rows = '<tr><td colspan="7" style="color:#86909C;">暂无 Git 数据源，点击“新增项目库”后保存，会显示在这里。</td></tr>'
    return f"""
<div class="soft-note" style="margin:8px 0 12px;">
  <b>同步策略：</b>首次 clone，后续先比较远端 commit；有更新才 fetch/checkout，没有更新直接跳过该仓库。
  WebUI 读取
  <code>{html.escape(str(USAGE_SYNC_CACHE))}</code> 下的 <code>docs/eval/*/eval-log.json</code>。
  当前启用 <b>{len(enabled_repos)}</b> / {len(repos)} 个仓库，全局计划时间 <code>{html.escape(str(cron))}</code>。
</div>
<table class="helixharness-config-table">
  <thead><tr><th>仓库</th><th>分支</th><th>采集目录</th><th>状态</th><th>同步</th><th>Commit</th><th>操作</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
"""


def _usage_cron_value() -> str:
    cfg = _load_usage_sync_config()
    cron = (cfg.get("schedule") or {}).get("cron", "0 * * * *")
    return str(cron or "0 * * * *")


def _valid_cron_expression(value: str) -> bool:
    """Accept one schedule only, never embedded commands or additional cron lines."""
    if "\n" in value or "\r" in value:
        return False
    if value.startswith("@"):
        return value in {"@reboot", "@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
    fields = value.split()
    return len(fields) == 5 and all(re.fullmatch(r"[a-zA-Z0-9*/,\-]+", field) for field in fields)


def _save_usage_schedule(cron_expr: str) -> tuple[str, str]:
    cron_expr = (cron_expr or "0 * * * *").strip()
    if not _valid_cron_expression(cron_expr):
        return (
            _usage_sync_summary_html(),
            '<div class="soft-note warn">cron 表达式格式不正确，例如 <code>0 * * * *</code> 表示每小时同步一次。</div>',
        )
    cfg = _load_usage_sync_config()
    schedule = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    schedule["cron"] = cron_expr
    schedule["enabled"] = True
    cfg["schedule"] = schedule
    _save_usage_sync_config(cfg)
    return (
        _usage_sync_summary_html(),
        f'<div class="soft-note ok">已保存全局自动同步时间：<code>{html.escape(cron_expr)}</code>。对所有启用项目库生效。</div>',
    )


def _repo_form_values(repo: dict) -> tuple[str, str, str, str, bool]:
    return (
        str(repo.get("name") or _usage_repo_name(str(repo.get("url") or ""))),
        str(repo.get("url") or ""),
        str(repo.get("branch") or "master"),
        str(repo.get("eval_dir") or "docs/eval"),
        bool(repo.get("enabled", True)),
    )


def _run_usage_sync(
    view_mode: str,
    default_roots: list[str],
    *,
    skip_ai: bool = False,
) -> tuple[str, str, str, str]:
    if not USAGE_SYNC_CONFIG.exists():
        status = '<div class="soft-note warn">还没有保存 Git 数据源配置。</div>'
        overview_html, detail_html = _usage_html_pair(default_roots, view_mode)
        return status, _usage_sync_summary_html(), overview_html, detail_html
    cmd = [
        sys.executable,
        str(USAGE_SYNC_SCRIPT),
        "--config",
        str(USAGE_SYNC_CONFIG),
        "--cache-dir",
        str(USAGE_SYNC_CACHE),
    ]
    if skip_ai:
        cmd.append("--skip-ai")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), text=True, capture_output=True)
    output = "\n".join(x for x in [proc.stdout.strip(), proc.stderr.strip()] if x)
    cls = "ok" if proc.returncode == 0 else "warn"
    status = (
        f'<div class="soft-note {cls}"><b>同步{"完成" if proc.returncode == 0 else "失败"}。</b>'
        f'<pre>{html.escape(output or "无输出")}</pre></div>'
    )
    roots = _current_usage_roots(default_roots)
    return status, _usage_sync_summary_html(), render_usage_global_overview_html(roots), render_usage_overview_html(roots, view_mode)


def _usage_ai_status_html(result: dict) -> str:
    status = str(result.get("status") or "unknown")
    generated_at = str(result.get("generated_at") or "")[:19].replace("T", " ")
    feature_items = [v for v in (result.get("features") or {}).values() if isinstance(v, dict)]
    installation_items = [
        value
        for value in (result.get("installations") or {}).values()
        if isinstance(value, dict)
    ]
    all_items = [
        item
        for item in [result.get("team"), *feature_items, *installation_items]
        if isinstance(item, dict)
    ]
    success_count = sum(1 for x in all_items if x.get("status") == "success")
    error_count = sum(1 for x in all_items if x.get("status") == "error")
    cls = "ok" if status == "success" else "warn"
    if status == "success":
        title = "AI 分析已重新生成。"
    elif status == "partial":
        title = "AI 分析已更新部分内容。"
    elif status == "disabled":
        title = "AI 分析暂未开启。"
    else:
        title = "AI 分析本次暂未更新。"
    parts = [f"已更新 {success_count}", f"待补充 {error_count}"]
    if generated_at:
        parts.append(f"生成于 {html.escape(generated_at)}")
    detail = " · ".join(parts)
    error = str(result.get("error") or "")
    error_html = f"<pre>{html.escape(error[:800])}</pre>" if error else ""
    return f'<div class="soft-note {cls}"><b>{title}</b><br>{detail}{error_html}</div>'


def _run_usage_ai_regenerate(view_mode: str, default_roots: list[str]) -> tuple[str, str, str, str]:
    roots = _current_usage_roots(default_roots)
    if is_demo_roots(roots):
        status = '<div class="soft-note warn">当前为合成示例。请接入本地项目或 Git 数据源后启用真实 AI 解读。</div>'
        overview_html, detail_html = _usage_html_pair(default_roots, view_mode)
        return status, _usage_sync_summary_html(), overview_html, detail_html
    if not roots:
        status = (
            '<div class="soft-note warn"><b>还没有可分析的数据。</b><br>'
            '请先配置项目库并完成一次同步，再点击“重新生成 AI 分析”。</div>'
        )
        overview_html, detail_html = _usage_html_pair(default_roots, view_mode)
        return status, _usage_sync_summary_html(), overview_html, detail_html
    try:
        from eval.usage_ai import generate_usage_ai_insights

        def _progress_cb(done: int, total: int, current: str) -> None:
            _TASKS.update_progress("ai_regenerate", done, total, current)

        result = generate_usage_ai_insights(
            _load_usage_sync_config(),
            [Path(r) for r in roots],
            USAGE_SYNC_CACHE,
            progress_cb=_progress_cb,
        )
        status = _usage_ai_status_html(result)
    except Exception as exc:
        status = (
            '<div class="soft-note warn"><b>AI 分析本次暂未更新。</b><br>'
            '模型 API 服务、token 或数据格式可能需要确认，处理后可重新点击刷新。'
            f'<pre>{html.escape(str(exc)[:1200])}</pre></div>'
        )
    return (
        status,
        _usage_sync_summary_html(),
        render_usage_global_overview_html(roots),
        render_usage_overview_html(roots, view_mode),
    )


def _run_usage_ai_team_regenerate(view_mode: str, default_roots: list[str]) -> tuple[str, str, str, str]:
    """Regenerate only the homepage AI summary against the current facts."""

    roots = _current_usage_roots(default_roots)
    if not roots or is_demo_roots(roots) or not (USAGE_SYNC_CACHE / "ai_insights.json").is_file():
        return _run_usage_ai_regenerate(view_mode, default_roots)
    try:
        from eval.usage_ai import regenerate_usage_team_ai_insight

        result = regenerate_usage_team_ai_insight(
            _load_usage_sync_config(),
            [Path(r) for r in roots],
            USAGE_SYNC_CACHE,
        )
        status = _usage_ai_status_html(result)
    except Exception as exc:
        status = (
            '<div class="soft-note warn"><b>首页 AI 分析本次暂未更新。</b><br>'
            f'<pre>{html.escape(str(exc)[:1200])}</pre></div>'
        )
    return (
        status,
        _usage_sync_summary_html(),
        render_usage_global_overview_html(roots),
        render_usage_overview_html(roots, view_mode),
    )


def _usage_cron_command() -> str:
    log_path = PROJECT_ROOT / "eval" / "logs" / "helixharness_usage_sync.log"
    return (
        f'cd {shlex.quote(str(PROJECT_ROOT))} && {shlex.quote(sys.executable)} {shlex.quote(str(USAGE_SYNC_SCRIPT))} '
        f'--config {shlex.quote(str(USAGE_SYNC_CONFIG))} --cache-dir {shlex.quote(str(USAGE_SYNC_CACHE))} '
        f'>> {shlex.quote(str(log_path))} 2>&1'
    )


def _install_usage_cron(cron_expr: str) -> str:
    cron_expr = (cron_expr or "0 * * * *").strip()
    if not _valid_cron_expression(cron_expr):
        return '<div class="soft-note warn">cron 表达式格式不正确，例如 <code>0 * * * *</code> 表示每小时同步一次。</div>'
    PROJECT_ROOT.joinpath("eval", "logs").mkdir(parents=True, exist_ok=True)
    new_block = f"{USAGE_CRON_BEGIN}\n{cron_expr} {_usage_cron_command()}\n{USAGE_CRON_END}"
    try:
        current = subprocess.run(
            ["crontab", "-l"], text=True, capture_output=True,
            env={**os.environ, "LC_ALL": "C"}, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return '<div class="soft-note warn">读取 crontab 超时，未写入定时任务。请检查当前运行环境。</div>'
    except (OSError, PermissionError) as exc:
        return f'<div class="soft-note warn">当前运行环境不能写 crontab：<pre>{html.escape(str(exc))}</pre>部署到服务器后再安装定时任务。</div>'
    no_schedule = (
        current.returncode == 1
        and not current.stdout.strip()
        and re.search(r"\bno crontab for\b", current.stderr, re.IGNORECASE) is not None
    )
    if current.returncode and not no_schedule:
        detail = current.stderr or current.stdout or f"exit status {current.returncode}"
        return f'<div class="soft-note warn">读取 crontab 失败，未写入定时任务：<pre>{html.escape(detail)}</pre></div>'
    existing = current.stdout if current.returncode == 0 else ""
    lines = existing.splitlines(keepends=True)
    kept = []
    skip = False
    for line in lines:
        if line.strip() == USAGE_CRON_BEGIN:
            if skip:
                return '<div class="soft-note warn">已有定时任务标记不完整，未修改 crontab。</div>'
            skip = True
            continue
        if line.strip() == USAGE_CRON_END:
            if not skip:
                return '<div class="soft-note warn">已有定时任务标记不完整，未修改 crontab。</div>'
            skip = False
            continue
        if not skip:
            kept.append(line)
    if skip:
        return '<div class="soft-note warn">已有定时任务标记不完整，未修改 crontab。</div>'
    preserved = "".join(kept)
    if preserved and not preserved.endswith("\n"):
        preserved += "\n"
    updated = preserved + new_block + "\n"
    try:
        proc = subprocess.run(["crontab", "-"], input=updated, text=True, capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        return '<div class="soft-note warn">安装 cron 超时，安装结果尚未确认。请先用 <code>crontab -l</code> 核对当前任务，再决定是否重试。</div>'
    except (OSError, PermissionError) as exc:
        return f'<div class="soft-note warn">当前运行环境不能写 crontab：<pre>{html.escape(str(exc))}</pre>部署到服务器后再安装定时任务。</div>'
    if proc.returncode:
        return f'<div class="soft-note warn">安装 cron 失败：<pre>{html.escape(proc.stderr or proc.stdout)}</pre></div>'
    cfg = _load_usage_sync_config()
    cfg["schedule"] = {"cron": cron_expr, "enabled": True}
    _save_usage_sync_config(cfg)
    return f'<div class="soft-note ok">已安装定时同步：<code>{html.escape(cron_expr)}</code></div>'


def _install_usage_cron_with_summary(cron_expr: str) -> tuple[str, str]:
    status = _install_usage_cron(cron_expr)
    return _usage_sync_summary_html(), status


class _BgTaskManager:
    """长任务后台执行 + 状态轮询（线程安全，模块级单例）。

    用于「立即同步」「重新生成 AI 分析」等耗时操作：路由立即返回，
    后台线程跑实际工作，前端 HTMX 轮询 GET /config/task-status/{id} 拿状态。
    """

    _TASK_LABELS = {"sync": "同步", "ai_regenerate": "AI 分析生成"}

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, task_id: str, fn, *args, rerun_if_running: bool = False) -> bool:
        """启动后台任务，必要时合并为当前任务结束后的一次重跑。"""
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing and existing.get("running"):
                if rerun_if_running:
                    existing["pending_rerun"] = (fn, args)
                return False
            self._tasks[task_id] = {
                "running": True,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "finished_at": "",
                "result_html": "",
                "progress": {"done": 0, "total": 0, "current": ""},
            }

        def _runner() -> None:
            current_fn = fn
            current_args = args
            while True:
                try:
                    status_html, *_ = current_fn(*current_args)
                except Exception as exc:  # noqa: BLE001 — 后台线程必须吞掉异常
                    status_html = (
                        f'<div class="soft-note warn"><b>执行出错。</b>'
                        f'<pre>{html.escape(str(exc)[:1200])}</pre></div>'
                    )
                with self._lock:
                    task = self._tasks.get(task_id)
                    if not task:
                        return
                    pending_rerun = task.pop("pending_rerun", None)
                    if pending_rerun:
                        current_fn, current_args = pending_rerun
                        task["progress"] = {"done": 0, "total": 0, "current": ""}
                        continue
                    task["running"] = False
                    task["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    task["result_html"] = status_html or ""
                    return

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        return True

    def update_progress(self, task_id: str, done: int, total: int, current: str = "") -> None:
        """后台线程执行中回调：更新进度 (done/total/当前对象), 供前端轮询展示。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t["progress"] = {"done": done, "total": total, "current": current}

    def status(self, task_id: str) -> dict:
        """返回任务状态：{running, started_at, finished_at, result_html, progress}。"""
        with self._lock:
            t = self._tasks.get(task_id)
        if not t:
            return {"running": False, "started_at": "", "finished_at": "", "result_html": "", "progress": {}}
        return dict(t)

    def status_html(self, task_id: str, just_started: bool = False) -> str:
        """渲染状态 HTML 片段（供 HTMX swap 到 #action-status，用 innerHTML）。

        - 运行中：进度文案 + 带 hx-trigger 的轮询元素（每 3s 继续轮询）+ 运行中标记
        - 完成：结果 HTML，不带轮询（自动停止）+ 完成标记

        注意：返回内容会被 swap 进外层 #action-status（innerHTML），所以这里
        不再带 id；轮询元素的 hx-target 指向外层 #action-status，确保 id 不丢失。
        进度文案形如 "已完成 3/7 · 正在分析 cdd-p1"，无进度数据时回退到 "进行中…"。

        按钮禁用/恢复不在此处用脚本/OOB (易与 innerHTML swap 冲突), 而是放一个
        data-task 标记元素; base.html 全局监听 htmx:afterSwap, 检测 #action-status
        内的标记来切换原按钮 disabled。纯声明式, 零事件时机依赖。
        """
        label = self._TASK_LABELS.get(task_id, task_id)
        st = self.status(task_id)
        # 标记元素: 全局脚本据此控制按钮 disabled (运行中 data-running, 完成无此属性)
        marker = f'<span class="helixharness-task-marker" data-task="{task_id}" data-running="1" hidden></span>'
        if st["running"]:
            progress = st.get("progress") or {}
            done = int(progress.get("done") or 0)
            total = int(progress.get("total") or 0)
            current = str(progress.get("current") or "").strip()
            if just_started and total == 0:
                hint = f"{label}已在后台开始，约需几分钟，完成后自动显示结果。"
            elif total > 0:
                hint = f"{label}进行中：已完成 {done}/{total}"
                if current:
                    hint = f"{label}进行中：已完成 {done}/{total} · 正在分析 {html.escape(current)}…"
            else:
                hint = f"{label}进行中，请稍候…"
            return (
                f'<div class="soft-note"><b>{hint}</b></div>'
                f'<div hx-get="/config/task-status/{task_id}" hx-trigger="every 3s" '
                f'hx-target="#action-status" hx-swap="innerHTML"></div>'
                f'{marker}'
            )
        # 已完成（或未启动）——展示结果，停止轮询 + 完成标记 (无 data-running → 全局脚本恢复按钮)
        result = st["result_html"] or '<div class="soft-note">暂无执行记录。</div>'
        return result + f'<span class="helixharness-task-marker" data-task="{task_id}" hidden></span>'


_TASKS = _BgTaskManager()


def schedule_usage_ai_regeneration(default_roots: list[str]) -> bool:
    """Queue one non-blocking AI refresh, coalescing while a refresh is running."""

    return _TASKS.start(
        "ai_regenerate",
        _run_usage_ai_regenerate,
        "按需求维度",
        list(default_roots),
        rerun_if_running=True,
    )
