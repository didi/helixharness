"""Exercise data-source selection through the real dashboard and partial routes."""

import pytest
from fastapi.testclient import TestClient

from eval import usage_sync
from web_ui.main import create_app


@pytest.fixture
def source_environment(tmp_path, monkeypatch):
    cache = tmp_path / "sync-cache"
    config = tmp_path / "repos.yaml"
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CACHE", cache)
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CONFIG", config)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE", str(cache))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_SYNC_CONFIG", str(config))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE", "auto")
    return cache, config


@pytest.mark.parametrize("mode", ["auto", "demo"])
def test_unconfigured_dashboard_generates_isolated_synthetic_data(source_environment, monkeypatch, mode):
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE", mode)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_FIRST_SEEN_DB_PATH", str(source_environment[0].parent / "real-first-seen.db"))
    app = create_app()
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Demo project (synthetic)" in page.text
        assert "72.0k" in page.text
        assert "合成示例" in page.text
        for url in ("/partial/detail?view=user", "/partial/runtime?runtime_period=all"):
            partial = client.get(url)
            assert partial.status_code == 200
            assert "暂无统计数据" not in partial.text
        # Viewing examples must not create real telemetry/evolution evidence.
        assert app.state.telemetry_quality_store.automation_evidence() == []
        assert app.state.stage_supplement_store.active_linked_records() == []
        assert app.state.usage_first_seen_store.snapshot()["projects"] == {}
        monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_ENABLED", "true")
        status, *_ = usage_sync._run_usage_ai_regenerate("按需求维度", [])
        assert "当前为合成示例" in status


@pytest.mark.parametrize("selection", ["sync", "configured_repo", "empty_cache", "local"])
def test_real_source_selection_does_not_silently_show_examples(source_environment, monkeypatch, tmp_path, selection):
    cache, config = source_environment
    if selection == "sync":
        monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE", "sync")
    elif selection == "configured_repo":
        config.write_text('repos:\n  - name: synthetic\n    url: https://example.test/repo.git\n')
    elif selection == "empty_cache":
        cache.mkdir()
        (cache / "roots.txt").write_text("")
    else:
        local = tmp_path / "local-project"
        local.mkdir()
        monkeypatch.setenv("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", str(local))
    with TestClient(create_app()) as client:
        page = client.get("/")
        assert page.status_code == 200
        if selection != "local":
            assert "暂无统计数据" in page.text
        assert "Demo project (synthetic)" not in page.text


def test_synced_source_overrides_local_roots(source_environment, monkeypatch, tmp_path):
    cache, _ = source_environment
    cache.mkdir()
    synced = tmp_path / "synced"
    synced.mkdir()
    (cache / "roots.txt").write_text(str(synced))
    assert usage_sync._current_usage_roots([str(tmp_path / "local")], "auto") == [str(synced)]
