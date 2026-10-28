"""Public-seam tests for controlled framework-evaluation clarification."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bench.codex_execution import (
    CodexExecutionError,
    CodexExecutionRequest,
    CodexPythonSdkAdapter,
    CodexTurnExecutionResult,
    FrozenWorkflowClosurePolicy,
    FrozenWorkflowClosureResume,
    FrozenWorkflowStep,
)
from bench.turn_control import (
    EVIDENCE_BOUND_USER_RESPONSE,
    EVIDENCE_BOUND_USER_RESPONSE_VERSION,
    FrozenTurnClassifier,
    TurnClassification,
    has_valid_turn_status,
)
from bench.workflow_goal_control import (
    WorkflowGoalAction,
    WorkflowGoalObservation,
    WorkflowGoalPolicy,
)


def test_unattended_workflow_choices_are_frozen_by_the_run_strategy() -> None:
    assert EVIDENCE_BOUND_USER_RESPONSE_VERSION == "evidence-bound-user-response.v3"
    assert "Review finding" in EVIDENCE_BOUND_USER_RESPONSE
    assert "不得无理由 defer" in EVIDENCE_BOUND_USER_RESPONSE
    assert "不得直接豁免" in EVIDENCE_BOUND_USER_RESPONSE
    assert "经验总结选择自动生成" in EVIDENCE_BOUND_USER_RESPONSE
    assert "一律不 commit、不 push" in EVIDENCE_BOUND_USER_RESPONSE


def test_turn_classifier_has_exactly_three_structured_results() -> None:
    classifier = FrozenTurnClassifier()

    assert set(TurnClassification) == {
        TurnClassification.NEEDS_USER_INPUT,
        TurnClassification.COMPLETED,
        TurnClassification.FAILED,
    }
    assert (
        classifier.classify(
            sdk_status="COMPLETED",
            final_response=("请选择一种兼容策略。\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT"),
            error=None,
        )
        is TurnClassification.NEEDS_USER_INPUT
    )
    assert (
        classifier.classify(
            sdk_status="COMPLETED",
            final_response=("实现和验证已完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"),
            error=None,
        )
        is TurnClassification.COMPLETED
    )
    assert (
        classifier.classify(
            sdk_status="COMPLETED",
            final_response=("缺少不可替代的业务前置条件，无法继续。\nHELIXHARNESS_TURN_STATUS: FAILED"),
            error=None,
        )
        is TurnClassification.FAILED
    )


def test_turn_status_protocol_requires_one_exact_final_marker() -> None:
    assert has_valid_turn_status("done\nHELIXHARNESS_TURN_STATUS: COMPLETED")
    assert not has_valid_turn_status("done without marker")
    assert not has_valid_turn_status("HELIXHARNESS_TURN_STATUS: UNKNOWN")
    assert not has_valid_turn_status("HELIXHARNESS_TURN_STATUS: COMPLETED\nextra")
    assert not has_valid_turn_status(
        "HELIXHARNESS_TURN_STATUS: COMPLETED\nHELIXHARNESS_TURN_STATUS: COMPLETED"
    )


def test_turn_classifier_accepts_completion_after_a_fixable_test_failure() -> None:
    classifier = FrozenTurnClassifier()

    result = classifier.classify(
        sdk_status="COMPLETED",
        final_response=("测试曾经失败，已在当前 Turn 内修复并重新验证通过。\nHELIXHARNESS_TURN_STATUS: COMPLETED"),
        error=None,
    )

    assert result is TurnClassification.COMPLETED


def test_turn_classifier_does_not_advance_a_workflow_step_that_still_asks_for_a_choice() -> None:
    classifier = FrozenTurnClassifier()

    result = classifier.classify(
        sdk_status="COMPLETED",
        final_response=(
            "是否现在执行索引升级？\n\n请回复：\n\n1. 现在升级并继续\n2. 跳过升级\n\nHELIXHARNESS_TURN_STATUS: COMPLETED"
        ),
        error=None,
    )

    assert result is TurnClassification.NEEDS_USER_INPUT


def test_turn_classifier_catches_a_direct_question_even_when_completion_is_declared() -> None:
    classifier = FrozenTurnClassifier()

    result = classifier.classify(
        sdk_status="COMPLETED",
        final_response=("当前 harness 索引是旧格式，建议先升级。是否现在执行？\n\nHELIXHARNESS_TURN_STATUS: COMPLETED"),
        error=None,
    )

    assert result is TurnClassification.NEEDS_USER_INPUT


def test_turn_classifier_does_not_treat_an_explicit_blocker_as_completion() -> None:
    classifier = FrozenTurnClassifier()

    result = classifier.classify(
        sdk_status="COMPLETED",
        final_response=("BLOCKED: prerequisite_missing\n缺少 Phase 1 工件。\nHELIXHARNESS_TURN_STATUS: COMPLETED"),
        error=None,
    )

    assert result is TurnClassification.FAILED


def test_turn_classifier_does_not_treat_a_rejected_archive_as_completion() -> None:
    classifier = FrozenTurnClassifier()

    result = classifier.classify(
        sdk_status="COMPLETED",
        final_response=(
            "归档被前置门禁阻止，未移动或修改任何文件：\n"
            "- 综合 review：`REJECT`\n"
            "- 独立验收：`不通过`\n"
            "HELIXHARNESS_TURN_STATUS: COMPLETED"
        ),
        error=None,
    )

    assert result is TurnClassification.FAILED


class _ScriptedTurn:
    def __init__(
        self,
        turn_id: str,
        response: str,
        *,
        failed_command: bool = False,
        disconnect_after_response: bool = False,
        completion_error: dict[str, str] | None = None,
        trailing_message_update: bool = False,
        response_method: str = "item/completed",
    ) -> None:
        self.id = turn_id
        self.response = response
        self.failed_command = failed_command
        self.disconnect_after_response = disconnect_after_response
        self.completion_error = completion_error
        self.trailing_message_update = trailing_message_update
        self.response_method = response_method

    def interrupt(self) -> None:
        return None

    def stream(self) -> Any:
        events = []
        if self.failed_command:
            events.append(
                SimpleNamespace(
                    method="item/completed",
                    payload={
                        "completedAtMs": 1200,
                        "item": {
                            "type": "commandExecution",
                            "command": "pytest",
                            "status": "failed",
                            "exitCode": 1,
                        },
                    },
                )
            )
        events.append(
            SimpleNamespace(
                method=self.response_method,
                payload={
                    "completedAtMs": 1500,
                    "item": {
                        "type": "agentMessage",
                        "text": self.response,
                    },
                },
            )
        )
        if self.trailing_message_update:
            events.append(
                SimpleNamespace(
                    method="item/updated",
                    payload={
                        "item": {
                            "type": "agentMessage",
                            "text": self.response,
                        },
                    },
                )
            )
        if self.disconnect_after_response:
            def disconnected_stream() -> Any:
                yield from events
                raise RuntimeError("Reconnecting... 1/5")

            return disconnected_stream()
        events.extend(
            [
                SimpleNamespace(
                    method="turn/completed",
                    payload={
                        "turn": {
                            "id": self.id,
                            "status": "failed" if self.completion_error else "completed",
                            "startedAtMs": 1000,
                            "completedAtMs": 2000,
                            "durationMs": 1000,
                            "error": self.completion_error,
                        }
                    },
                ),
            ]
        )
        return iter(events)


class _ScriptedThread:
    id = "one-episode-thread"

    def __init__(
        self,
        responses: list[str],
        *,
        failed_command_on_first_turn: bool = False,
        disconnect_after_response: bool = False,
        completion_error: dict[str, str] | None = None,
        completion_error_on_turn: int | None = None,
        trailing_message_update: bool = False,
        response_method: str = "item/completed",
    ) -> None:
        self.responses = responses
        self.inputs: list[str] = []
        self.failed_command_on_first_turn = failed_command_on_first_turn
        self.disconnect_after_response = disconnect_after_response
        self.completion_error = completion_error
        self.completion_error_on_turn = completion_error_on_turn
        self.trailing_message_update = trailing_message_update
        self.response_method = response_method

    def turn(self, prompt: str, **_kwargs: object) -> _ScriptedTurn:
        self.inputs.append(prompt)
        response = self.responses[len(self.inputs) - 1]
        return _ScriptedTurn(
            f"sdk-turn-{len(self.inputs)}",
            response,
            failed_command=(self.failed_command_on_first_turn and len(self.inputs) == 1),
            disconnect_after_response=self.disconnect_after_response,
            completion_error=(
                self.completion_error
                if self.completion_error_on_turn in {None, len(self.inputs)}
                else None
            ),
            trailing_message_update=self.trailing_message_update,
            response_method=self.response_method,
        )


class _ScriptedCodexContext:
    def __init__(self, owner: "_ScriptedCodexFactory") -> None:
        self.owner = owner

    def __enter__(self) -> "_ScriptedCodexContext":
        return self

    def __exit__(self, *_args: object) -> None:
        if self.owner.disconnect_on_exit:
            raise RuntimeError("Reconnecting... 1/5")
        return None

    def thread_start(self, **kwargs: object) -> _ScriptedThread:
        self.owner.thread_start_kwargs = kwargs
        return self.owner.thread


class _ScriptedCodexFactory:
    def __init__(
        self,
        responses: list[str],
        *,
        failed_command_on_first_turn: bool = False,
        disconnect_after_response: bool = False,
        completion_error: dict[str, str] | None = None,
        completion_error_on_turn: int | None = None,
        trailing_message_update: bool = False,
        response_method: str = "item/completed",
        disconnect_on_exit: bool = False,
    ) -> None:
        self.thread = _ScriptedThread(
            responses,
            failed_command_on_first_turn=failed_command_on_first_turn,
            disconnect_after_response=disconnect_after_response,
            completion_error=completion_error,
            completion_error_on_turn=completion_error_on_turn,
            trailing_message_update=trailing_message_update,
            response_method=response_method,
        )
        self.thread_start_kwargs: dict[str, object] = {}
        self.config: Any | None = None
        self.disconnect_on_exit = disconnect_on_exit

    def __call__(self, config: Any) -> _ScriptedCodexContext:
        self.config = config
        return _ScriptedCodexContext(self)


def _request(
    tmp_path: Path,
    *,
    universal_response: str,
    max_api_step_retries: int = 3,
    workflow_steps: tuple[FrozenWorkflowStep, ...] = (),
    workflow_driver_steps: tuple[FrozenWorkflowStep, ...] = (),
    workflow_closure: FrozenWorkflowClosurePolicy | None = None,
    workflow_closure_resume: FrozenWorkflowClosureResume | None = None,
) -> CodexExecutionRequest:
    workspace = tmp_path / "workspace"
    codex_home = tmp_path / "codex-home"
    snapshot = tmp_path / "snapshot"
    for path in (workspace, codex_home, snapshot):
        path.mkdir()
    return CodexExecutionRequest(
        prompt="实现最小需求",
        workspace_path=workspace,
        codex_home_path=codex_home,
        helixharness_snapshot_path=snapshot,
        model="gpt-test",
        reasoning_effort="medium",
        permission_mode="workspace-write",
        wall_timeout_seconds=30,
        token_budget=1000,
        tool_call_budget=100,
        disk_budget_mb=100,
        universal_user_response=universal_response,
        max_business_clarifications=3,
        max_api_step_retries=max_api_step_retries,
        workflow_steps=workflow_steps,
        workflow_driver_steps=workflow_driver_steps,
        workflow_closure=workflow_closure,
        workflow_closure_resume=workflow_closure_resume,
    )


def _stage_rework_policy(*, max_cycles: int = 2) -> FrozenWorkflowClosurePolicy:
    return FrozenWorkflowClosurePolicy(
        stage_findings_prompt="处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        max_stage_rework_cycles=max_cycles,
    )


def test_chain_workflow_driver_runs_every_frozen_step_in_one_thread(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "方案完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "实现完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "验证完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    steps = (
        FrozenWorkflowStep("PROPOSE", "/helix:propose", "SPEC_READY"),
        FrozenWorkflowStep("APPLY", "/helix:apply", "IMPLEMENTED"),
        FrozenWorkflowStep("VERIFY", "/helix:verify", "VERIFIED"),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="采用推荐方案继续。",
            workflow_steps=steps,
        )
    )

    assert result.status == "COMPLETED"
    assert "[Workflow bootstrap]" in factory.thread.inputs[0]
    assert "Do not implement the request during this bootstrap turn" in factory.thread.inputs[0]
    assert factory.thread.inputs[1:] == [step.command for step in steps]
    assert tuple(turn.input_kind for turn in result.turns) == (
        "INITIAL_REQUEST",
        "WORKFLOW_STEP",
        "WORKFLOW_STEP",
        "WORKFLOW_STEP",
    )
    assert tuple(turn.workflow_step_id for turn in result.turns) == (
        None,
        "PROPOSE",
        "APPLY",
        "VERIFY",
    )
    assert tuple(turn.milestone_id for turn in result.turns) == (
        None,
        "SPEC_READY",
        "IMPLEMENTED",
        "VERIFIED",
    )








def test_workflow_apply_does_not_trigger_an_inferred_conditional_review(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "HelixHarness Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "HelixHarness Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="采用推荐方案继续。",
            workflow_steps=(apply_step, archive_step),
            workflow_closure=FrozenWorkflowClosurePolicy(
            ),
        )
    )

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "/helix:archive demo",
    ]
    assert all(turn.input_kind != "CONDITIONAL_REVIEW" for turn in result.turns)


def test_chain_workflow_driver_answers_stage_questions_and_finishes_all_steps(
    tmp_path: Path,
) -> None:
    fallback = "只使用冻结事实，采用推荐项并继续当前阶段。"
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "需要确认流程选项。\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT",
            "方案完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "需要确认仓库内配置。\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT",
            "实现与验证完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "归档完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    steps = (
        FrozenWorkflowStep("PROPOSE", "/helix:propose demo", "SPEC_READY"),
        FrozenWorkflowStep("APPLY", "/helix:apply demo", "IMPLEMENTED"),
        FrozenWorkflowStep("ARCHIVE", "/helix:archive demo", "ARCHIVED"),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response=fallback,
            workflow_steps=steps,
        )
    )

    assert result.status == "COMPLETED"
    assert result.clarification_count == 2
    assert "[Workflow bootstrap]" in factory.thread.inputs[0]
    assert factory.thread.inputs[1:] == [
        steps[0].command,
        fallback,
        steps[1].command,
        fallback,
        steps[2].command,
    ]
    assert [
        turn.workflow_step_id
        for turn in result.turns
        if turn.workflow_step_id and turn.classification == "COMPLETED"
    ] == ["PROPOSE", "APPLY", "ARCHIVE"]


def test_chain_workflow_driver_stops_when_a_stage_is_explicitly_interrupted(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "方案阶段无法继续。\nHELIXHARNESS_TURN_STATUS: FAILED",
            "确认缺少不可替代的业务事实。\nHELIXHARNESS_TURN_STATUS: FAILED",
        ]
    )
    steps = (
        FrozenWorkflowStep("PROPOSE", "/helix:propose", "SPEC_READY"),
        FrozenWorkflowStep("APPLY", "/helix:apply", "IMPLEMENTED"),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="采用推荐方案继续。",
            workflow_steps=steps,
        )
    )

    assert result.status == "FAILED"
    assert factory.thread.inputs[1] == "/helix:propose"
    assert len(factory.thread.inputs) == 2
    assert result.turns[-1].input_kind == "WORKFLOW_STEP"


def test_chain_workflow_driver_repairs_and_reruns_a_failed_stage(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "编译失败：缺少仓库内生成文件。\nHELIXHARNESS_TURN_STATUS: FAILED",
            "已定位并修复生成步骤。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "实现完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "验证完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    steps = (
        FrozenWorkflowStep("APPLY", "/helix:apply demo", "IMPLEMENTED"),
        FrozenWorkflowStep("VERIFY", "/helix:verify demo", "VERIFIED"),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=steps,
            workflow_closure=_stage_rework_policy(),
        )
    )

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:apply demo",
        "/helix:verify demo",
    ]
    assert tuple(turn.input_kind for turn in result.turns[1:]) == (
        "WORKFLOW_STEP",
        "HANDLE_STAGE_FINDINGS",
        "WORKFLOW_STEP_RERUN",
        "WORKFLOW_STEP",
    )


def test_stage_recovery_never_uses_universal_answer_for_missing_business_fact(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "实现失败：缺少值域。\nHELIXHARNESS_TURN_STATUS: FAILED",
            "必须由业务确认值域。\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT",
        ]
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="假设值域后继续。",
            workflow_steps=(FrozenWorkflowStep("APPLY", "/helix:apply demo", "IMPLEMENTED"),),
            workflow_closure=_stage_rework_policy(),
        )
    )

    assert result.status == "FAILED"
    assert factory.thread.inputs[-1] != "假设值域后继续。"
    assert len(factory.thread.inputs) == 3
    assert result.turns[-1].input_kind == "HANDLE_STAGE_FINDINGS"


def test_stage_recovery_includes_the_exact_mechanical_artifact_failure(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Verify 已通过。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "已补齐缺失产物。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Verify 已通过。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    callback_count = 0

    def save_stage(_turns: tuple[object, ...]) -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count == 1:
            raise CodexExecutionError(
                "Workflow checkpoint required artifacts are missing: "
                "docs/specs/active/demo/verify-report.md"
            )

    request = replace(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(
                FrozenWorkflowStep(
                    "VERIFY",
                    "/helix:verify demo",
                    "VERIFIED",
                ),
            ),
            workflow_closure=_stage_rework_policy(),
        ),
        on_workflow_step_completed=save_stage,
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "COMPLETED"
    repair_prompt = factory.thread.inputs[2]
    assert "[平台机械校验结果]" in repair_prompt
    assert "docs/specs/active/demo/verify-report.md" in repair_prompt
    assert "不得只在回复中声明阶段通过" in repair_prompt


def test_archive_continuation_reworks_an_incomplete_mechanical_move(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "归档前需要补齐 exec-done。\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT",
            "exec-done 已补齐，请重新归档。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "已处理归档前置缺口。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "归档已完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    callback_count = 0

    def save_archive(_turns: tuple[object, ...]) -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count == 1:
            raise CodexExecutionError("Workflow checkpoint archive move is incomplete")

    request = replace(
        _request(
            tmp_path,
            universal_response="补齐可验证的流程前置条件后继续。",
            workflow_steps=(
                FrozenWorkflowStep(
                    "ARCHIVE",
                    "/helix:archive demo",
                    "ARCHIVED",
                ),
            ),
            workflow_closure=_stage_rework_policy(),
        ),
        on_workflow_step_completed=save_archive,
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:archive demo",
        "补齐可验证的流程前置条件后继续。",
        (
            "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。\n\n"
            "[平台机械校验结果]\n"
            "Workflow checkpoint archive move is incomplete\n"
            "必须修复上述具体产物或契约缺口并真实落盘；不得只在回复中声明阶段通过，"
            "也不得伪造未执行的验证证据。当前轮不得执行其他 /helix:* 阶段命令，"
            "不得在 docs/specs/active 与 docs/specs/completed 之间移动需求目录；"
            "下一轮平台会重新执行冻结的恢复阶段。"
        ),
        "/helix:archive demo",
    ]
    assert tuple(turn.input_kind for turn in result.turns[1:]) == (
        "WORKFLOW_STEP",
        "UNIVERSAL_USER_RESPONSE",
        "HANDLE_STAGE_FINDINGS",
        "WORKFLOW_STEP_RERUN",
    )


def test_chain_workflow_driver_stops_after_the_frozen_stage_rework_limit(
    tmp_path: Path,
) -> None:
    repeated_failure = "编译仍失败：同一个缺失符号。\nHELIXHARNESS_TURN_STATUS: FAILED"
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            repeated_failure,
            "已尝试定向修复。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            repeated_failure,
            "已再次定向修复。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            repeated_failure,
        ]
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(FrozenWorkflowStep("APPLY", "/helix:apply demo", "IMPLEMENTED"),),
            workflow_closure=_stage_rework_policy(max_cycles=2),
        )
    )

    assert result.status == "FAILED"
    assert len(factory.thread.inputs) == 6
    assert tuple(turn.input_kind for turn in result.turns[1:]) == (
        "WORKFLOW_STEP",
        "HANDLE_STAGE_FINDINGS",
        "WORKFLOW_STEP_RERUN",
        "HANDLE_STAGE_FINDINGS",
        "WORKFLOW_STEP_RERUN",
    )
    assert any(
        event.payload.get("reason") == "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"
        for event in result.events
        if event.event_type == "workflow/closure"
    )


def test_chain_workflow_driver_can_rework_a_non_apply_stage(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "方案检查发现遗漏。\nHELIXHARNESS_TURN_STATUS: FAILED",
            "已补齐方案证据。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "方案完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "实现完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    steps = (
        FrozenWorkflowStep("PROPOSE", "/helix:propose demo", "SPEC_READY"),
        FrozenWorkflowStep("APPLY", "/helix:apply demo", "IMPLEMENTED"),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=steps,
            workflow_closure=_stage_rework_policy(),
        )
    )

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:propose demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:propose demo",
        "/helix:apply demo",
    ]


def test_failed_workflow_archive_repair_reruns_recommended_apply_then_archive(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "实现完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "recommendation=/helix:apply demo\n"
                "HELIXHARNESS_TURN_STATUS: FAILED"
            ),
            "当前状态仍需由冻结 Archive 重检。\nHELIXHARNESS_TURN_STATUS: FAILED",
            "Apply 已完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(apply_step, archive_step),
            workflow_closure=_stage_rework_policy(),
        )
    )

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "/helix:archive demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:apply demo",
        "/helix:archive demo",
    ]
    assert tuple(turn.input_kind for turn in result.turns[1:]) == (
        "WORKFLOW_STEP",
        "WORKFLOW_STEP",
        "HANDLE_STAGE_FINDINGS",
        "WORKFLOW_STEP_RERUN",
        "WORKFLOW_STEP",
    )


def test_resumed_workflow_archive_recovery_reruns_apply_then_archive(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        [
            (
                "recommendation=/helix:apply demo\n"
                "HELIXHARNESS_TURN_STATUS: FAILED"
            ),
            "当前状态仍需由冻结 Archive 重检。\nHELIXHARNESS_TURN_STATUS: FAILED",
            "Apply 已完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )
    request = replace(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(archive_step,),
            workflow_driver_steps=(apply_step, archive_step),
            workflow_closure=_stage_rework_policy(),
        ),
        resumed_after_step_id="APPLY",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:apply demo",
        "/helix:archive demo",
    ]
    assert tuple(turn.input_kind for turn in result.turns) == (
        "WORKFLOW_STEP",
        "HANDLE_STAGE_FINDINGS",
        "WORKFLOW_STEP_RERUN",
        "WORKFLOW_STEP",
    )


def test_completed_workflow_archive_recommendation_backroutes_before_checkpoint(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "recommendation=/helix:apply demo\n"
                "HELIXHARNESS_TURN_STATUS: COMPLETED"
            ),
            "已处理 Archive 前置状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "重新 Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )
    completed_steps: list[str | None] = []
    request = replace(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(apply_step, archive_step),
            workflow_closure=_stage_rework_policy(),
        ),
        on_workflow_step_completed=lambda turns: completed_steps.append(
            turns[-1].workflow_step_id
        ),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "/helix:archive demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:apply demo",
        "/helix:archive demo",
    ]
    assert completed_steps == ["APPLY", "APPLY", "ARCHIVE"]
    assert result.turns[2].classification == "FAILED"
    assert any(
        event.event_type == "workflow/stage-recommendation"
        for event in result.turns[2].events
    )


def test_completed_workflow_archive_ambiguous_recommendation_falls_back_without_checkpoint(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Propose 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "recommendation=/helix:propose demo\n"
                "recommendation=/helix:apply demo\n"
                "HELIXHARNESS_TURN_STATUS: COMPLETED"
            ),
            "已处理 Archive 当前状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    propose_step = FrozenWorkflowStep(
        "PROPOSE",
        "/helix:propose demo",
        "SPEC_READY",
        "HELIXHARNESS_PROPOSE",
        "PROPOSE",
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )
    completed_steps: list[str | None] = []
    request = replace(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(propose_step, apply_step, archive_step),
            workflow_closure=_stage_rework_policy(),
        ),
        on_workflow_step_completed=lambda turns: completed_steps.append(
            turns[-1].workflow_step_id
        ),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:propose demo",
        "/helix:apply demo",
        "/helix:archive demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:archive demo",
    ]
    assert completed_steps == ["PROPOSE", "APPLY", "ARCHIVE"]
    assert result.turns[3].classification == "FAILED"
    assert any(
        event.event_type == "workflow/stage-recommendation"
        and event.payload.get("recommendation_resolved") is False
        for event in result.turns[3].events
    )


def test_completed_workflow_apply_nonfrozen_recommendation_falls_back_without_checkpoint(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "recommendation=/helix:propose other-feature\n"
                "HELIXHARNESS_TURN_STATUS: COMPLETED"
            ),
            "已处理 Apply 当前状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )
    completed_steps: list[str | None] = []
    request = replace(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(apply_step, archive_step),
            workflow_closure=_stage_rework_policy(),
        ),
        on_workflow_step_completed=lambda turns: completed_steps.append(
            turns[-1].workflow_step_id
        ),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:apply demo",
        "/helix:archive demo",
    ]
    assert completed_steps == ["APPLY", "ARCHIVE"]
    assert result.turns[1].classification == "FAILED"


def test_completed_workflow_apply_recommendation_backroutes_to_propose_suffix(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Propose 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "recommendation=/helix:propose demo\n"
                "HELIXHARNESS_TURN_STATUS: COMPLETED"
            ),
            "已处理 Apply 前置状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "重新 Propose 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    propose_step = FrozenWorkflowStep(
        "PROPOSE",
        "/helix:propose demo",
        "SPEC_READY",
        "HELIXHARNESS_PROPOSE",
        "PROPOSE",
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )
    completed_steps: list[str | None] = []
    request = replace(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(propose_step, apply_step, archive_step),
            workflow_closure=_stage_rework_policy(),
        ),
        on_workflow_step_completed=lambda turns: completed_steps.append(
            turns[-1].workflow_step_id
        ),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:propose demo",
        "/helix:apply demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:propose demo",
        "/helix:apply demo",
        "/helix:archive demo",
    ]
    assert tuple(turn.input_kind for turn in result.turns[1:]) == (
        "WORKFLOW_STEP",
        "WORKFLOW_STEP",
        "HANDLE_STAGE_FINDINGS",
        "WORKFLOW_STEP_RERUN",
        "WORKFLOW_STEP",
        "WORKFLOW_STEP",
    )
    assert completed_steps == ["PROPOSE", "PROPOSE", "APPLY", "ARCHIVE"]


def test_completed_workflow_apply_can_rerun_the_recommended_same_stage(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "recommendation=/helix:apply demo\n"
                "HELIXHARNESS_TURN_STATUS: COMPLETED"
            ),
            "已处理 Apply 当前状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "重新 Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(apply_step, archive_step),
            workflow_closure=_stage_rework_policy(),
        )
    )

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:apply demo",
        "/helix:archive demo",
    ]
    assert result.turns[1].classification == "FAILED"


def test_workflow_same_stage_recommendation_stops_at_the_bounded_cycle_limit(
    tmp_path: Path,
) -> None:
    recommendation = (
        "recommendation=/helix:apply demo\n"
        "HELIXHARNESS_TURN_STATUS: COMPLETED"
    )
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            recommendation,
            "已处理 Apply 当前状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            recommendation,
        ]
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(apply_step,),
            workflow_closure=_stage_rework_policy(max_cycles=1),
        )
    )

    assert result.status == "FAILED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:apply demo",
    ]
    assert any(
        event.event_type == "workflow/closure"
        and event.payload.get("reason") == "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"
        for event in result.events
    )


def test_workflow_apply_to_propose_recovery_survives_closure_checkpoint_resume(
    tmp_path: Path,
) -> None:
    propose_step = FrozenWorkflowStep(
        "PROPOSE",
        "/helix:propose demo",
        "SPEC_READY",
        "HELIXHARNESS_PROPOSE",
        "PROPOSE",
    )
    apply_step = FrozenWorkflowStep(
        "APPLY",
        "/helix:apply demo",
        "IMPLEMENTED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )
    steps = (propose_step, apply_step, archive_step)
    checkpoints: list[
        tuple[
            tuple[CodexTurnExecutionResult, ...],
            FrozenWorkflowClosureResume,
            tuple[FrozenWorkflowStep, ...],
        ]
    ] = []
    first_factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Propose 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "recommendation=/helix:propose demo\n"
                "HELIXHARNESS_TURN_STATUS: COMPLETED"
            ),
            "已处理 Apply 前置状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "重新 Propose 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    first_request = replace(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=steps,
            workflow_closure=_stage_rework_policy(),
        ),
        on_workflow_closure_checkpoint=lambda turns, resume, remaining: checkpoints.append(
            (turns, resume, remaining)
        ),
    )

    first_result = CodexPythonSdkAdapter(codex_factory=first_factory).execute(first_request)

    assert first_result.status == "COMPLETED"
    assert checkpoints
    _, frozen_resume, remaining_steps = checkpoints[0]
    assert frozen_resume.origin_step == propose_step
    assert frozen_resume.next_input_kind == "HANDLE_STAGE_FINDINGS"
    assert remaining_steps == (apply_step, archive_step)

    resume_root = tmp_path / "resume"
    resume_root.mkdir()
    resumed_factory = _ScriptedCodexFactory(
        [
            "已恢复。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "已处理 Apply 前置状态。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "重新 Propose 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Apply 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "Archive 完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    resumed_result = CodexPythonSdkAdapter(codex_factory=resumed_factory).execute(
        _request(
            resume_root,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=remaining_steps,
            workflow_driver_steps=steps,
            workflow_closure=_stage_rework_policy(),
            workflow_closure_resume=frozen_resume,
        )
    )

    assert resumed_result.status == "COMPLETED"
    assert resumed_factory.thread.inputs[1:] == [
        "处理当前阶段失败；保留正确结果，只修复有证据支持的问题。",
        "/helix:propose demo",
        "/helix:apply demo",
        "/helix:archive demo",
    ]




def test_resumed_stage_rework_reruns_the_exact_origin_then_remaining_steps(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已从检查点恢复。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "实现返工完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "验证完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    apply_step = FrozenWorkflowStep("APPLY", "/helix:apply demo", "IMPLEMENTED")
    verify_step = FrozenWorkflowStep("VERIFY", "/helix:verify demo", "VERIFIED")

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="不得编造缺失业务事实。",
            workflow_steps=(verify_step,),
            workflow_driver_steps=(apply_step, verify_step),
            workflow_closure=_stage_rework_policy(),
            workflow_closure_resume=FrozenWorkflowClosureResume(
                origin_step=apply_step,
                next_input_kind="WORKFLOW_STEP_RERUN",
                stage_rework_count=1,
            ),
        )
    )

    assert result.status == "COMPLETED"
    assert factory.thread.inputs[1:] == [
        "/helix:apply demo",
        "/helix:verify demo",
    ]
    assert tuple(turn.input_kind for turn in result.turns[1:]) == (
        "WORKFLOW_STEP_RERUN",
        "WORKFLOW_STEP",
    )


def test_chain_workflow_driver_recovers_a_transient_stage_start(
    tmp_path: Path,
) -> None:
    class RecoveringThread(_ScriptedThread):
        def __init__(self) -> None:
            super().__init__(
                [
                    "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
                    "方案完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
                    "实现完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
                    "验证完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
                ]
            )
            self.apply_attempts = 0

        def turn(self, prompt: str, **kwargs: object) -> _ScriptedTurn:
            if prompt == "/helix:apply":
                self.apply_attempts += 1
                if self.apply_attempts == 1:
                    raise ConnectionError("temporary workflow interruption")
            return super().turn(prompt, **kwargs)

    factory = _ScriptedCodexFactory([])
    factory.thread = RecoveringThread()
    steps = (
        FrozenWorkflowStep("PROPOSE", "/helix:propose", "SPEC_READY"),
        FrozenWorkflowStep("APPLY", "/helix:apply", "IMPLEMENTED"),
        FrozenWorkflowStep("VERIFY", "/helix:verify", "VERIFIED"),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="采用推荐方案继续。",
            workflow_steps=steps,
        )
    )

    assert result.status == "COMPLETED"
    assert factory.thread.apply_attempts == 2
    assert factory.thread.inputs[1:] == [step.command for step in steps]
    assert any(event.event_type == "api/retry" for event in result.events)


def test_transient_api_start_retries_stay_inside_one_episode_attempt(
    tmp_path: Path,
) -> None:
    class TransientThread(_ScriptedThread):
        def __init__(self) -> None:
            super().__init__(["完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"])
            self.start_calls = 0

        def turn(self, prompt: str, **kwargs: object) -> _ScriptedTurn:
            self.start_calls += 1
            if self.start_calls <= 2:
                raise ConnectionError("temporary API transport failure")
            return super().turn(prompt, **kwargs)

    factory = _ScriptedCodexFactory([])
    transient_thread = TransientThread()
    factory.thread = transient_thread

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="采用推荐项并继续。",
            max_api_step_retries=2,
        )
    )

    assert result.status == "COMPLETED"
    assert transient_thread.start_calls == 3
    assert len(result.turns) == 1
    assert [event.payload["retry_number"] for event in result.events if event.event_type == "api/retry"] == [1, 2]


def test_adapter_continues_one_thread_with_the_frozen_user_response(
    tmp_path: Path,
) -> None:
    frozen_response = "采用推荐的最小必要假设并继续。"
    factory = _ScriptedCodexFactory(
        [
            "请确认排序规则。\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT",
            "还需要确认异常策略。\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT",
            "实现完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response=frozen_response)
    )

    assert result.status == "COMPLETED"
    assert result.thread_id == "one-episode-thread"
    assert result.clarification_count == 2
    assert [turn.classification for turn in result.turns] == [
        "NEEDS_USER_INPUT",
        "NEEDS_USER_INPUT",
        "COMPLETED",
    ]
    assert factory.thread.inputs[0].endswith("[Initial request]\n实现最小需求")
    assert factory.thread.inputs[1:] == [frozen_response, frozen_response]


def test_fourth_business_clarification_ends_as_stuck(
    tmp_path: Path,
) -> None:
    frozen_response = "采用推荐项并继续。"
    factory = _ScriptedCodexFactory(
        [f"问题 {index}\nHELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT" for index in range(1, 5)]
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response=frozen_response)
    )

    assert result.status == "STUCK_CLARIFICATION"
    assert result.clarification_count == 3
    assert len(result.turns) == 4
    assert factory.thread.inputs[1:] == [frozen_response] * 3


def test_archive_commit_choice_uses_frozen_policy_without_business_clarification(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            """归档前请选择：

1. 提交已封存的 Apply Scope 并继续归档
2. 暂不提交 Apply Scope，归档后再决定
3. 取消归档""",
            "已按选择 2 保留未提交状态并完成归档。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    archive = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
        "GENERIC_STAGE_EVIDENCE",
        "ARCHIVE",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="采用推荐项并继续。",
            workflow_steps=(archive,),
        )
    )

    assert result.status == "COMPLETED"
    assert result.clarification_count == 0
    assert factory.thread.inputs[1] == archive.command
    assert factory.thread.inputs[2].startswith("选择 2：暂不提交 Apply Scope")
    assert result.turns[1].input_kind == "WORKFLOW_STEP"
    assert result.turns[2].input_kind == "WORKFLOW_POLICY_RESPONSE"
    assert any(
        event.event_type == "workflow/policy-response"
        and event.status == "TRANSITIONED"
        for event in result.events
    )


def test_propose_confirmation_uses_frozen_policy_without_business_clarification(
    tmp_path: Path,
) -> None:
    frozen_response = "只使用冻结事实继续，不补造业务答案。"
    factory = _ScriptedCodexFactory(
        [
            "已准备。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            (
                "共享理解已闭合。请回复“确认”以生成并校验 spec.md 与 tasks.md。\n"
                "HELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT"
            ),
            "Propose 已完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ]
    )
    propose = FrozenWorkflowStep(
        "PROPOSE",
        "/helix:propose demo request.md --workflow",
        "SPEC_READY",
        "GENERIC_STAGE_EVIDENCE",
        "PROPOSE",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response=frozen_response,
            workflow_steps=(propose,),
        )
    )

    assert result.status == "COMPLETED"
    assert result.clarification_count == 0
    assert factory.thread.inputs[2] == f"确认。{frozen_response}"
    assert result.turns[2].input_kind == "WORKFLOW_POLICY_RESPONSE"


def test_failed_command_does_not_end_turn_before_codex_recovers(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        ["首次测试失败后已修复，并重新验证通过。\nHELIXHARNESS_TURN_STATUS: COMPLETED"],
        failed_command_on_first_turn=True,
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "COMPLETED"
    assert result.clarification_count == 0
    assert any(event.category == "command" and event.status == "FAILED" for event in result.turns[0].events)


def test_preconfigured_system_confirmation_does_not_consume_clarification(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        ["越界操作已由策略拒绝，已改用工作区内方案完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"]
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert factory.thread_start_kwargs["approval_mode"].value == "deny_all"
    assert result.status == "COMPLETED"
    assert result.clarification_count == 0
    assert len(factory.thread.inputs) == 1


def test_execution_agent_uses_persisted_root_thread_for_collaboration(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        ["执行完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"]
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "COMPLETED"
    assert factory.thread_start_kwargs["ephemeral"] is False


def test_execution_agent_disables_remote_plugin_discovery(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        ["执行完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"]
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "COMPLETED"
    assert factory.config is not None
    assert "features.remote_plugin=false" in factory.config.launch_args_override


def test_completed_final_response_survives_a_late_stream_disconnect(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        ["执行完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"],
        disconnect_after_response=True,
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "COMPLETED"
    assert result.error is None
    assert result.turns[0].classification == "COMPLETED"
    assert any(event.event_type == "stream/disconnect-after-final" for event in result.events)


def test_unstructured_response_does_not_hide_a_stream_disconnect(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        ["执行中断，尚未形成可信终态。"],
        disconnect_after_response=True,
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "FAILED"
    assert result.error == {"type": "RuntimeError", "message": "Reconnecting... 1/5"}
    assert result.turns == ()


def test_structured_response_survives_a_trailing_update_and_failed_turn_completion(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        ["执行完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"],
        completion_error={"message": "Reconnecting... 1/5"},
        trailing_message_update=True,
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "COMPLETED"
    assert result.error is None
    assert result.turns[0].classification == "COMPLETED"
    assert any(event.event_type == "turn/disconnect-after-final" for event in result.events)


def test_failed_turn_completion_accepts_a_structured_final_response_from_an_update(
    tmp_path: Path,
) -> None:
    factory = _ScriptedCodexFactory(
        ["执行完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"],
        completion_error={"message": "Reconnecting... 1/5"},
        response_method="item/updated",
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "COMPLETED"
    assert result.error is None
    assert result.turns[0].classification == "COMPLETED"
    assert any(event.event_type == "turn/disconnect-after-final" for event in result.events)


def test_structured_failure_survives_a_transient_failed_turn_completion(tmp_path: Path) -> None:
    factory = _ScriptedCodexFactory(
        ["任务已形成业务失败终态。\nHELIXHARNESS_TURN_STATUS: FAILED"],
        completion_error={"message": "Reconnecting... 1/5"},
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "FAILED"
    assert result.error is None
    assert result.turns[0].classification == "FAILED"
    assert any(event.event_type == "turn/disconnect-after-final" for event in result.events)


def test_late_context_exit_disconnect_preserves_workflow_terminal_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    factory = _ScriptedCodexFactory(
        [
            "已从检查点恢复。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
            "归档门禁未通过。\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        ],
        disconnect_on_exit=True,
    )
    archive_step = FrozenWorkflowStep(
        "ARCHIVE",
        "/helix:archive demo",
        "ARCHIVED",
    )
    goal_policy = WorkflowGoalPolicy(
        require_archive_completed=True,
        max_archive_attempts=0,
        max_inferred_actions=1,
        max_stage_rework_cycles=0,
        stage_findings_prompt="",
        actions=(
            WorkflowGoalAction(
                action="ARCHIVE",
                kind="COMMAND",
                input=archive_step.command,
            ),
        ),
    )
    monkeypatch.setattr(
        CodexPythonSdkAdapter,
        "_workflow_goal_observation",
        lambda *_args, **_kwargs: WorkflowGoalObservation(False),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(
            tmp_path,
            universal_response="采用推荐项并继续。",
            workflow_steps=(archive_step,),
            workflow_driver_steps=(archive_step,),
            workflow_closure=FrozenWorkflowClosurePolicy(
                goal_policy=goal_policy,
            ),
        )
    )

    assert result.status == "FAILED"
    assert result.error is None
    assert any(event.event_type == "session/disconnect-after-terminal" for event in result.events)
    assert any(
        event.event_type == "workflow/goal-control"
        and event.payload.get("reason") == "ARCHIVE_ATTEMPT_LIMIT_EXHAUSTED"
        for event in result.events
    )


def test_structured_response_does_not_hide_a_non_transient_turn_failure(tmp_path: Path) -> None:
    completion_error = {"type": "PolicyViolation", "message": "permission denied"}
    factory = _ScriptedCodexFactory(
        ["执行完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"],
        completion_error=completion_error,
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(
        _request(tmp_path, universal_response="采用推荐项并继续。")
    )

    assert result.status == "FAILED"
    assert result.error == completion_error
    assert result.turns[0].classification == "FAILED"
    assert not any(event.event_type == "turn/disconnect-after-final" for event in result.events)


def test_execution_disk_budget_includes_persisted_codex_home(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    factory = _ScriptedCodexFactory(
        ["执行完成。\nHELIXHARNESS_TURN_STATUS: COMPLETED"],
        failed_command_on_first_turn=True,
    )
    request = _request(tmp_path, universal_response="采用推荐项并继续。")
    sizes = {
        request.workspace_path: 0,
        request.codex_home_path: (request.disk_budget_mb + 1) * 1024 * 1024,
    }
    monkeypatch.setattr(
        CodexPythonSdkAdapter,
        "_directory_size_bytes",
        staticmethod(lambda path: sizes.get(path, 0)),
    )

    result = CodexPythonSdkAdapter(codex_factory=factory).execute(request)

    assert result.status == "BUDGET_EXHAUSTED"
    assert result.error == {
        "type": "BudgetExceeded",
        "budget": "disk_budget_mb",
        "limit": request.disk_budget_mb,
        "message": "disk_budget_mb budget exceeded",
    }
