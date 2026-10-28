from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from web_ui.main import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH", str(tmp_path / "evaluation.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(tmp_path / "platform-config.json"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_TELEMETRY_DB_PATH", str(tmp_path / "telemetry.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH", str(tmp_path / "stage-supplements.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_DB_PATH", str(tmp_path / "evolution.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_SCHEDULER_ENABLED", "false")
    return TestClient(create_app())


def test_evaluation_center_exposes_only_current_routes(client: TestClient) -> None:
    overview = client.get("/framework-evaluation")

    assert overview.status_code == 200
    assert 'data-evaluation-overview="true"' in overview.text
    assert 'href="/framework-evaluation/runs/new"' in overview.text
    assert 'href="/framework-evaluation/cases"' in overview.text
    assert overview.text.count('data-overview-metric="true"') == 3
    assert "新版结构" not in overview.text
    assert "Scenario 结构" not in overview.text
    assert "可正式评测" not in overview.text
    assert "发现型" not in overview.text
    assert "修复型" not in overview.text
    assert "当前 Case 库中已发布并可查看的评测用例" in overview.text
    assert "候选方案相对基准方案的得分回退或额外硬门禁" in overview.text
    evolution_card = overview.text.split('data-metric="evolution-runs"', 1)[1].split("</a>", 1)[0]
    assert "已完成 0" in evolution_card
    assert "已出报告 0" in evolution_card
    assert "查看自进化任务" not in evolution_card
    assert "平台管理" not in overview.text

    for path in (
        "/framework-evaluation/legacy",
        "/framework-evaluation/legacy/cases",
        "/framework-evaluation/legacy/runs",
        "/framework-evaluation/v2/runs",
        "/framework-evaluation/v2/runs/new",
        "/framework-evaluation/reports",
    ):
        assert client.get(path, follow_redirects=False).status_code == 404


def test_config_and_rules_remain_available(client: TestClient) -> None:
    config = client.get("/framework-evaluation/config")
    assert config.status_code == 200
    assert "Codex 子 Agent Provider" not in config.text
    assert "Codex Provider" not in config.text
    assert "本页面不会显示或保存凭据" not in config.text
    assert "subagent_config_json" not in config.text
    assert "spawn_agent" not in config.text
    assert 'data-config-row="repository-reference"' in config.text
    repository_row = config.text.split('data-config-row="repository-reference"', 1)[1].split("</div>", 1)[0]
    assert 'name="helixharness_repository"' not in repository_row
    assert "readonly" in repository_row
    assert "Bench 评测" in config.text
    assert "Candidate 生成模型" in config.text
    assert "单任务执行侧并发" in config.text
    assert config.text.count('name="formal_max_concurrency" type="number" min="1" max="4"') == 1
    assert config.text.count('type="number" min="1" max="4"') == 4
    assert 'data-config-tip=' not in config.text
    assert config.text.count('_token_budget_k"') == 4
    assert 'data-config-group="model-bindings"' in config.text
    assert 'data-config-group="formal-evaluation"' in config.text
    assert 'data-config-group="self-evolution"' in config.text
    assert 'data-config-group="case-calibration"' not in config.text
    for role in ("execution", "judge", "analysis", "candidate"):
        assert config.text.count(f'name="{role}_model"') == 1
        assert config.text.count(f'name="{role}_reasoning_effort"') == 1
    assert client.get("/framework-evaluation/rules").status_code == 200
    assert client.get("/framework-evaluation/resource-status").status_code == 200


def test_config_save_ignores_removed_subagent_field(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "saved-platform-config.json"
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(config_path))

    def configured_runtime(*args, **kwargs):
        from bench.task_configuration import task_platform_configuration

        current = task_platform_configuration()
        return SimpleNamespace(
            analyzer=None, evaluation_gateway=None,
            candidate_builder=SimpleNamespace(
                model=current.models.candidate_model,
                reasoning_effort=current.models.candidate_reasoning_effort,
                timeout_seconds=current.evolution_candidate.wall_timeout_seconds,
            ),
        )

    monkeypatch.setattr("web_ui.main._evolution_service", configured_runtime)

    form = {
        "helixharness_repository": str(tmp_path / "helixharness"),
        "reference_ref": "main",
        "execution_model": "execution-model",
        "judge_model": "judge-model",
        "analysis_model": "analysis-model",
        "candidate_model": "candidate-model",
        "execution_reasoning_effort": "low",
        "judge_reasoning_effort": "high",
        "analysis_reasoning_effort": "medium",
        "candidate_reasoning_effort": "xhigh",
        "permission_mode": "workspace-write",
        "formal_wall_timeout_seconds": "600",
        "formal_token_budget_k": "50",
        "formal_tool_call_budget": "200",
        "formal_disk_budget_mb": "2048",
        "formal_max_concurrency": "2",
        "analysis_wall_timeout_seconds": "180",
        "analysis_token_budget_k": "20",
        "analysis_tool_call_budget": "40",
        "analysis_disk_budget_mb": "512",
        "analysis_max_concurrency": "1",
        "candidate_wall_timeout_seconds": "900",
        "candidate_token_budget_k": "80",
        "candidate_tool_call_budget": "400",
        "candidate_disk_budget_mb": "4096",
        "candidate_max_concurrency": "1",
        "evolution_evaluation_wall_timeout_seconds": "1200",
        "evolution_evaluation_token_budget_k": "100",
        "evolution_evaluation_tool_call_budget": "500",
        "evolution_evaluation_disk_budget_mb": "5120",
        "evolution_evaluation_max_concurrency": "1",
        "subagent_config_json": '{"enabled":true}',
    }
    response = client.post(
        "/framework-evaluation/config",
        data=form,
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "helixharness_repository" not in saved
    assert "subagent_config" not in saved
    assert saved["schema_version"] == "helixharness-platform-workload-config.v4"
    assert saved["models"] == {
        "execution_model": "execution-model",
        "judge_model": "judge-model",
        "analysis_model": "analysis-model",
        "candidate_model": "candidate-model",
        "execution_reasoning_effort": "low",
        "judge_reasoning_effort": "high",
        "analysis_reasoning_effort": "medium",
        "candidate_reasoning_effort": "xhigh",
    }
    assert saved["resources"]["formal_evaluation"]["max_concurrency"] == 2
    assert saved["resources"]["formal_evaluation"]["token_budget"] == 50_000
    assert saved["resources"]["evolution_analysis"]["max_concurrency"] == 1
    assert saved["resources"]["evolution_analysis"]["token_budget"] == 20_000
    assert saved["resources"]["evolution_candidate"]["token_budget"] == 80_000
    assert saved["resources"]["evolution_evaluation"]["max_concurrency"] == 1
    assert saved["resources"]["evolution_evaluation"]["token_budget"] == 100_000
    assert "case_calibration" not in saved["resources"]
    service = client.app.state.evolution_service
    service.run_cycle([], force=True)
    assert service.candidate_builder.model == "candidate-model"
    assert service.candidate_builder.reasoning_effort == "xhigh"
    assert service.candidate_builder.timeout_seconds == 900

    form["formal_max_concurrency"] = "5"
    rejected = client.post("/framework-evaluation/config", data=form)
    assert rejected.status_code == 422
    assert "formal_max_concurrency 必须在 1 到 4 之间" in rejected.text


def test_summary_uses_only_scenario_v2_store(client: TestClient) -> None:
    response = client.get("/framework-evaluation/api/summary")

    assert response.status_code == 200
    assert response.json() == {
        "published_scenarios": 0,
        "calibrated_scenarios": 0,
        "pending_scenarios": 0,
        "failed_scenarios": 0,
        "runs": 0,
        "completed_runs": 0,
        "failed_runs": 0,
        "active_runs": 0,
        "reported_runs": 0,
        "evolution_runs": 0,
        "evolution_completed_runs": 0,
        "evolution_failed_runs": 0,
        "evolution_active_runs": 0,
        "evolution_reported_runs": 0,
    }
