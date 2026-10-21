from __future__ import annotations

from dataclasses import dataclass

from bench.resource_accounting import (
    attributable_resource_observation,
    summarize_resource_observation,
)


@dataclass(frozen=True)
class _Turn:
    duration_ms: int | None
    input_kind: str


def test_attributable_resources_use_turn_duration_instead_of_episode_wall_time() -> None:
    observation = attributable_resource_observation(
        (
            _Turn(1_000, "WORKFLOW_STEP"),
            _Turn(2_000, "HANDLE_REVIEW_FINDINGS"),
            _Turn(None, "WORKFLOW_STEP"),
        ),
        cumulative_usage={"total_tokens": 500},
    )

    assert observation == {
        "agent_tokens": 500,
        "active_execution_seconds": 3.0,
        "workflow_rework_actions": 1,
    }


def test_resource_summary_separates_attributable_and_actual_retry_costs() -> None:
    observation = summarize_resource_observation(
        attributable={
            "agent_tokens": 500,
            "active_execution_seconds": 3.0,
            "workflow_rework_actions": 1,
        },
        attempts=(
            {
                "started_at_ms": 1_000,
                "completed_at_ms": 7_000,
                "duration_ms": 6_000,
                "usage": {"total_tokens": 700},
                "evidence_complete": True,
            },
            {
                "started_at_ms": 10_000,
                "completed_at_ms": 14_000,
                "duration_ms": 4_000,
                "usage": {"total_tokens": 300},
                "evidence_complete": True,
            },
        ),
        expected_attempt_count=2,
    )

    assert observation["schema_version"] == "framework-eval-resource-observation.v2"
    assert observation["accounting_status"] == "COMPLETE"
    assert observation["attributable"] == {
        "agent_tokens": 500,
        "active_execution_seconds": 3.0,
        "workflow_rework_actions": 1,
    }
    assert observation["actual"] == {
        "agent_tokens": 1_000,
        "execution_seconds": 10.0,
        "attempt_elapsed_seconds": 13.0,
        "observed_attempts": 2,
        "expected_attempts": 2,
    }
    assert observation["excluded"] == {
        "non_attributable_tokens": 500,
        "non_attributable_execution_seconds": 7.0,
        "idle_between_attempts_seconds": 3.0,
    }
    assert "agent_tokens" not in observation
    assert "active_execution_seconds" not in observation


def test_resource_summary_marks_missing_attempt_evidence_partial() -> None:
    observation = summarize_resource_observation(
        attributable={
            "agent_tokens": 100,
            "active_execution_seconds": 1.0,
            "workflow_rework_actions": 0,
        },
        attempts=(
            {
                "started_at_ms": 5_000,
                "completed_at_ms": 7_000,
                "duration_ms": 2_000,
                "usage": {"total_tokens": 100},
                "evidence_complete": True,
            },
        ),
        expected_attempt_count=2,
    )

    assert observation["accounting_status"] == "PARTIAL"
    assert observation["actual"]["observed_attempts"] == 1
    assert observation["actual"]["expected_attempts"] == 2
