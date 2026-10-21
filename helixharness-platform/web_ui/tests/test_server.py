"""Smoke tests for the non-evaluation FastAPI routes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from web_ui import create_app
from web_ui.main import _refresh_evolution_source
from web_ui.routes.evolution import _candidate_change_view, _cycle_view, _quality_view, run_evolution, select_hypothesis


def test_candidate_adoption_keeps_web_event_loop_responsive() -> None:
    async def exercise():
        loop = asyncio.get_running_loop()
        responsive = threading.Event()

        def adopt(hypothesis_id):
            assert hypothesis_id == "synthetic-adoption"
            loop.call_soon_threadsafe(responsive.set)
            assert responsive.wait(1), "Candidate adoption blocked the Web event loop"

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            evolution_service=SimpleNamespace(start_selected_hypothesis=adopt),
        )))
        response = await select_hypothesis(request, "synthetic-adoption")
        assert response.status_code == 303

    asyncio.run(exercise())


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(
        "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH",
        str(tmp_path / "framework-eval" / "evaluation.db"),
    )
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_TELEMETRY_DB_PATH", str(tmp_path / "eval" / "telemetry.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_DB_PATH", str(tmp_path / "evolution" / "evolution.db"))
    monkeypatch.setenv(
        "HELIXHARNESS_PLATFORM_EVOLUTION_RUNTIME_CONFIG_PATH",
        str(tmp_path / "evolution" / "runtime_config.json"),
    )
    catalog_path = tmp_path / "evolution" / "capabilities.yaml"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        """schema_version: evolution-capability-catalog.v1
capabilities:
  - id: workflow-discipline
    name: HelixHarness 工作流纪律
    description: Demo workflow discipline
    selectors: [archive]
    owned_paths: [commands/]
    protected_paths: [bench/]
    case_version_ids: []
    auto_repair: false
    growth_confirmed: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_CATALOG_PATH", str(catalog_path))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_HELIXHARNESS_REPOSITORY", "")
    return TestClient(create_app())


def test_candidate_change_view_groups_diff_and_stats_by_file() -> None:
    view = _candidate_change_view(
        {
            "changed_paths": [
                "commands/archive.md",
                "skills/check/SKILL.md",
                "skills/check/tests",
            ],
            "diff_patch": (
                "diff --git a/commands/archive.md b/commands/archive.md\n"
                "index 1111111..2222222 100644\n"
                "--- a/commands/archive.md\n"
                "+++ b/commands/archive.md\n"
                "@@ -1 +1 @@\n"
                "-先归档\n"
                "+先验证\n"
                "diff --git a/skills/check/SKILL.md b/skills/check/SKILL.md\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/skills/check/SKILL.md\n"
                "@@ -0,0 +1 @@\n"
                "+检查结果\n"
            ),
        }
    )

    assert view["file_count"] == 2
    assert view["changed_paths"] == (
        "commands/archive.md",
        "skills/check/SKILL.md",
        "skills/check/tests",
    )
    assert view["additions"] == 2
    assert view["deletions"] == 1
    archive, check = view["files"]
    assert archive["change_type"] == "修改"
    assert archive["additions"] == 1
    assert archive["deletions"] == 1
    assert "skills/check/SKILL.md" not in archive["diff_patch"]
    assert check["change_type"] == "新增"
    assert check["additions"] == 1
    assert check["deletions"] == 0
    assert "commands/archive.md" not in check["diff_patch"]


def test_dashboard_overview(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "数据看板" in response.text
    assert "需求维度" in response.text
    assert 'hx-get="/partial/detail?view=requirement"' not in response.text
    assert "eval-log 消费端质量 Gate" not in response.text

    assert 'data-evolution-quality-gate="true"' not in response.text
    assert 'data-evolution-entry="true"' not in response.text

    combined = client.get("/partial/detail?view=project")
    assert combined.status_code == 200
    assert re.search(
        r'href="/\?view=project".*?bg-accent text-white',
        combined.text,
        re.DOTALL,
    )
    legacy_requirement = client.get("/partial/detail?view=requirement")
    assert legacy_requirement.status_code == 200
    assert re.search(
        r'href="/\?view=project".*?bg-accent text-white',
        legacy_requirement.text,
        re.DOTALL,
    )


@pytest.mark.parametrize("configured", [None, "missing.mp4", "private.txt"])
def test_tutorial_video_unavailable_keeps_quickstart(client, tmp_path, monkeypatch, configured):
    if configured:
        path = tmp_path / configured
        if path.suffix == ".txt":
            path.write_text("not a video")
        monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVALUATION_TUTORIAL_VIDEO_PATH", str(path))
    page = client.get("/framework-evaluation")
    assert page.status_code == 200
    assert 'data-operation-video="true"' not in page.text
    assert "开始评测" in page.text
    assert client.get("/framework-evaluation/tutorial-video").status_code == 404


def test_tutorial_video_streams_configured_file_and_supports_seeking(client, tmp_path, monkeypatch):
    video = tmp_path / "synthetic.mp4"
    video.write_bytes(b"0123456789abcdef")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVALUATION_TUTORIAL_VIDEO_PATH", str(video))
    page = client.get("/framework-evaluation")
    assert 'data-operation-video="true"' in page.text
    assert 'src="/framework-evaluation/tutorial-video"' in page.text
    assert str(video) not in page.text
    response = client.get("/framework-evaluation/tutorial-video", headers={"Range": "bytes=4-7"})
    assert response.status_code == 206
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-range"] == "bytes 4-7/16"
    assert response.content == b"4567"


def test_runtime_date_filter_refreshes_only_charts_and_preserves_custom_dates(client: TestClient) -> None:
    response = client.get("/partial/runtime", params={
        "runtime_period": "custom", "runtime_start": "2026-09-01", "runtime_end": "2026-09-08", "runtime_expanded": "true",
    })
    assert response.status_code == 200
    assert 'id="helixharness-runtime-overview"' in response.text
    assert '<details class="helixharness-runtime-disclosure" open>' in response.text
    assert 'value="2026-09-01"' in response.text
    assert "2026-09-01 至 2026-09-08" in response.text
    assert 'data-distribution="version"' in response.text
    assert 'data-distribution="platform"' in response.text
    assert '阶段耗时占比</div><div class="helixharness-runtime-filter">' in response.text
    assert "版本与平台使用情况" in response.text
    assert 'id="detail-section"' not in response.text
    assert "平台统计时间" not in response.text
    invalid = client.get("/partial/runtime?runtime_period=custom&runtime_start=2026-09-09&runtime_end=2026-09-08")
    assert 'role="alert"' in invalid.text
    assert "开始日期不能晚于结束日期" in invalid.text

def test_dashboard_links_to_evolution_only_when_content_exists(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypotheses",
        lambda: [
            *[
                {
                    "payload_json": json.dumps(
                        {"semantic_source": "MODEL"}
                    )
                }
                for _ in range(4)
            ],
            {"payload_json": json.dumps({"semantic_source": "PARTIAL_REHEARSAL"})},
            *[
                {
                    "payload_json": json.dumps(
                        {"source_kind": "BENCH_RUN", "semantic_source": "BENCH_REMEDIATION"}
                    )
                }
                for _ in range(3)
            ],
        ],
    )
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_experiments",
        lambda **kwargs: [
            *[
                {
                    "hypothesis_json": json.dumps(
                        {"semantic_source": "MODEL"}
                    )
                }
                for _ in range(4)
            ],
            {
                "hypothesis_json": json.dumps(
                    {"source_kind": "BENCH_RUN", "semantic_source": "BENCH_REMEDIATION"}
                )
            },
        ],
    )

    response = client.get("/")

    assert response.status_code == 200
    assert 'data-evolution-entry="true"' in response.text
    assert 'href="/evolution/hypotheses?source=EVAL"' in response.text
    assert re.search(r"数据上报已产生 <b>4</b> 个自进化假设", response.text)
    assert "自进化已产生新内容" not in response.text
    assert re.search(r"，其中 <b>4</b> 个已进入实验流程", response.text)
    assert re.search(r"<b>4</b> 个来自合格 HelixHarness eval-log 与已关联异步上报的假设", response.text)
    assert "受控评测发现" not in response.text
    assert "每 48 小时检查一次当前全部合格 HelixHarness 数据" in response.text
    assert "checkpoint commit 的代码变更摘要" in response.text
    assert "只有新增或发生变化的事实可以触发待验证假设" in response.text
    assert "旧事实不会被重复生成" in response.text
    assert "进入实验不代表已经改进生效" in response.text
    assert "查看自进化" in response.text


def test_dashboard_hides_evolution_entry_when_only_benchmark_findings_exist(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark_payload = json.dumps(
        {"source_kind": "BENCH_RUN", "semantic_source": "BENCH_REMEDIATION"}
    )
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypotheses",
        lambda: [{"payload_json": benchmark_payload}],
    )
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_experiments",
        lambda **kwargs: [{"hypothesis_json": benchmark_payload}],
    )

    response = client.get("/")

    assert response.status_code == 200
    assert 'data-evolution-entry="true"' not in response.text


def test_pages_use_local_frontend_assets_and_compress_large_html(
    client: TestClient,
) -> None:
    response = client.get("/", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert 'href="/static/css/tailwind.css"' in response.text
    assert 'src="/static/vendor/htmx.min.js?v=1.9.12-local3"' in response.text
    assert "cdn.tailwindcss.com" not in response.text
    assert "unpkg.com" not in response.text
    assert client.get("/static/css/tailwind.css").status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_dashboard_config(client: TestClient) -> None:
    response = client.get("/config")
    assert response.status_code == 200
    assert "配置" in response.text
    assert 'data-config-layout="true"' in response.text
    assert "lg:grid-cols-[minmax(0,1fr)_260px]" in response.text
    assert 'data-repo-table-viewport="true"' in response.text
    assert 'class="w-full min-w-[760px] table-fixed' in response.text


def test_dashboard_config_separates_repositories_with_valid_eval_from_eval_record_count(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import usage_sync

    config_path = tmp_path / "usage_repos.yaml"
    state_path = tmp_path / "sync_state.json"
    config_path.write_text(
        """repos:
  - name: repo-a
    url: file:///repo-a
  - name: repo-b
    url: file:///repo-b
  - name: repo-c
    url: file:///repo-c
""",
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps(
            {
                "repos": {
                    "repo-a": {"eval_log_count": 3, "invalid_log_count": 1},
                    "repo-b": {"eval_log_count": 1, "invalid_log_count": 1},
                    "repo-c": {"eval_log_count": 3, "invalid_log_count": 0},
                },
                "last_run": {
                    "finished_at": "2026-08-11T10:00:00+08:00",
                    "total": 3,
                    "succeeded": 3,
                    "unchanged": 3,
                    "with_eval": 3,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CONFIG", config_path)
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_STATE", state_path)

    response = client.get("/config")

    assert response.status_code == 200
    assert "有 eval 数据仓库" in response.text
    assert "有效 eval 数据" in response.text
    assert "无效 eval 数据" in response.text
    assert 'data-eval-repo-count="true">2</b> 个' in response.text
    assert 'data-valid-eval-count="true">5</b> 条' in response.text
    assert 'data-invalid-eval-count="true">2</b> 条' in response.text

    header = re.search(r'<div[^>]+data-repo-list-header="true"[^>]*>(.*?)</div>', response.text, re.DOTALL)
    assert header is not None
    assert "项目库列表" in header.group(1)
    assert "最近同步" in header.group(1)
    assert 'id="helixharness-last-run-time"' in header.group(1)

    summary = re.search(r'<div class="([^"]+)" data-repo-summary="true">', response.text)
    assert summary is not None
    assert "flex-nowrap" in summary.group(1)
    assert "overflow-x-auto" in summary.group(1)
    assert "whitespace-nowrap" in summary.group(1)
    stat_classes = re.findall(r'<span class="([^"]+)" data-repo-stat="true">', response.text)
    assert stat_classes
    assert len(set(stat_classes)) == 1


def test_saving_dashboard_schedule_installs_automatic_sync(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import usage_sync

    monkeypatch.setattr(usage_sync, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CONFIG", tmp_path / "eval" / "configs" / "usage_repos.yaml")
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CACHE", tmp_path / "eval" / ".cache")
    monkeypatch.setattr(
        usage_sync,
        "USAGE_SYNC_SCRIPT",
        tmp_path / "eval" / "cli" / "sync_helixharness_usage.py",
    )
    installed: dict[str, str] = {}

    def fake_crontab(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args == ["crontab", "-l"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        assert args == ["crontab", "-"]
        installed["content"] = str(kwargs["input"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(usage_sync.subprocess, "run", fake_crontab)

    response = client.post("/config/save-cron", data={"cron": "15 2 * * *"})

    assert response.status_code == 200
    assert "已安装定时同步" in response.text
    assert "15 2 * * *" in installed["content"]
    assert "--skip-ai" not in installed["content"]
    assert usage_sync.USAGE_CRON_BEGIN in installed["content"]
    assert usage_sync.USAGE_CRON_END in installed["content"]






def test_automatic_dashboard_schedule_regenerates_ai_after_sync() -> None:
    from eval.usage_sync import _usage_cron_command

    assert "--skip-ai" not in _usage_cron_command()


def test_evolution_refresh_does_not_wait_for_optional_dashboard_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import usage_sync

    calls: list[dict[str, object]] = []

    def fake_sync(view_mode: str, roots: list[str], *, skip_ai: bool = False) -> tuple[str, str, str, str]:
        calls.append({"view_mode": view_mode, "roots": roots, "skip_ai": skip_ai})
        return "", "", "", ""

    monkeypatch.setattr(usage_sync, "_run_usage_sync", fake_sync)
    monkeypatch.setattr(usage_sync, "_current_usage_roots", lambda roots: [*roots, "/synced"])
    refreshed: list[list[str]] = []
    app = SimpleNamespace(
        context=SimpleNamespace(
            usage_data_source="sync",
            current_roots=lambda: ["/default"],
        )
    )
    telemetry_store = SimpleNamespace(refresh=lambda roots: refreshed.append(roots))

    _refresh_evolution_source(app, telemetry_store)

    assert calls == [
        {"view_mode": "按需求维度", "roots": ["/default"], "skip_ai": True}
    ]
    assert refreshed == [["/default", "/synced"]]


def test_config_task_status_route(client: TestClient) -> None:
    from eval.usage_sync import _TASKS

    assert not _TASKS.status("sync")["running"]
    response = client.get("/config/task-status/sync")
    assert response.status_code == 200
    assert "暂无执行记录" in response.text or "soft-note" in response.text


def test_background_task_start_and_poll() -> None:
    from eval.usage_sync import _BgTaskManager

    manager = _BgTaskManager()

    def fake_long_task(_view: str, _roots: list[str]) -> tuple[str, str, str, str]:
        time.sleep(0.05)
        return ('<div class="soft-note ok">done</div>', "", "", "")

    assert manager.start("sync", fake_long_task, "v", []) is True
    assert "hx-trigger" in manager.status_html("sync", just_started=True)
    assert manager.start("sync", fake_long_task, "v", []) is False
    time.sleep(0.1)
    assert not manager.status("sync")["running"]
    completed = manager.status_html("sync")
    assert "done" in completed
    assert "hx-trigger" not in completed


def test_evolution_summary(client: TestClient) -> None:
    response = client.get("/evolution/summary")
    assert response.status_code == 200
    assert "HelixHarness Platform" in response.text
    assert 'class="topbtn px-3.5' in response.text
    assert 'data-active-sidebar-path="/evolution/summary"' in response.text
    assert "HelixHarness" in response.text
    assert "自进化" in response.text
    assert "48 小时" in response.text
    assert "eval-log 消费端质量 Gate" not in response.text
    assert 'data-evolution-quality-gate="true"' not in response.text
    assert 'data-evolution-readiness="true"' not in response.text
    assert "实验启动条件" not in response.text
    assert "候选生成器未配置" not in response.text
    assert 'data-evolution-run-button="true"' not in response.text
    assert 'data-evolution-page="summary"' in response.text
    assert 'data-evolution-hypotheses="true"' not in response.text
    assert 'data-evolution-experiments="true"' not in response.text
    assert "进化总览" in response.text
    assert "进化假设" in response.text
    assert "进化实验" in response.text
    assert "进化配置" in response.text
    assert 'data-evolution-flow="true"' not in response.text
    assert 'data-evolution-summary-cards="true"' in response.text
    assert 'data-evolution-flow-image="true"' in response.text
    assert response.text.index('data-evolution-summary-cards="true"') < response.text.index(
        'data-evolution-flow-image="true"'
    )
    assert 'src="/static/images/evolution-evidence-loop.png"' in response.text
    assert 'max-w-[1040px]' in response.text
    assert 'min-w-[760px]' in response.text
    assert "生产数据上报与人工评测是两条证据入口" not in response.text
    assert "eval-log 提出证据，大模型生成语义假设" not in response.text
    architecture = client.get("/static/images/evolution-evidence-loop.png")
    assert architecture.status_code == 200
    assert architecture.headers["content-type"] == "image/png"


def test_evolution_config_exposes_real_editors_and_fixed_contracts(client: TestClient) -> None:
    response = client.get("/evolution/config")

    assert response.status_code == 200
    assert 'data-active-sidebar-path="/evolution/config"' in response.text
    assert 'data-evolution-page="config"' in response.text
    assert 'data-evolution-config="true"' in response.text
    assert "固定契约保持只读" in response.text
    assert 'data-evolution-run-button="true"' in response.text
    assert "检查新证据" in response.text
    assert "在进化假设中审阅候选，再采用进入实验" in response.text
    assert 'data-config-group="schedule"' in response.text
    assert 'data-config-group="rule-sources"' in response.text
    assert 'data-config-group="boundaries"' in response.text
    assert 'data-config-rule="evidence"' in response.text
    assert 'data-config-rule="capabilities"' in response.text
    assert 'data-config-rule="resources"' in response.text
    assert 'data-config-editor="schedule"' in response.text
    assert 'data-config-editor="catalog"' in response.text
    assert 'action="/evolution/config/schedule"' in response.text
    assert 'action="/evolution/config/catalog"' in response.text
    assert "保存周期配置" in response.text
    assert "到期检查间隔（秒）" in response.text
    assert "每隔多久检查 48 小时周期是否已到；不会触发重复运行。" in response.text
    assert "评测结果检查间隔（秒）" in response.text
    assert "每隔多久处理已结束的 Benchmark；符合条件时生成修复假设。" in response.text
    assert 'name="remediation_poll_seconds" min="0.05" max="3600" step="0.05" value="30"' in response.text
    assert "后台检查（秒）" not in response.text
    assert "评测回收（秒）" not in response.text
    assert "验证并保存能力目录" in response.text
    assert 'name="interval_hours" min="1" max="720" step="1" value="48"' in response.text
    assert "Log 质量范围" in response.text
    assert "自动周期仅使用合格 VALID HelixHarness log" in response.text
    assert "部分可用 <code>PARTIAL</code>" in response.text
    assert "仅单条人工演练" in response.text
    assert "无效 <code>INVALID</code>" in response.text
    assert "PARTIAL 不能批量进入自动周期" in response.text
    assert 'class="grid gap-3 md:grid-cols-3" data-config-group="rule-sources"' in response.text
    assert "capabilities.yaml" in response.text
    assert "当前 1 项能力" in response.text
    assert "能力与改动范围（高级配置）" in response.text
    assert "问题归到哪个能力、Candidate 允许或禁止修改哪些目录、正式评测可选择哪些 Case" in response.text
    assert 'style="min-height: 520px;" rows="26"' in response.text
    assert "Candidate 只能人工发布" in response.text
    assert 'href="/framework-evaluation/config"' in response.text
    assert 'data-config-group="ownership"' not in response.text
    assert 'data-resource-policy=' not in response.text
    assert 'data-capability-config=' not in response.text
    assert 'data-evolution-quality-gate="true"' not in response.text
    assert 'data-evolution-hypotheses="true"' not in response.text
    assert 'data-evolution-experiments="true"' not in response.text


def test_evolution_schedule_config_persists_and_updates_the_running_service(client: TestClient) -> None:
    response = client.post(
        "/evolution/config/schedule",
        data={
            "interval_hours": "24",
            "poll_seconds": "120",
            "remediation_poll_seconds": "2.5",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/evolution/config?saved=schedule"
    assert client.app.state.evolution_service.interval.total_seconds() == 24 * 3600
    assert client.app.state.evolution_scheduler.poll_seconds == 120
    assert client.app.state.evolution_scheduler.remediation_poll_seconds == 2.5
    saved = Path(os.environ["HELIXHARNESS_PLATFORM_EVOLUTION_RUNTIME_CONFIG_PATH"])
    assert json.loads(saved.read_text(encoding="utf-8")) == {
        "interval_hours": 24,
        "poll_seconds": 120,
        "remediation_poll_seconds": 2.5,
    }
    page = client.get("/evolution/config?saved=schedule")
    assert "周期配置已保存并立即生效" in page.text
    assert 'name="interval_hours" min="1" max="720" step="1" value="24"' in page.text


def test_evolution_catalog_editor_validates_before_hot_reload(client: TestClient) -> None:
    catalog_path = Path(client.app.state.evolution_catalog_path)
    original = catalog_path.read_text(encoding="utf-8")
    updated = original.replace("auto_repair: false", "auto_repair: true")

    saved = client.post(
        "/evolution/config/catalog",
        data={"catalog_yaml": updated},
        follow_redirects=False,
    )

    assert saved.status_code == 303
    assert saved.headers["location"] == "/evolution/config?saved=catalog"
    assert "auto_repair: true" in catalog_path.read_text(encoding="utf-8")
    assert client.app.state.evolution_service.capabilities[0].auto_repair is True

    rejected = client.post(
        "/evolution/config/catalog",
        data={"catalog_yaml": "schema_version: broken\ncapabilities: []\n"},
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    assert rejected.headers["location"].startswith("/evolution/config?error=")
    assert "auto_repair: true" in catalog_path.read_text(encoding="utf-8")


def test_evolution_quality_and_cycle_views_are_explained_in_chinese() -> None:
    quality = _quality_view(
        {
            "total": 10,
            "counts": {"VALID": 0, "PARTIAL": 10, "INVALID": 0},
            "automation_eligible": 0,
            "issue_counts": {"RESOURCE_USAGE_UNAVAILABLE": 24, "ENTRY_PENDING": 15},
        }
    )
    cycle = _cycle_view(
        {
            "state": "NOOP",
            "reason": "no qualified eval-log evidence",
            "evidence_count": 0,
            "started_at": "2026-08-06T13:28:31.972579Z",
        }
    )

    assert quality["statuses"][1]["label"] == "部分可用"
    assert quality["issues"][0] == {
        "code": "RESOURCE_USAGE_UNAVAILABLE",
        "count": 24,
        "label": "资源用量未采集",
        "description": "某条执行记录没有采集 Token、耗时等资源指标。",
    }
    assert cycle is not None
    assert cycle["state_label"] == "本轮无可执行动作"
    assert cycle["reason_label"] == "没有通过质量 Gate 的 eval-log 证据"

    waiting = _cycle_view(
        {
            "state": "AWAITING_HUMAN_SELECTION",
            "reason": "terminal Benchmark produced one remediation plan draft",
        }
    )
    assert waiting is not None
    assert waiting["state_label"] == "有假设待处理"
    assert waiting["reason_label"] == "评测结果已生成一条修复假设"


def test_evolution_summary_renders_current_quality_and_noop_explanations(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.telemetry_quality_store,
        "summary",
        lambda: {
            "total": 10,
            "supported_total": 8,
            "counts": {"VALID": 0, "PARTIAL": 10, "INVALID": 0},
            "automation_eligible": 0,
            "issue_counts": {"RESOURCE_USAGE_UNAVAILABLE": 24},
        },
    )
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "cycle_summary",
        lambda: {
            "latest": {
                "state": "NOOP",
                "reason": "no qualified eval-log evidence",
                "evidence_count": 0,
                "started_at": "2026-08-06T13:28:31.972579Z",
            },
            "hypothesis_count": 0,
            "hypothesis_counts": {"REPAIR": 0, "GROWTH": 0},
            "experiment_counts": {},
        },
    )
    response = client.get("/evolution/summary")

    assert response.status_code == 200
    assert "0 / 8 份" in response.text
    assert "修复 0 个" in response.text
    assert "增长 0 个" in response.text
    assert "合格记录 / 记录总数" in response.text
    assert "0 / 10" not in response.text
    assert "本轮无可执行动作" in response.text
    assert "2026-08-06 21:28:31" in response.text
    assert "（北京时间）" not in response.text
    assert 'data-evolution-latest-cycle="true"' not in response.text
    assert "资源用量未采集" not in response.text
    assert "数字是出现次数" not in response.text
    assert 'data-evolution-quality-gate="true"' not in response.text
    assert "实验启动条件" not in response.text


def test_evolution_summary_turns_waiting_cycle_into_hypothesis_action(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "cycle_summary",
        lambda: {
            "latest": {
                "state": "AWAITING_HUMAN_SELECTION",
                "reason": "terminal Benchmark produced one remediation plan draft",
                "evidence_count": 4,
                "started_at": "2026-08-23T07:35:47Z",
            },
            "hypothesis_count": 8,
            "hypothesis_counts": {"REPAIR": 6, "GROWTH": 2},
            "experiment_counts": {},
        },
    )

    response = client.get("/evolution/summary")

    assert response.status_code == 200
    assert "有假设待处理" in response.text
    assert "修复 6 个" in response.text
    assert "增长 2 个" in response.text
    assert "本轮开始于 2026-08-23 15:35:47" in response.text
    assert 'href="/evolution/hypotheses">查看待处理假设</a>' in response.text
    assert 'data-evolution-latest-cycle="true"' not in response.text
    assert "AWAITING_HUMAN_SELECTION" not in response.text
    assert "terminal Benchmark produced one remediation plan draft" not in response.text


def test_evolution_config_shows_live_manual_cycle_status(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_scheduler,
        "manual_cycle_status",
        lambda: {
            "state": "RUNNING",
            "started_at": "2026-08-10T11:59:00Z",
            "finished_at": None,
            "outcome_state": None,
            "outcome_reason": None,
            "error": None,
        },
    )

    config = client.get("/evolution/config")
    status = client.get("/evolution/run-status")

    assert config.status_code == 200
    assert "正在检查新证据" in config.text
    assert "正在同步 eval-log、回收评测结论并分析新增合格证据" in config.text
    assert "新候选准备完成后，需在进化假设中审阅并采用" in config.text
    assert 'data-evolution-run-button="true"' in config.text
    assert "正在检查…" in config.text
    assert " disabled" in config.text
    assert status.status_code == 200
    assert 'hx-get="/evolution/run-status"' in status.text
    assert 'hx-trigger="every 2s"' in status.text


def test_manual_evolution_run_does_not_block_the_web_event_loop() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingScheduler:
        def start_manual_cycle(self) -> bool:
            def run() -> None:
                started.set()
                assert release.wait(timeout=1)

            threading.Thread(target=run, daemon=True).start()
            return True

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(evolution_scheduler=BlockingScheduler()),
        ),
    )

    async def exercise() -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        response = await run_evolution(request)
        route_latency = loop.time() - started_at
        body_sent = asyncio.Event()

        async def send(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.body":
                body_sent.set()

        async def receive() -> dict[str, object]:
            return {"type": "http.disconnect"}

        response_task = asyncio.create_task(response({"type": "http"}, receive, send))
        await asyncio.wait_for(body_sent.wait(), timeout=0.1)
        body_latency = loop.time() - started_at
        release.set()
        await response_task
        return route_latency, body_latency

    timer = threading.Timer(0.25, release.set)
    timer.start()
    try:
        route_latency, body_latency = asyncio.run(exercise())
    finally:
        release.set()
        timer.cancel()

    assert started.is_set()
    assert route_latency < 0.1
    assert body_latency < 0.1


def test_evolution_hypotheses(client: TestClient) -> None:
    response = client.get("/evolution/hypotheses")
    assert response.status_code == 200
    assert "进化假设" in response.text
    assert 'data-evolution-page="hypotheses"' in response.text
    assert 'data-evolution-hypotheses="true"' in response.text
    assert 'data-evolution-readable="true"' in response.text
    assert r'.text-\[9px\] { font-size: 11px; }' in response.text
    assert r'.text-\[14px\] { font-size: 16px; }' in response.text
    assert 'class="mt-1 text-[14px] text-muted">先看问题来源和修改方案' in response.text
    assert 'data-evolution-quality-gate="true"' not in response.text
    assert 'data-evolution-readiness="true"' not in response.text
    assert 'data-evolution-experiments="true"' not in response.text
    assert "当前没有可生成假设的证据" in response.text
    assert "其中 0 份“合格”" in response.text


def test_evolution_hypotheses_render_table_with_collapsible_dossiers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypotheses",
        lambda: [
            {
                "hypothesis_id": "hyp-1",
                "capability_id": "workflow-discipline",
                "kind": "REPAIR",
                "title": "修复归档前验证遗漏",
                "confidence": 0.82,
                "selected_at": None,
                "cycle_started_at": "2026-08-10T08:00:00Z",
                "payload_json": json.dumps(
                    {
                        "rationale": "归档前缺少验证证据",
                        "created_at": "2026-08-10T08:00:00Z",
                        "acceptance": {
                            "case_version_ids": [],
                            "case_selection_mode": "AI",
                            "auto_workflow_confirmation": True,
                            "expected_effect": "减少归档失败",
                        },
                    }
                ),
            }
        ],
    )
    monkeypatch.setattr(client.app.state.evolution_store, "list_experiments", lambda **kwargs: [])

    response = client.get("/evolution/hypotheses")

    assert response.status_code == 200
    assert 'data-hypothesis-table="true"' in response.text
    assert 'data-hypothesis-column-layout="balanced"' in response.text
    assert '<col style="width: 33%;">' in response.text
    assert response.text.count('<col style="width: 14%;">') >= 2
    assert 'style="min-width: 960px;"' in response.text
    assert 'class="px-5 py-3 text-right font-semibold">详情</th>' in response.text
    assert 'lg:whitespace-nowrap' in response.text
    assert 'data-hypothesis-row="hyp-1"' in response.text
    assert 'data-source-label="数据上报"' in response.text
    assert "eval 观察" not in response.text
    assert 'data-hypothesis-detail="hyp-1"' in response.text
    assert 'aria-expanded="false"' in response.text
    assert 'action="/evolution/hypotheses/hyp-1/delete"' in response.text
    assert 'data-evolution-delete="hypothesis"' in response.text
    assert 'data-hypothesis-detail-actions="hyp-1"' in response.text
    assert response.text.index('data-hypothesis-detail="hyp-1"') < response.text.index(
        'data-evolution-delete="hypothesis"'
    )
    assert "window.location.hash" in response.text
    assert "history.replaceState" in response.text
    assert "当前没有生成任务运行" in response.text
    assert 'data-status-code="PENDING_DECISION"' in response.text
    assert 'data-status-code="EXPERIMENT_STARTED"' in response.text
    assert 'data-status-code="IGNORED"' in response.text
    assert 'data-status-code="BLOCKED"' not in response.text
    assert response.text.count('data-status-code="') == 3
    assert "假设可信度" in response.text
    assert "仅用于排序" in response.text
    assert 'data-hypothesis-reasoning-grid="compact"' in response.text
    assert "展开详情" in response.text


def test_evolution_hypotheses_show_source_chinese_capability_and_change_plan(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = []
    for index, (source_kind, semantic_source) in enumerate(
        (("EVAL_LOG", "RULE"), ("BENCH_RUN", "BENCH_REMEDIATION")),
        start=1,
    ):
        rows.append(
            {
                "hypothesis_id": f"hyp-{index}",
                "capability_id": "workflow-discipline",
                "kind": "REPAIR",
                "title": f"假设 {index}",
                "confidence": 0.8,
                "selected_at": None,
                "ignored_at": None,
                "ignore_reason": None,
                "cycle_started_at": f"2026-08-{10 + index}T08:00:00Z",
                "payload_json": json.dumps(
                    {
                        "source_kind": source_kind,
                        "semantic_source": semantic_source,
                        "rationale": "发现工作流问题",
                        "intervention": "只修改归档命令的验证顺序。",
                        "validation_plan": "复用原 Case 做 Base/Candidate 对比。",
                        "acceptance": {
                            "case_version_ids": ["case-1"],
                            "expected_effect": "归档前完成验证",
                        },
                    }
                ),
            }
        )
    monkeypatch.setattr(client.app.state.evolution_store, "list_hypotheses", lambda: rows)
    monkeypatch.setattr(client.app.state.evolution_store, "list_experiments", lambda **kwargs: [])

    response = client.get("/evolution/hypotheses?source=BENCH&page=1&page_size=1")

    assert response.status_code == 200
    assert 'data-source-label="评测发现"' in response.text
    assert 'data-source-label="数据上报"' not in response.text
    assert "HelixHarness 工作流纪律" in response.text
    assert "目标能力" in response.text
    assert "系统根据假设自动生成并冻结候选版本" in response.text
    assert "当前没有生成任务运行" in response.text
    assert "目标能力还没有确认可使用的评测工作流" not in response.text
    assert "工作流未授权" not in response.text
    assert "缺少评测 Case" not in response.text
    assert "暂不可推进" not in response.text
    assert "只修改归档命令的验证顺序。" in response.text
    assert "评测问题自计划" not in response.text
    assert "（北京时间）" not in response.text
    assert "第 1 / 1 页" in response.text
    assert "状态说明" in response.text
    assert "不采纳" in response.text


def test_evolution_hypotheses_can_focus_one_rejected_benchmark_run(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = []
    for run_id in ("sv2_rejected", "sv2_other"):
        rows.append(
            {
                "hypothesis_id": f"hyp-{run_id}",
                "capability_id": "workflow-discipline",
                "kind": "REPAIR",
                "title": f"修复 {run_id}",
                "confidence": 0.8,
                "selected_at": None,
                "ignored_at": None,
                "ignore_reason": None,
                "cycle_started_at": "2026-08-24T08:00:00Z",
                "payload_json": json.dumps(
                    {
                        "source_kind": "BENCH_RUN",
                        "semantic_source": "BENCH_REMEDIATION",
                        "source_run_id": run_id,
                        "rationale": "候选版本出现明确回退",
                        "intervention": "修复候选版本回退。",
                        "acceptance": {
                            "case_version_ids": ["case-1"],
                            "expected_effect": "消除回退",
                        },
                    }
                ),
            }
        )
    monkeypatch.setattr(client.app.state.evolution_store, "list_hypotheses", lambda: rows)
    monkeypatch.setattr(client.app.state.evolution_store, "list_experiments", lambda **kwargs: [])

    response = client.get(
        "/evolution/hypotheses",
        params={"source": "BENCH", "source_run_id": "sv2_rejected"},
    )

    assert response.status_code == 200
    assert 'data-source-run-filter="sv2_rejected"' in response.text
    assert 'name="source_run_id" value="sv2_rejected"' in response.text
    assert "候选版本出现明确回退" in response.text
    assert "修复 sv2_rejected" in response.text
    assert "修复 sv2_other" not in response.text
    assert 'href="/framework-evaluation/runs/sv2_rejected/view"' in response.text
    run_path = client.app.url_path_for("scenario_v2_run_view", run_id="sv2_rejected")
    assert f'href="{run_path}">来源 Run sv2_rejected</a>' in response.text
    assert 'data-hypothesis-generation-basis="hyp-sv2_rejected"' not in response.text
    assert "仅显示基于该任务不采纳结论形成的进化假设" in response.text


def test_evolution_hypotheses_explains_when_rejected_run_has_no_remediation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.scenario_store,
        "get_remediation_projection",
        lambda requested_run_id: {
            "run_id": requested_run_id,
            "status": "PENDING",
            "reason": "",
            "hypothesis_id": None,
            "updated_at": "2026-08-31T00:00:00Z",
        },
    )
    pending = client.get(
        "/evolution/hypotheses",
        params={"source": "BENCH", "source_run_id": "sv2_without_hypothesis"},
    )

    assert pending.status_code == 200
    assert 'data-source-remediation-status="PENDING"' in pending.text
    assert "后台正在检查该评测任务" in pending.text

    monkeypatch.setattr(
        client.app.state.scenario_store,
        "get_remediation_projection",
        lambda requested_run_id: {
            "run_id": requested_run_id,
            "status": "NOT_ELIGIBLE",
            "reason": "没有 Candidate 独有且证据充分的可修复问题",
            "hypothesis_id": None,
            "updated_at": "2026-08-31T00:00:01Z",
        },
    )
    ineligible = client.get(
        "/evolution/hypotheses",
        params={"source": "BENCH", "source_run_id": "sv2_without_hypothesis"},
    )

    assert ineligible.status_code == 200
    assert 'data-source-remediation-status="NOT_ELIGIBLE"' in ineligible.text
    assert "该评测任务没有可生成的修复假设" in ineligible.text
    assert "没有 Candidate 独有且证据充分的可修复问题" in ineligible.text


def test_evolution_hypothesis_stops_at_experiment_and_links_to_it(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypotheses",
        lambda: [
            {
                "hypothesis_id": "hyp-unclear",
                "capability_id": "workflow-discipline",
                "kind": "REPAIR",
                "title": "修复归档验证顺序",
                "confidence": 0.75,
                "selected_at": "2026-08-11T08:00:00Z",
                "ignored_at": None,
                "ignore_reason": None,
                "cycle_started_at": "2026-08-11T08:00:00Z",
                "payload_json": json.dumps(
                    {
                        "source_kind": "BENCH_RUN",
                        "semantic_source": "BENCH_REMEDIATION",
                        "rationale": "归档发生在验证之前",
                        "intervention": "调整归档命令的执行顺序。",
                        "acceptance": {
                            "case_version_ids": ["case-1"],
                            "auto_workflow_confirmation": True,
                        },
                    }
                ),
            }
        ],
    )
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_experiments",
        lambda **kwargs: [
            {
                "experiment_id": "exp-unclear",
                "hypothesis_id": "hyp-unclear",
                "state": "INCONCLUSIVE",
                "decision_reason": "证据不足",
                "candidate_id": "cand-1",
                "candidate_json": json.dumps(
                    {
                        "candidate_id": "cand-1",
                        "candidate_commit": "abc123def456",
                        "changed_paths": ["commands/archive.md"],
                        "diff_patch": "diff --git a/commands/archive.md b/commands/archive.md\n+先验证再归档",
                    }
                ),
            }
        ],
    )

    response = client.get("/evolution/hypotheses")

    assert response.status_code == 200
    assert 'data-status-code="EXPERIMENT_INCONCLUSIVE"' not in response.text
    assert 'data-status-code="EXPERIMENT_STARTED"' in response.text
    assert "已进入实验" in response.text
    assert 'href="/evolution/experiments#experiment-exp-unclear"' in response.text
    assert 'data-hypothesis-code-diff="hyp-unclear"' in response.text
    assert "候选版本改动" in response.text
    assert "变更概览" in response.text
    assert "范围校验通过" in response.text
    assert "越界版本会在生成阶段被阻断" in response.text
    assert "commands/archive.md" in response.text
    assert 'data-file-diff="commands/archive.md"' in response.text
    assert "展开 Diff" in response.text
    assert "diff --git a/commands/archive.md b/commands/archive.md" in response.text
    assert "<details open" not in response.text


def test_evolution_hypothesis_without_candidate_explains_why_diff_is_unavailable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypotheses",
        lambda: [
            {
                "hypothesis_id": "hyp-plan",
                "capability_id": "workflow-discipline",
                "kind": "REPAIR",
                "title": "待选择的修复方案",
                "confidence": 0.7,
                "selected_at": None,
                "cycle_started_at": "2026-08-11T08:00:00Z",
                "payload_json": json.dumps(
                    {
                        "source_kind": "BENCH_RUN",
                        "semantic_source": "BENCH_REMEDIATION",
                        "acceptance": {
                            "case_version_ids": ["case-1"],
                            "auto_workflow_confirmation": True,
                        },
                    }
                ),
            }
        ],
    )
    monkeypatch.setattr(client.app.state.evolution_store, "list_experiments", lambda **kwargs: [])

    response = client.get("/evolution/hypotheses")

    assert response.status_code == 200
    assert "当前没有生成任务运行" in response.text
    assert "生成候选版本" in response.text
    assert 'action="/evolution/hypotheses/hyp-plan/prepare-candidate"' in response.text


def test_evolution_hypothesis_shows_background_candidate_preparation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypotheses",
        lambda: [
            {
                "hypothesis_id": "hyp-building",
                "capability_id": "workflow-discipline",
                "kind": "REPAIR",
                "title": "后台生成候选",
                "confidence": 0.7,
                "selected_at": None,
                "cycle_started_at": "2026-08-11T08:00:00Z",
                "payload_json": json.dumps(
                    {
                        "source_kind": "BENCH_RUN",
                        "semantic_source": "BENCH_REMEDIATION",
                        "acceptance": {
                            "case_version_ids": ["case-1"],
                            "auto_workflow_confirmation": True,
                        },
                    }
                ),
            }
        ],
    )
    monkeypatch.setattr(client.app.state.evolution_store, "list_experiments", lambda **kwargs: [])
    monkeypatch.setattr(
        client.app.state.evolution_scheduler,
        "candidate_preparation_statuses",
        lambda: {
            "hyp-building": {
                "state": "RUNNING",
                "started_at": "2026-08-11T08:01:00Z",
                "finished_at": None,
                "error": None,
            }
        },
        raising=False,
    )

    response = client.get("/evolution/hypotheses")

    assert response.status_code == 200
    assert "候选版本准备中" in response.text
    assert "系统正在后台生成并冻结实际代码版本" in response.text
    assert 'action="/evolution/hypotheses/hyp-building/prepare-candidate"' not in response.text


def test_evolution_benchmark_remediation_candidate_is_only_approved_or_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypotheses",
        lambda: [
            {
                "hypothesis_id": "hyp-ready",
                "capability_id": "workflow-discipline",
                "kind": "REPAIR",
                "title": "修复归档验证顺序",
                "confidence": 0.75,
                "selected_at": None,
                "ignored_at": None,
                "ignore_reason": None,
                "cycle_started_at": "2026-08-11T08:00:00Z",
                "payload_json": json.dumps(
                    {
                        "source_kind": "BENCH_RUN",
                        "semantic_source": "BENCH_REMEDIATION",
                        "rationale": "归档发生在验证之前",
                        "intervention": "调整归档命令的执行顺序。",
                        "acceptance": {
                            "case_version_ids": [],
                            "auto_workflow_confirmation": False,
                            "expected_effect": "归档前完成验证",
                            "regression_scope": ["case-regression-1"],
                        },
                    }
                ),
            }
        ],
    )
    monkeypatch.setattr(client.app.state.evolution_store, "list_experiments", lambda **kwargs: [])
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_hypothesis_candidates",
        lambda: [
            {
                "hypothesis_id": "hyp-ready",
                "payload_json": json.dumps(
                    {
                        "candidate_id": "cand-ready",
                        "candidate_commit": "abc123def456",
                        "changed_paths": ["skills/apply/SKILL.md", "skills/apply/tests"],
                        "diff_summary": "legacy candidate stat summary",
                        "diff_patch": "diff --git a/skills/apply/SKILL.md b/skills/apply/SKILL.md\n+先验证再归档",
                    }
                ),
            }
        ],
    )

    response = client.get("/evolution/hypotheses")

    assert response.status_code == 200
    assert "为什么要改" in response.text
    assert "计划怎么改" in response.text
    assert "候选版本改动" in response.text
    assert "变更概览" in response.text
    assert "1 个文件" in response.text
    assert 'data-file-diff="skills/apply/SKILL.md"' in response.text
    assert "skills/apply/tests" not in response.text
    assert "legacy candidate stat summary" not in response.text
    assert "展开 Diff" in response.text
    assert "范围校验通过" in response.text
    assert "采纳并进入实验" in response.text
    assert "不采纳并结束" in response.text
    assert "待确认" in response.text
    assert "工作流未授权" not in response.text
    assert "缺少评测 Case" not in response.text
    assert 'data-hypothesis-generation-basis="hyp-ready"' in response.text
    assert "生成依据与评测范围" in response.text
    assert "case-regression-1" in response.text
    assert response.text.index('data-hypothesis-code-diff="hyp-ready"') < response.text.index(
        'data-hypothesis-generation-basis="hyp-ready"'
    )
    assert "不采纳" in response.text
    assert "生成候选方案" not in response.text
    assert "发起评测实验" not in response.text


def test_ignore_hypothesis_action_redirects_to_filtered_pool(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ignored: list[tuple[str, str]] = []
    monkeypatch.setattr(
        client.app.state.evolution_service,
        "ignore_hypothesis",
        lambda hypothesis_id, reason: ignored.append((hypothesis_id, reason)),
        raising=False,
    )

    response = client.post(
        "/evolution/hypotheses/hyp-1/ignore",
        data={"reason": "暂不采纳"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/evolution/hypotheses"
    assert ignored == [("hyp-1", "暂不采纳")]


def test_delete_hypothesis_action_removes_it_from_the_worklist(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        client.app.state.evolution_service,
        "delete_hypothesis",
        lambda hypothesis_id, reason: deleted.append((hypothesis_id, reason)),
        raising=False,
    )

    response = client.post(
        "/evolution/hypotheses/hyp-1/delete",
        data={"reason": "人工从工作台删除"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/evolution/hypotheses"
    assert deleted == [("hyp-1", "人工从工作台删除")]


def test_prepare_hypothesis_candidate_action_returns_to_diff_review(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared: list[str] = []
    monkeypatch.setattr(
        client.app.state.evolution_service,
        "prepare_hypothesis_candidate",
        lambda hypothesis_id: prepared.append(hypothesis_id),
        raising=False,
    )

    response = client.post(
        "/evolution/hypotheses/hyp-1/prepare-candidate",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/evolution/hypotheses#hypothesis-hyp-1"
    assert client.app.state.evolution_scheduler.wait_for_candidate_preparation(
        "hyp-1",
        timeout=1,
    )
    assert prepared == ["hyp-1"]


def test_candidate_preparation_does_not_block_other_http_requests(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_preparation(hypothesis_id: str) -> None:
        assert hypothesis_id == "hyp-1"
        started.set()
        release.wait(timeout=0.5)

    monkeypatch.setattr(
        client.app.state.evolution_service,
        "prepare_hypothesis_candidate",
        blocking_preparation,
    )

    started_at = time.monotonic()
    response = client.post(
        "/evolution/hypotheses/hyp-1/prepare-candidate",
        follow_redirects=False,
    )
    route_latency = time.monotonic() - started_at
    try:
        assert response.status_code == 303
        assert started.wait(timeout=0.2)
        assert route_latency < 0.2
        assert client.get("/").status_code == 200
    finally:
        release.set()


def test_evolution_experiments(client: TestClient) -> None:
    response = client.get("/evolution/experiments")
    assert response.status_code == 200
    assert "实验只评测" in response.text
    assert 'data-evolution-page="experiments"' in response.text
    assert 'data-evolution-experiments="true"' in response.text
    assert 'data-evolution-readable="true"' in response.text
    assert r'.text-\[9px\] { font-size: 11px; }' in response.text
    assert r'.text-\[14px\] { font-size: 16px; }' in response.text
    assert 'class="mt-1 text-[14px] text-muted">验证一个已冻结的 Candidate' in response.text
    assert 'data-evolution-quality-gate="true"' not in response.text
    assert 'data-evolution-readiness="true"' not in response.text
    assert 'data-evolution-hypotheses="true"' not in response.text


def test_evolution_experiments_explain_change_validation_and_result(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client.app.state.evolution_store,
        "list_experiments",
        lambda **kwargs: [
            {
                "experiment_id": "exp-1",
                "hypothesis_id": "hyp-1",
                "cycle_id": "cycle-1",
                "kind": "REPAIR",
                "capability_id": "workflow-discipline",
                "title": "修复归档验证遗漏",
                "state": "REJECTED",
                "decision_reason": "Candidate 未消除原问题",
                "created_at": "2026-08-11T08:00:00Z",
                "updated_at": "2026-08-11T09:00:00Z",
                "benchmark_run_id": "run-1",
                "candidate_json": json.dumps(
                    {
                        "changed_paths": ["commands/archive.md"],
                        "diff_patch": "diff --git a/commands/archive.md b/commands/archive.md\n+先验证再归档",
                    }
                ),
                "evaluation_plan_json": json.dumps({"case_ids": ["case-1"]}),
                "hypothesis_json": json.dumps(
                    {
                        "source_kind": "BENCH_RUN",
                        "intervention": "调整归档命令，先验证再归档。",
                        "validation_plan": "复用失败 Run 的 Case 和工作流重评。",
                        "acceptance": {"expected_effect": "不再遗漏验证"},
                    }
                ),
            }
        ],
    )

    response = client.get("/evolution/experiments?state=REJECTED")

    assert response.status_code == 200
    run_path = client.app.url_path_for("scenario_v2_run_view", run_id="run-1")
    assert f'href="{run_path}">查看 Benchmark Run run-1</a>' in response.text
    assert "假设决定要不要做，实验记录做完后的结果" in response.text
    assert 'data-experiment-flow="compact"' in response.text
    assert 'data-experiment-table="true"' in response.text
    assert 'data-experiment-column-layout="balanced"' in response.text
    assert '<col style="width: 30%;">' in response.text
    assert '<col style="width: 15%;">' in response.text
    assert 'style="min-width: 960px;"' in response.text
    assert 'class="px-5 py-3 text-right font-semibold">详情</th>' in response.text
    assert 'lg:whitespace-nowrap' in response.text
    assert 'data-experiment-row="exp-1"' in response.text
    assert 'data-experiment-detail="exp-1"' in response.text
    assert 'data-experiment-toggle="exp-1"' in response.text
    assert 'action="/evolution/experiments/exp-1/delete"' in response.text
    assert 'data-evolution-delete="experiment"' in response.text
    assert 'data-experiment-detail-actions="exp-1"' in response.text
    assert response.text.index('data-experiment-detail="exp-1"') < response.text.index(
        'data-evolution-delete="experiment"'
    )
    assert 'name="q"' in response.text
    assert 'name="source"' in response.text
    assert 'name="kind"' in response.text
    assert 'name="state"' in response.text
    assert "第 1 / 1 页 · 共 1 条" in response.text
    assert "实际代码改动" in response.text
    assert "commands/archive.md" in response.text
    assert 'data-file-diff="commands/archive.md"' in response.text
    assert "展开 Diff" in response.text
    assert "diff --git a/commands/archive.md b/commands/archive.md" in response.text
    assert "这次改什么" not in response.text
    assert "Benchmark 执行" in response.text
    assert "复用失败 Run 的 Case 和工作流重评。" in response.text
    assert "评测结论" in response.text
    assert "Candidate 未消除原问题" in response.text
    assert "HelixHarness 工作流纪律" in response.text


def test_delete_experiment_action_removes_it_from_the_worklist(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        client.app.state.evolution_service,
        "delete_experiment",
        lambda experiment_id, reason: deleted.append((experiment_id, reason)),
        raising=False,
    )

    response = client.post(
        "/evolution/experiments/exp-1/delete",
        data={"reason": "人工从工作台删除"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/evolution/experiments"
    assert deleted == [("exp-1", "人工从工作台删除")]


def test_removed_improve_training_routes_are_gone(client: TestClient) -> None:
    assert client.get("/evolution/train-config").status_code == 404
    assert client.get("/evolution/monitor").status_code == 404


def test_favicon_redirects_to_static_asset(client: TestClient) -> None:
    response = client.get("/favicon.ico", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/static/favicon.svg"


def test_dashboard_workflow_scope_survives_partial_navigation(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval.stage_supplements import StageSupplementStore
    from eval.tests.test_dashboard_scope import _write
    from eval.tests.test_usage_loader import _workflow_log
    from web_ui.routes import eval as routes

    root = tmp_path / "repo"
    _write(root, "workflow-scope", _workflow_log())
    monkeypatch.setattr(routes, "_resolve_roots", lambda: [str(root)])
    client.app.state.stage_supplement_store = StageSupplementStore(tmp_path / "supplements.db")
    response = client.get('/?workflow=workflow&view=user&runtime_period=all')
    assert response.status_code == 200
    assert 'name="workflow"' not in response.text
    assert 'data-distribution="workflow"' not in response.text
    assert 'hx-get="/partial/detail?view=user"' in response.text
    partial = client.get('/partial/detail?workflow=workflow&view=user&runtime_period=7d')
    assert partial.status_code == 200
    assert 'workflow=workflow' not in partial.text
    assert 'runtime_period=7d' in partial.text
    runtime = client.get('/partial/runtime?workflow=workflow&view=user&runtime_period=all')
    assert runtime.status_code == 200
    assert 'name="workflow"' not in runtime.text
    assert 'name="view" value="user"' in runtime.text
    assert 'data-period="all"' in runtime.text
    default = client.get('/?workflow=workflow')
    assert 'data-period="all"' in default.text
    assert 'data-period="all"' in client.get('/partial/runtime?workflow=workflow').text
    explicit = client.get('/?workflow=workflow&runtime_period=30d')
    assert 'data-period="30d"' in explicit.text
    assert 'hx-get="/partial/detail?view=user&amp;runtime_period=30d' in explicit.text

    assert 'workflow=workflow' not in runtime.text
    unknown = client.get('/?workflow=unrecognized')
    assert 'name="workflow"' not in unknown.text


def test_recent_growth_controls_preserve_date_and_dimension(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval.stage_supplements import StageSupplementStore
    from eval.tests.test_dashboard_scope import _write
    from eval.tests.test_usage_loader import _workflow_log
    from web_ui.routes import eval as routes

    root = tmp_path / "repo"
    _write(root, "recent-scope", _workflow_log())
    monkeypatch.setattr(routes, "_resolve_roots", lambda: [str(root)])
    client.app.state.stage_supplement_store = StageSupplementStore(tmp_path / "supplements.db")
    result = client.get('/?workflow=workflow&view=user&recent_days=3')
    assert result.status_code == 200
    assert 'data-days="3"' in result.text
    assert '<span>新增用户 <strong data-new-users>' in result.text
    assert '首次使用 HelixHarness 的用户' not in result.text
    assert 'name="recent_days" value="3"' in result.text
    assert 'name="workflow"' not in result.text
    assert 'name="view" value="user"' in result.text
    assert 'recent_days=3' in result.text
    partial = client.get('/partial/detail?workflow=workflow&view=user&recent_days=30')
    assert 'recent_days=30' in partial.text and 'workflow=workflow' not in partial.text
    runtime = client.get('/partial/runtime?workflow=workflow&recent_days=30')
    assert 'name="recent_days" value="30"' in runtime.text
    assert 'data-days="1"' in client.get('/?recent_days=90').text
    default = client.get('/')
    assert 'data-days="1"' in default.text
    assert 'value="1" form="helixharness-overview-form" aria-current="true">今日</button>' in default.text
    assert 'name="recent_days" value="1"' in client.get('/partial/runtime').text
    today_partial = client.get('/partial/detail?recent_days=1')
    assert 'hx-get="/partial/detail?view=project"' in today_partial.text
    today = client.get('/?recent_days=1')
    assert 'data-days="1"' in today.text
    assert '<b>今日新增</b>' in today.text
    assert 'value="1" form="helixharness-overview-form" aria-current="true">今日</button>' in today.text


def test_dashboard_persists_admissions_instead_of_old_workflow_dates(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval.stage_supplements import StageSupplementStore
    from eval.tests.test_dashboard_scope import _write
    from eval.tests.test_usage_loader import _workflow_log
    from eval.usage_first_seen import UsageFirstSeenStore
    from web_ui.routes import eval as routes

    root = tmp_path / "repo"
    monkeypatch.setattr(routes, "_resolve_roots", lambda: [str(root)])
    client.app.state.stage_supplement_store = StageSupplementStore(tmp_path / "supplements.db")
    # Initialize an empty platform, then discover a project with historical workflow dates.
    assert client.get("/").status_code == 200
    _write(root, "arriving-today", _workflow_log())
    response = client.get("/")
    assert "<strong data-new-projects>1</strong>" in response.text
    assert "<strong data-new-users>1</strong>" in response.text
    admissions = client.app.state.usage_first_seen_store
    snapshot = admissions.snapshot()
    client.app.state.usage_first_seen_store = UsageFirstSeenStore(admissions.cache_dir)
    for workflow in ("all", "workflow", "workflow", "all"):
        result = client.get(f"/?workflow={workflow}")
        assert result.status_code == 200
        assert "<span>新增用户 <strong data-new-users>" in result.text
    assert client.app.state.usage_first_seen_store.snapshot() == snapshot


def test_feedback_is_registered_and_explains_setup(client: TestClient) -> None:
    page = client.get("/feedback")
    assert page.status_code == 200
    assert "~/.helixharness/feedback/config.json" in page.text
    assert client.get("/feedback/detail/example").status_code == 404
    assert client.post("/feedback/report", json={}).status_code == 400
    assert 'href="/feedback"' in client.get("/").text
    assert hasattr(client.app.state, "feedback_store")


def test_dashboard_aggregates_each_feature_once_per_request(client, monkeypatch, tmp_path):
    from collections import Counter

    import eval.usage_loader as loader
    from eval.stage_supplements import StageSupplementStore
    from eval.tests.test_dashboard_scope import _write
    from eval.tests.test_usage_loader import _workflow_log
    from web_ui.routes import eval as routes

    root = tmp_path / "repo"
    _write(root, "performance-feature", _workflow_log())
    monkeypatch.setattr(routes, "_resolve_roots", lambda: [str(root)])
    client.app.state.stage_supplement_store = StageSupplementStore(tmp_path / "supplements.db")
    calls = Counter()
    original = loader.aggregate_feature

    def counted(log, fallback_root=""):
        calls[(fallback_root, str(log.get("feature")))] += 1
        return original(log, fallback_root)

    monkeypatch.setattr(loader, "aggregate_feature", counted)
    response = client.get("/")
    assert response.status_code == 200
    assert calls
    assert max(calls.values()) == 1, calls
