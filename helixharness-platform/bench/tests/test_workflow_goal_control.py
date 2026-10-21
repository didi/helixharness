"""Contracts for current stage recovery and bounded Archive completion."""
from __future__ import annotations

import pytest

from bench.workflow_goal_control import (
    WorkflowGoalContractError,
    WorkflowGoalObservation,
    WorkflowGoalProgress,
    infer_next_workflow_actions,
    parse_workflow_goal_policy,
)


def _policy() -> dict:
    return {
        "schema_version": "framework-eval-workflow-goal-control.v1",
        "mode": "BOUNDED_GOAL_DIRECTED",
        "success_conditions": {"archive": "COMPLETED"},
        "limits": {
            "max_archive_attempts": 2, "max_inferred_actions": 8, "max_stage_rework_cycles": 2,
        },
        "actions": {
            "HANDLE_STAGE_FINDINGS": {"kind": "PROMPT", "input": "handle stage findings"},
            "ARCHIVE": {"kind": "COMMAND", "input": "/helix:archive demo"},
        },
    }


def test_goal_controller_runs_frozen_archive_until_completed() -> None:
    policy = parse_workflow_goal_policy(_policy(), feature_name="demo")
    first = infer_next_workflow_actions(policy, WorkflowGoalObservation(False), WorkflowGoalProgress())
    assert first.status == "EXECUTE" and first.actions == ("ARCHIVE",)
    assert first.progress.archive_attempts == first.progress.inferred_actions == 1
    complete = infer_next_workflow_actions(policy, WorkflowGoalObservation(True), first.progress)
    assert complete.status == "COMPLETE" and complete.actions == ()
    assert complete.progress == first.progress


@pytest.mark.parametrize("progress,reason", [
    (WorkflowGoalProgress(archive_attempts=2), "ARCHIVE_ATTEMPT_LIMIT_EXHAUSTED"),
    (WorkflowGoalProgress(inferred_actions=8), "INFERRED_ACTION_LIMIT_EXHAUSTED"),
])
def test_goal_controller_stops_at_frozen_budget(progress: WorkflowGoalProgress, reason: str) -> None:
    policy = parse_workflow_goal_policy(_policy(), feature_name="demo")
    decision = infer_next_workflow_actions(policy, WorkflowGoalObservation(False), progress)
    assert decision.status == "STOP" and decision.reason == reason
    assert decision.actions == () and decision.progress == progress


@pytest.mark.parametrize("condition,value", [("review", "APPROVE"), ("verify", "PASS")])
def test_goal_policy_rejects_retired_success_conditions(condition: str, value: str) -> None:
    raw = _policy()
    raw["success_conditions"][condition] = value
    with pytest.raises(WorkflowGoalContractError, match="仅支持 Archive"):
        parse_workflow_goal_policy(raw, feature_name="demo")


@pytest.mark.parametrize("action", ["REVIEW", "VERIFY", "HANDLE_REVIEW_FINDINGS", "HANDLE_VERIFY_FINDINGS"])
def test_goal_policy_rejects_retired_recovery_actions(action: str) -> None:
    raw = _policy()
    raw["actions"][action] = {"kind": "PROMPT", "input": "obsolete action"}
    with pytest.raises(WorkflowGoalContractError, match="不支持的动作"):
        parse_workflow_goal_policy(raw, feature_name="demo")


@pytest.mark.parametrize("limit", ["max_review_cycles", "max_verify_cycles"])
def test_goal_policy_rejects_retired_retry_budgets(limit: str) -> None:
    raw = _policy()
    raw["limits"][limit] = 1
    with pytest.raises(WorkflowGoalContractError, match="预算包含缺失或未知字段"):
        parse_workflow_goal_policy(raw, feature_name="demo")


def test_stage_recovery_requires_frozen_prompt() -> None:
    raw = _policy()
    del raw["actions"]["HANDLE_STAGE_FINDINGS"]
    with pytest.raises(WorkflowGoalContractError, match="缺少达成成功条件"):
        parse_workflow_goal_policy(raw, feature_name="demo")
    raw["limits"].pop("max_stage_rework_cycles")
    with pytest.raises(WorkflowGoalContractError, match="预算包含缺失或未知字段"):
        parse_workflow_goal_policy(raw, feature_name="demo")


@pytest.mark.parametrize("command", [
    "/helix:archive another-feature", "/helix:review demo",
    "/helix:verify demo", "/helix:archive demo ; echo done",
])
def test_goal_policy_rejects_unfrozen_commands(command: str) -> None:
    raw = _policy()
    raw["actions"]["ARCHIVE"]["input"] = command
    with pytest.raises(WorkflowGoalContractError, match="同一个 feature"):
        parse_workflow_goal_policy(raw, feature_name="demo")


def test_goal_policy_accepts_frozen_loop_archive_driver() -> None:
    raw = _policy()
    raw["actions"]["ARCHIVE"]["input"] = "/helix:propose demo --loop"
    policy = parse_workflow_goal_policy(raw, feature_name="demo")
    assert policy.action("ARCHIVE").input == "/helix:propose demo --loop"


def test_goal_policy_accepts_only_exact_frozen_custom_archive_adapter() -> None:
    raw = _policy()
    raw["actions"]["ARCHIVE"]["input"] = "/custom:deliver demo"
    policy = parse_workflow_goal_policy(raw, feature_name="demo", allowed_command_inputs={"ARCHIVE": "/custom:deliver demo"})
    assert policy.action("ARCHIVE").input == "/custom:deliver demo"
    with pytest.raises(WorkflowGoalContractError, match="同一个 feature"):
        parse_workflow_goal_policy(raw, feature_name="demo", allowed_command_inputs={"ARCHIVE": "/custom:finish demo"})
