"""Fixed, evidence-scoped semantic Judge contracts for Scenario v2."""

from __future__ import annotations

from typing import Any

from bench.semantic_judging import JudgeContractError, trusted_score_contract, validate_judge_output

OUTCOME_AXES = ("requirement_fulfillment", "implementation_quality")

_TEST_DELIVERABLE_TERMS = (
    "测试类",
    "测试套件",
    "测试场景",
    "测试代码",
    "测试用例",
    "单元测试",
    "集成场景",
    "对应测试",
    "test class",
    "test suite",
    "test scenario",
    "test code",
    "test case",
    "unit test",
    "integration test",
)

AXIS_CRITERIA = {
    "requirement_fulfillment": (
        "在所有冻结业务检查均已通过的前提下，评价最终实现对每一条冻结 Requirement 的满足程度。"
        "业务检查 PASS 是对应可观察行为已满足的确定性事实，不得仅凭源码推测否定已经 PASS 的行为；"
        "PASS 只覆盖检查明确验证的范围，不能自动证明未覆盖的 Requirement、边界和非回归全部满足。"
        "阶段产物只能用于理解意图和定位实现，产物中的完成声明不能证明业务成功。"
        "具体实现证据和实际执行的检查结果，其证据优先级高于文档声明。"
        "没有执行证据支持时，不得声称某个行为必然失败，只能记录实现风险或证据限制。"
    ),
    "implementation_quality": (
        "评价本次 Requirement 明确要求交付的实现质量。默认只评价本次新增或修改的生产代码。"
        "只有冻结 Requirement 明确要求交付测试类、测试套件或测试场景时，才评价对应测试代码的实现质量。"
        "普通辅助测试仅作为验证证据，不得因其排版、命名或代码风格扣分；"
        "不得评价既有测试、平台测试或隐藏测试的代码风格。"
        "评价范围包括清晰度、局部复杂度、重复、异常处理、仓库一致性、可维护性和可测试性。"
        "不得要求实现采用 Gold 架构，也不得因无关的既有代码而扣分。"
    ),
    "goal_requirement_decomposition": (
        "评价阶段产物语料是否准确理解目标，并充分拆解 Requirement、范围、约束、边界和失败场景。"
        "不得根据文件名、文件数量或文档长度评分。"
    ),
    "decision_planning_quality": (
        "评价阶段产物语料中的决策、取舍、依赖、风险和实施计划是否具体、连贯且可执行。"
    ),
    "implementation_traceability": (
        "只评价阶段产物中 Requirement、决策、Tasks、实现说明、验证说明和保留证据能否保持一致追溯；"
        "不得据此判断代码是否真实实现。"
    ),
    "verification_completion_quality": (
        "结合冻结 Requirement，评价阶段产物中的验证与完成说明是否完整、清晰且前后一致："
        "是否定义验证范围、方法和预期结果，是否覆盖正常行为、边界、失败处理和必要非回归，"
        "是否清楚记录完成项、未完成项、遗留风险和后续事项。"
        "只评价阶段产物的语义质量，不得判断测试命令是否真实执行或业务行为是否实际通过；"
        "阶段产物中的 PASS 或完成声明不视为业务成功证据。"
    ),
}

AXIS_ANCHORS = {
    "requirement_fulfillment": {
        "0": "实现与冻结 Requirement 冲突，或没有证据支持任何 Requirement 达成",
        "25": "仅少量 Requirement 有实现证据，主体业务目标未落实",
        "50": "部分 Requirement 得到落实，但存在明显业务、边界或非回归缺口",
        "75": "核心 Requirement 已落实，但仍存在有证据支持的实质性非阻断缺口",
        "100": "所有冻结 Requirement 均有业务检查和实现证据支持，没有实质性缺口",
    },
    "implementation_quality": {
        "0": "本次交付实现不可维护、明显错误，或与仓库基本约束冲突",
        "25": "实现存在广泛且严重的清晰度、复杂度、重复或维护性问题",
        "50": "实现可工作但有多个明显质量缺口，显著增加维护或回归风险",
        "75": "整体实现清晰可维护，但仍存在有证据支持的实质性非阻断质量缺口",
        "100": "本次要求交付的实现清晰、简洁、一致且可维护，没有实质性质量缺口",
    },
    "goal_requirement_decomposition": {
        "0": "阶段产物未识别目标和 Requirement，或与其明确冲突",
        "25": "只记录少量目标或 Requirement，主体范围未拆解",
        "50": "覆盖部分目标和 Requirement，但范围、约束、边界或失败场景有明显缺失",
        "75": "核心目标和 Requirement 已拆解，但仍缺少部分重要边界、约束或失败场景",
        "100": "目标、Requirement、范围、约束、边界和失败场景均被准确且充分拆解",
    },
    "decision_planning_quality": {
        "0": "没有可识别的决策或实施计划，或计划与 Requirement 冲突",
        "25": "只有零散任务或意图，无法形成可执行计划",
        "50": "存在计划框架，但关键决策、步骤、依赖、风险或完成条件明显缺失",
        "75": "主体计划具体可执行，但仍缺少部分重要取舍、风险应对、依赖处理或完成条件",
        "100": "关键决策、取舍、依赖、风险、步骤和完成条件均具体、连贯且可执行",
    },
    "implementation_traceability": {
        "0": "阶段产物之间无法建立 Requirement 到实现说明的追溯关系",
        "25": "仅少量 Requirement 能追溯到决策、Tasks 或实现说明",
        "50": "部分链路可追溯，但存在多处明显断点或跨文件矛盾",
        "75": "核心链路保持一致追溯，但仍有部分重要断点、模糊映射或不一致",
        "100": "Requirement、决策、Tasks、实现说明、验证说明和证据之间均可一致追溯",
    },
    "verification_completion_quality": {
        "0": "阶段产物没有验证与完成说明，或说明与 Requirement 明确冲突",
        "25": "只记录少量验证意图或完成声明，主体 Requirement 没有对应说明",
        "50": "覆盖部分验证与完成内容，但范围、方法、预期结果或遗留项有明显缺失",
        "75": "核心验证与完成说明完整，但仍缺少部分重要边界、失败处理、非回归或遗留风险说明",
        "100": "验证范围、方法、预期结果、边界、失败处理、非回归、完成项和遗留项说明完整且一致",
    },
}


def build_outcome_judge_request(
    contract: dict[str, object],
    *,
    gold: dict[str, Any],
    verification: dict[str, object],
    evidence: list[dict[str, object]],
) -> dict[str, Any]:
    """Build one Outcome request after deterministic business gates pass."""

    if verification.get("status") != "PASS":
        raise JudgeContractError("Outcome Judge requires every frozen business check to pass")
    profile = _object(contract.get("scoring_profile"), "Scoring Profile")
    outcome_weights = _object(profile.get("outcome_weights"), "Outcome weights")
    if set(outcome_weights) != set(OUTCOME_AXES):
        raise JudgeContractError("Scoring Profile Outcome axes are invalid")
    traceability = _traceability(gold)
    test_deliverable_requirement_ids = _test_deliverable_requirement_ids(traceability)
    all_ids = [str(item.get("evidence_id")) for item in evidence]
    production_code_ids = [
        str(item.get("evidence_id"))
        for item in evidence
        if str(item.get("kind", "")).startswith("CODE_")
    ]
    test_code_ids = [
        str(item.get("evidence_id"))
        for item in evidence
        if str(item.get("kind", "")).startswith("TEST_")
    ]
    scored_test_code_ids = test_code_ids if test_deliverable_requirement_ids else []
    implementation_ids = [*production_code_ids, *scored_test_code_ids]
    if not implementation_ids:
        raise JudgeContractError("Outcome Judge requires code or test evidence")
    verification_id = "business_verification"
    return {
        "schema_version": "framework-eval-anonymous-outcome-judge-input.v2",
        "axes": list(OUTCOME_AXES),
        "score_contract": {
            **trusted_score_contract(),
            "criteria": {axis: AXIS_CRITERIA[axis] for axis in OUTCOME_AXES},
            "axis_anchors": {axis: AXIS_ANCHORS[axis] for axis in OUTCOME_AXES},
            "weights": outcome_weights,
            "evidence_authority": [
                "冻结 Requirement",
                "业务验证结果",
                "最终实现代码",
                "阶段产物声明",
            ],
            "stage_artifact_policy": "阶段产物只作为上下文，其文档质量不能直接提高 Outcome 分数。",
            "business_pass_policy": (
                "业务检查 PASS 不得被源码推测推翻，但只证明该检查明确覆盖的可观察行为。"
            ),
            "cross_axis_deduction_policy": (
                "同一个缺口不得在 requirement_fulfillment 和 implementation_quality 重复扣分。"
                "直接违反冻结 Requirement 的缺口只在 requirement_fulfillment 扣分；"
                "Requirement 已满足但实现存在维护性问题时，只在 implementation_quality 扣分。"
            ),
            "implementation_evidence_policy": {
                "production_code_evidence_ids": production_code_ids,
                "test_code_evidence_ids": scored_test_code_ids,
                "test_deliverable_requirement_ids": test_deliverable_requirement_ids,
                "test_code_scoring_rule": (
                    "仅当冻结 Requirement 明确要求测试类、测试套件或测试场景作为交付物时，"
                    "才能评价对应 TEST_* 的实现质量；否则 TEST_* 只能支持 Requirement 满足度，"
                    "不得用于测试代码风格或维护性扣分。"
                ),
            },
        },
        "traceability": traceability,
        "verification": {"evidence_id": verification_id, **verification},
        "evidence": evidence,
        "evidence_scopes": [
            {
                "axis": "requirement_fulfillment",
                "allowed_evidence_ids": [verification_id, *all_ids],
            },
            {
                "axis": "implementation_quality",
                "allowed_evidence_ids": implementation_ids,
            },
        ],
        "citation_requirements": [
            {
                "axis": "requirement_fulfillment",
                "required_requirement_ids": [
                    str(item["requirement_id"])
                    for item in traceability["requirements"]
                ],
                "required_evidence_ids": [verification_id],
                "at_least_one_evidence_id_from": implementation_ids,
            }
        ],
    }


def build_workflow_judge_request(
    contract: dict[str, object],
    *,
    gold: dict[str, Any],
    evidence: list[dict[str, object]],
) -> dict[str, Any]:
    """Build one workflow request over the complete Markdown feature package."""

    profile = _object(contract.get("scoring_profile"), "Scoring Profile")
    workflow_axes = _object(profile.get("workflow_axes"), "Workflow axes")
    axes = tuple(str(axis) for axis in workflow_axes)
    if len(axes) != 4 or any(int(weight) != 25 for weight in workflow_axes.values()):
        raise JudgeContractError("Workflow Judge requires four equally weighted axes")
    stage_evidence = [item for item in evidence if item.get("kind") == "STAGE_ARTIFACT"]
    stage_ids = [str(item.get("evidence_id")) for item in stage_evidence]
    if not stage_ids:
        raise JudgeContractError("Workflow Judge requires stage artifact evidence")
    return {
        "schema_version": "framework-eval-anonymous-workflow-judge-input.v2",
        "axes": list(axes),
        "score_contract": {
            **trusted_score_contract(),
            "criteria": {axis: AXIS_CRITERIA[axis] for axis in axes},
            "axis_anchors": {axis: AXIS_ANCHORS[axis] for axis in axes},
            "weights": workflow_axes,
            "corpus_policy": (
                "只评价语义质量和跨文件一致性。不得根据文件名、文件数量、文档长度、工作流变体，"
                "或与参考产物集合的相似度评分。"
            ),
            "execution_fact_policy": (
                "Workflow 只评价阶段产物本身，不得判断业务是否实际成功，不得用代码、运行日志、"
                "F2P/P2P 或 Outcome 结果对 Workflow 加分或扣分。"
            ),
        },
        "traceability": _traceability(gold),
        "evidence": stage_evidence,
        "evidence_scopes": [
            {"axis": axis, "allowed_evidence_ids": stage_ids}
            for axis in axes
        ],
    }


def validate_scenario_judgment(
    output: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, float]:
    """Validate anchored traceability and every evidence citation."""

    raw_scopes = request.get("evidence_scopes")
    traceability = request.get("traceability")
    if not isinstance(raw_scopes, list) or not isinstance(traceability, dict):
        raise JudgeContractError("Scenario Judge request is invalid")
    scopes = {
        str(item["axis"]): {str(value) for value in item["allowed_evidence_ids"]}
        for item in raw_scopes
        if isinstance(item, dict)
        and isinstance(item.get("axis"), str)
        and isinstance(item.get("allowed_evidence_ids"), list)
    }
    requirements = traceability.get("requirements")
    gold_refs = traceability.get("gold_evidence_refs")
    valid_requirement_ids = {
        str(item["requirement_id"])
        for item in requirements
        if isinstance(item, dict) and isinstance(item.get("requirement_id"), str)
    } if isinstance(requirements, list) else set()
    valid_gold_refs = {
        str(item["ref_id"])
        for item in gold_refs
        if isinstance(item, dict) and isinstance(item.get("ref_id"), str)
    } if isinstance(gold_refs, list) else set()
    scores = validate_judge_output(
        output,
        expected_schema="framework-eval-judge-output.v2",
        valid_evidence_ids=set().union(*scopes.values()) if scopes else set(),
        valid_evidence_ids_by_axis=scopes,
        valid_requirement_ids=valid_requirement_ids,
        valid_gold_evidence_refs=valid_gold_refs,
        expected_axes=tuple(str(axis) for axis in request.get("axes", [])),
    )
    _validate_required_citations(output, request)
    _validate_implementation_test_citations(output, request)
    return scores


def _validate_required_citations(output: dict[str, Any], request: dict[str, Any]) -> None:
    """Fail closed when a trusted axis omits its frozen mandatory citations."""

    raw_requirements = request.get("citation_requirements", [])
    if not isinstance(raw_requirements, list):
        raise JudgeContractError("Judge citation requirements are invalid")
    axis_items = {
        str(item.get("axis")): item
        for item in output.get("axis_scores", [])
        if isinstance(item, dict)
    }
    for requirement in raw_requirements:
        if not isinstance(requirement, dict):
            raise JudgeContractError("Judge citation requirements are invalid")
        axis = requirement.get("axis")
        required_requirement_ids = requirement.get("required_requirement_ids")
        required_evidence_ids = requirement.get("required_evidence_ids")
        evidence_group = requirement.get("at_least_one_evidence_id_from")
        item = axis_items.get(str(axis))
        if (
            item is None
            or not isinstance(required_requirement_ids, list)
            or not required_requirement_ids
            or not isinstance(required_evidence_ids, list)
            or not required_evidence_ids
            or not isinstance(evidence_group, list)
            or not evidence_group
        ):
            raise JudgeContractError("Judge citation requirements are invalid")
        cited_requirements = set(item.get("requirement_ids", []))
        cited_evidence = set(item.get("evidence_ids", []))
        if not set(required_requirement_ids).issubset(cited_requirements):
            raise JudgeContractError("Judge omitted a required Requirement citation")
        if not set(required_evidence_ids).issubset(cited_evidence):
            raise JudgeContractError("Judge omitted a required evidence citation")
        if cited_evidence.isdisjoint(evidence_group):
            raise JudgeContractError("Judge omitted the required implementation evidence citation")


def _validate_implementation_test_citations(
    output: dict[str, Any],
    request: dict[str, Any],
) -> None:
    """Require explicit frozen Requirement citations whenever test code is scored."""

    score_contract = request.get("score_contract")
    policy = score_contract.get("implementation_evidence_policy") if isinstance(score_contract, dict) else None
    if not isinstance(policy, dict):
        return
    test_ids = policy.get("test_code_evidence_ids")
    requirement_ids = policy.get("test_deliverable_requirement_ids")
    if not isinstance(test_ids, list) or not isinstance(requirement_ids, list):
        raise JudgeContractError("Implementation evidence policy is invalid")
    implementation = next(
        (
            item
            for item in output.get("axis_scores", [])
            if isinstance(item, dict) and item.get("axis") == "implementation_quality"
        ),
        None,
    )
    if not isinstance(implementation, dict):
        return
    cited_evidence = set(implementation.get("evidence_ids", []))
    if cited_evidence.isdisjoint(test_ids):
        return
    cited_requirements = set(implementation.get("requirement_ids", []))
    if not requirement_ids or not set(requirement_ids).issubset(cited_requirements):
        raise JudgeContractError("Judge omitted a test-deliverable Requirement citation")


def _traceability(gold: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    requirements = gold.get("requirements")
    source_evidence = gold.get("source_evidence")
    if not isinstance(requirements, list) or not isinstance(source_evidence, list):
        raise JudgeContractError("Scenario Gold traceability is invalid")
    gold_refs = [
        {"ref_id": str(item["evidence_id"]), **item}
        for item in source_evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    ]
    return {"requirements": requirements, "gold_evidence_refs": gold_refs}


def _test_deliverable_requirement_ids(traceability: dict[str, Any]) -> list[str]:
    """Identify Requirements whose own statement makes test code a deliverable."""

    result: list[str] = []
    for item in traceability.get("requirements", []):
        if not isinstance(item, dict):
            continue
        statement = item.get("statement", item.get("meaning", ""))
        requirement_id = item.get("requirement_id")
        if not isinstance(statement, str) or not isinstance(requirement_id, str):
            continue
        normalized = statement.casefold()
        if any(term in normalized for term in _TEST_DELIVERABLE_TERMS):
            result.append(requirement_id)
    return result


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JudgeContractError(f"{label} must be an object")
    return value
