"""Freeze the current HelixHarness command chain."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Any, Iterable

DEFAULT_WORKFLOW_REWORK_LIMIT = 5


class WorkflowContractError(ValueError):
    """Invalid or unverifiable current workflow."""


def parse_helixharness_command(command: str) -> tuple[str, list[str], set[str]]:
    """Validate current command options, preserving literal requirements after --."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise WorkflowContractError("命令参数引号不完整") from exc
    match = re.fullmatch(r"/helix:(init|propose|apply|archive|ut|bug-fix)", tokens[0]) if tokens else None
    if not match or any(token in {"&&", "||", ";", "|"} for token in tokens):
        raise WorkflowContractError("必须是单条受支持的 HelixHarness 命令")
    name = match.group(1)
    option_tokens = tokens[1:tokens.index("--")] if "--" in tokens else tokens[1:]
    options = {token.split("=", 1)[0] for token in option_tokens if token.startswith("-")}
    allowed = {
        "init": {"--workspace", "--repos", "--refresh"},
        "propose": {"--loop"},
        "archive": {"--commit", "--no-commit"},
        "ut": {"--packages", "-p", "--concurrency", "-c"},
    }.get(name, set())
    if options - allowed:
        raise WorkflowContractError("当前工作流不支持这些选项")
    switches = {"--workspace", "--refresh", "--loop", "--commit", "--no-commit"}
    if any("=" in token and token.split("=", 1)[0] in switches for token in option_tokens):
        raise WorkflowContractError("开关选项不接受赋值")
    if {"--commit", "--no-commit"} <= options:
        raise WorkflowContractError("Archive 提交选项不能同时使用")
    if name == "init" and options & {"--repos", "--refresh"} and "--workspace" not in options:
        raise WorkflowContractError("Workspace 初始化选项需要 --workspace")
    return name, tokens, options


def resolve_workflow_contract(
    commands: Iterable[str],
    *,
    profile_name: str,
    side: str,
    source_commit: str | None,
    workflow_closure: dict[str, object],
    declared_steps: Iterable[dict[str, object]] | None = None,
) -> dict[str, object]:
    values = tuple(command.strip() for command in commands)
    if not values or any(not command for command in values):
        raise WorkflowContractError("工作流不能为空")
    declared = tuple(declared_steps or ())
    if declared and len(declared) != len(values):
        raise WorkflowContractError("声明步骤与命令数量不一致")
    feature_name = None
    steps = []
    for index, command in enumerate(values, start=1):
        name, tokens, options = parse_helixharness_command(command)
        if name == "init":
            if index != 1:
                raise WorkflowContractError("init 必须是首个阶段")
            contract = "INIT"
        elif name == "ut":
            contract = "UT"
        else:
            explicit = tokens[1] if len(tokens) > 1 and not tokens[1].startswith("-") else None
            if explicit and not re.fullmatch(r"[A-Za-z0-9_.-]+", explicit):
                raise WorkflowContractError("feature 名称非法")
            if feature_name and explicit and feature_name != explicit:
                raise WorkflowContractError("所有阶段必须使用同一个 feature")
            feature_name = feature_name or explicit
            if not feature_name:
                raise WorkflowContractError("首条业务命令必须指定 feature")
            loop = name == "propose" and "--loop" in options
            if loop and index != len(values):
                raise WorkflowContractError("propose --loop 必须是终态命令")
            contract = (
                "HELIXHARNESS_ARCHIVE"
                if loop
                else {
                    "propose": "HELIXHARNESS_PROPOSE",
                    "apply": "HELIXHARNESS_APPLY",
                    "archive": "HELIXHARNESS_ARCHIVE",
                    "ut": "UT",
                    "bug-fix": "HELIXHARNESS_APPLY",
                }[name]
            )
            if loop:
                name = "archive"
        item = declared[index - 1] if declared else {}
        steps.append(
            {
                "step_id": str(item.get("step_id") or f"SIDE_STEP_{index:02d}"),
                "command": command,
                "milestone_id": str(item.get("milestone_id") or f"SIDE_STEP_{index:02d}_COMPLETED"),
                "stage_semantics": name.upper(),
                "artifact_contract": contract,
            }
        )
    if not feature_name or steps[-1]["artifact_contract"] != "HELIXHARNESS_ARCHIVE":
        raise WorkflowContractError("工作流必须包含可验证的 Archive 终态")
    result = {
        "schema_version": "framework-eval-workflow-contract.v1",
        "profile": profile_name,
        "side": side,
        "source_commit": source_commit,
        "feature": feature_name,
        "resolver": {
            "kind": "DETERMINISTIC_HELIXHARNESS_ADAPTER",
            "version": "helixharness-workflow-adapter.v1",
            "requires_creator_confirmation": True,
        },
        "steps": steps,
        "workflow_closure": workflow_closure,
        "goal_control": _goal_control_contract(steps, feature_name=feature_name, workflow_closure=workflow_closure),
        "terminal": {"state": "ARCHIVED", "evidence_contract": "HELIXHARNESS_ARCHIVE"},
    }
    result["contract_hash"] = _stable_hash(result)
    return result


def _goal_control_contract(
    steps: list[dict[str, object]],
    *,
    feature_name: str | None,
    workflow_closure: dict[str, object],
) -> dict[str, object]:
    """Freeze the goal, exact action inputs, and bounded adaptive budget."""

    if not feature_name:
        raise WorkflowContractError("工作流目标缺少可验证的 feature")
    by_semantics = {
        str(step.get("stage_semantics", "")).upper(): str(step.get("command", ""))
        for step in steps
        if isinstance(step, dict)
    }
    archive_command = by_semantics.get("ARCHIVE")
    if not archive_command:
        raise WorkflowContractError("工作流目标缺少冻结 Archive 动作")
    max_stage_rework_cycles = workflow_closure.get("max_stage_rework_cycles")
    stage_prompt = workflow_closure.get("stage_findings_prompt")
    if (
        not isinstance(max_stage_rework_cycles, int)
        or isinstance(max_stage_rework_cycles, bool)
        or not 1 <= max_stage_rework_cycles <= 10
        or not isinstance(stage_prompt, str)
        or not stage_prompt.strip()
    ):
        raise WorkflowContractError("工作流目标无法冻结有限阶段返工")

    success_conditions: dict[str, str] = {"archive": "COMPLETED"}
    actions: dict[str, dict[str, str]] = {
        "ARCHIVE": {"kind": "COMMAND", "input": archive_command},
        "HANDLE_STAGE_FINDINGS": {
            "kind": "PROMPT",
            "input": stage_prompt,
        },
    }
    return {
        "schema_version": "framework-eval-workflow-goal-control.v1",
        "mode": "BOUNDED_GOAL_DIRECTED",
        "success_conditions": success_conditions,
        "limits": {
            "max_archive_attempts": DEFAULT_WORKFLOW_REWORK_LIMIT,
            "max_inferred_actions": 12,
            "max_stage_rework_cycles": max_stage_rework_cycles,
        },
        "actions": actions,
    }


def _stable_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
