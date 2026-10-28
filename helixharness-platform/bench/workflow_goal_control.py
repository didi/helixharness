"""Bounded goal-directed decisions for one frozen HelixHarness workflow."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, replace
from typing import Mapping


class WorkflowGoalContractError(ValueError):
    """Raised when a frozen goal or action catalog is unsafe to execute."""


@dataclass(frozen=True)
class WorkflowGoalAction:
    """One exact prompt or command the controller is allowed to select."""

    action: str
    kind: str
    input: str


@dataclass(frozen=True)
class WorkflowGoalPolicy:
    """Frozen success conditions, action catalog, and retry limits."""

    require_archive_completed: bool
    max_archive_attempts: int
    max_inferred_actions: int
    max_stage_rework_cycles: int
    stage_findings_prompt: str
    actions: tuple[WorkflowGoalAction, ...]

    def action(self, name: str) -> WorkflowGoalAction:
        """Return one frozen action without allowing runtime invention."""

        match = next((item for item in self.actions if item.action == name), None)
        if match is None:
            raise WorkflowGoalContractError(f"工作流目标缺少冻结动作：{name}")
        return match


@dataclass(frozen=True)
class WorkflowGoalObservation:
    """Mechanically observed workflow state after one completed turn."""

    archive_completed: bool


@dataclass(frozen=True)
class WorkflowGoalProgress:
    """Attempts already consumed by goal-directed inference."""

    archive_attempts: int = 0
    inferred_actions: int = 0


@dataclass(frozen=True)
class WorkflowGoalDecision:
    """The next frozen actions, completion, or a fail-closed stop."""

    status: str
    reason: str
    actions: tuple[str, ...]
    progress: WorkflowGoalProgress


@dataclass(frozen=True)
class StrategyWorkflowRecovery:
    """Validated Strategy wrapper around one frozen bounded goal policy."""

    feature_name: str
    goal_policy: WorkflowGoalPolicy


_ACTION_NAMES = {"HANDLE_STAGE_FINDINGS", "ARCHIVE"}
_COMMAND_ACTIONS = {"ARCHIVE"}


def parse_strategy_workflow_recovery(raw: object) -> StrategyWorkflowRecovery | None:
    """Validate the exact recovery wrapper frozen into a Scenario Strategy."""

    if raw == {} or raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkflowGoalContractError("Strategy recovery policy must be an object")
    if set(raw) != {"schema_version", "feature", "workflow_goal_control"}:
        raise WorkflowGoalContractError("Strategy recovery policy contains unsupported fields")
    if raw.get("schema_version") != "framework-eval-strategy-recovery.v1":
        raise WorkflowGoalContractError("Strategy recovery policy schema is invalid")
    feature_name = raw.get("feature")
    if not isinstance(feature_name, str) or re.fullmatch(r"[A-Za-z0-9_.-]+", feature_name) is None:
        raise WorkflowGoalContractError("Strategy recovery policy feature is invalid")
    return StrategyWorkflowRecovery(
        feature_name=feature_name,
        goal_policy=parse_workflow_goal_policy(
            raw.get("workflow_goal_control"),
            feature_name=feature_name,
        ),
    )


def parse_workflow_goal_policy(
    raw: object,
    *,
    feature_name: str,
    allowed_command_inputs: Mapping[str, str] | None = None,
) -> WorkflowGoalPolicy:
    """Validate a frozen goal contract at the execution seam."""

    if not isinstance(raw, dict):
        raise WorkflowGoalContractError("工作流目标控制契约必须是对象")
    if (
        raw.get("schema_version")
        != "framework-eval-workflow-goal-control.v1"
        or raw.get("mode") != "BOUNDED_GOAL_DIRECTED"
    ):
        raise WorkflowGoalContractError("工作流目标控制契约版本无效")

    success = raw.get("success_conditions")
    if not isinstance(success, dict) or success.get("archive") != "COMPLETED":
        raise WorkflowGoalContractError("工作流必须冻结可验证的 Archive 成功条件")
    if set(success) != {"archive"}:
        raise WorkflowGoalContractError("当前工作流仅支持 Archive 成功条件；Review 与 Verify 已合并至 Apply")

    limits = raw.get("limits")
    if not isinstance(limits, dict):
        raise WorkflowGoalContractError("工作流目标控制缺少有限重试预算")
    if set(limits) != {"max_archive_attempts", "max_inferred_actions", "max_stage_rework_cycles"}:
        raise WorkflowGoalContractError("工作流目标预算包含缺失或未知字段")
    parsed_limits: dict[str, int] = {}
    for name, minimum, maximum in (
        ("max_archive_attempts", 1, 5),
        ("max_inferred_actions", 1, 50),
        ("max_stage_rework_cycles", 0, 10),
    ):
        value = limits.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not minimum <= value <= maximum
        ):
            raise WorkflowGoalContractError(f"工作流目标预算无效：{name}")
        parsed_limits[name] = value

    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, dict) or not raw_actions:
        raise WorkflowGoalContractError("工作流目标控制缺少冻结动作目录")
    unexpected = set(raw_actions) - _ACTION_NAMES
    if unexpected:
        raise WorkflowGoalContractError("工作流目标包含平台不支持的动作")

    required_actions = {"ARCHIVE"}
    if parsed_limits["max_stage_rework_cycles"] > 0:
        required_actions.add("HANDLE_STAGE_FINDINGS")
    if not required_actions.issubset(raw_actions):
        raise WorkflowGoalContractError("工作流目标缺少达成成功条件所需的冻结动作")

    actions: list[WorkflowGoalAction] = []
    for action_name in sorted(raw_actions):
        item = raw_actions[action_name]
        if not isinstance(item, dict):
            raise WorkflowGoalContractError(f"冻结动作无效：{action_name}")
        kind = item.get("kind")
        action_input = item.get("input")
        if kind not in {"PROMPT", "COMMAND"} or not isinstance(action_input, str) or not action_input.strip():
            raise WorkflowGoalContractError(f"冻结动作缺少可执行输入：{action_name}")
        if action_name in _COMMAND_ACTIONS:
            if kind != "COMMAND":
                raise WorkflowGoalContractError(f"冻结动作必须是命令：{action_name}")
            _validate_command(
                action_input,
                action_name=action_name,
                feature_name=feature_name,
                allowed_command_inputs=allowed_command_inputs,
            )
        elif kind != "PROMPT":
            raise WorkflowGoalContractError(f"Finding 处理动作必须是冻结 Prompt：{action_name}")
        actions.append(
            WorkflowGoalAction(
                action=action_name,
                kind=kind,
                input=action_input.strip(),
            )
        )

    return WorkflowGoalPolicy(
        require_archive_completed=True,
        max_archive_attempts=parsed_limits["max_archive_attempts"],
        max_inferred_actions=parsed_limits["max_inferred_actions"],
        max_stage_rework_cycles=parsed_limits["max_stage_rework_cycles"],
        stage_findings_prompt=(
            next(
                action.input
                for action in actions
                if action.action == "HANDLE_STAGE_FINDINGS"
            )
            if parsed_limits["max_stage_rework_cycles"] > 0
            else ""
        ),
        actions=tuple(actions),
    )


def infer_next_workflow_actions(
    policy: WorkflowGoalPolicy,
    observation: WorkflowGoalObservation,
    progress: WorkflowGoalProgress,
) -> WorkflowGoalDecision:
    """Infer the smallest safe action plan that advances the frozen goal."""

    if observation.archive_completed:
        return WorkflowGoalDecision("COMPLETE", "GOAL_SATISFIED", (), progress)
    if progress.inferred_actions >= policy.max_inferred_actions:
        return WorkflowGoalDecision(
            "STOP",
            "INFERRED_ACTION_LIMIT_EXHAUSTED",
            (),
            progress,
        )

    if progress.archive_attempts >= policy.max_archive_attempts:
        return WorkflowGoalDecision("STOP", "ARCHIVE_ATTEMPT_LIMIT_EXHAUSTED", (), progress)
    actions = ("ARCHIVE",)
    reason = "ARCHIVE_REQUIRED"
    next_progress = replace(progress, archive_attempts=progress.archive_attempts + 1)

    for action in actions:
        policy.action(action)
    inferred_actions = progress.inferred_actions + len(actions)
    if inferred_actions > policy.max_inferred_actions:
        return WorkflowGoalDecision(
            "STOP",
            "INFERRED_ACTION_LIMIT_EXHAUSTED",
            (),
            progress,
        )
    return WorkflowGoalDecision(
        "EXECUTE",
        reason,
        actions,
        replace(next_progress, inferred_actions=inferred_actions),
    )


def _validate_command(
    command: str,
    *,
    action_name: str,
    feature_name: str,
    allowed_command_inputs: Mapping[str, str] | None,
) -> None:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise WorkflowGoalContractError("冻结动作命令无法解析") from exc
    expected_commands = {
        "archive": "/helix:archive",
    }
    expected_input = (
        allowed_command_inputs.get(action_name)
        if allowed_command_inputs is not None
        else None
    )
    if (
        len(tokens) < 2
        or re.fullmatch(r"/[a-z][a-z0-9-]*:[a-z][a-z0-9-]*", tokens[0]) is None
        or tokens[1] != feature_name
        or any(token in {"&&", "||", ";", "|"} for token in tokens)
        or (
            allowed_command_inputs is None
            and tokens[0] != expected_commands.get(action_name.lower())
            and not (action_name.lower() == "archive" and tokens[0] == "/helix:propose" and "--loop" in tokens[2:])
        )
        or (
            allowed_command_inputs is not None
            and command.strip() != expected_input
        )
    ):
        raise WorkflowGoalContractError("冻结动作命令必须使用同一个 feature 且不能包含 Shell 组合")
