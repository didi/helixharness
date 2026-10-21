"""Build retry-aware attributable and actual execution resource observations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def attributable_resource_observation(
    turns: Iterable[Any],
    *,
    cumulative_usage: Mapping[str, object],
) -> dict[str, int | float]:
    """Count the completed logical business trajectory without Episode wall overhead."""

    active_duration_ms = 0
    rework_actions = 0
    for turn in turns:
        duration_ms = getattr(turn, "duration_ms", None)
        if isinstance(duration_ms, int) and not isinstance(duration_ms, bool) and duration_ms >= 0:
            active_duration_ms += duration_ms
        input_kind = getattr(turn, "input_kind", "")
        if isinstance(input_kind, str) and "FINDING" in input_kind.upper():
            rework_actions += 1
    total_tokens = _non_negative_int(cumulative_usage.get("total_tokens")) or 0
    return {
        "agent_tokens": total_tokens,
        "active_execution_seconds": round(active_duration_ms / 1000, 3),
        "workflow_rework_actions": rework_actions,
    }


def summarize_resource_observation(
    *,
    attributable: Mapping[str, object],
    attempts: Iterable[Mapping[str, object]],
    expected_attempt_count: int,
) -> dict[str, object]:
    """Expose scoring resources separately from all observed Attempt costs."""

    materialized = tuple(attempts)
    actual_tokens = 0
    actual_duration_ms = 0
    starts: list[int] = []
    completions: list[int] = []
    complete_records = True
    for attempt in materialized:
        usage = attempt.get("usage")
        tokens = _non_negative_int(usage.get("total_tokens")) if isinstance(usage, Mapping) else None
        duration_ms = _non_negative_int(attempt.get("duration_ms"))
        started_at_ms = _non_negative_int(attempt.get("started_at_ms"))
        completed_at_ms = _non_negative_int(attempt.get("completed_at_ms"))
        evidence_complete = attempt.get("evidence_complete") is True
        complete_records = complete_records and evidence_complete and tokens is not None and duration_ms is not None
        actual_tokens += tokens or 0
        actual_duration_ms += duration_ms or 0
        if started_at_ms is not None:
            starts.append(started_at_ms)
        if completed_at_ms is not None:
            completions.append(completed_at_ms)

    observed_attempts = len(materialized)
    accounting_complete = (
        expected_attempt_count >= 0
        and observed_attempts == expected_attempt_count
        and complete_records
        and len(starts) == observed_attempts
        and len(completions) == observed_attempts
    )
    elapsed_ms = max(0, max(completions) - min(starts)) if starts and completions else 0
    attributable_tokens = _number(attributable.get("agent_tokens"))
    attributable_seconds = _number(attributable.get("active_execution_seconds"))
    rework_actions = _number(attributable.get("workflow_rework_actions"))
    attributable_payload = {
        "agent_tokens": _stable_number(attributable_tokens),
        "active_execution_seconds": round(attributable_seconds, 3),
        "workflow_rework_actions": _stable_number(rework_actions),
    }
    actual_seconds = round(actual_duration_ms / 1000, 3)
    elapsed_seconds = round(elapsed_ms / 1000, 3)
    return {
        "schema_version": "framework-eval-resource-observation.v2",
        "accounting_status": "COMPLETE" if accounting_complete else "PARTIAL",
        "attributable": attributable_payload,
        "actual": {
            "agent_tokens": actual_tokens,
            "execution_seconds": actual_seconds,
            "attempt_elapsed_seconds": elapsed_seconds,
            "observed_attempts": observed_attempts,
            "expected_attempts": expected_attempt_count,
        },
        "excluded": {
            "non_attributable_tokens": max(0, int(actual_tokens - attributable_tokens)),
            "non_attributable_execution_seconds": round(max(0.0, actual_seconds - attributable_seconds), 3),
            "idle_between_attempts_seconds": round(max(0.0, elapsed_seconds - actual_seconds), 3),
        },
    }


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return 0.0


def _stable_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value
