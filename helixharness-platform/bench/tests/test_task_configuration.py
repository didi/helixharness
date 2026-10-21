from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.runtime_secrets import EvaluationProviderConfig
from bench.task_configuration import task_platform_configuration
from bench.workload_policy import (
    CASE_CALIBRATION,
    EVOLUTION_ANALYSIS,
    EVOLUTION_CANDIDATE,
    EVOLUTION_EVALUATION,
    FORMAL_EVALUATION,
)


def _provider() -> EvaluationProviderConfig:
    return EvaluationProviderConfig("codex", "openai", "", "", "provider-default", "TEST_KEY")


def test_default_formal_concurrency_matches_deployed_worker_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.delenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_MAX_CONCURRENCY", raising=False)

    configuration = task_platform_configuration(_provider())

    assert configuration.formal_evaluation.max_concurrency == 2
    assert configuration.evolution_evaluation.max_concurrency == 2


def test_workload_concurrency_is_capped_at_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "platform-config.json"
    path.write_text(
        json.dumps(
            {
                "resources": {
                    "formal_evaluation": {"max_concurrency": 12},
                    "evolution_evaluation": {"max_concurrency": 8},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(path))

    configuration = task_platform_configuration(_provider())

    assert configuration.formal_evaluation.max_concurrency == 4
    assert configuration.evolution_evaluation.max_concurrency == 4


def test_nested_platform_config_resolves_models_and_workload_policies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "platform-config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "helixharness-platform-workload-config.v4",
                "helixharness_repository": str(tmp_path / "helixharness"),
                "reference_ref": "main",
                "models": {
                    "execution_model": "exec-model",
                    "judge_model": "judge-model",
                    "analysis_model": "analysis-model",
                    "candidate_model": "candidate-model",
                    "execution_reasoning_effort": "medium",
                    "judge_reasoning_effort": "high",
                    "analysis_reasoning_effort": "low",
                    "candidate_reasoning_effort": "xhigh",
                },
                "resources": {
                    "formal_evaluation": {
                        "wall_timeout_seconds": 600,
                        "token_budget": 50_000,
                        "tool_call_budget": 200,
                        "disk_budget_mb": 2_048,
                        "max_concurrency": 2,
                    },
                    "evolution_analysis": {
                        "wall_timeout_seconds": 180,
                        "token_budget": 20_000,
                        "tool_call_budget": 40,
                        "disk_budget_mb": 512,
                        "max_concurrency": 1,
                    },
                    "evolution_candidate": {
                        "wall_timeout_seconds": 900,
                        "token_budget": 80_000,
                        "tool_call_budget": 400,
                        "disk_budget_mb": 4_096,
                        "max_concurrency": 1,
                    },
                    "evolution_evaluation": {
                        "wall_timeout_seconds": 1_200,
                        "token_budget": 100_000,
                        "tool_call_budget": 500,
                        "disk_budget_mb": 5_120,
                        "max_concurrency": 1,
                    },
                    "case_calibration": {
                        "wall_timeout_seconds": 1_800,
                        "disk_budget_mb": 8_192,
                        "max_concurrency": 1,
                        "gradle_heap_mb": 4_096,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(path))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVALUATION_GRADLE_MAX_HEAP_MB", "3072")

    configuration = task_platform_configuration(_provider())

    assert configuration.models.execution_model == "exec-model"
    assert configuration.models.judge_model == "judge-model"
    assert configuration.models.analysis_model == "analysis-model"
    assert configuration.models.candidate_model == "candidate-model"
    assert configuration.models.execution_reasoning_effort == "medium"
    assert configuration.models.candidate_reasoning_effort == "xhigh"
    assert configuration.resource_policy(FORMAL_EVALUATION).max_concurrency == 2
    assert configuration.resource_policy(EVOLUTION_ANALYSIS).token_budget == 20_000
    assert configuration.resource_policy(EVOLUTION_CANDIDATE).wall_timeout_seconds == 900
    assert configuration.resource_policy(EVOLUTION_EVALUATION).max_concurrency == 1
    assert configuration.calibration_policy(CASE_CALIBRATION).gradle_heap_mb == 3_072






def test_v2_role_models_remain_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "v2-platform-config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "helixharness-platform-workload-config.v2",
                "models": {
                    "execution_model": "shared-after-migration",
                    "judge_model": "legacy-judge",
                    "analysis_model": "legacy-analysis",
                    "candidate_model": "legacy-candidate",
                    "execution_reasoning_effort": "medium",
                    "judge_reasoning_effort": "high",
                    "analysis_reasoning_effort": "low",
                    "candidate_reasoning_effort": "xhigh",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(path))

    configuration = task_platform_configuration(_provider())

    assert configuration.models.execution_model == "shared-after-migration"
    assert configuration.models.judge_model == "legacy-judge"
    assert configuration.models.analysis_model == "legacy-analysis"
    assert configuration.models.candidate_model == "legacy-candidate"
    assert configuration.models.execution_reasoning_effort == "medium"
    assert configuration.models.judge_reasoning_effort == "high"
    assert configuration.models.analysis_reasoning_effort == "low"
    assert configuration.models.candidate_reasoning_effort == "xhigh"


def test_server_repository_environment_overrides_saved_browser_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "platform-config.json"
    path.write_text(
        json.dumps({"helixharness_repository": "/Users/browser/helixharness"}),
        encoding="utf-8",
    )
    server_repository = tmp_path / "server-helixharness"
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_CONFIG_PATH", str(path))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_HELIXHARNESS_REPOSITORY", str(server_repository))

    configuration = task_platform_configuration(_provider())

    assert configuration.helixharness_repository == str(server_repository)
