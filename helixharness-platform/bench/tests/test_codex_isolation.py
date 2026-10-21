from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from bench.codex_execution import write_isolated_codex_config
from bench.codex_runtime_launcher import sanitized_environment


def test_isolated_codex_config_enables_native_multi_agent(tmp_path: Path) -> None:
    write_isolated_codex_config(tmp_path, tmp_path, tmp_path)

    root = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert root["features"]["multi_agent"] is True
    assert root["features"]["multi_agent_v2"] is True
    assert "agents" not in root
    assert "model_providers" not in root
    assert not (tmp_path / "agents").exists()


def test_isolated_codex_config_applies_evaluator_gradle_limits(tmp_path: Path) -> None:
    write_isolated_codex_config(tmp_path, tmp_path, tmp_path)

    gradle_properties = (tmp_path / "gradle-user-home/gradle.properties").read_text(encoding="utf-8")
    assert "org.gradle.jvmargs=-Xmx4096m" in gradle_properties
    assert "org.gradle.daemon=false" in gradle_properties


def test_sanitized_environment_drops_legacy_subagent_secrets(tmp_path: Path) -> None:
    environment = sanitized_environment(
        {
            "PATH": "/usr/bin",
            "HELIXHARNESS_PLATFORM_EVALUATION_SUBAGENT_REVIEWER_API_KEY": "do-not-forward",
        },
        codex_home=tmp_path,
    )

    assert environment["PATH"] == "/usr/bin"
    assert "HELIXHARNESS_PLATFORM_EVALUATION_SUBAGENT_REVIEWER_API_KEY" not in environment
