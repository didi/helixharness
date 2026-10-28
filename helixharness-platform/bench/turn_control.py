"""Frozen protocol for classifying and continuing Codex turns."""

from enum import Enum

TURN_PROTOCOL_VERSION = "framework-eval-turn-protocol.v1"
TURN_STATUS_PREFIX = "HELIXHARNESS_TURN_STATUS: "
DEFAULT_MAX_BUSINESS_CLARIFICATIONS = 3
EVIDENCE_BOUND_USER_RESPONSE_VERSION = "evidence-bound-user-response.v3"
EVIDENCE_BOUND_USER_RESPONSE = (
    "请先检查冻结的 Initial Request、代码库和已有项目资料，只使用其中可追溯的事实继续。"
    "以下流程选择已经由本次 Run 执行策略冻结，不属于业务事实："
    "Review finding 若与冻结需求及证据一致且能在当前范围修复则接受并闭环；"
    "若与冻结需求冲突或缺少证据则拒绝并说明依据，不得无理由 defer。"
    "基线编译或测试失败只有在证据证明其预先存在、与当前需求无关且不会破坏验收时才可记录后继续；"
    "否则必须修复或明确失败，不得直接豁免。"
    "归档时，对当前需求且有仓库证据支持的知识更新选择执行，经验总结选择自动生成；"
    "评测工作区一律不 commit、不 push。"
    "其他不改变业务需求、实现契约或验收标准的流程性可选项，采用推荐项并记录选择；"
    "推荐项必须由当前命令明确给出。"
    "仓库中可查询的事实请自行查证；若仍缺少只能由业务提供的事实，请明确列出并返回 "
    "NEEDS_USER_INPUT，不得补造假设、值域、隐私属性或验收证据。"
)
TURN_PROTOCOL = """\
[HelixHarness evaluation turn protocol]
At the end of every response, append exactly one of these lines:
HELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT
HELIXHARNESS_TURN_STATUS: COMPLETED
HELIXHARNESS_TURN_STATUS: FAILED

Use NEEDS_USER_INPUT only when missing business information prevents further work.
System permissions, telemetry authorization, and dangerous-operation confirmation are
preconfigured and are not business clarification. A compilation or test failure is not
terminal while it can still be investigated or fixed within the remaining budget. Use
FAILED only when there is no viable path to continue. Do not put any text after the
status line.
"""

TURN_STATUS_REPAIR_PROMPT = """\
[HelixHarness evaluation protocol repair]
The previous response omitted or malformed its final HELIXHARNESS_TURN_STATUS line.
Do not continue business work and do not add explanations. Based only on the previous
response's real intent, reply with exactly one of these lines:
HELIXHARNESS_TURN_STATUS: NEEDS_USER_INPUT
HELIXHARNESS_TURN_STATUS: COMPLETED
HELIXHARNESS_TURN_STATUS: FAILED
"""


def has_valid_turn_status(final_response: str | None) -> bool:
    """Return whether the response ends with exactly one supported status marker."""

    if not isinstance(final_response, str):
        return False
    lines = final_response.rstrip().splitlines()
    status_lines = [line for line in lines if line.startswith(TURN_STATUS_PREFIX)]
    if len(status_lines) != 1:
        return False
    final_line = lines[-1] if lines else ""
    if not final_line.startswith(TURN_STATUS_PREFIX):
        return False
    try:
        TurnClassification(final_line.removeprefix(TURN_STATUS_PREFIX))
    except ValueError:
        return False
    return True


class TurnClassification(str, Enum):
    """The complete output domain of the frozen Turn Classifier."""

    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FrozenTurnClassifier:
    """Classify an SDK turn through the explicit frozen response contract."""

    def classify(
        self,
        *,
        sdk_status: str,
        final_response: str | None,
        error: dict[str, object] | None,
    ) -> TurnClassification:
        """Return exactly one supported result without generating an answer."""

        if sdk_status != "COMPLETED" or error is not None or final_response is None:
            return TurnClassification.FAILED
        final_line = final_response.rstrip().rsplit("\n", maxsplit=1)[-1]
        if not final_line.startswith(TURN_STATUS_PREFIX):
            return TurnClassification.FAILED
        value = final_line.removeprefix(TURN_STATUS_PREFIX)
        try:
            declared = TurnClassification(value)
        except ValueError:
            return TurnClassification.FAILED
        response_lines = tuple(
            line.strip()
            for line in final_response.rstrip().rsplit("\n", maxsplit=1)[0].splitlines()
        )
        if any(line.startswith("BLOCKED:") for line in response_lines):
            return TurnClassification.FAILED
        if declared is TurnClassification.COMPLETED and any(
            "被前置门禁阻止" in line
            or "review：`REJECT`" in line
            or "验收：`不通过`" in line
            for line in response_lines
        ):
            return TurnClassification.FAILED
        if declared is TurnClassification.COMPLETED and any(
            line in {"请回复：", "请选择："} for line in response_lines
        ):
            return TurnClassification.NEEDS_USER_INPUT
        if declared is TurnClassification.COMPLETED and any(
            line.endswith(("?", "？"))
            and any(marker in line for marker in ("是否", "要不要", "能否", "可否", "吗"))
            for line in response_lines
        ):
            return TurnClassification.NEEDS_USER_INPUT
        return declared


def initial_turn_prompt(initial_request: str) -> str:
    """Attach the frozen classifier contract to the Case request."""

    return f"{TURN_PROTOCOL}\n[Initial request]\n{initial_request}"


def workflow_bootstrap_prompt(initial_request: str) -> str:
    """Seed requirement context without bypassing the frozen workflow commands."""

    return (
        f"{TURN_PROTOCOL}\n[Workflow bootstrap]\n"
        "Read the request context below and acknowledge readiness. "
        "Do not implement the request during this bootstrap turn; "
        "the evaluator will send each required HelixHarness command next.\n\n"
        f"[Initial request]\n{initial_request}"
    )


def resumed_workflow_step_prompt(
    initial_request: str,
    command: str,
    resumed_after_step_id: str,
) -> str:
    """Resume directly at the next frozen step while reseeding the Turn protocol."""

    return (
        f"{TURN_PROTOCOL}\n[Checkpoint resume after {resumed_after_step_id}]\n"
        "The workspace already contains every validated artifact from the completed "
        "workflow steps. Do not repeat those steps. Execute only the next frozen "
        "workflow step below.\n\n"
        f"[Initial request]\n{initial_request}\n\n"
        f"[Next workflow step]\n{command}"
    )
