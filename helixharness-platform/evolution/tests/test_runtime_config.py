from __future__ import annotations

import json

import pytest

from evolution.runtime_config import load_runtime_config, save_runtime_config


def test_runtime_config_defaults_remediation_poll_to_thirty_seconds(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_RUNTIME_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.delenv("HELIXHARNESS_PLATFORM_EVOLUTION_REMEDIATION_POLL_SECONDS", raising=False)

    configuration = load_runtime_config()

    assert configuration.remediation_poll_seconds == 30.0


def test_runtime_config_round_trips_validated_values(tmp_path, monkeypatch) -> None:
    path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_RUNTIME_CONFIG_PATH", str(path))

    saved = save_runtime_config(
        {
            "interval_hours": "24",
            "poll_seconds": "120",
            "remediation_poll_seconds": "2.5",
        }
    )

    assert saved.interval_hours == 24
    assert load_runtime_config() == saved
    assert json.loads(path.read_text(encoding="utf-8"))["poll_seconds"] == 120


def test_runtime_config_rejects_out_of_range_values_without_overwriting(tmp_path, monkeypatch) -> None:
    path = tmp_path / "runtime_config.json"
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_RUNTIME_CONFIG_PATH", str(path))
    save_runtime_config(
        {
            "interval_hours": 48,
            "poll_seconds": 300,
            "remediation_poll_seconds": 1,
        }
    )
    before = path.read_bytes()

    with pytest.raises(ValueError, match="检查周期"):
        save_runtime_config(
            {
                "interval_hours": 0,
                "poll_seconds": 300,
                "remediation_poll_seconds": 1,
            }
        )

    assert path.read_bytes() == before
