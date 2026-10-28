"""Shared anonymous semantic-Judge contracts for independent Profiles."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Protocol

from bench.runtime_secrets import (
    EvaluationProviderConfig,
    codex_responses_base_url,
)

JUDGE_INPUT_SCHEMA = "framework-eval-anonymous-judge-input.v1"
JUDGE_REQUEST_TIMEOUT_SECONDS = 120.0
JUDGE_SDK_MAX_RETRIES = 1
JUDGE_EVIDENCE_PATHS = {
    "messages.json",
    "milestones.json",
    "git.patch",
    "verification.json",
    "artifacts/index.json",
    "stage-artifacts.json",
}


class JudgeContractError(RuntimeError):
    """Raised when anonymous Judge input or output cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        partial_outcome: object | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_outcome = partial_outcome


class SemanticJudgeAdapter(Protocol):
    """No-tool model boundary for one anonymous semantic judgment."""

    def judge(
        self,
        request: dict[str, Any],
        *,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, Any]:
        """Return one versioned structured judgment."""


class OpenAISemanticJudgeAdapter:
    """Call a no-tool OpenAI Judge with strict structured output."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
        provider_config: EvaluationProviderConfig | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._provider_config = provider_config

    def judge(
        self,
        request: dict[str, Any],
        *,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, Any]:
        """Return parsed JSON without giving the model tools or filesystem access."""

        axes = request.get("axes")
        if not isinstance(axes, list) or not axes or any(not isinstance(axis, str) or not axis for axis in axes):
            raise JudgeContractError("Judge request axes are invalid")
        if self._client_factory is None:
            from openai import OpenAI

            provider = self._provider_config
            if provider is None or not provider.api_key:
                raise JudgeContractError("OpenAI Judge provider is not configured")
            client = OpenAI(
                api_key=provider.api_key,
                base_url=codex_responses_base_url(provider.base_url) or None,
                timeout=JUDGE_REQUEST_TIMEOUT_SECONDS,
                max_retries=JUDGE_SDK_MAX_RETRIES,
            )
        else:
            client = self._client_factory()
        trusted = "score_contract" in request
        scoped_evidence_ids = (
            evidence_ids_by_axis(request)
            if "evidence_scopes" in request
            else {}
        )
        legal_evidence_ids = tuple(
            sorted(set().union(*scoped_evidence_ids.values()))
        ) if scoped_evidence_ids else ()
        legal_requirement_ids: tuple[str, ...] = ()
        legal_gold_evidence_refs: tuple[str, ...] = ()
        if trusted:
            requirement_ids, gold_evidence_refs = traceability_ids(request)
            legal_requirement_ids = tuple(sorted(requirement_ids))
            legal_gold_evidence_refs = tuple(sorted(gold_evidence_refs))
        try:
            response = client.responses.create(
                model=model,
                reasoning={"effort": reasoning_effort},
                tools=[],
                store=False,
                input=[
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "你是匿名评测 Judge。只能根据提供的证据，对请求中的语义评分轴打分。"
                                    "Token、完成时间、工具调用次数和尝试次数不得影响任何语义评分。"
                                    "不得推断版本身份、执行侧角色或版本先后关系，也不得作升级决策。"
                                    "只能在请求 Rubric 授权的范围内评价业务目标是否达成、可观察行为、"
                                    "边界和失败处理、非回归以及实现质量。不得依据源码或文档与 Gold 或参考实现的"
                                    "文本相似度打分。Gold Reference 只能用于识别有来源支持的 Requirement、决策、"
                                    "边界、失败处理和追溯关系；不得要求实现采用相同架构。功能等价的实现应获得相同分数。"
                                    "每项判断必须引用提供的 evidence_id，每个评分轴只能引用 evidence_scope 允许的证据。"
                                    "若请求包含 citation_requirements，必须完整引用其中要求的 Requirement 和证据。"
                                    "若请求包含 axis_anchors，必须按照对应评分轴的专用档位定义评分。"
                                    "分数范围为 0 到 100，且只能使用 0、25、50、75、100 五档。"
                                    "涉及 Requirement 满足程度的判断必须引用对应的冻结 Requirement ID 和 Gold Evidence Reference；"
                                    "其他评分轴仅在二者与判断直接相关时引用，不得为了满足格式要求添加无关引用。"
                                    "低于 100 分时必须列出明确扣分项。不得编造 Requirement、Gold Reference 或 Evidence ID。"
                                ),
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": json.dumps(
                                    request,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                            }
                        ],
                    },
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "framework_eval_semantic_judgment",
                        "strict": True,
                        "schema": self._output_schema(
                            tuple(axes),
                            trusted=trusted,
                            legal_evidence_ids=legal_evidence_ids,
                            legal_requirement_ids=legal_requirement_ids,
                            legal_gold_evidence_refs=legal_gold_evidence_refs,
                        ),
                    }
                },
            )
        except Exception as exc:
            error_name = type(exc).__name__
            if error_name == "APITimeoutError":
                raise TimeoutError("Judge request timed out") from exc
            if error_name in {
                "APIConnectionError",
                "InternalServerError",
                "RateLimitError",
            }:
                raise ConnectionError("Judge request is temporarily unavailable") from exc
            raise
        raw_output = getattr(response, "output_text", None)
        if not isinstance(raw_output, str) or not raw_output:
            raise JudgeContractError("Judge returned no structured output")
        try:
            output = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise JudgeContractError("Judge output is not valid JSON") from exc
        if not isinstance(output, dict):
            raise JudgeContractError("Judge output must be an object")
        return output

    @staticmethod
    def _output_schema(
        axes: tuple[str, ...],
        *,
        trusted: bool = True,
        legal_evidence_ids: tuple[str, ...] = (),
        legal_requirement_ids: tuple[str, ...] = (),
        legal_gold_evidence_refs: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        evidence_id_schema: dict[str, Any] = {"type": "string"}
        if legal_evidence_ids:
            evidence_id_schema["enum"] = list(legal_evidence_ids)
        requirement_id_schema: dict[str, Any] = {"type": "string"}
        if legal_requirement_ids:
            requirement_id_schema["enum"] = list(legal_requirement_ids)
        gold_evidence_ref_schema: dict[str, Any] = {"type": "string"}
        if legal_gold_evidence_refs:
            gold_evidence_ref_schema["enum"] = list(legal_gold_evidence_refs)
        if not trusted:
            return {
                "type": "object",
                "additionalProperties": False,
                "required": ["schema_version", "status", "axis_scores"],
                "properties": {
                    "schema_version": {
                        "type": "string",
                        "const": "framework-eval-judge-output.v1",
                    },
                    "status": {"type": "string", "const": "COMPLETED"},
                    "axis_scores": {
                        "type": "array",
                        "minItems": len(axes),
                        "maxItems": len(axes),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["axis", "score", "rationale", "evidence_ids"],
                            "properties": {
                                "axis": {"type": "string", "enum": list(axes)},
                                "score": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 100,
                                    "description": (
                                        "按 0 到 100 分评价该评分轴：0 表示没有证据支持目标达成，"
                                        "100 表示完整满足评分标准。不得使用 0 到 1 的小数评分。"
                                    ),
                                },
                                "rationale": {"type": "string", "minLength": 1},
                                "evidence_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": evidence_id_schema,
                                },
                            },
                        },
                    },
                },
            }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "status", "axis_scores"],
            "properties": {
                "schema_version": {
                    "type": "string",
                    "const": "framework-eval-judge-output.v2",
                },
                "status": {"type": "string", "const": "COMPLETED"},
                "axis_scores": {
                    "type": "array",
                    "minItems": len(axes),
                    "maxItems": len(axes),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "axis",
                            "criterion_id",
                            "criterion",
                            "score",
                            "rationale",
                            "evidence_ids",
                            "requirement_ids",
                            "gold_evidence_refs",
                            "deductions",
                            "limitations",
                        ],
                        "properties": {
                            "axis": {"type": "string", "enum": list(axes)},
                            "criterion_id": {"type": "string", "enum": list(axes)},
                            "criterion": {"type": "string", "minLength": 1},
                            "score": {
                                "type": "number",
                                "enum": [0, 25, 50, 75, 100],
                                "description": (
                                    "固定五档评分：0 表示未满足，25 表示大部分未满足，50 表示部分满足，"
                                    "75 表示核心要求满足但仍有实质性缺口，100 表示完整满足。"
                                ),
                            },
                            "rationale": {"type": "string", "minLength": 1},
                            "evidence_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": evidence_id_schema,
                            },
                            "requirement_ids": {
                                "type": "array",
                                "items": requirement_id_schema,
                            },
                            "gold_evidence_refs": {
                                "type": "array",
                                "items": gold_evidence_ref_schema,
                            },
                            "deductions": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "limitations": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        }


def judge_with_retries(
    adapter: SemanticJudgeAdapter,
    request: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str,
    max_retries: int,
) -> dict[str, Any]:
    """Retry only transient Judge failures under the frozen API-step budget."""

    for attempt in range(max(0, max_retries) + 1):
        try:
            return adapter.judge(
                request,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except (ConnectionError, TimeoutError):
            if attempt >= max(0, max_retries):
                raise
    raise AssertionError("unreachable")




def anonymize_text(text: str, identities: tuple[str, ...]) -> str:
    """Remove Side/version identity while preserving evidence semantics."""

    for identity in identities:
        text = text.replace(identity, "$VERSION")
    return re.sub(
        r"(?:(?<![A-Za-z0-9])(?i:base|candidate)|Base|Candidate)",
        "$SIDE",
        text,
    )


def anonymize_value(value: Any, identities: tuple[str, ...]) -> Any:
    """Recursively anonymize a JSON-compatible value."""

    if isinstance(value, dict):
        return {anonymize_text(str(key), identities): anonymize_value(item, identities) for key, item in value.items()}
    if isinstance(value, list):
        return [anonymize_value(item, identities) for item in value]
    if isinstance(value, str):
        return anonymize_text(value, identities)
    return value














def evidence_ids_by_axis(request: dict[str, Any]) -> dict[str, set[str]]:
    """Project the frozen Judge Request scopes into output-validation sets."""

    scopes = request.get("evidence_scopes")
    if not isinstance(scopes, list):
        raise JudgeContractError("Judge evidence scopes are invalid")
    result: dict[str, set[str]] = {}
    for scope in scopes:
        axis = scope.get("axis") if isinstance(scope, dict) else None
        evidence_ids = scope.get("allowed_evidence_ids") if isinstance(scope, dict) else None
        if (
            not isinstance(axis, str)
            or axis in result
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or any(not isinstance(item, str) or not item for item in evidence_ids)
        ):
            raise JudgeContractError("Judge evidence scopes are invalid")
        result[axis] = set(evidence_ids)
    return result


def traceability_ids(request: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return the only frozen Requirement and Gold references Judge may cite."""

    traceability = request.get("traceability")
    requirements = traceability.get("requirements") if isinstance(traceability, dict) else None
    gold_refs = traceability.get("gold_evidence_refs") if isinstance(traceability, dict) else None
    if not isinstance(requirements, list) or not isinstance(gold_refs, list):
        raise JudgeContractError("Judge traceability contract is missing")
    requirement_ids = {
        str(item["requirement_id"])
        for item in requirements
        if isinstance(item, dict) and isinstance(item.get("requirement_id"), str)
    }
    gold_evidence_refs = {
        str(item["ref_id"])
        for item in gold_refs
        if isinstance(item, dict) and isinstance(item.get("ref_id"), str)
    }
    if not requirement_ids or not gold_evidence_refs:
        raise JudgeContractError("Judge traceability contract is invalid")
    return requirement_ids, gold_evidence_refs




def evidence_id(content: bytes) -> str:
    """Return the stable content-addressed identifier used by Judge citations."""

    return f"evidence_{hashlib.sha256(content).hexdigest()[:24]}"


def trusted_score_contract() -> dict[str, object]:
    """Return the anchored semantic score contract used by trusted Runs."""

    return {
        "anchors": [0, 25, 50, 75, 100],
        "anchor_meanings": {
            "0": "未实现或与要求冲突",
            "25": "仅满足少量要求，主体目标未达成",
            "50": "部分满足，存在明显业务或质量缺口",
            "75": "核心要求满足，但存在实质性非阻断缺口",
            "100": "完整满足，没有实质性缺口",
        },
        "deduction_required_below": 100,
        "every_score_requires_evidence": True,
        "requirement_gold_required_when_applicable": True,
    }


def validate_judge_output(
    output: dict[str, Any],
    *,
    expected_schema: str,
    valid_evidence_ids: set[str],
    valid_evidence_ids_by_axis: dict[str, set[str]] | None = None,
    valid_requirement_ids: set[str] | None = None,
    valid_gold_evidence_refs: set[str] | None = None,
    expected_axes: tuple[str, ...],
) -> dict[str, float]:
    """Fail closed unless all Profile semantic axes cite retained evidence."""

    if not isinstance(output, dict) or set(output) != {
        "schema_version",
        "status",
        "axis_scores",
    }:
        raise JudgeContractError("Judge output shape is invalid")
    if output.get("schema_version") != expected_schema:
        raise JudgeContractError("Judge output schema is invalid")
    if output.get("status") != "COMPLETED":
        raise JudgeContractError("Judge did not complete")
    items = output.get("axis_scores")
    if not isinstance(items, list):
        raise JudgeContractError("Judge axis scores are invalid")
    scores: dict[str, float] = {}
    trusted_output = expected_schema == "framework-eval-judge-output.v2"
    v1_fields = {
            "axis",
            "score",
            "rationale",
            "evidence_ids",
    }
    v2_fields = {
        *v1_fields,
        "criterion_id",
        "criterion",
        "requirement_ids",
        "gold_evidence_refs",
        "deductions",
        "limitations",
    }
    for item in items:
        expected_fields = v2_fields if trusted_output else v1_fields
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise JudgeContractError("Judge axis result shape is invalid")
        axis = str(item.get("axis"))
        score = item.get("score")
        rationale = item.get("rationale")
        evidence_ids = item.get("evidence_ids")
        if (
            axis not in expected_axes
            or axis in scores
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not 0 <= float(score) <= 100
            or not isinstance(rationale, str)
            or not rationale.strip()
            or not isinstance(evidence_ids, list)
            or not evidence_ids
        ):
            raise JudgeContractError("Judge axis result is invalid")
        if any(item_id not in valid_evidence_ids for item_id in evidence_ids):
            raise JudgeContractError("Judge cited unknown evidence id")
        if valid_evidence_ids_by_axis is not None:
            scoped_ids = valid_evidence_ids_by_axis.get(axis)
            if scoped_ids is None or any(item_id not in scoped_ids for item_id in evidence_ids):
                raise JudgeContractError("Judge cited evidence outside axis scope")
        if trusted_output:
            criterion_id = item.get("criterion_id")
            criterion = item.get("criterion")
            requirement_ids = item.get("requirement_ids")
            gold_refs = item.get("gold_evidence_refs")
            deductions = item.get("deductions")
            limitations = item.get("limitations")
            if (
                criterion_id != axis
                or not isinstance(criterion, str)
                or not criterion.strip()
                or float(score) not in {0.0, 25.0, 50.0, 75.0, 100.0}
            ):
                raise JudgeContractError("Judge score is not anchored to its criterion")
            if (
                not isinstance(requirement_ids, list)
                or valid_requirement_ids is None
                or any(item_id not in valid_requirement_ids for item_id in requirement_ids)
            ):
                raise JudgeContractError("Judge cited unknown requirement id")
            if (
                not isinstance(gold_refs, list)
                or valid_gold_evidence_refs is None
                or any(ref not in valid_gold_evidence_refs for ref in gold_refs)
            ):
                raise JudgeContractError("Judge cited unknown Gold evidence reference")
            if (
                not isinstance(deductions, list)
                or any(not isinstance(reason, str) or not reason.strip() for reason in deductions)
                or (float(score) < 100.0 and not deductions)
                or not isinstance(limitations, list)
                or any(not isinstance(reason, str) or not reason.strip() for reason in limitations)
            ):
                raise JudgeContractError("Judge deductions or limitations are invalid")
        scores[axis] = float(score)
    if set(scores) != set(expected_axes):
        raise JudgeContractError("Judge semantic axes are incomplete")
    return scores
