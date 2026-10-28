"""Server-owned model bindings and workload policies for reproducible tasks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.gradle_runtime import DEFAULT_GRADLE_HEAP_MB, MAX_GRADLE_HEAP_MB, MIN_GRADLE_HEAP_MB
from bench.helixharness_source import HelixHarnessSourceError, git_repository_root, validate_plugin_path
from bench.platform_config import load_platform_config
from bench.runtime_secrets import EvaluationProviderConfig
from bench.workload_policy import (
    CASE_CALIBRATION,
    EVOLUTION_ANALYSIS,
    EVOLUTION_CANDIDATE,
    EVOLUTION_EVALUATION,
    FORMAL_EVALUATION,
    MAX_WORKLOAD_CONCURRENCY,
    canonical_workload_kind,
)

DEFAULT_REFERENCE_REF = "master"
DEFAULT_EVALUATION_MODEL = ""
DEFAULT_EVALUATION_REASONING_EFFORT = "low"
PLATFORM_CONFIG_SCHEMA_VERSION = "helixharness-platform-workload-config.v4"


def default_trusted_helixharness_repository() -> Path:
    """Return the restart-stable checkout used when no operator override exists."""

    return Path(__file__).resolve().parent / ".cache" / "helixharness-trusted"


@dataclass(frozen=True)
class ModelConfiguration:
    """Independent model bindings for each model-backed role."""

    execution_model: str
    judge_model: str
    analysis_model: str
    candidate_model: str
    execution_reasoning_effort: str = DEFAULT_EVALUATION_REASONING_EFFORT
    judge_reasoning_effort: str = DEFAULT_EVALUATION_REASONING_EFFORT
    analysis_reasoning_effort: str = DEFAULT_EVALUATION_REASONING_EFFORT
    candidate_reasoning_effort: str = "high"


@dataclass(frozen=True)
class WorkloadResourcePolicy:
    """Frozen execution limits shared by model-backed workload adapters."""

    wall_timeout_seconds: int
    token_budget: int
    tool_call_budget: int
    disk_budget_mb: int
    max_concurrency: int

    def runtime_fields(self) -> dict[str, int]:
        """Return the fields persisted into a Scenario Run runtime policy."""

        return {
            "wall_timeout_seconds": self.wall_timeout_seconds,
            "token_budget": self.token_budget,
            "tool_call_budget": self.tool_call_budget,
            "disk_budget_mb": self.disk_budget_mb,
            "max_concurrency": self.max_concurrency,
        }


@dataclass(frozen=True)
class CalibrationResourcePolicy:
    """Deterministic Case calibration limits; no model binding is required."""

    wall_timeout_seconds: int
    disk_budget_mb: int
    max_concurrency: int
    gradle_heap_mb: int


@dataclass(frozen=True)
class TaskPlatformConfiguration:
    """One interface for role-specific models and workload-specific policies."""

    helixharness_repository: str
    reference_ref: str
    models: ModelConfiguration
    permission_mode: str
    formal_evaluation: WorkloadResourcePolicy
    evolution_analysis: WorkloadResourcePolicy
    evolution_candidate: WorkloadResourcePolicy
    evolution_evaluation: WorkloadResourcePolicy
    case_calibration: CalibrationResourcePolicy
    helixharness_plugin_path: str | None = None

    def resource_policy(self, workload_kind: str) -> WorkloadResourcePolicy:
        """Resolve one model-backed workload without exposing config storage shape."""

        policies = {
            FORMAL_EVALUATION: self.formal_evaluation,
            EVOLUTION_ANALYSIS: self.evolution_analysis,
            EVOLUTION_CANDIDATE: self.evolution_candidate,
            EVOLUTION_EVALUATION: self.evolution_evaluation,
        }
        try:
            return policies[canonical_workload_kind(workload_kind)]
        except KeyError as exc:
            raise ValueError(f"unknown model-backed workload: {workload_kind}") from exc



    def calibration_policy(self, workload_kind: str = CASE_CALIBRATION) -> CalibrationResourcePolicy:
        """Resolve the deterministic calibration policy."""

        if workload_kind != CASE_CALIBRATION:
            raise ValueError(f"unknown calibration workload: {workload_kind}")
        return self.case_calibration

    @property
    def execution_model(self) -> str:
        return self.models.execution_model

    @property
    def judge_model(self) -> str:
        return self.models.judge_model

    @property
    def execution_reasoning_effort(self) -> str:
        return self.models.execution_reasoning_effort

    @property
    def judge_reasoning_effort(self) -> str:
        return self.models.judge_reasoning_effort

    @property
    def wall_timeout_seconds(self) -> int:
        return self.formal_evaluation.wall_timeout_seconds

    @property
    def token_budget(self) -> int:
        return self.formal_evaluation.token_budget

    @property
    def tool_call_budget(self) -> int:
        return self.formal_evaluation.tool_call_budget

    @property
    def disk_budget_mb(self) -> int:
        return self.formal_evaluation.disk_budget_mb

    @property
    def max_concurrency(self) -> int:
        return self.formal_evaluation.max_concurrency

    @property
    def repository_readiness_issue(self) -> str | None:
        path = Path(self.helixharness_repository).expanduser()
        if not self.helixharness_repository.strip():
            return "平台尚未配置受信 HelixHarness 仓库。"
        if not path.is_dir():
            return "受信 HelixHarness 仓库目录不存在，请平台管理员完成部署。"
        try:
            git_repository_root(path)
            if self.helixharness_plugin_path is not None:
                validate_plugin_path(self.helixharness_plugin_path)
        except HelixHarnessSourceError as exc:
            return str(exc)
        # Plugin identity is checked against each selected commit at preview time,
        # never inferred from whatever happens to be checked out on this server.
        return None

    @property
    def repository_ready(self) -> bool:
        return self.repository_readiness_issue is None

    @property
    def model_ready(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.models.execution_model,
                self.models.judge_model,
                self.models.analysis_model,
                self.models.candidate_model,
            )
        )

    @property
    def ready(self) -> bool:
        return self.repository_ready and self.model_ready




def task_platform_configuration(
    provider: EvaluationProviderConfig | None = None,
) -> TaskPlatformConfiguration:
    """Load shared model bindings and workload policies from current model and workload configuration."""

    saved = load_platform_config()
    models = _mapping(saved.get("models"))
    resources = _mapping(saved.get("resources"))
    configured_repository = os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_HELIXHARNESS_REPOSITORY", "").strip()
    repository = (
        configured_repository
        or _text_value(saved, "helixharness_repository", "")
        or str(default_trusted_helixharness_repository())
    )
    try:
        repository = str(git_repository_root(Path(repository)))
    except HelixHarnessSourceError:
        pass  # Report unconfigured checkouts through repository_readiness_issue.
    environment_concurrency = _environment_int("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_MAX_CONCURRENCY", 2)

    formal = _resource_policy(
        _mapping(resources.get("formal_evaluation")),
        defaults=(86_400, 200_000, 1_000, 10_240, environment_concurrency),
    )
    analysis = _resource_policy(
        _mapping(resources.get("evolution_analysis")),
        defaults=(
            _environment_int("HELIXHARNESS_PLATFORM_EVOLUTION_ANALYSIS_TIMEOUT_SECONDS", 300),
            50_000,
            100,
            1_024,
            1,
        ),
    )
    candidate = _resource_policy(
        _mapping(resources.get("evolution_candidate")),
        defaults=(
            _environment_int("HELIXHARNESS_PLATFORM_EVOLUTION_CANDIDATE_TIMEOUT_SECONDS", 900),
            100_000,
            500,
            5_120,
            1,
        ),
    )
    evaluation = _resource_policy(
        _mapping(resources.get("evolution_evaluation")),
        defaults=(
            formal.wall_timeout_seconds,
            formal.token_budget,
            formal.tool_call_budget,
            formal.disk_budget_mb,
            2,
        ),
    )
    calibration = CalibrationResourcePolicy(
        wall_timeout_seconds=1_800,
        disk_budget_mb=10_240,
        max_concurrency=1,
        gradle_heap_mb=min(
            MAX_GRADLE_HEAP_MB,
            max(
                MIN_GRADLE_HEAP_MB,
                _environment_int("HELIXHARNESS_PLATFORM_EVALUATION_GRADLE_MAX_HEAP_MB", DEFAULT_GRADLE_HEAP_MB),
            ),
        ),
    )
    shared_model = os.environ.get("HELIXHARNESS_PLATFORM_EVALUATION_CODEX_MODEL", "").strip() or (provider.model if provider else "") or DEFAULT_EVALUATION_MODEL
    shared_reasoning_effort = _text_value(
        models,
        "reasoning_effort",
        DEFAULT_EVALUATION_REASONING_EFFORT,
    )
    execution_model = _text_value(models, "execution_model", shared_model)
    candidate_reasoning_fallback = shared_reasoning_effort if "reasoning_effort" in models else "high"
    return TaskPlatformConfiguration(
        helixharness_repository=repository,
        helixharness_plugin_path=os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_PLUGIN_PATH") or None,
        reference_ref=_text_value(
            saved,
            "reference_ref",
            os.environ.get("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REFERENCE_REF", DEFAULT_REFERENCE_REF).strip()
            or DEFAULT_REFERENCE_REF,
        ),
        models=ModelConfiguration(
            execution_model=execution_model,
            judge_model=_text_value(models, "judge_model", execution_model),
            analysis_model=_text_value(models, "analysis_model", execution_model),
            candidate_model=_text_value(models, "candidate_model", execution_model),
            execution_reasoning_effort=_text_value(
                models, "execution_reasoning_effort", shared_reasoning_effort
            ),
            judge_reasoning_effort=_text_value(models, "judge_reasoning_effort", shared_reasoning_effort),
            analysis_reasoning_effort=_text_value(
                models, "analysis_reasoning_effort", shared_reasoning_effort
            ),
            candidate_reasoning_effort=_text_value(
                models,
                "candidate_reasoning_effort",
                candidate_reasoning_fallback,
            ),
        ),
        permission_mode=_text_value(saved, "permission_mode", "workspace-write"),
        formal_evaluation=formal,
        evolution_analysis=analysis,
        evolution_candidate=candidate,
        evolution_evaluation=evaluation,
        case_calibration=calibration,
    )


def _resource_policy(
    values: dict[str, Any],
    *,
    defaults: tuple[int, int, int, int, int],
) -> WorkloadResourcePolicy:
    timeout, tokens, tools, disk, concurrency = defaults
    return WorkloadResourcePolicy(
        wall_timeout_seconds=_int_value(values, "wall_timeout_seconds", timeout, 60, 604_800),
        token_budget=_int_value(values, "token_budget", tokens, 1_000, 2_000_000),
        tool_call_budget=_int_value(values, "tool_call_budget", tools, 1, 10_000),
        disk_budget_mb=_int_value(values, "disk_budget_mb", disk, 128, 102_400),
        max_concurrency=_int_value(
            values,
            "max_concurrency",
            concurrency,
            1,
            MAX_WORKLOAD_CONCURRENCY,
        ),
    )


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text_value(values: dict[str, Any], key: str, fallback: str) -> str:
    value = values.get(key)
    return str(value).strip() if isinstance(value, str) and value.strip() else fallback


def _int_value(values: dict[str, Any], key: str, fallback: int, minimum: int, maximum: int) -> int:
    try:
        value = int(values.get(key, fallback))
    except (TypeError, ValueError):
        value = fallback
    return min(maximum, max(minimum, value))


def _environment_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, str(fallback)).strip())
    except ValueError:
        return fallback
