"""Tests for persistent operator-owned eval Feature exclusions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.cli.sync_helixharness_usage import _eval_inventory
from eval.feature_exclusions import FeatureExclusionStore
from eval.telemetry_quality import TelemetryQualityStore
from eval.usage_loader import discover_projects


def _write_eval_log(root: Path, feature: str) -> None:
    from eval.tests.test_usage_loader import _workflow_log

    data = _workflow_log()
    data["feature"] = feature
    data["project"]["project_name"] = root.name
    path = root / "docs/eval" / feature / "eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))


def test_operator_exclusion_filters_dashboard_and_sync_inventory(tmp_path: Path) -> None:
    repository = tmp_path / "demo-service"
    _write_eval_log(repository, "remove-me")
    _write_eval_log(repository, "keep-me")
    store = FeatureExclusionStore(tmp_path / "feature-exclusions.db")
    store.exclude("demo-service", "remove-me", reason="operator request")

    exclusions = store.active_pairs()
    projects = discover_projects([str(repository)], excluded_features=exclusions)
    inventory = _eval_inventory(
        repository,
        "docs/eval",
        repository="demo-service",
        excluded_features=exclusions,
    )

    assert [item["feature"] for item in projects[0]["features"]] == ["keep-me"]
    assert inventory == {
        "eval_log_count": 1,
        "schema_2_log_count": 1,
        "valid_log_count": 1,
        "invalid_log_count": 0,
    }


def test_exclusion_store_is_idempotent_and_repository_scoped(tmp_path: Path) -> None:
    store = FeatureExclusionStore(tmp_path / "feature-exclusions.db")

    store.exclude("repo-a", "same-feature", reason="first request")
    store.exclude("repo-a", "same-feature", reason="repeated request")

    assert store.active_pairs() == {("repo-a", "same-feature")}
    assert ("repo-b", "same-feature") not in store.active_pairs()


def test_quality_projection_removes_excluded_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo-a"
    _write_eval_log(repository, "remove-me")
    exclusion_path = tmp_path / "feature-exclusions.db"
    store = FeatureExclusionStore(exclusion_path)
    quality_store = TelemetryQualityStore(tmp_path / "telemetry.db")

    first = quality_store.refresh([repository])
    assert first.discovered == 1

    store.exclude("repo-a", "remove-me", reason="operator request")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FEATURE_EXCLUSION_DB_PATH", str(exclusion_path))
    second = quality_store.refresh([repository])

    assert second.discovered == 0
    assert quality_store.invalid_source_paths() == set()
