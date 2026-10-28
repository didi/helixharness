"""Follow the documented environment-only configuration through generation/display."""

from eval import usage_ai, usage_sync
from eval.demo import seed_demo
from eval.stage_supplements import StageSupplementStore
from eval.usage_loader import load_usage_dashboard_data, render_usage_global_overview_html


def test_local_project_environment_only_generation_is_displayed(tmp_path, monkeypatch):
    project = tmp_path / "business" / "project"
    cache = tmp_path / "separate-platform-cache"
    database = cache / "stage_supplements.db"
    seed_demo(project, database)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE", "auto")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE", str(cache))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH", str(database))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_ENABLED", "true")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_BASE_URL", "https://model.example.test/v1")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_MODEL", "synthetic-model")
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CONFIG", tmp_path / "absent.yaml")
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CACHE", cache)
    calls = []

    def answer(endpoint, question, token, timeout_sec=600, *, model):
        calls.append((endpoint, model))
        return {"answer": "合成测试分析已生成，三个阶段均已归档。", "stats": None, "mode": "direct_api"}

    monkeypatch.setattr(usage_ai, "_call_api", answer)
    status, *_ = usage_sync._run_usage_ai_regenerate("按需求维度", [str(project)])
    assert "AI 分析已重新生成" in status
    assert calls and all(call == ("https://model.example.test/v1", "synthetic-model") for call in calls)
    assert not (tmp_path / "absent.yaml").exists()
    store = StageSupplementStore(database)
    store.refresh_links([str(project)])
    linked = store.active_linked_records()
    data = load_usage_dashboard_data([str(project)], linked_supplements=linked)
    page = render_usage_global_overview_html([str(project)], linked_supplements=linked, dashboard_data=data)
    assert "合成测试分析已生成" in page
    assert "synthetic-test-key" not in (cache / "ai_insights.json").read_text()
