"""Web composition for the greenfield evolution module."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from evolution.catalog import CatalogError, save_catalog_text
from evolution.runtime_config import save_runtime_config
from web_ui.deps import render_template, templates

router = APIRouter(prefix="/evolution", tags=["evolution"])

_QUALITY_STATUS_VIEWS = {
    "VALID": {
        "label": "合格",
        "description": "结构、时序、归因、工作流顺序和结束状态全部通过，可作为自动自进化证据。",
    },
    "PARTIAL": {
        "label": "部分可用",
        "description": "数据可在看板展示，但存在未完成或不可精确归因的问题，不能触发自动自进化。",
    },
    "INVALID": {
        "label": "无效",
        "description": "存在结构或契约错误，数据已隔离，不进入业务聚合和自动自进化。",
    },
}

_QUALITY_ISSUE_VIEWS = {
    "RESOURCE_USAGE_UNAVAILABLE": ("资源用量未采集", "某条执行记录没有采集 Token、耗时等资源指标。"),
    "RESOURCE_ATTRIBUTION_UNSAFE": (
        "资源归因不可靠",
        "资源数据不能精确归到本次显式调用或准确的会话时间窗。",
    ),
    "ENTRY_PENDING": ("执行记录未结束", "某条执行记录仍是 pending，尚未关闭。"),
    "UNAVAILABLE_METRICS": ("部分指标不可用", "文档已声明一个或多个最终指标无法获取。"),
    "NOT_FINALIZED": ("文档尚未收口", "整份 eval-log 还没有进入可用于自动化的最终状态。"),
    "PARTIAL_COVERAGE": ("采集覆盖不完整", "埋点在功能开始后才接入，没有覆盖完整过程。"),
    "WORKFLOW_ORDER_INVALID": ("工作流顺序异常", "记录的阶段顺序不符合 HelixHarness 工作流约定。"),
}

_CYCLE_STATE_VIEWS = {
    "NOOP": ("本轮无可执行动作", "周期已正常完成，但没有产生假设或实验。"),
    "COMPLETED": ("本轮已完成", "周期已完成，并处理了一个实验。"),
    "AWAITING_HUMAN_SELECTION": (
        "有假设待处理",
        "本轮已经形成进化假设，等待人工决定是否进入实验。",
    ),
    "AWAITING_EVALUATION": ("已提交评测", "本轮已提交冻结 Candidate，最终结论请查看进化实验。"),
    "BLOCKED": ("本轮已阻断", "周期命中安全门禁，没有继续执行。"),
    "FAILED": ("本轮运行失败", "周期没有正常完成，请查看调度异常。"),
    "RUNNING": ("正在运行", "周期正在处理中。"),
}

_CYCLE_REASON_VIEWS = {
    "no qualified eval-log evidence": "没有通过质量 Gate 的 eval-log 证据",
    "no new qualified eval-log evidence": "没有新增且通过质量 Gate 的 eval-log 证据",
    "growth hypotheses or non-automatic repair hypotheses require operator selection": (
        "已生成需要人工判断的进化假设"
    ),
    "terminal Benchmark produced one remediation plan draft": "评测结果已生成一条修复假设",
    "remediation draft already exists": "该评测结果已有修复假设",
}

_EXPERIMENT_STATE_VIEWS = {
    "PROPOSED": ("待启动", "bg-[#F5F7FA] text-[#4E5969]"),
    "AWAITING_HUMAN_SELECTION": ("待人工选择", "bg-[#FFF7E6] text-[#AD6800]"),
    "BUILDING_CANDIDATE": ("历史状态：生成 Candidate", "bg-[#F5F7FA] text-[#4E5969]"),
    "AWAITING_EVALUATION": ("Benchmark 执行中", "bg-[#F0F5FF] text-[#2F54EB]"),
    "ACCEPTED": ("评测建议采纳", "bg-[#F6FFED] text-[#237804]"),
    "REJECTED": ("评测未建议采纳", "bg-[#FFF1F0] text-[#A8071A]"),
    "INCONCLUSIVE": ("结果不确定", "bg-[#FFF7E6] text-[#AD6800]"),
    "BLOCKED": ("执行被阻断", "bg-[#FFF1F0] text-[#A8071A]"),
}

_HYPOTHESIS_STATE_VIEWS = {
    "PENDING_DECISION": ("待确认", "bg-[#FFF7E6] text-[#AD6800]"),
    "EXPERIMENT_STARTED": ("已进入实验", "bg-[#F0F5FF] text-[#2F54EB]"),
    "IGNORED": ("已忽略", "bg-[#F5F7FA] text-[#86909C]"),
}

_HYPOTHESIS_STATE_DESCRIPTIONS = {
    "PENDING_DECISION": "等待人工确认是否采纳；候选版本准备情况只在详情中展示。",
    "EXPERIMENT_STARTED": "人工已采纳该假设并进入实验；后续状态和结论在关联实验中查看。",
    "IGNORED": "人工决定不采纳，假设流程结束；候选版本和来源证据继续保留。",
}

_EXPERIMENT_STATE_DESCRIPTIONS = {
    "PROPOSED": "实验草案已建立，尚未启动。",
    "AWAITING_HUMAN_SELECTION": "等待人工授权开始实验。",
    "BUILDING_CANDIDATE": "旧流程曾在实验中生成 Candidate；新流程不再产生此状态。",
    "AWAITING_EVALUATION": "候选版本已冻结，等待 Benchmark 终态。",
    "ACCEPTED": "Benchmark 建议采纳，仍需人工发布。",
    "REJECTED": "Benchmark 不建议采纳，本次实验结束。",
    "INCONCLUSIVE": "证据不足以判断是否值得采纳。",
    "BLOCKED": "安全门禁或执行条件阻止实验继续。",
}

_HYPOTHESIS_STATUS_VIEWS = _HYPOTHESIS_STATE_VIEWS
_HYPOTHESIS_STATUS_DESCRIPTIONS = _HYPOTHESIS_STATE_DESCRIPTIONS

_SOURCE_VIEWS = {
    "EVAL": ("数据上报", "来自真实使用的 eval-log。"),
    "BENCH": ("评测发现", "来自受控 Benchmark 的 Candidate 失败或回退。"),
}

_SOURCE_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_RETRY_PATTERN = re.compile(r"retry_of=(exp_[0-9a-f]+)")
_REHEARSAL_TITLE_PATTERN = re.compile(r"^\[PARTIAL 演练(?:重试)?\]\s*")


def _quality_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Add Chinese operator explanations without changing eval's quality contract."""

    result = dict(summary)
    result["supported_total"] = int(summary.get("supported_total", summary.get("total", 0)))
    result["statuses"] = tuple(
        {"code": code, "count": int(summary["counts"].get(code, 0)), **view}
        for code, view in _QUALITY_STATUS_VIEWS.items()
    )
    fallback = ("未分类问题", "请根据原始代码继续排查。")
    result["issues"] = tuple(
        {
            "code": code,
            "count": int(count),
            "label": _QUALITY_ISSUE_VIEWS.get(code, fallback)[0],
            "description": _QUALITY_ISSUE_VIEWS.get(code, fallback)[1],
        }
        for code, count in summary.get("issue_counts", {}).items()
    )
    return result


def _cycle_view(latest: dict[str, Any] | None) -> dict[str, Any] | None:
    """Translate persisted cycle state while retaining its stable machine code."""

    if latest is None:
        return None
    state = str(latest.get("state") or "UNKNOWN")
    label, description = _CYCLE_STATE_VIEWS.get(state, ("状态待确认", "请根据原始状态代码继续排查。"))
    reason = str(latest.get("reason") or "")
    return {
        **latest,
        "state_code": state,
        "state_label": label,
        "state_description": description,
        "reason_label": _CYCLE_REASON_VIEWS.get(reason, reason or "未记录原因"),
    }


def _manual_cycle_view(status: dict[str, Any]) -> dict[str, Any]:
    """Translate the live manual-cycle state for a small polling surface."""

    state = str(status.get("state") or "IDLE")
    outcome_state = str(status.get("outcome_state") or "")
    outcome_reason = str(status.get("outcome_reason") or "")
    if state == "RUNNING":
        label = "正在检查新证据"
        description = (
            "正在同步 eval-log、回收评测结论并分析新增合格证据；"
            "新候选准备完成后，需在进化假设中审阅并采用。"
        )
    elif state == "COMPLETED":
        cycle_label, cycle_description = _CYCLE_STATE_VIEWS.get(
            outcome_state,
            ("运行已完成", "本轮已结束。"),
        )
        label = cycle_label
        reason_label = _CYCLE_REASON_VIEWS.get(outcome_reason, outcome_reason or cycle_description)
        description = f"{cycle_description} 原因：{reason_label}"
    elif state == "FAILED":
        label = "本轮运行失败"
        description = str(status.get("error") or "请查看服务日志。")
    else:
        label = ""
        description = ""
    return {
        **status,
        "running": state == "RUNNING",
        "visible": state != "IDLE",
        "label": label,
        "description": description,
    }


def _sidebar(active: str) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    items = [
        ("/evolution/summary", "进化总览", active == "/evolution/summary"),
        ("/evolution/hypotheses", "进化假设", active == "/evolution/hypotheses"),
        ("/evolution/experiments", "进化实验", active == "/evolution/experiments"),
        ("/evolution/config", "进化配置", active == "/evolution/config"),
    ]
    return [("自进化", items)]


def _evolution_configuration(request: Request) -> dict[str, Any]:
    """Project the few operator-facing evolution rules without exposing secrets."""

    service = request.app.state.evolution_service
    scheduler = request.app.state.evolution_scheduler
    interval_hours = service.interval.total_seconds() / 3600
    interval_label = (
        f"{int(interval_hours)} 小时"
        if interval_hours.is_integer()
        else f"{interval_hours:g} 小时"
    )
    remediation_poll_seconds = scheduler.remediation_poll_seconds
    return {
        "schedule": {
            "interval_hours": int(interval_hours) if interval_hours.is_integer() else interval_hours,
            "interval_label": interval_label,
            "poll_seconds": scheduler.poll_seconds,
            "remediation_poll_seconds": (
                int(remediation_poll_seconds)
                if remediation_poll_seconds.is_integer()
                else remediation_poll_seconds
            ),
        },
        "evidence": {
            "rule_label": "自动周期仅使用合格 VALID HelixHarness log",
            "statuses": (
                {"code": "VALID", "label": "合格", "usage": "自动分析", "class": "text-[#237804] bg-[#F6FFED]"},
                {"code": "PARTIAL", "label": "部分可用", "usage": "仅单条人工演练", "class": "text-[#AD6800] bg-[#FFF7E6]"},
                {"code": "INVALID", "label": "无效", "usage": "不使用", "class": "text-[#A8071A] bg-[#FFF1F0]"},
            ),
        },
        "capabilities": {
            "count": len(service.capabilities),
            "path": str(request.app.state.evolution_catalog_path),
            "yaml": Path(request.app.state.evolution_catalog_path).read_text(encoding="utf-8"),
        },
    }


def _hypothesis_views(
    rows: list[dict[str, Any]],
    capabilities: tuple[Any, ...],
    experiments: list[dict[str, Any]],
    quality_issue_lookup: Any | None = None,
    candidates: list[dict[str, Any]] | None = None,
    candidate_preparations: dict[str, dict[str, object | None]] | None = None,
) -> list[dict[str, Any]]:
    """Build one evidence dossier per logical hypothesis, with retry attempts nested."""

    capability_by_id = {item.capability_id: item for item in capabilities}
    experiment_by_hypothesis = {str(item["hypothesis_id"]): item for item in experiments}
    retry_by_experiment = {
        str(payload["retry_of_experiment_id"]): str(row["hypothesis_id"])
        for row in rows
        if (payload := _json_object(row.get("payload_json"))).get("retry_of_experiment_id")
    }
    candidate_by_hypothesis = {
        str(item["hypothesis_id"]): item for item in (candidates or ())
    }
    prepared: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        payload = json.loads(str(item["payload_json"]))
        acceptance = payload.get("acceptance") if isinstance(payload, dict) else None
        capability = capability_by_id.get(str(item["capability_id"]))
        evidence_ids = tuple(str(value) for value in payload.get("evidence_ids") or ())
        source_hash = next(
            (
                match.group(0)
                for evidence_id in evidence_ids
                if (match := _SOURCE_HASH_PATTERN.search(evidence_id))
            ),
            "",
        )
        issue_codes = tuple(str(value) for value in payload.get("source_quality_issues") or ())
        if not issue_codes and source_hash and quality_issue_lookup is not None:
            try:
                issue_codes = tuple(quality_issue_lookup(source_hash))
            except ValueError:
                issue_codes = ()
        experiment = experiment_by_hypothesis.get(str(item["hypothesis_id"]))
        candidate_row = candidate_by_hypothesis.get(str(item["hypothesis_id"]))
        prepared_candidate = _json_object(candidate_row.get("payload_json")) if candidate_row else {}
        candidate_preparation = (candidate_preparations or {}).get(
            str(item["hypothesis_id"]),
            {},
        )
        candidate_preparation_state = str(candidate_preparation.get("state") or "")
        semantic_source = str(payload.get("semantic_source") or "UNKNOWN")
        blocker_code = ""
        blocker_reason = ""

        if item.get("ignored_at"):
            state = "IGNORED"
        elif experiment is not None or item.get("selected_at"):
            state = "EXPERIMENT_STARTED"
        else:
            state = "PENDING_DECISION"
        if candidate_preparation_state == "FAILED":
            blocker_code = "CANDIDATE_FAILED"
            blocker_reason = str(candidate_preparation.get("error") or "候选版本生成失败。")
        elif capability is None:
            blocker_code = "CAPABILITY_REMOVED"
            blocker_reason = "目标能力已不在当前能力目录，无法重新生成候选版本。"
        elif item.get("selected_at") and experiment is None:
            blocker_code = "EXPERIMENT_MISSING"
            blocker_reason = "假设已批准，但关联实验记录缺失。"

        item["selection_state"] = state
        item["manual_selectable"] = (
            state == "PENDING_DECISION"
            and bool(prepared_candidate)
            and not item.get("selected_at")
        )
        item["candidate_preparable"] = (
            state == "PENDING_DECISION" and not prepared_candidate
            and capability is not None and candidate_preparation_state != "RUNNING"
            and blocker_code in {"", "CANDIDATE_FAILED"}
        )
        item["rejectable"] = state == "PENDING_DECISION" and not item.get("selected_at")
        item["retry_hypothesis_id"] = retry_by_experiment.get(str(experiment["experiment_id"])) if experiment else None
        item["experiment_retryable"] = bool(
            experiment
            and experiment.get("state") == "BLOCKED"
            and not experiment.get("benchmark_run_id")
            and not item.get("ignored_at")
            and capability is not None
            and payload.get("source_kind", "EVAL_LOG") == "EVAL_LOG"
            and semantic_source not in {"PARTIAL_REHEARSAL", "BENCH_REMEDIATION"}
        )
        item["blocker_code"] = blocker_code
        item["blocker_reason"] = blocker_reason
        item["candidate_preparation_state"] = candidate_preparation_state
        item["candidate_preparation_error"] = str(candidate_preparation.get("error") or "")
        item["rationale"] = str(payload.get("rationale") or "未记录改进原因")
        item["rationale_display"] = _RETRY_PATTERN.sub("", item["rationale"]).rstrip("； ")
        item["expected_effect"] = str(
            acceptance.get("expected_effect") if isinstance(acceptance, dict) else ""
        )
        item["regression_scope"] = tuple(
            acceptance.get("regression_scope") or () if isinstance(acceptance, dict) else ()
        )
        item["semantic_source"] = semantic_source
        raw_source_kind = str(payload.get("source_kind") or "")
        item["source_code"] = (
            "BENCH"
            if raw_source_kind == "BENCH_RUN" or item["semantic_source"] == "BENCH_REMEDIATION"
            else "EVAL"
        )
        item["source_label"], item["source_description"] = _SOURCE_VIEWS[item["source_code"]]
        if item["semantic_source"] == "PARTIAL_REHEARSAL":
            item["rationale_display"] = item["rationale_display"].replace(
                "合格 eval-log",
                "人工选定的 PARTIAL eval-log",
            )
        item["semantic_source_label"] = {
            "RULE": "契约规则",
            "MODEL": "AI 语义分析",
            "PARTIAL_REHEARSAL": "人工授权 PARTIAL 演练",
            "BENCH_REMEDIATION": "评测修复假设",
        }.get(item["semantic_source"], item["semantic_source"])
        item["source_run_id"] = str(payload.get("source_run_id") or "")
        item["source_root_run_id"] = str(payload.get("source_root_run_id") or "")
        item["intervention"] = str(payload.get("intervention") or "")
        item["intervention_display"] = item["intervention"]
        item["falsification_condition"] = str(payload.get("falsification_condition") or "")
        item["falsification_condition_display"] = item["falsification_condition"]
        item["validation_plan"] = str(payload.get("validation_plan") or "")
        item["validation_plan_display"] = item["validation_plan"]
        item["expected_effect_display"] = item["expected_effect"]
        item["generation_basis"] = _generation_basis_view(payload)
        item["evidence_count"] = len(evidence_ids)
        item["capability_name"] = (
            str(getattr(capability, "name", item["capability_id"]))
            if capability
            else str(item["capability_id"])
        )
        item["capability_description"] = (
            str(getattr(capability, "description", "未记录能力说明"))
            if capability
            else "能力已不在当前目录"
        )
        item["owned_paths"] = tuple(getattr(capability, "owned_paths", ())) if capability else ()
        item["ignored_at"] = str(item.get("ignored_at") or "")
        item["ignore_reason"] = str(item.get("ignore_reason") or "")
        item["source_hash"] = source_hash
        item["source_hash_short"] = source_hash[:12] if source_hash else "—"
        item["quality_issues"] = tuple(
            {
                "code": code,
                "label": _QUALITY_ISSUE_VIEWS.get(code, (code, ""))[0],
                "description": _QUALITY_ISSUE_VIEWS.get(code, (code, ""))[1],
            }
            for code in issue_codes
        )
        item["created_at"] = str(payload.get("created_at") or item.get("cycle_started_at") or "")
        item["title"] = str(item.get("title") or payload.get("title") or item["hypothesis_id"])
        item["title_display"] = item["title"]
        retry_of = payload.get("retry_of_experiment_id")
        if not retry_of:
            retry_match = _RETRY_PATTERN.search(item["rationale"])
            retry_of = retry_match.group(1) if retry_match else None
        item["retry_of_experiment_id"] = retry_of
        item["experiment"] = _hypothesis_experiment_view(experiment)
        item["candidate"] = _candidate_change_view(prepared_candidate or (
            item["experiment"]["candidate"] if item["experiment"] else {}
        ))
        prepared.append(item)

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    group_order: list[tuple[str, str, str]] = []
    for item in prepared:
        if item["semantic_source"] == "PARTIAL_REHEARSAL" and item["source_hash"]:
            key = ("PARTIAL_REHEARSAL", item["source_hash"], str(item["capability_id"]))
        else:
            key = ("HYPOTHESIS", str(item["hypothesis_id"]), str(item["capability_id"]))
        if key not in grouped:
            group_order.append(key)
            grouped[key] = []
        grouped[key].append(item)

    result: list[dict[str, Any]] = []
    for key in group_order:
        attempts = sorted(
            grouped[key],
            key=lambda value: (str(value.get("created_at") or ""), str(value["hypothesis_id"])),
        )
        for index, attempt in enumerate(attempts, start=1):
            attempt["attempt_index"] = index
            attempt["attempt_label"] = "首次执行" if index == 1 else f"第 {index} 次执行（重试）"
        latest = dict(attempts[-1])
        latest["attempts"] = attempts
        latest["attempt_count"] = len(attempts)
        latest["logical_title"] = _REHEARSAL_TITLE_PATTERN.sub("", str(latest["title_display"]))
        latest["state_label"], latest["state_class"] = _HYPOTHESIS_STATE_VIEWS.get(
            str(latest["selection_state"]),
            (str(latest["selection_state"]), "bg-surface-1 text-muted"),
        )
        result.append(latest)
    return result




def _generation_basis_view(payload: dict[str, Any]) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for index, basis in enumerate(payload.get("generation_basis") or (), start=1):
        if not isinstance(basis, dict):
            continue
        if str(basis.get("source_kind") or "") == "BENCH_RUN":
            decision = " · ".join(
                str(value)
                for value in (basis.get("decision_status"), basis.get("decision_reason"))
                if value
            )
            if decision:
                rows.append({"label": "评测结论", "value": decision})
            for finding_index, finding in enumerate(basis.get("findings") or (), start=1):
                if not isinstance(finding, dict):
                    continue
                detail = "；".join(
                    str(value)
                    for value in (
                        f"Case {finding.get('case_id')}" if finding.get("case_id") else "",
                        finding.get("category"),
                        finding.get("summary"),
                        finding.get("observed_difference"),
                        finding.get("diagnosis"),
                    )
                    if value
                )
                if detail:
                    rows.append({"label": f"评测依据 {finding_index}", "value": detail})
            continue
        detail = "；".join(
            str(value)
            for value in (
                basis.get("project_name"),
                basis.get("command"),
                basis.get("stage_kind"),
                basis.get("status"),
                basis.get("gate_result"),
                basis.get("outcome_reason"),
                basis.get("occurred_at"),
            )
            if value not in (None, "")
        )
        if detail:
            rows.append({"label": f"上报依据 {index}", "value": detail})
    return tuple(rows)




def _hypothesis_experiment_view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    state = str(row.get("state") or "UNKNOWN")
    state_label, state_class = _EXPERIMENT_STATE_VIEWS.get(
        state,
        (state, "bg-surface-1 text-muted"),
    )
    candidate = _candidate_change_view(_json_object(row.get("candidate_json")))
    plan = _json_object(row.get("evaluation_plan_json"))
    recommendations = plan.get("workflow_recommendations")
    base_recommendation = (
        recommendations.get("BASE") if isinstance(recommendations, dict) else None
    )
    workflow_source = (
        str(base_recommendation.get("recommendation_source") or "AI")
        if isinstance(base_recommendation, dict)
        else "AI"
    )
    return {
        "experiment_id": str(row.get("experiment_id") or ""),
        "deleted_at": row.get("deleted_at"),
        "state": state,
        "state_label": state_label,
        "state_class": state_class,
        "reason": str(row.get("decision_reason") or "未记录原因"),
        "candidate_id": row.get("candidate_id"),
        "benchmark_run_id": row.get("benchmark_run_id"),
        "candidate": candidate,
        "plan": plan,
        "workflow_source": workflow_source,
    }


def _json_object(raw: object) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _candidate_change_view(candidate: dict[str, Any]) -> dict[str, Any]:
    """Summarize one frozen patch and group its unified diff by file."""

    if not candidate:
        return {}
    result = dict(candidate)
    changed_paths = tuple(str(path) for path in candidate.get("changed_paths") or ())
    patch = str(candidate.get("diff_patch") or "")
    stats = {path: {"additions": 0, "deletions": 0, "change_type": "修改"} for path in changed_paths}
    file_patches: dict[str, str] = {}
    discovered_paths: list[str] = []
    current_path = ""
    current_patch: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git ") and " b/" in line:
            if current_path:
                file_patches[current_path] = "".join(current_patch)
            current_path = line.rsplit(" b/", 1)[-1].rstrip("\r\n")
            current_patch = [line]
            if current_path not in discovered_paths:
                discovered_paths.append(current_path)
            stats.setdefault(current_path, {"additions": 0, "deletions": 0, "change_type": "修改"})
            continue
        if current_path:
            current_patch.append(line)
        if current_path and line.startswith("new file mode"):
            stats[current_path]["change_type"] = "新增"
        elif current_path and line.startswith("deleted file mode"):
            stats[current_path]["change_type"] = "删除"
        elif current_path and line.startswith("+") and not line.startswith("+++"):
            stats[current_path]["additions"] += 1
        elif current_path and line.startswith("-") and not line.startswith("---"):
            stats[current_path]["deletions"] += 1
    if current_path:
        file_patches[current_path] = "".join(current_patch)

    display_paths = tuple(discovered_paths)
    files = tuple(
        {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "directory": path.rsplit("/", 1)[0] if "/" in path else "项目根目录",
            "diff_patch": file_patches.get(path, ""),
            **stats.get(path, {"additions": 0, "deletions": 0, "change_type": "修改"}),
        }
        for path in display_paths
    )
    result["changed_paths"] = changed_paths
    result["files"] = files
    result["file_count"] = len(files)
    result["additions"] = sum(int(item["additions"]) for item in files)
    result["deletions"] = sum(int(item["deletions"]) for item in files)
    result["has_file_diffs"] = any(bool(item["diff_patch"]) for item in files)
    result["diff_patch"] = patch
    return result


def _experiment_views(
    rows: list[dict[str, Any]],
    capabilities: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """Expose the frozen evaluation plan without leaking workflow source content."""

    result: list[dict[str, Any]] = []
    capability_by_id = {item.capability_id: item for item in capabilities}
    for row in rows:
        item = dict(row)
        item["evaluation_plan"] = _json_object(item.get("evaluation_plan_json"))
        item["candidate"] = _candidate_change_view(_json_object(item.get("candidate_json")))
        item["hypothesis"] = _json_object(item.get("hypothesis_json"))
        acceptance = item["hypothesis"].get("acceptance")
        acceptance = acceptance if isinstance(acceptance, dict) else {}
        capability = capability_by_id.get(str(item.get("capability_id") or ""))
        state_label, state_class = _EXPERIMENT_STATE_VIEWS.get(
            str(item.get("state") or "UNKNOWN"),
            (str(item.get("state") or "UNKNOWN"), "bg-surface-1 text-muted"),
        )
        item["state_label"] = state_label
        item["state_class"] = state_class
        item["state_description"] = _EXPERIMENT_STATE_DESCRIPTIONS.get(
            str(item.get("state") or "UNKNOWN"), "请结合原始状态继续判断。"
        )
        raw_source_kind = str(item["hypothesis"].get("source_kind") or "")
        semantic_source = str(item["hypothesis"].get("semantic_source") or "")
        item["source_code"] = (
            "BENCH" if raw_source_kind == "BENCH_RUN" or semantic_source == "BENCH_REMEDIATION" else "EVAL"
        )
        item["source_label"] = _SOURCE_VIEWS[item["source_code"]][0]
        item["capability_name"] = (
            str(getattr(capability, "name", item.get("capability_id") or "—"))
            if capability
            else str(item.get("capability_id") or "—")
        )
        item["intervention"] = str(
            item["hypothesis"].get("intervention") or "尚未记录具体改动方案，请先补充后再判断。"
        )
        item["expected_effect"] = str(acceptance.get("expected_effect") or "尚未记录预期效果")
        item["validation_plan"] = str(
            item["hypothesis"].get("validation_plan")
            or "按冻结的 Case 与工作流执行 Base/Candidate 对比。"
        )
        item["next_step"] = {
            "PROPOSED": "确认实验范围后启动",
            "AWAITING_HUMAN_SELECTION": "等待人工选择",
            "BUILDING_CANDIDATE": "等待 Candidate 冻结",
            "AWAITING_EVALUATION": "等待 Benchmark 终态",
            "ACCEPTED": "人工审查后决定是否发布",
            "REJECTED": "保留结果，不采纳本次 Candidate",
            "INCONCLUSIVE": "补充证据后重新判断",
            "BLOCKED": "处理阻断条件后再决定是否重试",
        }.get(str(item.get("state") or ""), "核对状态后人工处理")
        result.append(item)
    return result


def _status_options(views: dict[str, tuple[str, str]], descriptions: dict[str, str]) -> tuple[dict[str, str], ...]:
    return tuple(
        {"code": code, "label": label, "description": descriptions.get(code, "")}
        for code, (label, _state_class) in views.items()
    )


def _page_number(raw: str | None, default: int) -> int:
    try:
        return max(1, int(raw or default))
    except ValueError:
        return default


def _paginate(items: list[dict[str, Any]], request: Request, *, default_size: int = 10) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    page_size = min(50, _page_number(request.query_params.get("page_size"), default_size))
    total = len(items)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(_page_number(request.query_params.get("page"), 1), page_count)
    start = (page - 1) * page_size

    def page_url(target: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(target)
        params["page_size"] = str(page_size)
        return f"{request.url.path}?{urlencode(params)}"

    return items[start : start + page_size], {
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "total": total,
        "previous_url": page_url(page - 1) if page > 1 else "",
        "next_url": page_url(page + 1) if page < page_count else "",
    }


def _page_context(request: Request, active: str) -> dict[str, Any]:
    store = request.app.state.evolution_store
    context: dict[str, Any] = {
        "active_section": "evolution",
        "sidebar_items": _sidebar(active),
        "active_path": active,
        "page_id": active.rsplit("/", maxsplit=1)[-1],
    }
    if active == "/evolution/summary":
        scheduler = request.app.state.evolution_scheduler
        service = request.app.state.evolution_service
        summary = store.cycle_summary()
        manual_cycle = _manual_cycle_view(scheduler.manual_cycle_status())
        interval_hours = service.interval.total_seconds() / 3600
        context.update(
            {
                "summary": summary,
                "latest_cycle": _cycle_view(summary.get("latest")),
                "scheduler": {
                    "interval_hours": (
                        int(interval_hours) if interval_hours.is_integer() else interval_hours
                    ),
                    "last_state": scheduler.last_outcome.state if scheduler.last_outcome else None,
                    "last_reason": scheduler.last_outcome.reason if scheduler.last_outcome else None,
                    "last_error": scheduler.last_error,
                    "last_remediation_error": scheduler.last_remediation_error,
                    "manual_cycle": manual_cycle,
                },
                "quality": _quality_view(request.app.state.telemetry_quality_store.summary()),
            }
        )
    elif active == "/evolution/config":
        context["configuration"] = _evolution_configuration(request)
        context["manual_cycle"] = _manual_cycle_view(
            request.app.state.evolution_scheduler.manual_cycle_status()
        )
        context["config_saved"] = request.query_params.get("saved", "")
        context["config_error"] = request.query_params.get("error", "")
    elif active == "/evolution/hypotheses":
        experiments = store.list_experiments(include_deleted=True)
        telemetry_store = request.app.state.telemetry_quality_store

        def quality_issue_lookup(source_hash: str) -> tuple[str, ...]:
            selected = telemetry_store.partial_rehearsal_evidence(source_hash)
            return tuple(selected["issue_codes"])

        hypotheses = _hypothesis_views(
            store.list_hypotheses(),
            request.app.state.evolution_service.capabilities,
            experiments,
            quality_issue_lookup,
            store.list_hypothesis_candidates(),
            request.app.state.evolution_scheduler.candidate_preparation_statuses(),
        )
        filters = {
            "q": request.query_params.get("q", "").strip(),
            "source": request.query_params.get("source", "").strip().upper(),
            "source_run_id": request.query_params.get("source_run_id", "").strip(),
            "kind": request.query_params.get("kind", "").strip().upper(),
            "state": request.query_params.get("state", "").strip().upper(),
        }
        if filters["q"]:
            query = filters["q"].casefold()
            hypotheses = [
                item
                for item in hypotheses
                if query
                in " ".join(
                    (
                        str(item.get("logical_title") or ""),
                        str(item.get("rationale_display") or ""),
                        str(item.get("intervention_display") or ""),
                        str(item.get("capability_name") or ""),
                    )
                ).casefold()
            ]
        if filters["source"]:
            hypotheses = [item for item in hypotheses if item["source_code"] == filters["source"]]
        if filters["source_run_id"]:
            hypotheses = [
                item for item in hypotheses if item["source_run_id"] == filters["source_run_id"]
            ]
        if filters["kind"]:
            hypotheses = [item for item in hypotheses if str(item["kind"]) == filters["kind"]]
        if filters["state"]:
            hypotheses = [
                item for item in hypotheses if str(item["selection_state"]) == filters["state"]
            ]
        context["hypotheses"], context["pagination"] = _paginate(hypotheses, request)
        context["filters"] = filters
        context["filters_active"] = any(filters.values())
        context["source_remediation"] = (
            request.app.state.scenario_store.get_remediation_projection(filters["source_run_id"])
            if filters["source_run_id"]
            else None
        )
        context["source_poll_url"] = request.url.path + (
            f"?{request.url.query}" if request.url.query else ""
        )
        context["hypothesis_statuses"] = _status_options(
            _HYPOTHESIS_STATUS_VIEWS,
            _HYPOTHESIS_STATUS_DESCRIPTIONS,
        )
        context["source_options"] = tuple(
            {"code": code, "label": label, "description": description}
            for code, (label, description) in _SOURCE_VIEWS.items()
        )
        context["quality"] = _quality_view(request.app.state.telemetry_quality_store.summary())
    elif active == "/evolution/experiments":
        experiments = _experiment_views(
            store.list_experiments(),
            request.app.state.evolution_service.capabilities,
        )
        filters = {
            "q": request.query_params.get("q", "").strip(),
            "source": request.query_params.get("source", "").strip().upper(),
            "kind": request.query_params.get("kind", "").strip().upper(),
            "state": request.query_params.get("state", "").strip().upper(),
        }
        if filters["q"]:
            query = filters["q"].casefold()
            experiments = [
                item
                for item in experiments
                if query
                in " ".join(
                    (
                        str(item.get("title") or ""),
                        str(item.get("intervention") or ""),
                        str(item.get("decision_reason") or ""),
                        str(item.get("capability_name") or ""),
                    )
                ).casefold()
            ]
        if filters["source"]:
            experiments = [item for item in experiments if item["source_code"] == filters["source"]]
        if filters["kind"]:
            experiments = [item for item in experiments if str(item["kind"]) == filters["kind"]]
        if filters["state"]:
            experiments = [item for item in experiments if str(item["state"]) == filters["state"]]
        context["experiments"], context["pagination"] = _paginate(experiments, request)
        context["filters"] = filters
        context["filters_active"] = any(filters.values())
        context["experiment_statuses"] = _status_options(
            _EXPERIMENT_STATE_VIEWS,
            _EXPERIMENT_STATE_DESCRIPTIONS,
        )
        context["source_options"] = tuple(
            {"code": code, "label": label, "description": description}
            for code, (label, description) in _SOURCE_VIEWS.items()
        )
    return context


def _render_page(request: Request, active: str) -> Response:
    context = _page_context(request, active)
    html = render_template("evolution/index.html", context)
    if request.headers.get("HX-Request"):
        return HTMLResponse(html)
    return templates.TemplateResponse(
        request,
        "base.html",
        {
            "sidebar_items": context["sidebar_items"],
            "active_section": context["active_section"],
            "content_html": html,
        },
    )


@router.get("/summary", response_class=HTMLResponse)
async def evolution_summary(request: Request) -> Response:
    return _render_page(request, "/evolution/summary")


@router.get("/hypotheses", response_class=HTMLResponse)
async def evolution_hypotheses(request: Request) -> Response:
    return _render_page(request, "/evolution/hypotheses")


@router.get("/experiments", response_class=HTMLResponse)
async def evolution_experiments(request: Request) -> Response:
    return _render_page(request, "/evolution/experiments")


@router.get("/config", response_class=HTMLResponse)
async def evolution_config(request: Request) -> Response:
    return _render_page(request, "/evolution/config")


@router.post("/config/schedule")
async def save_evolution_schedule(request: Request) -> Response:
    form = await request.form()
    try:
        configuration = save_runtime_config(
            {
                "interval_hours": form.get("interval_hours"),
                "poll_seconds": form.get("poll_seconds"),
                "remediation_poll_seconds": form.get("remediation_poll_seconds"),
            }
        )
    except ValueError as exc:
        return RedirectResponse(
            "/evolution/config?" + urlencode({"error": str(exc)}),
            status_code=303,
        )
    service = request.app.state.evolution_service
    scheduler = request.app.state.evolution_scheduler
    service.interval = timedelta(hours=configuration.interval_hours)
    scheduler.poll_seconds = configuration.poll_seconds
    scheduler.remediation_poll_seconds = configuration.remediation_poll_seconds
    return RedirectResponse("/evolution/config?saved=schedule", status_code=303)


@router.post("/config/catalog")
async def save_evolution_catalog(request: Request) -> Response:
    form = await request.form()
    content = str(form.get("catalog_yaml") or "")
    try:
        capabilities = save_catalog_text(request.app.state.evolution_catalog_path, content)
    except (CatalogError, OSError) as exc:
        return RedirectResponse(
            "/evolution/config?" + urlencode({"error": f"能力目录保存失败：{exc}"}),
            status_code=303,
        )
    request.app.state.evolution_service.replace_capabilities(capabilities)
    return RedirectResponse("/evolution/config?saved=catalog", status_code=303)


@router.get("/run-status", response_class=HTMLResponse)
async def evolution_run_status(request: Request) -> HTMLResponse:
    manual_cycle = _manual_cycle_view(
        request.app.state.evolution_scheduler.manual_cycle_status()
    )
    return HTMLResponse(
        render_template(
            "evolution/_run_status.html",
            {
                "manual_cycle": manual_cycle,
                "refresh_config": manual_cycle["visible"] and not manual_cycle["running"],
            },
        )
    )


@router.post("/run")
async def run_evolution(request: Request) -> Response:
    scheduler = request.app.state.evolution_scheduler
    scheduler.start_manual_cycle()
    return RedirectResponse("/evolution/config", status_code=303)


@router.post("/hypotheses/{hypothesis_id}/select")
async def select_hypothesis(request: Request, hypothesis_id: str) -> Response:
    from starlette.concurrency import run_in_threadpool

    try:
        await run_in_threadpool(
            request.app.state.evolution_service.start_selected_hypothesis, hypothesis_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/evolution/experiments", status_code=303)


@router.post("/hypotheses/{hypothesis_id}/prepare-candidate")
async def prepare_hypothesis_candidate(request: Request, hypothesis_id: str) -> Response:
    request.app.state.evolution_scheduler.start_candidate_preparation(hypothesis_id)
    return RedirectResponse(
        f"/evolution/hypotheses#hypothesis-{hypothesis_id}",
        status_code=303,
    )


@router.post("/hypotheses/{hypothesis_id}/ignore")
async def ignore_hypothesis(request: Request, hypothesis_id: str) -> Response:
    form = await request.form()
    reason = str(form.get("reason") or "人工判断不采纳")
    try:
        request.app.state.evolution_service.ignore_hypothesis(hypothesis_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/evolution/hypotheses", status_code=303)


@router.post("/experiments/{experiment_id}/retry")
async def retry_blocked_experiment(request: Request, experiment_id: str) -> Response:
    from starlette.concurrency import run_in_threadpool

    service = request.app.state.evolution_service
    try:
        hypothesis_id = await run_in_threadpool(service.retry_blocked_experiment, experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = service.store.get_hypothesis(hypothesis_id)
    if row and not row.get("selected_at") and not row.get("ignored_at") and not service.store.get_hypothesis_candidate(hypothesis_id):
        request.app.state.evolution_scheduler.start_candidate_preparation(hypothesis_id)
    return RedirectResponse(f"/evolution/hypotheses#hypothesis-{hypothesis_id}", status_code=303)


@router.post("/hypotheses/{hypothesis_id}/delete")
async def delete_hypothesis(request: Request, hypothesis_id: str) -> Response:
    form = await request.form()
    reason = str(form.get("reason") or "人工从工作台删除")
    try:
        request.app.state.evolution_service.delete_hypothesis(hypothesis_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/evolution/hypotheses", status_code=303)


@router.post("/experiments/{experiment_id}/delete")
async def delete_experiment(request: Request, experiment_id: str) -> Response:
    form = await request.form()
    reason = str(form.get("reason") or "人工从工作台删除")
    try:
        request.app.state.evolution_service.delete_experiment(experiment_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/evolution/experiments", status_code=303)
