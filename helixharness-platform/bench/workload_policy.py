"""Stable workload identities shared by frozen Runs, workers, and resource views."""

from __future__ import annotations

from collections.abc import Mapping

FORMAL_EVALUATION = "FORMAL_EVALUATION"
EVOLUTION_ANALYSIS = "EVOLUTION_ANALYSIS"
EVOLUTION_CANDIDATE = "EVOLUTION_CANDIDATE"
EVOLUTION_EVALUATION = "EVOLUTION_EVALUATION"
CASE_CALIBRATION = "CASE_CALIBRATION"

MAX_SCENARIOS_PER_RUN = 10
MAX_WORKLOAD_CONCURRENCY = 4

SCENARIO_EXECUTION_WORKLOADS = (
    FORMAL_EVALUATION,
    EVOLUTION_EVALUATION,
)
CONFIGURABLE_WORKLOADS = (
    *SCENARIO_EXECUTION_WORKLOADS,
    EVOLUTION_ANALYSIS,
    EVOLUTION_CANDIDATE,
    CASE_CALIBRATION,
)


def canonical_workload_kind(workload_kind: str) -> str:
    if workload_kind not in CONFIGURABLE_WORKLOADS:
        raise ValueError(f"unknown workload: {workload_kind}")
    return workload_kind


def workload_kinds_for_pool(workload_kind: str) -> tuple[str, ...]:
    if workload_kind not in SCENARIO_EXECUTION_WORKLOADS:
        raise ValueError(f"unknown Scenario workload: {workload_kind}")
    return (workload_kind,)


def workload_kind_from_runtime_policy(policy: Mapping[str, object]) -> str:
    workload = str(policy.get("workload_kind") or FORMAL_EVALUATION)
    if workload not in SCENARIO_EXECUTION_WORKLOADS:
        raise ValueError(f"unknown Scenario workload: {workload}")
    return workload
