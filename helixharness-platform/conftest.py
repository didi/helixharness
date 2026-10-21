import os

import pytest

os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_COUNT"] = "3"
os.environ["GIT_CONFIG_KEY_0"] = "core.hooksPath"
os.environ["GIT_CONFIG_VALUE_0"] = os.devnull
os.environ["GIT_CONFIG_KEY_1"] = "user.name"
os.environ["GIT_CONFIG_VALUE_1"] = "Demo"
os.environ["GIT_CONFIG_KEY_2"] = "user.email"
os.environ["GIT_CONFIG_VALUE_2"] = "demo@example.test"


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("HELIXHARNESS_PLATFORM_"):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "HELIXHARNESS_PLATFORM_FEEDBACK_DB_PATH",
        "HELIXHARNESS_PLATFORM_TELEMETRY_DB_PATH",
        "HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH",
        "HELIXHARNESS_PLATFORM_EVOLUTION_DB_PATH",
        "HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH",
    ):
        monkeypatch.setenv(key, str(tmp_path / (key + ".db")))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE", str(tmp_path / "usage-cache"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_SYNC_CONFIG", str(tmp_path / "usage-repos.yaml"))
    from eval import usage_sync

    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CACHE", tmp_path / "usage-cache")
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_CONFIG", tmp_path / "usage-repos.yaml")
    monkeypatch.setattr(usage_sync, "USAGE_SYNC_STATE", tmp_path / "usage-cache/sync_state.json")

    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVALUATION_CODEX_MODEL", "demo-model")
